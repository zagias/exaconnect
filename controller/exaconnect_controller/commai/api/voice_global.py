"""CommAI voice stage 5 API (ADR 0033): numbers by country, port orders,
emergency addresses, fraud protection, carriers and the LiveKit agent.

Who may call what:
- anyone in the business: their own emergency notice, the island rules
- the business's voice admins: number search, port orders, emergency
  addresses, fraud settings; spend permission to submit a port or restore
  international calling
- ExaCarib admins: carriers, rate sheets, routing, trunk health, supplier
  records per carrier, the high-risk destination list, Kamailio files
- the LiveKit agent worker (EXA_LIVEKIT_AGENT_SECRET): its call endpoints
Every write is audited.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access, inbox
from ..ai import agent
from ..ai import voice as ai_voice
from ..voice import carriers, countries, emergency, fraud, livekit, perms, porting
from ..voice import provider as providers
from ..voice.common import VoiceError, month_start, now
from .common import errors

router = APIRouter(tags=["commai: voice stage 5"])
public = APIRouter(prefix="/voice/livekit", tags=["commai: voice LiveKit agent"])
C = "/customers/{customer_id}/voice"
A = "/voice-admin"


def clean(v: Any) -> Any:
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items() if k not in ("storage_key", "sip_password", "token_hash")}
    if isinstance(v, list):
        return [clean(x) for x in v]
    return v


def _period(p: str | None) -> dt.date:
    if not p:
        return month_start(now())
    if not re.fullmatch(r"\d{4}-\d{2}", p):
        raise HTTPException(422, "Give the month as YYYY-MM.")
    return dt.date(int(p[:4]), int(p[5:]), 1)


def _data_dir(request: Request) -> str:
    return request.app.state.settings.data_dir


def _admin(conn, user, cid) -> None:
    perms.require_voice_admin(conn, user, cid)


# ---- countries and numbers ----------------------------------------------------------------


@router.get(f"{C}/countries")
def list_countries(customer_id: str, user: UserDep) -> list[dict]:
    """Countries where CommAI voice offers numbers, and whether each is on for you."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return countries.available(conn, customer_id)


