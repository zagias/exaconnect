"""Integration catalogue and standard interfaces API (ADR 0028).

Signed in, under /api/v1/commai/customers/{customer_id}: the catalogue grouped
by category, credentials entry, inbound webhooks, the business's own REST
apps (OpenAPI import), outbound webhook formats, the iCalendar feed, vCard
and CSV exchange, and IMAP/SMTP mailboxes.

Public: inbound webhooks, app webhooks, the iCalendar feed and invites, and
the published OpenAPI and AsyncAPI documents.

Reads need commai:read; changes need commai:admin and a business admin. Every
write is audited. Secrets are accepted through secure entry and never returned.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access
from ..automation import integrations, vault
from .common import errors

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: integrations catalogue"])
public = APIRouter(tags=["commai: integrations catalogue"])


@contextmanager
def _errors() -> Iterator[None]:
    with errors():
        try:
            yield
        except integrations.SetupError as e:
            raise HTTPException(e.code, str(e)) from e
        except vault.VaultError as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e


def _admin(conn, user, customer_id: str) -> None:
    access.require_business_admin(user)
    if access.seat(conn, user, customer_id) == "internal":
        raise HTTPException(403, "Your seat can't change integration settings.")


class CredentialsIn(BaseModel):
    credentials: dict[str, str] = Field(max_length=10)


@router.post("/integrations/{app}/credentials")
def enter_credentials(customer_id: str, app: str, body: CredentialsIn, user: UserDep) -> dict:
    """Secure entry of an app's credentials (API key, user name and app password).
    Checked with one call, stored encrypted and never shown again."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = integrations.enter_credentials(conn, customer_id, app, body.credentials, user.actor)
        audit.record(conn, user.actor, "commai.integration.credentials", app, customer_id)  # never the values
    return row
