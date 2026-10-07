"""Jibsy voice API (ADR 0021): phone system, self-service, orders and billing.

Who may call what:
- staff (any account of the business): /voice/me..., /voice/say...
- the business's voice admins: the phone system, orders, fraud limits
- spend permission: any change that alters the bill, and approving an order
- ExaCarib admins: prices (rate cards, bundles), invoices, ports, supplier side
Every write is audited. Money is always a string, never a float.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access
from ..voice import billing, bulk, carriers, config, freeswitch, perms, provisioning, selfservice
from ..voice import provider as providers
from ..voice.common import VoiceError, month_start, now, policy, set_policy
from .common import errors

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: voice"])
public = APIRouter(prefix="/voice", tags=["commai: voice devices"])


def clean(v: Any) -> Any:
    """Decimals to strings (never floats), recursively."""
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items() if k not in ("sip_password", "token_hash")}
    if isinstance(v, list):
        return [clean(x) for x in v]
    return v


def _period(p: str | None) -> dt.date:
    if not p:
        return month_start(now())
    if not re.fullmatch(r"\d{4}-\d{2}", p):
        raise HTTPException(422, "Give the month as YYYY-MM.")
    return dt.date(int(p[:4]), int(p[5:]), 1)


def _admin(conn, user, cid) -> None:
    perms.require_voice_admin(conn, user, cid)


# ---- overview and settings -------------------------------------------------------------


@router.get("/voice")
def overview(customer_id: str, user: UserDep) -> dict:
    """The phone system. Voice admins see everything; staff see their own extension."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        is_admin = perms.is_voice_admin(conn, user, customer_id)
        out: dict = {
            "is_admin": is_admin,
            "can_spend": perms.can_spend(conn, user, customer_id),
            "is_exacarib": user.role == "admin",
            "policy": policy(conn, customer_id),
            "provider": {"name": providers.get().name, "live": providers.get().live},
        }
        try:
            out["me"] = selfservice.mine(conn, customer_id, user.id)
        except VoiceError:
            out["me"] = None
        if not is_admin:
            return clean(out)
        snap = config.snapshot(conn, customer_id)
        out.update(snap)
        out["users"] = conn.execute(
            """SELECT v.id, v.name, v.email, v.mobile, v.extension, v.site_id, s.name AS site, v.team_id,
                      t.name AS team, v.user_id, u.email AS portal_email, v.forward_to, v.dnd, v.billing_from,
                      v.emergency_address
               FROM voice_users v LEFT JOIN voice_sites s ON s.id = v.site_id
               LEFT JOIN commai_teams t ON t.id = v.team_id LEFT JOIN users u ON u.id = v.user_id
               WHERE v.customer_id = %s AND v.status = 'active' ORDER BY v.extension""",
            (customer_id,),
        ).fetchall()
        out["numbers"] = conn.execute(
            """SELECT n.id, n.e164, n.source, n.status, n.target_type, n.target_id, n.site_id, s.name AS site,
                      n.emergency_address, n.billing_from, n.country, n.outbound_enabled, n.outbound_reason
               FROM voice_numbers n LEFT JOIN voice_sites s ON s.id = n.site_id
               WHERE n.customer_id = %s AND n.status <> 'removed' ORDER BY n.e164""",
            (customer_id,),
        ).fetchall()
        out["devices"] = conn.execute(
            """SELECT d.id, d.kind, d.mac, d.model, d.status, d.last_seen_at, v.extension, v.name AS user_name
               FROM voice_devices d LEFT JOIN voice_users v ON v.id = d.voice_user_id
               WHERE d.customer_id = %s AND d.status <> 'removed' ORDER BY v.extension, d.kind""",
            (customer_id,),
        ).fetchall()
        out["teams"] = conn.execute(
            "SELECT id, name FROM commai_teams WHERE customer_id = %s ORDER BY name", (customer_id,)
        ).fetchall()
        out["version"] = config.current_version(conn, customer_id)
        out["scheduled"] = conn.execute(
            """SELECT id, summary, run_at, status, error, created_by, version, price_impact FROM voice_changes
               WHERE customer_id = %s ORDER BY run_at DESC LIMIT 50""",
            (customer_id,),
        ).fetchall()
        out["pbx"] = conn.execute(
            "SELECT digest, rendered_at, written_to FROM voice_pbx_renders WHERE customer_id = %s", (customer_id,)
        ).fetchone()
    return clean(out)