@router.get(f"{C}/numbers/search")
def search_numbers(
    customer_id: str,
    user: UserDep,
    country: str = Query(min_length=2, max_length=2),
    area: str = Query(default="", max_length=6),
    contains: str = Query(default="", max_length=12),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        c = countries.require(conn, country, customer_id)
        prov = providers.get()
        found = prov.search_numbers(conn, c.code, area, contains, limit)
    return {"country": c.code, "numbers": found, "example": not prov.live}


# ---- emergency addresses ------------------------------------------------------------------


@router.get(f"{C}/emergency")
def emergency_overview(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        return clean(emergency.overview(conn, customer_id))


@router.get(f"{C}/emergency/rules")
def emergency_rules(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    return emergency.all_rules()


@router.post(f"{C}/emergency/sites/{{site_id}}/validate")
def validate_site(customer_id: str, site_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        out = emergency.submit_site(conn, customer_id, site_id, user.actor)
        audit.record(conn, user.actor, "commai.voice.emergency_validate", site_id, customer_id)
    return out


class AddressIn(BaseModel):
    address_line1: str = Field(min_length=1, max_length=200)
    address_line2: str = Field(default="", max_length=200)
    city: str = Field(default="", max_length=200)
    island: str = Field(default="", max_length=200)
    country: str = Field(default="TT", min_length=2, max_length=2)
    postcode: str = Field(default="", max_length=20)


class UserAddressIn(BaseModel):
    address: AddressIn | None = None  # None: use the site's address again


@router.put(f"{C}/emergency/users/{{voice_user_id}}")
def set_user_address(customer_id: str, voice_user_id: str, body: UserAddressIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        out = emergency.set_user_address(
            conn, customer_id, voice_user_id, body.address.model_dump() if body.address else None, user.actor
        )
        audit.record(
            conn, user.actor, "commai.voice.emergency_user", voice_user_id, customer_id, {"own": bool(body.address)}
        )
    return out


def _my_voice_user(conn, cid, user) -> dict:
    vu = conn.execute(
        "SELECT * FROM voice_users WHERE customer_id = %s AND user_id = %s AND status = 'active'", (cid, user.id)
    ).fetchone()
    if vu is None:
        raise VoiceError("You don't have a phone extension yet.", 404)
    return vu


@router.get(f"{C}/me/emergency-notice")
def my_notice(customer_id: str, user: UserDep) -> dict:
    """The emergency calling notice every person must read for their island."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, errors():
        return emergency.user_notice(conn, customer_id, _my_voice_user(conn, customer_id, user))


class AckIn(BaseModel):
    key: str = Field(min_length=3, max_length=80)


@router.post(f"{C}/me/emergency-notice")
def ack_notice(customer_id: str, body: AckIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, errors():
        vu = _my_voice_user(conn, customer_id, user)
        out = emergency.acknowledge(conn, customer_id, vu, body.key)
        audit.record(conn, user.actor, "commai.voice.emergency_notice_ack", body.key, customer_id)
    return out


# ---- notices ------------------------------------------------------------------------------


@router.get(f"{C}/notifications")
def notifications(customer_id: str, user: UserDep, limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        if perms.is_voice_admin(conn, user, customer_id):
            return conn.execute(
                "SELECT * FROM voice_notifications WHERE customer_id = %s ORDER BY at DESC LIMIT %s",
                (customer_id, limit),
            ).fetchall()
        return conn.execute(
            """SELECT n.* FROM voice_notifications n JOIN voice_users v ON v.id = n.voice_user_id
               WHERE n.customer_id = %s AND v.user_id = %s ORDER BY n.at DESC LIMIT %s""",
            (customer_id, user.id, limit),
        ).fetchall()


@router.post(f"{C}/notifications/read")
def read_notifications(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        n = conn.execute(
            """UPDATE voice_notifications SET read_at = now() WHERE customer_id = %s AND read_at IS NULL
               AND (voice_user_id IS NULL OR voice_user_id IN
                    (SELECT id FROM voice_users WHERE customer_id = %s AND user_id = %s))""",
            (customer_id, customer_id, user.id),
        ).rowcount
    return {"read": n}


# ---- port orders --------------------------------------------------------------------------


class PortCreateIn(BaseModel):
    e164: str = Field(min_length=5, max_length=30)
    losing_carrier: str = Field(min_length=1, max_length=120)
    account_name: str = Field(default="", max_length=200)
    account_number: str = Field(default="", max_length=60)
    requested_date: str | None = None
    target_type: str = Field(default="none", pattern="^(none|user|ring_group|queue|menu|ai)$")
    target: str | None = None
    site: str | None = None


@router.get(f"{C}/port-orders")
def list_port_orders(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        return porting.list_ports(conn, customer_id)


@router.post(f"{C}/port-orders", status_code=201)
def create_port_order(customer_id: str, body: PortCreateIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        p = porting.create(conn, customer_id, body.model_dump(), user.actor)
        audit.record(conn, user.actor, "commai.voice.port_create", str(p["id"]), customer_id, {"e164": p["e164"]})
    return clean(p)


@router.get(f"{C}/port-orders/{{port_id}}")
def get_port_order(customer_id: str, port_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        return clean(porting.get(conn, customer_id, port_id))


class PortDetailsIn(BaseModel):
    account_name: str | None = Field(default=None, max_length=200)
    account_number: str | None = Field(default=None, max_length=60)
    losing_carrier: str | None = Field(default=None, max_length=120)
    requested_date: str | None = None


@router.patch(f"{C}/port-orders/{{port_id}}")
def update_port_order(customer_id: str, port_id: str, body: PortDetailsIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        out = porting.update_details(conn, customer_id, port_id, body.model_dump(exclude_unset=True), user.actor)
        audit.record(conn, user.actor, "commai.voice.port_details", port_id, customer_id)
    return clean(out)


class PriceIn(BaseModel):
    monthly_delta: str
    one_time: str


class PortSubmitIn(BaseModel):
    accepted_price: PriceIn


@router.post(f"{C}/port-orders/{{port_id}}/submit")
def submit_port_order(customer_id: str, port_id: str, body: PortSubmitIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        out = porting.submit(
            conn,
            customer_id,
            port_id,
            user.actor,
            can_spend=perms.can_spend(conn, user, customer_id),
            accepted_price=body.accepted_price.model_dump(),
        )
    return clean(out)


@router.post(f"{C}/port-orders/{{port_id}}/documents", status_code=201)
async def upload_port_document(
    customer_id: str,
    port_id: str,
    user: UserDep,
    request: Request,
    doc_type: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    """A document for the losing carrier: PDF, PNG or JPEG, up to 10 MB."""
    access.check(user, customer_id, "commai:admin")
    data = await file.read(porting.MAX_DOC_BYTES + 1)
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        out = porting.add_document(
            conn,
            customer_id,
            port_id,
            doc_type,
            file.filename or "",
            file.content_type or "",
            data,
            user.actor,
            _data_dir(request),
        )
    return clean(out)


@router.get(f"{C}/port-orders/{{port_id}}/documents/{{doc_id}}")
def download_port_document(customer_id: str, port_id: str, doc_id: str, user: UserDep, request: Request) -> Response:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        d, data = porting.read_document(conn, customer_id, port_id, doc_id, _data_dir(request))
        audit.record(conn, user.actor, "commai.voice.port_document_read", doc_id, customer_id)
    return Response(
        data,
        media_type=d["content_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{d["filename"]}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.delete(f"{C}/port-orders/{{port_id}}/documents/{{doc_id}}")
def delete_port_document(customer_id: str, port_id: str, doc_id: str, user: UserDep, request: Request) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        porting.remove_document(conn, customer_id, port_id, doc_id, user.actor, _data_dir(request))
        audit.record(conn, user.actor, "commai.voice.port_document_remove", doc_id, customer_id)
    return {"removed": True}


class RescheduleIn(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


@router.post(f"{C}/port-orders/{{port_id}}/reschedule")
def reschedule_port_order(customer_id: str, port_id: str, body: RescheduleIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        out = porting.reschedule(conn, customer_id, port_id, body.date, user.actor)
        audit.record(conn, user.actor, "commai.voice.port_reschedule", port_id, customer_id, {"date": body.date})
    return clean(out)


@router.post(f"{C}/port-orders/{{port_id}}/cancel")
def cancel_port_order(customer_id: str, port_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        out = porting.cancel(conn, customer_id, port_id, user.actor)
        audit.record(conn, user.actor, "commai.voice.port_cancel", port_id, customer_id)
    return clean(out)


# ---- fraud ------------------------------------------------------------------------------


@router.get(f"{C}/fraud")
def get_fraud(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        return clean(fraud.status(conn, customer_id))


class FraudSettingsIn(BaseModel):
    intl_daily_cap: str | None = None
    intl_daily_calls: int | None = Field(default=None, ge=1, le=100000)
    after_hours_international: str | None = Field(default=None, pattern="^(allow|alert|block)$")
    hours_id: str | None = None
    spike_factor: str | None = None
    spike_min_calls: int | None = Field(default=None, ge=3, le=100000)
    allowed_high_risk: list[str] | None = Field(default=None, max_length=100)


@router.put(f"{C}/fraud")
def put_fraud(customer_id: str, body: FraudSettingsIn, user: UserDep) -> dict:
    """Allowing a high-risk destination needs spend permission."""
    access.check(user, customer_id, "commai:admin")
    values = body.model_dump(exclude_unset=True)
    with db.tx() as conn, errors():
        _admin(conn, user, customer_id)
        if "allowed_high_risk" in values:
            perms.require_spend(conn, user, customer_id)
        out = fraud.set_settings(conn, customer_id, values, user.actor)
        audit.record(conn, user.actor, "commai.voice.fraud_settings", "", customer_id, values)
    return clean(out)


class RestoreIn(BaseModel):
    note: str = Field(default="", max_length=300)


@router.post(f"{C}/fraud/restore")
def restore_international(customer_id: str, body: RestoreIn, user: UserDep) -> dict:
    """Turn international calling back on after a suspension (spend permission)."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        perms.require_spend(conn, user, customer_id)
        return clean(fraud.restore(conn, customer_id, user.actor, body.note))


@router.get(f"{C}/route-attempts")
def route_attempts(customer_id: str, user: UserDep, call_id: str = Query(max_length=120)) -> list[dict]:
    """Which carriers a call tried (names only; supplier prices stay with ExaCarib)."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        return conn.execute(
            """SELECT a.position, coalesce(c.name, a.carrier) AS carrier, a.ok, a.detail, a.at
               FROM voice_route_attempts a LEFT JOIN voice_carriers c ON c.key = a.carrier
               WHERE a.customer_id = %s AND a.call_id = %s ORDER BY a.position""",
            (customer_id, call_id),
        ).fetchall()


# ---- ExaCarib: carriers, routing, high-risk list ------------------------------------------------------


def _exacarib(user) -> None:
    if user.role != "admin":
        raise HTTPException(403, "Only ExaCarib manages carriers and routing.")


@router.get(f"{A}/carriers")
def list_carriers(user: UserDep) -> list[dict]:
    _exacarib(user)
    with db.tx() as conn:
        return clean(carriers.list_carriers(conn))


class CarrierIn(BaseModel):
    key: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    adapter: str = Field(default="simulated", pattern="^(simulated|sip)$")
    countries: list[str] = Field(default_factory=list, max_length=60)
    outbound: dict = Field(default_factory=dict)
    inbound: dict = Field(default_factory=dict)
    enabled: bool = True


@router.post(f"{A}/carriers", status_code=201)
def save_carrier(body: CarrierIn, user: UserDep) -> dict:
    """Add or change a carrier. A new one starts switched off in the go-live registry."""
    _exacarib(user)
    with db.tx() as conn, errors():
        out = carriers.save(conn, body.model_dump(), user.actor)
        audit.record(conn, user.actor, "commai.voice.carrier", body.key, None, {"enabled": body.enabled})
    return clean(out)


class RateLine(BaseModel):
    prefix: str = Field(default="", max_length=15)
    name: str = Field(default="", max_length=120)
    per_minute: str = Field(max_length=20)


class RatesIn(BaseModel):
    lines: list[RateLine] = Field(min_length=1, max_length=5000)


@router.put(f"{A}/carriers/{{key}}/rates")
def put_rates(key: str, body: RatesIn, user: UserDep) -> dict:
    _exacarib(user)
    with db.tx() as conn, errors():
        out = carriers.set_rates(conn, key, [ln.model_dump() for ln in body.lines], user.actor)
        audit.record(conn, user.actor, "commai.voice.carrier_rates", key, None, {"version": out["rate_version"]})
    return clean(out)


@router.post(f"{A}/carriers/{{key}}/options")
def check_trunk(key: str, user: UserDep) -> list[dict]:
    """Send SIP OPTIONS to the carrier's trunk now (simulated until Kamailio runs)."""
    _exacarib(user)
    with db.tx() as conn, errors():
        carriers.get(conn, key)
        return carriers.check_trunks(conn, key)


@router.get(f"{A}/routing")
def routing_rules(user: UserDep) -> list[dict]:
    _exacarib(user)
    with db.tx() as conn:
        return carriers.rules(conn)


class RuleIn(BaseModel):
    prefix: str = Field(default="", max_length=15)
    mode: str = Field(pattern="^(lcr|quality)$")


@router.put(f"{A}/routing")
def put_rule(body: RuleIn, user: UserDep) -> list[dict]:
    _exacarib(user)
    with db.tx() as conn, errors():
        carriers.set_rule(conn, body.prefix, body.mode, user.actor)
        audit.record(conn, user.actor, "commai.voice.route_rule", body.prefix, None, {"mode": body.mode})
        return carriers.rules(conn)


@router.delete(f"{A}/routing/{{prefix}}")
def delete_rule(prefix: str, user: UserDep) -> list[dict]:
    _exacarib(user)
    with db.tx() as conn, errors():
        carriers.delete_rule(conn, prefix)
        audit.record(conn, user.actor, "commai.voice.route_rule_delete", prefix, None)
        return carriers.rules(conn)


@router.get(f"{A}/route")
def route_plan(user: UserDep, customer_id: str, to: str = Query(min_length=3, max_length=30)) -> dict:
    """How a call from this business to this number would be routed, and why."""
    _exacarib(user)
    with db.tx() as conn:
        return carriers.plan(conn, customer_id, to)


@router.post(f"{A}/carriers/{{key}}/import")
def import_carrier_cdrs(key: str, user: UserDep, period: str | None = None) -> dict:
    """Fetch the carrier's call records for the month and import them."""
    _exacarib(user)
    with db.tx() as conn, errors():
        out = carriers.import_from_provider(conn, key, _period(period), user.actor)
        audit.record(conn, user.actor, "commai.voice.carrier_import", key, None, {"added": out["added"]})
    return clean(out)


@router.get(f"{A}/carriers/{{key}}/reconcile")
def reconcile_carrier(key: str, user: UserDep, period: str | None = None) -> dict:
    _exacarib(user)
    with db.tx() as conn, errors():
        return carriers.reconcile(conn, key, _period(period))


@router.get(f"{A}/kamailio")
def kamailio_files(user: UserDep) -> dict:
    """The Kamailio dispatcher and allow-list files rendered from the carriers."""
    _exacarib(user)
    with db.tx() as conn:
        out = carriers.render_kamailio(conn)
        audit.record(conn, user.actor, "commai.voice.kamailio_render", "", None)
    return {**out, "livekit": livekit.status()}


@router.get(f"{A}/high-risk")
def high_risk(user: UserDep) -> list[dict]:
    _exacarib(user)
    with db.tx() as conn:
        return fraud.list_high_risk(conn)


class HighRiskIn(BaseModel):
    origin: str = Field(default="*", min_length=1, max_length=2)
    prefix: str = Field(min_length=1, max_length=15)
    name: str = Field(default="", max_length=120)
    reason: str = Field(default="", max_length=300)


@router.put(f"{A}/high-risk")
def put_high_risk(body: HighRiskIn, user: UserDep) -> dict:
    _exacarib(user)
    with db.tx() as conn, errors():
        out = fraud.save_high_risk(conn, body.origin, body.prefix, body.name, body.reason, user.actor)
        audit.record(conn, user.actor, "commai.voice.high_risk", f"{body.origin}/{body.prefix}", None)
    return out


@router.delete(f"{A}/high-risk/{{origin}}/{{prefix}}")
def delete_high_risk(origin: str, prefix: str, user: UserDep) -> dict:
    _exacarib(user)
    with db.tx() as conn:
        fraud.delete_high_risk(conn, origin, prefix)
        audit.record(conn, user.actor, "commai.voice.high_risk_delete", f"{origin}/{prefix}", None)
    return {"removed": True}


# ---- LiveKit agent worker (its own shared secret, no portal sign-in) ------------------------------------


class AgentCallIn(BaseModel):
    dialled: str = Field(min_length=3, max_length=30)
    caller: str = Field(default="", max_length=40)


class AgentTurnIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@public.post("/calls", status_code=201)
def agent_start(body: AgentCallIn, authorization: str | None = Header(default=None)) -> dict:
    """A phone call reached the AI: open the conversation and give the greeting."""
    with db.tx() as conn, errors():
        livekit.check_agent(authorization)
        num = livekit.business_for(conn, body.dialled)
        cid = num["customer_id"]
        out = ai_voice.start_call(conn, cid, caller=body.caller, started_by="livekit")
        audit.record(conn, "system:livekit", "commai.voice.ai_phone_call", out["conversation_id"], cid)
    return {
        "customer_id": str(cid),
        "conversation_id": out["conversation_id"],
        "room": livekit.room_for(out["conversation_id"]),
        "greeting": out["greeting"],
        "handler": out["handler"],
    }


@public.post("/calls/{customer_id}/{conversation_id}/turns")
def agent_turn(
    customer_id: str, conversation_id: str, body: AgentTurnIn, authorization: str | None = Header(default=None)
) -> dict:
    with db.tx() as conn, errors():
        livekit.check_agent(authorization)
        msg = ai_voice.caller_turn(conn, customer_id, conversation_id, body.text)
    with db.tx() as conn, errors():
        result = agent.respond(conn, customer_id, conversation_id, msg["id"])
    with db.tx() as conn:
        replies = ai_voice.replies_after(conn, customer_id, conversation_id, msg)
        conv = inbox.get(conn, customer_id, conversation_id)
    return {
        "outcome": result.get("outcome"),
        "reply": " ".join(r["body"] for r in replies),
        "handed_over": conv["handler"] != "ai",
    }


@public.post("/calls/{customer_id}/{conversation_id}/end")
def agent_end(customer_id: str, conversation_id: str, authorization: str | None = Header(default=None)) -> dict:
    with db.tx() as conn, errors():
        livekit.check_agent(authorization)
        call = ai_voice.end_call(conn, customer_id, conversation_id, "livekit")
    return {"ended_at": call["ended_at"]}
