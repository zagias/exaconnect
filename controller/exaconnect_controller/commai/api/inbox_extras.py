"""Inbox, contact and widget extras (ADR 0032): service-target settings,
contact identities and history, typing and presence, staff attachments, and
the website visitor's AI browser call."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access, channels, events, history, inbox, inbox_jobs, ratelimit
from .common import page_after

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: inbox"])
public = APIRouter(tags=["commai: channels (public)"])


def _no_internal(conn, user, customer_id) -> None:
    if access.seat(conn, user, customer_id) == "internal":
        raise HTTPException(403, "Your seat can't change settings.")


# ---- service targets ---------------------------------------------------------------


class TargetsIn(BaseModel):
    reminders: bool = True
    remind_percent: int = Field(default=80, ge=10, le=99, description="Remind when this share of the time has gone")
    escalate: bool = True
    escalate_team_id: str | None = None


@router.get("/service-targets")
def get_service_targets(customer_id: str, user: UserDep) -> dict:
    """How reminders and escalations work for missed first-reply and resolution targets."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return inbox_jobs.target_settings(conn, customer_id)


@router.put("/service-targets")
def set_service_targets(customer_id: str, body: TargetsIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _no_internal(conn, user, customer_id)
        if (
            body.escalate_team_id
            and not conn.execute(
                "SELECT 1 FROM commai_teams WHERE id = %s AND customer_id = %s", (body.escalate_team_id, customer_id)
            ).fetchone()
        ):
            raise HTTPException(404, "Team not found.")
        inbox.settings(conn, customer_id)
        conn.execute(
            """UPDATE commai_settings SET config = jsonb_set(config, '{service_targets}', %s), updated_at = now()
               WHERE customer_id = %s""",
            (Jsonb(body.model_dump()), customer_id),
        )
        audit.record(conn, user.actor, "commai.service_targets.update", "", customer_id, body.model_dump())
        return inbox_jobs.target_settings(conn, customer_id)


# ---- API key rate limits ---------------------------------------------------------------------


class KeyRateIn(BaseModel):
    rate_per_min: int | None = Field(default=None, ge=1, le=100_000, description="None: the default budget")


@public.put("/rate-limits/keys/{key_id}")
def set_key_rate(key_id: int, body: KeyRateIn, user: UserDep) -> dict:
    """ExaCarib admins only: a key's own budget of requests a minute."""
    if user.role != "admin":
        raise HTTPException(403, "Admins only.")
    with db.tx() as conn:
        row = conn.execute(
            "UPDATE api_keys SET rate_per_min = %s WHERE id = %s RETURNING id, name, prefix, rate_per_min",
            (body.rate_per_min, key_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "API key not found.")
        audit.record(conn, user.actor, "commai.api_key.rate", str(key_id), detail=body.model_dump())
    return {**row, "default_per_min": ratelimit.default_limit()}


# ---- contacts: history and channel identities --------------------------------------------

events.register("contact.identity_added", "contact.identity_verified", "contact.identity_removed")


def _contact(conn, customer_id: str, contact_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM contacts WHERE id = %s AND customer_id = %s", (contact_id, customer_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Contact not found.")
    return row


def _identity(conn, customer_id: str, contact_id: str, identity_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM contact_identities WHERE id = %s AND contact_id = %s AND customer_id = %s",
        (identity_id, contact_id, customer_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Identity not found.")
    return row


IDENTITY_COLUMNS = "id, contact_id, channel, address, verified, opted_out, created_at"


@router.get("/contacts/{contact_id}/history")
def get_contact_history(customer_id: str, contact_id: str, user: UserDep, limit: int = 50) -> dict:
    """Past conversations, calls, open requests, linked records and bookings for one contact."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        contact = _contact(conn, customer_id, contact_id)
        return history.contact_history(conn, customer_id, contact, max(1, min(limit, 200)))


@router.get("/contacts/{contact_id}/identities")
def list_identities(
    customer_id: str, contact_id: str, user: UserDep, cursor: str | None = None, limit: int = 100
) -> list[dict] | dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _contact(conn, customer_id, contact_id)
        rows = conn.execute(
            f"SELECT {IDENTITY_COLUMNS} FROM contact_identities WHERE contact_id = %s ORDER BY created_at, id",
            (contact_id,),
        ).fetchall()
    return rows if cursor is None else page_after(rows, cursor, max(1, min(limit, 500)))


class IdentityIn(BaseModel):
    channel: str = Field(min_length=2, max_length=20, description="email, sms, whatsapp, web, voice...")
    address: str = Field(min_length=1, max_length=255)
    verified: bool = Field(default=False, description="Your own system has verified this person owns the address")
    evidence: str = Field(default="", max_length=300, description="How it was verified (required with verified)")


def _normalise(channel: str, address: str) -> str:
    from ..channels import providers

    address = address.strip()
    if channel == "email":
        address = address.lower()
        if "@" not in address or " " in address:
            raise HTTPException(422, "That email address doesn't look right.")
    elif channel in ("sms", "whatsapp", "voice", "phone"):
        address = providers.e164(address)
        if len(address) < 8:
            raise HTTPException(422, "Give the number with its country code, like +1 868 555 0100.")
    return address


@router.post("/contacts/{contact_id}/identities", status_code=201)
def add_identity(customer_id: str, contact_id: str, body: IdentityIn, user: UserDep) -> dict:
    """Add a channel identity to a contact. Marking it verified says your own
    system checked the person owns it; the customer AI then treats it as theirs
    (their history and memory), so say how in `evidence`."""
    access.check(user, customer_id, "commai:write")
    if body.verified and not body.evidence.strip():
        raise HTTPException(422, "Say how the address was verified.")
    if not channels.any_channel(body.channel) and body.channel != "phone":
        raise HTTPException(422, f"Unknown channel {body.channel}.")
    with db.tx() as conn:
        _contact(conn, customer_id, contact_id)
        address = _normalise(body.channel, body.address)
        existing = conn.execute(
            "SELECT * FROM contact_identities WHERE customer_id = %s AND channel = %s AND address = %s",
            (customer_id, body.channel, address),
        ).fetchone()
        if existing and str(existing["contact_id"]) != contact_id:
            raise HTTPException(409, "That address belongs to another contact. Merge or remove it there first.")
        if existing:
            raise HTTPException(409, "The contact already has that address.")
        row = conn.execute(
            f"""INSERT INTO contact_identities (customer_id, contact_id, channel, address, verified)
                VALUES (%s, %s, %s, %s, %s) RETURNING {IDENTITY_COLUMNS}""",
            (customer_id, contact_id, body.channel, address, body.verified),
        ).fetchone()
        detail = {"channel": body.channel, "verified": body.verified, "evidence": body.evidence.strip()}
        audit.record(conn, user.actor, "commai.contact.identity.add", str(row["id"]), customer_id, detail)
        events.emit(
            conn,
            customer_id,
            "contact.identity_added",
            {
                "contact_id": contact_id,
                "identity_id": str(row["id"]),
                "channel": body.channel,
                "verified": body.verified,
            },
            contact_id,
        )
    return row


class VerifyIn(BaseModel):
    verified: bool = True
    evidence: str = Field(default="", max_length=300)


@router.post("/contacts/{contact_id}/identities/{identity_id}/verify")
def verify_identity(customer_id: str, contact_id: str, identity_id: str, body: VerifyIn, user: UserDep) -> dict:
    """Mark an identity verified (or not). Verifying needs `evidence`; it is audited."""
    access.check(user, customer_id, "commai:write")
    if body.verified and not body.evidence.strip():
        raise HTTPException(422, "Say how the address was verified.")
    with db.tx() as conn:
        access.require_reply_seat(conn, user, customer_id)
        ident = _identity(conn, customer_id, contact_id, identity_id)
        row = conn.execute(
            f"UPDATE contact_identities SET verified = %s WHERE id = %s RETURNING {IDENTITY_COLUMNS}",
            (body.verified, ident["id"]),
        ).fetchone()
        audit.record(
            conn,
            user.actor,
            "commai.contact.identity.verify" if body.verified else "commai.contact.identity.unverify",
            identity_id,
            customer_id,
            {"evidence": body.evidence.strip(), "channel": ident["channel"]},
        )
        if body.verified and not ident["verified"]:
            events.emit(
                conn,
                customer_id,
                "contact.identity_verified",
                {"contact_id": contact_id, "identity_id": identity_id, "channel": ident["channel"]},
                contact_id,
            )
    return row


@router.delete("/contacts/{contact_id}/identities/{identity_id}", status_code=204)
def remove_identity(customer_id: str, contact_id: str, identity_id: str, user: UserDep) -> None:
    """Remove an address from a contact. Past conversations stay; a new message
    from that address starts a new contact."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn:
        access.require_reply_seat(conn, user, customer_id)
        ident = _identity(conn, customer_id, contact_id, identity_id)
        conn.execute("DELETE FROM contact_identities WHERE id = %s", (ident["id"],))
        audit.record(
            conn, user.actor, "commai.contact.identity.remove", identity_id, customer_id, {"channel": ident["channel"]}
        )
        events.emit(
            conn,
            customer_id,
            "contact.identity_removed",
            {"contact_id": contact_id, "identity_id": identity_id, "channel": ident["channel"]},
            contact_id,
        )