class PolicyIn(BaseModel):
    recording_access: str = Field(pattern="^(none|own|admins)$")


@router.put("/voice/policy")
def put_policy(customer_id: str, body: PolicyIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        out = set_policy(conn, customer_id, body.model_dump())
        audit.record(conn, user.actor, "commai.voice.policy", "", customer_id, body.model_dump())
    return out


@router.get("/voice/permissions")
def get_permissions(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        rows = conn.execute(
            """SELECT u.id AS user_id, u.email, coalesce(p.voice_admin, false) AS voice_admin,
                      coalesce(p.spend, false) AS spend
               FROM org_memberships m JOIN users u ON u.id = m.user_id
               LEFT JOIN voice_permissions p ON p.user_id = u.id AND p.customer_id = m.customer_id
               WHERE m.customer_id = %s ORDER BY u.email""",
            (customer_id,),
        ).fetchall()
        return {"configured": perms.configured(conn, customer_id), "people": rows}


class PersonPerm(BaseModel):
    user_id: str
    voice_admin: bool = False
    spend: bool = False


class PermissionsIn(BaseModel):
    people: list[PersonPerm] = Field(max_length=1000)


@router.put("/voice/permissions")
def put_permissions(customer_id: str, body: PermissionsIn, user: UserDep) -> dict:
    """Name the business's voice admins and who has spend permission. Giving
    spend permission needs spend permission; at least one person must keep it."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        if any(p.spend for p in body.people):
            perms.require_spend(conn, user, customer_id)
        if not any(p.spend and p.voice_admin for p in body.people):
            raise HTTPException(422, "At least one voice admin must keep spend permission.")
        for p in body.people:
            ok = conn.execute(
                "SELECT 1 FROM org_memberships WHERE user_id = %s AND customer_id = %s", (p.user_id, customer_id)
            ).fetchone()
            if not ok:
                raise HTTPException(422, "Everyone must be an account in this company.")
        conn.execute("DELETE FROM voice_permissions WHERE customer_id = %s", (customer_id,))
        for p in body.people:
            if p.voice_admin or p.spend:
                conn.execute(
                    "INSERT INTO voice_permissions (customer_id, user_id, voice_admin, spend) VALUES (%s, %s, %s, %s)",
                    (customer_id, p.user_id, p.voice_admin or p.spend, p.spend),
                )
        audit.record(
            conn,
            user.actor,
            "commai.voice.permissions",
            "",
            customer_id,
            {"people": [p.model_dump() for p in body.people]},
        )
    return get_permissions(customer_id, user)


# ---- changes -----------------------------------------------------------------------------


class Price(BaseModel):
    monthly_delta: str
    one_time: str


class ChangeIn(BaseModel):
    ops: list[dict] = Field(min_length=1, max_length=2000)
    accepted_price: Price | None = None
    run_at: dt.datetime | None = None
    summary: str = Field(default="", max_length=300)


def _device_links(request: Request, results: list[dict]) -> list[dict]:
    base = str(request.base_url).rstrip("/")
    for r in results:
        tok = r.pop("token", None)
        if not tok:
            continue
        if r.get("kind") == "desk":
            r["setup_url"] = f"{base}/api/v1/commai/voice/provision/{tok}/{r['mac']}.cfg"
        else:
            r["join_link"] = f"{base}/api/v1/commai/voice/softphone/join?token={tok}"
    return results


@router.post("/voice/preview")
def preview(customer_id: str, body: ChangeIn, user: UserDep) -> dict:
    """Try a change without saving it: errors, price impact and what would change."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        out = config.apply(conn, customer_id, body.ops, actor=user.actor, dry_run=True)
        out["needs_spend"] = out["price_impact"]["changes_bill"]
        out["can_spend"] = perms.can_spend(conn, user, customer_id)
    return clean(out)


@router.post("/voice/changes", status_code=201)
def save_change(customer_id: str, body: ChangeIn, user: UserDep, request: Request) -> dict:
    """Apply a change now, or at `run_at` through the job queue. If the bill
    changes, `accepted_price` must match the preview and the caller needs
    spend permission."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        spend = perms.can_spend(conn, user, customer_id)
        accepted = body.accepted_price.model_dump() if body.accepted_price else None
        if body.run_at is not None:
            if body.run_at.tzinfo is None:
                raise HTTPException(422, "Give the time with its time zone.")
            if body.run_at <= now():
                raise HTTPException(422, "Choose a time in the future, or apply it now.")
            out = config.schedule(
                conn, customer_id, body.ops, body.run_at, user.actor, can_spend=spend, accepted_price=accepted
            )
        else:
            out = config.apply(
                conn,
                customer_id,
                body.ops,
                actor=user.actor,
                summary=body.summary,
                can_spend=spend,
                accepted_price=accepted,
            )
        if not out.get("ok"):
            raise HTTPException(422, "; ".join(f"Change {e['index'] + 1}: {e['error']}" for e in out["errors"]))
        out["results"] = _device_links(request, out.get("results", []))
    return clean(out)


@router.post("/voice/changes/{change_id}/cancel")
def cancel_change(customer_id: str, change_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        row = conn.execute(
            """UPDATE voice_changes SET status = 'cancelled', finished_at = now()
               WHERE id = %s AND customer_id = %s AND status = 'scheduled' RETURNING id, summary, status""",
            (change_id, customer_id),
        ).fetchone()
        if row is None:
            raise HTTPException(409, "Only a change that hasn't run yet can be cancelled.")
        audit.record(conn, user.actor, "commai.voice.schedule_cancel", change_id, customer_id)
    return row


@router.get("/voice/versions")
def versions(customer_id: str, user: UserDep, limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        return clean(
            conn.execute(
                """SELECT version, kind, summary, created_by, created_at, price_impact,
                      jsonb_array_length(ops) AS changes FROM voice_config_versions
               WHERE customer_id = %s ORDER BY version DESC LIMIT %s""",
                (customer_id, limit),
            ).fetchall()
        )


@router.get("/voice/versions/{version}")
def version_detail(customer_id: str, version: int, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        rows = conn.execute(
            """SELECT version, kind, summary, ops, snapshot, created_by, created_at FROM voice_config_versions
               WHERE customer_id = %s AND version IN (%s, %s) ORDER BY version""",
            (customer_id, version - 1, version),
        ).fetchall()
        cur = next((r for r in rows if r["version"] == version), None)
        if cur is None:
            raise HTTPException(404, "There is no such version.")
        prev = next((r for r in rows if r["version"] == version - 1), None)
        cur["diff"] = config.diff(prev["snapshot"] if prev else {}, cur["snapshot"])
        return clean(cur)


class RollbackIn(BaseModel):
    preview: bool = False


@router.post("/voice/versions/{version}/rollback")
def rollback(customer_id: str, version: int, body: RollbackIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        return clean(config.rollback(conn, customer_id, version, user.actor, dry_run=body.preview))


class BulkIn(BaseModel):
    csv: str = Field(max_length=2_000_000)
    apply: bool = False
    accepted_price: Price | None = None


@router.get("/voice/bulk/template", response_class=PlainTextResponse)
def bulk_template(customer_id: str, user: UserDep) -> str:
    access.check(user, customer_id, "commai:read")
    return bulk.TEMPLATE


@router.post("/voice/bulk")
def bulk_change(customer_id: str, body: BulkIn, user: UserDep, request: Request) -> dict:
    """Check a CSV row by row; with apply=true and no errors, save it as one version."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        ops, row_errors, rows = bulk.parse(body.csv)
        out: dict = {"rows": len(ops) + len(row_errors), "errors": row_errors, "applied": False}
        if ops:
            dry = config.apply(conn, customer_id, ops, actor=user.actor, dry_run=True)
            out["errors"] += [{"row": rows[e["index"]], "error": e["error"]} for e in dry["errors"]]
            out["price_impact"] = dry["price_impact"]
            out["diff"] = dry["diff"]
        out["errors"].sort(key=lambda e: e["row"])
        if body.apply and not out["errors"] and ops:
            spend = perms.can_spend(conn, user, customer_id)
            accepted = body.accepted_price.model_dump() if body.accepted_price else None
            res = config.apply(
                conn,
                customer_id,
                ops,
                actor=user.actor,
                kind="bulk",
                summary=f"Bulk: {len(ops)} rows",
                can_spend=spend,
                accepted_price=accepted,
            )
            out.update({"applied": True, "version": res["version"], "results": _device_links(request, res["results"])})
    return clean(out)


# ---- self-service ------------------------------------------------------------------------


@router.get("/voice/me")
def get_me(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, errors():
        me = selfservice.mine(conn, customer_id, user.id)
        me["recording_access"] = policy(conn, customer_id)["recording_access"]
        return clean(me)


class MeIn(BaseModel):
    forward_to: str | None = Field(default=None, max_length=40)
    forward_until: dt.datetime | None = None
    dnd: bool | None = None
    dnd_until: dt.datetime | None = None
    voicemail_greeting: str | None = Field(default=None, max_length=500)
    voicemail_to_email: bool | None = None


@router.patch("/voice/me")
def patch_me(customer_id: str, body: MeIn, user: UserDep) -> dict:
    """Change your own forwarding, do not disturb and voicemail. Nobody can
    change another person's settings here: the extension is always yours."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        return clean(
            selfservice.update_mine(conn, customer_id, user.id, body.model_dump(exclude_unset=True), user.actor)
        )


@router.get("/voice/me/calls")
def my_calls(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, errors():
        me = selfservice.mine(conn, customer_id, user.id)
        return selfservice.calls(conn, customer_id, me["id"])


@router.get("/voice/me/recordings")
def my_recordings(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, errors():
        me = selfservice.mine(conn, customer_id, user.id)
        return selfservice.recordings(conn, customer_id, me["id"], perms.is_voice_admin(conn, user, customer_id))


class SayIn(BaseModel):
    text: str = Field(min_length=2, max_length=500)


@router.post("/voice/say")
def say(customer_id: str, body: SayIn, user: UserDep) -> dict:
    """Turn a sentence into the exact proposed change. Nothing changes until
    the same person confirms it."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        try:
            me = selfservice.mine(conn, customer_id, user.id)
        except VoiceError:
            me = None
        is_admin = perms.is_voice_admin(conn, user, customer_id)
        parsed = selfservice.parse(conn, customer_id, body.text, me, is_admin)
        if not parsed["understood"]:
            return {"understood": False, "message": parsed["message"]}
        out: dict = {"understood": True, "summary": parsed["summary"], "scope": parsed["scope"]}
        if parsed["scope"] == "admin":
            dry = config.apply(conn, customer_id, parsed["ops"], actor=user.actor, dry_run=True)
            if dry["errors"]:
                return {"understood": False, "message": dry["errors"][0]["error"]}
            out["price_impact"] = dry["price_impact"]
        prop = selfservice.propose(conn, customer_id, user.id, body.text, parsed)
        out.update({"id": prop["id"], "change": prop["ops"], "expires_at": prop["expires_at"]})
        audit.record(conn, user.actor, "commai.voice.say", str(prop["id"]), customer_id, {"scope": parsed["scope"]})
    return clean(out)


class ConfirmIn(BaseModel):
    accepted_price: Price | None = None


@router.post("/voice/say/{proposal_id}/confirm")
def confirm_say(customer_id: str, proposal_id: str, body: ConfirmIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        p = conn.execute(
            """SELECT * FROM voice_chat_proposals WHERE id = %s AND customer_id = %s AND user_id = %s
               FOR UPDATE""",
            (proposal_id, customer_id, user.id),
        ).fetchone()
        if p is None:
            raise HTTPException(404, "Proposal not found.")
        if p["status"] != "proposed":
            raise HTTPException(409, "This proposal has already been used or cancelled.")
        if p["expires_at"] < now():
            raise HTTPException(409, "This proposal has expired. Say it again.")
        if p["scope"] == "self":
            result = clean(selfservice.update_mine(conn, customer_id, user.id, p["ops"], user.actor))
        else:
            access.require_scope(user, "commai:admin")
            _admin(conn, user, customer_id)
            accepted = body.accepted_price.model_dump() if body.accepted_price else None
            result = clean(
                config.apply(
                    conn,
                    customer_id,
                    p["ops"],
                    actor=user.actor,
                    kind="chat",
                    summary=p["summary"],
                    can_spend=perms.can_spend(conn, user, customer_id),
                    accepted_price=accepted,
                )
            )
        conn.execute("UPDATE voice_chat_proposals SET status = 'applied' WHERE id = %s", (proposal_id,))
        audit.record(conn, user.actor, "commai.voice.say_confirm", proposal_id, customer_id)
    return {"applied": True, "summary": p["summary"], "result": result}


@router.post("/voice/say/{proposal_id}/cancel")
def cancel_say(customer_id: str, proposal_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn:
        row = conn.execute(
            """UPDATE voice_chat_proposals SET status = 'cancelled' WHERE id = %s AND customer_id = %s
               AND user_id = %s AND status = 'proposed' RETURNING id""",
            (proposal_id, customer_id, user.id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Proposal not found.")
    return {"cancelled": True}


# ---- orders, ports and devices -------------------------------------------------------------


class OrderIn(BaseModel):
    items: dict


@router.get("/voice/orders")
def list_orders(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        return clean(
            conn.execute(
                """SELECT id, status, price, failed_step, error, created_by, approved_by, created_at, activated_at,
                      jsonb_array_length(coalesce(items->'users', '[]')) AS users
               FROM voice_orders WHERE customer_id = %s ORDER BY created_at DESC LIMIT 100""",
                (customer_id,),
            ).fetchall()
        )


@router.post("/voice/orders", status_code=201)
def create_order(customer_id: str, body: OrderIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        o = provisioning.create(conn, customer_id, body.items, user.actor)
        audit.record(conn, user.actor, "commai.voice.order_create", str(o["id"]), customer_id, {"price": o["price"]})
    return clean(o)


@router.get("/voice/orders/{order_id}")
def get_order(customer_id: str, order_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        return clean(provisioning.get(conn, customer_id, order_id))


class ApproveIn(BaseModel):
    accepted_price: Price


@router.post("/voice/orders/{order_id}/approve")
def approve_order(customer_id: str, order_id: str, body: ApproveIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        o = provisioning.approve(
            conn,
            customer_id,
            order_id,
            user.actor,
            can_spend=perms.can_spend(conn, user, customer_id),
            accepted_price=body.accepted_price.model_dump(),
        )
        audit.record(conn, user.actor, "commai.voice.order_approve", order_id, customer_id, {"price": o["price"]})
    return clean(o)


@router.post("/voice/orders/{order_id}/retry")
def retry_order(customer_id: str, order_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        o = provisioning.retry(conn, customer_id, order_id, user.actor)
        audit.record(conn, user.actor, "commai.voice.order_retry", order_id, customer_id)
    return clean(o)


@router.post("/voice/orders/{order_id}/cancel")
def cancel_order(customer_id: str, order_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        o = provisioning.cancel(conn, customer_id, order_id)
        audit.record(conn, user.actor, "commai.voice.order_cancel", order_id, customer_id)
    return clean(o)


@router.get("/voice/ports")
def list_ports(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        return conn.execute(
            "SELECT * FROM voice_port_orders WHERE customer_id = %s ORDER BY created_at DESC", (customer_id,)
        ).fetchall()


class PortIn(BaseModel):
    status: str
    switch_date: str | None = None
    note: str = Field(default="", max_length=300)


@router.post("/voice/ports/{port_id}")
def update_port(customer_id: str, port_id: str, body: PortIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    perms.require_exacarib(user)
    with db.tx() as conn, errors():
        return provisioning.set_port(conn, customer_id, port_id, body.status, body.switch_date, body.note, user.actor)


@router.post("/voice/devices/{device_id}/link")
def device_link(customer_id: str, device_id: str, user: UserDep, request: Request) -> dict:
    """A fresh set-up link: the URL a desk phone loads by MAC, or the
    softphone sign-in link (also the text for its QR code). Shown once."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        d = provisioning.issue_device_link(conn, customer_id, device_id)
        audit.record(conn, user.actor, "commai.voice.device_link", device_id, customer_id)
    return _device_links(
        request, [{"device_id": str(d["id"]), "kind": d["kind"], "mac": d["mac"], "token": d["token"]}]
    )[0]


@router.get("/voice/pbx")
def pbx_config(customer_id: str, user: UserDep) -> dict:
    """The rendered FreeSWITCH configuration (no passwords: a1-hashes only)."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        out = freeswitch.render_business(conn, customer_id)
        audit.record(conn, user.actor, "commai.voice.render", out["digest"][:12], customer_id)
    return {
        **out,
        "live": False,
        "note": "Not live: no SIP provider account yet (phase 3). Calls between extensions work once FreeSWITCH runs.",
    }


# ---- calls -----------------------------------------------------------------------------------


class AuthoriseIn(BaseModel):
    to: str = Field(max_length=40)
    from_number: str | None = Field(default=None, max_length=40)
    voice_user_id: str | None = None


@router.post("/voice/calls/authorise")
def authorise_call(customer_id: str, body: AuthoriseIn, user: UserDep) -> dict:
    """Asked by the PBX before an outside call: fraud limits stop calls here."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        return billing.authorise(
            conn, customer_id, body.to, voice_user_id=body.voice_user_id, from_number=body.from_number
        )


class CallIn(BaseModel):
    call_id: str = Field(min_length=1, max_length=120)
    direction: str = Field(default="outbound", pattern="^(outbound|inbound|internal)$")
    from_number: str = Field(default="", max_length=40)
    to_number: str = Field(default="", max_length=40)
    voice_user_id: str | None = None
    started_at: dt.datetime
    ended_at: dt.datetime
    seconds: int | None = Field(default=None, ge=0, le=86400)
    ai_seconds: int = Field(default=0, ge=0, le=86400)
    status: str = Field(default="completed", pattern="^(completed|blocked|failed|missed)$")
    block_reason: str = Field(default="", max_length=300)
    recording_ref: str = Field(default="", max_length=300)
    provider_ref: str = Field(default="", max_length=120)


@router.post("/voice/calls", status_code=201)
def post_call(customer_id: str, body: CallIn, user: UserDep) -> dict:
    """A call record at the end of a call. Rated at once; posting the same
    call_id again changes nothing."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        cdr = billing.record_call(conn, customer_id, body.model_dump())
        audit.record(conn, user.actor, "commai.voice.cdr", body.call_id, customer_id)
        charges = conn.execute("SELECT * FROM voice_charges WHERE cdr_id = %s ORDER BY kind", (cdr["id"],)).fetchall()
    return clean({**cdr, "charges": charges})


class SimCallIn(BaseModel):
    extension: str = Field(max_length=6)
    from_number: str | None = Field(default=None, max_length=40)
    to: str = Field(max_length=40)
    seconds: int = Field(ge=1, le=14400)
    ai_seconds: int = Field(default=0, ge=0, le=14400)


@router.post("/voice/calls/simulate", status_code=201)
def simulate_call(customer_id: str, body: SimCallIn, user: UserDep) -> dict:
    """A simulated outside call (no SIP provider yet): authorised against the
    fraud limits, then recorded and rated like a real one."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        vu = conn.execute(
            "SELECT id FROM voice_users WHERE customer_id = %s AND extension = %s AND status = 'active'",
            (customer_id, body.extension),
        ).fetchone()
        if vu is None:
            raise HTTPException(422, f"There is no extension {body.extension}.")
        to = config.norm_e164(body.to) if len(re.sub(r"\D", "", body.to)) > 4 else body.to
        verdict = billing.authorise(conn, customer_id, to, voice_user_id=vu["id"], from_number=body.from_number)
        call_id = f"sim-{uuid.uuid4().hex[:16]}"
        routed = None
        if verdict["allowed"] and verdict.get("route"):
            # Several carriers (ADR 0033): try each in order until one takes the call.
            routed = carriers.connect(conn, customer_id, call_id, to, verdict["route"])
            if not routed["ok"]:
                verdict = {**verdict, "allowed": False, "reason": routed["reason"]}
        end = now()
        call = {
            "call_id": call_id,
            "direction": "outbound",
            "from_number": body.extension,
            "to_number": to,
            "voice_user_id": vu["id"],
            "started_at": end - dt.timedelta(seconds=body.seconds if verdict["allowed"] else 0),
            "ended_at": end,
            "seconds": body.seconds if verdict["allowed"] else 0,
            "ai_seconds": body.ai_seconds if verdict["allowed"] else 0,
            "status": "completed" if verdict["allowed"] else "blocked",
            "block_reason": verdict["reason"],
        }
        call["provider_ref"] = call["call_id"]
        if routed is not None and not routed["ok"]:
            call["status"] = "failed"
        cdr = billing.record_call(conn, customer_id, call)
        if routed and routed["ok"]:
            carriers.attach(conn, cdr, routed["carrier"])
            cdr = {**cdr, "carrier": routed["carrier"]}
        audit.record(conn, user.actor, "commai.voice.simulated_call", cdr["call_id"], customer_id)
        charges = conn.execute("SELECT * FROM voice_charges WHERE cdr_id = %s ORDER BY kind", (cdr["id"],)).fetchall()
    return clean(
        {
            **cdr,
            "allowed": verdict["allowed"],
            "reason": verdict["reason"],
            "charges": charges,
            "route": [{"carrier": a["carrier"], "ok": a["ok"]} for a in (routed or {}).get("attempts", [])],
        }
    )


@router.get("/voice/calls")
def list_calls(customer_id: str, user: UserDep, limit: int = Query(100, ge=1, le=500)) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        return clean(
            conn.execute(
                """SELECT d.call_id, d.direction, d.from_number, d.to_number, d.started_at, d.ended_at, d.seconds,
                      d.ai_seconds, d.status, d.block_reason, v.name AS user_name, v.extension,
                      (SELECT sum(amount) FROM voice_charges c WHERE c.cdr_id = d.id) AS amount,
                      (SELECT max(rate_card_version) FROM voice_charges c WHERE c.cdr_id = d.id) AS rate_card_version
               FROM voice_cdrs d LEFT JOIN voice_users v ON v.id = d.voice_user_id
               WHERE d.customer_id = %s ORDER BY d.ended_at DESC LIMIT %s""",
                (customer_id, limit),
            ).fetchall()
        )


# ---- billing ----------------------------------------------------------------------------------


@router.get("/voice/rate-cards")
def rate_cards(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return clean(billing.cards(conn, customer_id))


class CardIn(BaseModel):
    label: str = Field(default="", max_length=120)
    currency: str = Field(default="USD", pattern="^[A-Z]{3}$")
    monthly_user: str
    monthly_number: str
    ai_minute: str
    one_time: dict[str, str] = Field(default_factory=dict)
    destinations: list[dict] = Field(min_length=1, max_length=500)
    effective_from: dt.datetime | None = None
    example: bool = False


@router.post("/voice/rate-cards", status_code=201)
def new_rate_card(customer_id: str, body: CardIn, user: UserDep) -> dict:
    """A new version of the business's rate card. Old versions never change;
    calls are rated with the version in force when they ended."""
    access.check(user, customer_id, "commai:admin")
    perms.require_exacarib(user)
    with db.tx() as conn, errors():
        card = billing.new_card(
            conn, customer_id, body.model_dump(exclude={"effective_from"}), user.actor, body.effective_from
        )
        audit.record(conn, user.actor, "commai.voice.rate_card", f"v{card['version']}", customer_id)
    return clean(card)


@router.get("/voice/spend")
def spend(customer_id: str, user: UserDep, period: str | None = None) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        return billing.spend(conn, customer_id, _period(period))


class BundleIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    minutes: str
    prefixes: list[str] = Field(default_factory=list, max_length=50)
    alert_pct: int = 80
    active: bool = True


@router.post("/voice/bundles", status_code=201)
def save_bundle(customer_id: str, body: BundleIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    perms.require_exacarib(user)
    with db.tx() as conn, errors():
        out = billing.save_bundle(conn, customer_id, body.model_dump())
        audit.record(conn, user.actor, "commai.voice.bundle", body.name, customer_id, body.model_dump())
    return clean(out)


@router.get("/voice/fraud-limits")
def get_fraud(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        return billing.fraud_limits(conn, customer_id)


class FraudIn(BaseModel):
    daily_cap: str | None = None
    blocked_prefixes: list[str] = Field(default_factory=list, max_length=200)
    international: bool = True
    calls_per_hour_alert: int = Field(default=60, ge=1, le=100000)


@router.put("/voice/fraud-limits")
def put_fraud(customer_id: str, body: FraudIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        out = billing.set_fraud_limits(conn, customer_id, body.model_dump(), user.actor)
        audit.record(conn, user.actor, "commai.voice.fraud_limits", "", customer_id, body.model_dump())
        freeswitch.render_business(conn, customer_id)  # blocked destinations are in the dial plan too
    return out


@router.get("/voice/invoices")
def list_invoices(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        return billing.invoices(conn, customer_id)


@router.get("/voice/invoices/{invoice_id}")
def get_invoice(customer_id: str, invoice_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        return clean(billing.get_invoice(conn, customer_id, invoice_id))


class DraftIn(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")


@router.post("/voice/invoices/draft", status_code=201)
def draft_invoice(customer_id: str, body: DraftIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    perms.require_exacarib(user)
    with db.tx() as conn, errors():
        inv = billing.draft_invoice(conn, customer_id, _period(body.period), user.actor)
        audit.record(conn, user.actor, "commai.voice.invoice_draft", str(inv["id"]), customer_id)
    return clean(inv)


@router.post("/voice/invoices/{invoice_id}/issue")
def issue_invoice(customer_id: str, invoice_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    perms.require_exacarib(user)
    with db.tx() as conn, errors():
        inv = billing.issue_invoice(conn, customer_id, invoice_id, user.actor)
        audit.record(conn, user.actor, "commai.voice.invoice_issue", inv["number"], customer_id)
    return clean(inv)


class CreditLine(BaseModel):
    line_id: int
    amount: str | None = None


class CreditIn(BaseModel):
    lines: list[CreditLine] = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=3, max_length=300)


@router.post("/voice/invoices/{invoice_id}/credit", status_code=201)
def credit(customer_id: str, invoice_id: str, body: CreditIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    perms.require_exacarib(user)
    with db.tx() as conn, errors():
        note = billing.credit_note(
            conn, customer_id, invoice_id, [ln.model_dump() for ln in body.lines], body.reason, user.actor
        )
        audit.record(
            conn,
            user.actor,
            "commai.voice.credit_note",
            note["number"],
            customer_id,
            {"invoice": invoice_id, "reason": body.reason},
        )
    return clean(note)


class SupplierIn(BaseModel):
    supplier: str = Field(default="simulated", max_length=60)
    csv: str = Field(max_length=5_000_000)
    filename: str = Field(default="", max_length=200)


@router.post("/voice/supplier/import", status_code=201)
def supplier_import(customer_id: str, body: SupplierIn, user: UserDep) -> dict:
    """Import the SIP provider's charges (columns: call_ref, started_at,
    destination, seconds, cost). Rows match calls of any business."""
    access.check(user, customer_id, "commai:admin")
    perms.require_exacarib(user)
    with db.tx() as conn, errors():
        out = billing.import_supplier_csv(conn, body.supplier, body.csv, body.filename, user.actor)
        audit.record(
            conn,
            user.actor,
            "commai.voice.supplier_import",
            str(out["id"]),
            customer_id,
            {"added": out["added"], "rejected": len(out["rejected"])},
        )
    return clean(out)


@router.get("/voice/supplier/reconcile")
def supplier_reconcile(customer_id: str, user: UserDep, period: str | None = None) -> dict:
    access.check(user, customer_id, "commai:read")
    perms.require_exacarib(user)
    with db.tx() as conn:
        return billing.reconcile(conn, customer_id, _period(period))


# ---- devices (no sign-in: the device token is the credential) ------------------------------


@public.get("/provision/{token}/{mac}.cfg", response_class=PlainTextResponse)
def provision_desk_phone(token: str, mac: str) -> str:
    """The set-up file a desk phone loads by its MAC address. The token in the
    URL is per device; a wrong token or MAC gets 404, never a hint."""
    try:
        mac = config.norm_mac(mac)
    except config.OpError as e:
        raise HTTPException(404, "Not found.") from e
    with db.tx() as conn:
        d = provisioning.device_by_token(conn, token, mac)
        if d is None or d["kind"] != "desk":
            raise HTTPException(404, "Not found.")
        conn.execute("UPDATE voice_devices SET status = 'provisioned', last_seen_at = now() WHERE id = %s", (d["id"],))
        dom = freeswitch.tenant_domain(d["customer_id"])
    # A generic SIP account file (Yealink-style keys); per-model templates come with the first real phones.
    return "\n".join(
        [
            "#!version:1.0.0.1",
            "account.1.enable = 1",
            f"account.1.label = {d['extension']} {d['user_name']}",
            f"account.1.display_name = {d['user_name']}",
            f"account.1.user_name = {d['extension']}",
            f"account.1.auth_name = {d['extension']}",
            f"account.1.password = {d['sip_password']}",
            f"account.1.sip_server.1.address = {dom}",
            "account.1.sip_server.1.transport_type = 2",
            "",
        ]
    )


class JoinIn(BaseModel):
    token: str = Field(min_length=10, max_length=100)


@public.post("/softphone/join")
def softphone_join(body: JoinIn) -> dict:
    """Redeem a softphone sign-in link once: returns the SIP account."""
    with db.tx() as conn:
        d = provisioning.device_by_token(conn, body.token)
        if d is None or d["kind"] != "softphone":
            raise HTTPException(404, "This sign-in link has expired or been used. Ask your voice admin for a new one.")
        conn.execute(
            """UPDATE voice_devices SET status = 'provisioned', token_hash = '', last_seen_at = now()
               WHERE id = %s""",
            (d["id"],),
        )
        audit.record(conn, f"device:{d['id']}", "commai.voice.softphone_join", str(d["id"]), d["customer_id"])
        return {
            "extension": d["extension"],
            "display_name": d["user_name"],
            "password": d["sip_password"],
            "domain": freeswitch.tenant_domain(d["customer_id"]),
            "transport": "tls",
        }
