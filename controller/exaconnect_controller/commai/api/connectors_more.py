"""API for the helpdesk, team chat, commerce, payments and knowledge
connectors (ADR 0029). Setting up an app (connect, sign in, credentials,
actions, mapping, test, approve, health) uses the integrations API (ADR 0020,
ADR 0028); these are the extras:

- set up the app's change notifications (webhooks) from CommAI;
- tickets linked to a conversation;
- link a Slack or Teams user to a CommAI person (approvals from team chat);
- a one-time code that links a Teams channel to the business;
- knowledge sync from Google Drive and OneDrive/SharePoint: run now, and see
  each file's state;
- the Slack interactivity and Teams bot endpoints (public, each request
  verified: Slack request signing, Bot Framework JWT).

Reads need commai:read; changes need commai:admin. Every write is audited.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access, connectors
from ..connectors import more_common
from .common import errors

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: connectors"])
public = APIRouter(tags=["commai: connectors"])


@contextmanager
def _errors() -> Iterator[None]:
    with errors():
        try:
            yield
        except connectors.ConnectorError as e:
            code = {"input": 422, "mapping": 409, "permission": 403, "expired_signin": 409}.get(e.cause, 502)
            raise HTTPException(code, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e


def _admin(conn, user, customer_id: str) -> None:
    access.require_business_admin(user)
    if access.seat(conn, user, customer_id) == "internal":
        raise HTTPException(403, "Your seat can't change integration settings.")


def _connector(app: str):
    try:
        return connectors.get(app)
    except KeyError:
        raise HTTPException(404, f"There is no {app} integration.") from None


def _connection(conn, customer_id: str, app: str) -> dict:
    row = connectors.connection(conn, customer_id, app)
    if row is None:
        raise HTTPException(409, "Connect the app first.")
    return {**row, "test": False}


@router.post("/connectors/{app}/webhooks")
def register_webhooks(customer_id: str, app: str, user: UserDep) -> dict:
    """Set up the app's change notifications to CommAI. Where the app can't be
    set up from here, the answer says what to paste into it (shown once)."""
    access.check(user, customer_id, "commai:admin")
    c = _connector(app)
    if not hasattr(c, "register_webhooks"):
        raise HTTPException(404, f"{c.label} doesn't send change notifications.")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = c.register_webhooks(conn, _connection(conn, customer_id, app), user.actor)
        audit.record(conn, user.actor, "commai.connector.webhooks", app, customer_id)  # never the secret
    return out


@router.get("/connectors/tickets")
def linked_tickets(customer_id: str, conversation_id: str, user: UserDep) -> list[dict]:
    """Helpdesk tickets linked to a conversation, with their synced status."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return more_common.tickets_for(conn, customer_id, conversation_id)


class IdentityIn(BaseModel):
    external_user: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    user_email: str = Field(min_length=3, max_length=255)
