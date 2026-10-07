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

import json
import os
import urllib.parse
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access, connectors
from ..actions import ActionRefused
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


CHAT_APPS = ("slack", "teams")


class IdentityIn(BaseModel):
    external_user: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    user_email: str = Field(min_length=3, max_length=255)


@router.get("/connectors/{app}/identities")
def list_identities(customer_id: str, app: str, user: UserDep) -> list[dict]:
    """Slack or Teams users linked to CommAI people (who may press Approve there)."""
    access.check(user, customer_id, "commai:read")
    if app not in CHAT_APPS:
        raise HTTPException(404, "Only Slack and Teams link people.")
    with db.tx() as conn:
        return conn.execute(
            """SELECT external_user, user_email, created_by, created_at FROM commai_chat_identities
               WHERE customer_id = %s AND app = %s ORDER BY user_email""",
            (customer_id, app),
        ).fetchall()


@router.put("/connectors/{app}/identities")
def link_identity(customer_id: str, app: str, body: IdentityIn, user: UserDep) -> dict:
    """Link a Slack user ID (U...) or Teams user (Entra object id) to a member
    of this business. Only members with a reply seat can approve."""
    access.check(user, customer_id, "commai:admin")
    if app not in CHAT_APPS:
        raise HTTPException(404, "Only Slack and Teams link people.")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        member = conn.execute(
            """SELECT u.email, m.seat FROM users u JOIN commai_members m ON m.user_id = u.id
               WHERE m.customer_id = %s AND lower(u.email) = lower(%s)""",
            (customer_id, body.user_email),
        ).fetchone()
        if member is None:
            raise HTTPException(422, "That person is not a member of this business.")
        if member["seat"] == "internal":
            raise HTTPException(422, "That person's seat can't approve actions.")
        row = conn.execute(
            """INSERT INTO commai_chat_identities (customer_id, app, external_user, user_email, created_by)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (customer_id, app, external_user) DO UPDATE SET user_email = EXCLUDED.user_email,
                 created_by = EXCLUDED.created_by, created_at = now()
               RETURNING external_user, user_email, created_by, created_at""",
            (customer_id, app, body.external_user, member["email"], user.actor),
        ).fetchone()
        audit.record(
            conn,
            user.actor,
            "commai.connector.identity",
            f"{app}:{body.external_user}",
            customer_id,
            {"email": member["email"]},
        )
    return row


@router.delete("/connectors/{app}/identities/{external_user}", status_code=204)
def unlink_identity(customer_id: str, app: str, external_user: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        cur = conn.execute(
            "DELETE FROM commai_chat_identities WHERE customer_id = %s AND app = %s AND external_user = %s",
            (customer_id, app, external_user),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Not linked.")
        audit.record(conn, user.actor, "commai.connector.identity_removed", f"{app}:{external_user}", customer_id)


# ---- Slack interactivity ---------------------------------------------------------------


def _slack_reply(text: str, replace: bool) -> JSONResponse:
    if replace:
        return JSONResponse({"replace_original": True, "text": text})
    return JSONResponse({"response_type": "ephemeral", "replace_original": False, "text": text})


@public.post("/slack/interactions", include_in_schema=False)
async def slack_interactions(request: Request) -> JSONResponse:
    """Approve and Reject buttons pressed in Slack. Checked with Slack request
    signing; the workspace must be the business's; the Slack user must be
    linked to a CommAI person; then the action service decides."""
    from ..connectors import slack

    raw = await request.body()
    secret = os.environ.get(slack.SIGNING_ENV, "")
    if not slack.verify_request(
        secret,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        raw,
        request.headers.get("X-Slack-Signature", ""),
    ):
        raise HTTPException(401, "Signature check failed.")
    form = dict(urllib.parse.parse_qsl(raw.decode("utf-8", "replace")))
    try:
        payload = json.loads(form.get("payload", ""))
    except ValueError:
        raise HTTPException(400, "No payload.") from None
    if payload.get("type") != "block_actions":
        return JSONResponse({})
    act = next(
        (a for a in payload.get("actions") or [] if a.get("action_id") in ("commai_approve", "commai_reject")), None
    )
    if act is None:
        return JSONResponse({})
    try:
        value = json.loads(act.get("value") or "{}")
        customer_id, run_id = str(uuid.UUID(value["c"])), str(uuid.UUID(value["r"]))
    except (ValueError, KeyError, TypeError):
        return _slack_reply("That button is not valid.", False)
    decision = "approve" if act["action_id"] == "commai_approve" else "reject"
    team = (payload.get("team") or {}).get("id", "")
    slack_user = (payload.get("user") or {}).get("id", "")
    with db.tx() as conn:
        known = more_common.state_get(conn, customer_id, "slack", "team").get("id", "")
        if not known or known != team:
            return _slack_reply("This Slack workspace is not connected to that business.", False)
        approver = more_common.person_for(conn, customer_id, "slack", slack_user)
        if approver is None:
            return _slack_reply("Your Slack user is not linked to a CommAI person who may approve.", False)
        try:
            with conn.transaction():
                run = more_common.decide(conn, customer_id, run_id, decision, approver)
        except ActionRefused as e:
            return _slack_reply(f"Not done: {e}", False)
        audit.record(conn, approver, f"commai.action.{decision}", run_id, customer_id, {"via": "slack"})
    word = "Approved" if run["status"] == "approved" else "Rejected"
    return _slack_reply(f"{word} by <@{slack_user}> in CommAI.", True)


# ---- Microsoft Teams bot -----------------------------------------------------------------


@router.post("/connectors/teams/link-code", status_code=201)
def teams_link_code(customer_id: str, user: UserDep) -> dict:
    """A one-time code (30 minutes) to send the CommAI bot in a Teams channel:
    "link <code>". Shown once."""
    access.check(user, customer_id, "commai:admin")
    from ..connectors import teams

    with db.tx() as conn:
        _admin(conn, user, customer_id)
        if connectors.connection(conn, customer_id, "teams") is None:
            raise HTTPException(409, "Connect Microsoft Teams first.")
        code = teams.link_code(conn, customer_id)
        audit.record(conn, user.actor, "commai.connector.teams_link_code", "teams", customer_id)
    return {"code": code, "say": f"link {code}", "expires_in_s": teams.LINK_TTL_S}


@public.post("/teams/messages", include_in_schema=False)
async def teams_messages(request: Request) -> JSONResponse:
    """The Bot Framework messaging endpoint. Each request carries a JWT that
    must be Microsoft's, for our bot; then: "link <code>" links the channel,
    and Approve or Reject presses go to the action service."""
    import hashlib
    import re
    import time

    from ..connectors import teams

    try:
        activity = json.loads(await request.body())
    except ValueError:
        raise HTTPException(400, "Not JSON.") from None
    service_url = str(activity.get("serviceUrl", ""))
    if not re.fullmatch(teams.SERVICE_URL, service_url):
        raise HTTPException(401, "Unknown service.")
    if teams.verify_bot_token(request.headers.get("Authorization", ""), service_url) is None:
        raise HTTPException(401, "Token check failed.")
    if activity.get("type") != "message":
        return JSONResponse({})
    conv = activity.get("conversation") or {}
    tenant = str(conv.get("tenantId") or ((activity.get("channelData") or {}).get("tenant") or {}).get("id") or "")
    sender = str((activity.get("from") or {}).get("aadObjectId") or "")
    value = activity.get("value") if isinstance(activity.get("value"), dict) else {}
    text = re.sub(r"<at>.*?</at>", "", str(activity.get("text") or "")).strip()
    with db.tx() as conn:
        m = re.fullmatch(r"link\s+([0-9A-Fa-f]{8})", text)
        if m:
            digest = hashlib.sha256(m.group(1).upper().encode()).hexdigest()
            owners = more_common.customer_for(conn, "teams", "link_code", digest)
            if len(owners) != 1:
                return JSONResponse({"type": "message", "text": "That code is not valid."})
            cid = owners[0]
            code = more_common.state_get(conn, cid, "teams", "link_code")
            if code.get("expires", 0) < time.time():
                return JSONResponse({"type": "message", "text": "That code has expired. Make a new one in CommAI."})
            more_common.state_put(conn, cid, "teams", "link_code", {})
            more_common.state_put(
                conn,
                cid,
                "teams",
                "bot_conversation",
                {"id": tenant, "service_url": service_url, "conversation_id": str(conv.get("id", ""))},
            )
            audit.record(conn, f"teams:{sender}", "commai.connector.teams_linked", "teams", cid)
            connectors.get("teams").bot_reply(conn, cid, "This channel is now linked to CommAI.")
            return JSONResponse({"type": "message", "text": "This channel is now linked to CommAI."})
        if value.get("commai") not in ("approve", "reject"):
            return JSONResponse({})
        try:
            customer_id, run_id = str(uuid.UUID(str(value["c"]))), str(uuid.UUID(str(value["r"])))
        except (ValueError, KeyError):
            return JSONResponse({"type": "message", "text": "That button is not valid."})
        linked = more_common.state_get(conn, customer_id, "teams", "bot_conversation")
        if not tenant or linked.get("id") != tenant:
            return JSONResponse({"type": "message", "text": "This Teams organisation is not linked to that business."})
        approver = more_common.person_for(conn, customer_id, "teams", sender)
        if approver is None:
            return JSONResponse({"type": "message", "text": "Your Teams user is not linked to a CommAI person."})
        try:
            with conn.transaction():
                run = more_common.decide(conn, customer_id, run_id, value["commai"], approver)
        except ActionRefused as e:
            return JSONResponse({"type": "message", "text": f"Not done: {e}"})
        audit.record(conn, approver, f"commai.action.{value['commai']}", run_id, customer_id, {"via": "teams"})
        word = "Approved" if run["status"] == "approved" else "Rejected"
        connectors.get("teams").bot_reply(conn, customer_id, f"{word} in CommAI by {approver[5:]}.")
    # Bot Framework does not show a reply in the HTTP answer; bot_reply posts it.
    return JSONResponse({"type": "message", "text": f"{word} in CommAI."})
