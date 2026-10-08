"""Voice provisioning (ADR 0021): an approved order becomes a working service.

Steps, in order: tenant -> numbers -> devices -> confirm -> billing.

- Each step runs in a savepoint: if it fails, its own work is rolled back,
  the failure is recorded on the step, and the order shows which step failed.
- Each step is idempotent. Records carry an order_ref (unique), and every
  provider call carries an idempotency key derived from the order, so a retry
  never creates a second user, number, port or device.
- The job retries on its own with back-off; after the last attempt the order
  stays failed until someone presses Retry.
- Confirm places a test call to each new number. The order is active only
  when every test call works, never just because it was approved or paid.
- Billing starts at activation (billing_from) and stops when a user or
  number is removed (billing_until).
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ... import audit
from ...security import token_hash
from .. import events, jobs
from . import billing, config, countries, emergency, porting
from . import provider as providers
from .common import VoiceError, now

events.register("voice.order_approved", "voice.order_failed", "voice.order_active", "voice.port_updated")

STEPS = ("tenant", "numbers", "devices", "confirm", "billing")
PORT_STATUSES = ("submitted", "accepted", "scheduled", "completed", "rejected", "cancelled")


# ---- the order -----------------------------------------------------------------------


def _counts(items: dict) -> dict:
    users = items.get("users") or []
    nums = items.get("numbers") or {}
    new = int(nums.get("new", 0)) + sum(1 for u in users if u.get("number"))
    ported = len(nums.get("ported") or [])
    return {
        "users": len(users),
        "numbers": new + ported,
        "desk_phones": sum(1 for u in users if u.get("desk_phone")),
        "softphones": sum(1 for u in users if u.get("softphone")),
        "new_numbers": new,
        "ported_numbers": ported,
    }


def _check_items(conn, cid, items: dict) -> dict:
    users = items.get("users") or []
    nums = items.get("numbers") or {}
    if not users and not int(nums.get("new", 0)) and not nums.get("ported"):
        raise VoiceError("An order needs at least one user or number.", 422)
    if len(users) > 500:
        raise VoiceError("Up to 500 users in one order.", 422)
    exts, macs = set(), set()
    try:
        for i, u in enumerate(users):
            if not str(u.get("name", "")).strip():
                raise config.OpError(f"User {i + 1} needs a name.")
            config._site(conn, cid, u.get("site") or items.get("site"))
            config._team(conn, cid, u.get("team"))
            if u.get("extension"):
                ext = config._check_ext(conn, cid, u["extension"])
                if ext in exts:
                    raise config.OpError(f"Extension {ext} is in the order twice.")
                exts.add(ext)
            if u.get("desk_phone"):
                mac = config.norm_mac((u["desk_phone"] or {}).get("mac", ""))
                if mac in macs:
                    raise config.OpError(f"MAC {mac} is in the order twice.")
                macs.add(mac)
        for p in nums.get("ported") or []:
            config.norm_e164(p.get("e164", ""))
            if p.get("switch_date"):
                dt.date.fromisoformat(str(p["switch_date"]))
    except config.OpError as e:
        raise VoiceError(str(e), 422) from e
    except ValueError as e:
        raise VoiceError("Switch-over dates look like 2026-11-30.", 422) from e
    if int(nums.get("new", 0)) < 0 or int(nums.get("new", 0)) > 100:
        raise VoiceError("Order 0 to 100 extra numbers.", 422)
    _check_countries(conn, cid, items)
    return items


def _check_countries(conn, cid, items: dict) -> None:
    """Real numbers by country (ADR 0033). With a country named, or with the
    live provider, numbers and ports need their country switched on. Without
    one, the simulated provider's sandbox numbers are used (stage 3)."""
    nums = items.get("numbers") or {}
    country = str(nums.get("country") or "").upper()
    live = providers.get().live
    wanted = int(nums.get("new", 0)) + sum(1 for u in items.get("users") or [] if u.get("number"))
    if wanted and (country or live):
        if not country:
            raise VoiceError("Say which country the new numbers are in.", 422)
        c = countries.require(conn, country, cid)
        nums["area"] = countries.check_area(c, nums.get("area"))
        nums["country"] = c.code
    chosen = nums.get("choose") or []
    if chosen:
        if not country:
            raise VoiceError("Pick numbers from a search by country.", 422)
        if len(chosen) > wanted:
            raise VoiceError("More numbers were picked than the order has.", 422)
        try:
            nums["choose"] = [config.norm_e164(n) for n in chosen]
        except config.OpError as e:
            raise VoiceError(str(e), 422) from e
    if country or live:
        for p in nums.get("ported") or []:
            countries.require(conn, countries.country_of(p.get("e164", "")), cid, "porting")


def create(conn: psycopg.Connection, cid: Any, items: dict, actor: str) -> dict:
    items = _check_items(conn, cid, items)
    card = billing.card_at(conn, cid)
    price = billing.price_impact(card, _counts(items))
    row = conn.execute(
        """INSERT INTO voice_orders (customer_id, items, price, rate_card_id, created_by)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (cid, Jsonb(items), Jsonb(price), card["id"], actor),
    ).fetchone()
    return get(conn, cid, row["id"])


def get(conn, cid, order_id) -> dict:
    o = conn.execute("SELECT * FROM voice_orders WHERE id = %s AND customer_id = %s", (order_id, cid)).fetchone()
    if o is None:
        raise VoiceError("Order not found.", 404)
    o["steps"] = conn.execute(
        "SELECT step, status, attempts, error, result, updated_at FROM voice_order_steps WHERE order_id = %s"
        " ORDER BY position",
        (order_id,),
    ).fetchall() or [
        {"step": s, "status": "pending", "attempts": 0, "error": "", "result": {}, "updated_at": None} for s in STEPS
    ]
    o["ports"] = conn.execute(
        "SELECT * FROM voice_port_orders WHERE order_id = %s ORDER BY e164", (order_id,)
    ).fetchall()
    o["test_calls"] = conn.execute(
        "SELECT e164, ok, detail, at FROM voice_test_calls WHERE order_id = %s ORDER BY id", (order_id,)
    ).fetchall()
    return o


def approve(conn, cid, order_id, actor: str, *, can_spend: bool, accepted_price: dict | None) -> dict:
    """Approve the price and start provisioning. Needs spend permission."""
    if not can_spend:
        raise VoiceError("Only someone with spend permission can approve a voice order.", 403)
    o = conn.execute(
        "SELECT * FROM voice_orders WHERE id = %s AND customer_id = %s FOR UPDATE", (order_id, cid)
    ).fetchone()
    if o is None:
        raise VoiceError("Order not found.", 404)
    if o["status"] != "draft":
        raise VoiceError("This order has already been approved or cancelled.", 409)
    price = billing.price_impact(billing.card_at(conn, cid), _counts(o["items"]))
    if (price["monthly_delta"], price["one_time"]) != (o["price"]["monthly_delta"], o["price"]["one_time"]):
        conn.execute("UPDATE voice_orders SET price = %s, updated_at = now() WHERE id = %s", (Jsonb(price), order_id))
        raise VoiceError("The prices have changed since this order was made. Check the new price.", 409)
    seen = accepted_price or {}
    if (seen.get("monthly_delta"), seen.get("one_time")) != (price["monthly_delta"], price["one_time"]):
        raise VoiceError(f"Check the price first: {price['monthly_delta']} a month and {price['one_time']} once.", 409)
    conn.execute(
        """UPDATE voice_orders SET status = 'approved', approved_by = %s, approved_at = now(), updated_at = now()
           WHERE id = %s""",
        (actor, order_id),
    )
    for pos, step in enumerate(STEPS):
        conn.execute(
            "INSERT INTO voice_order_steps (order_id, position, step) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (order_id, pos, step),
        )
    jobs.enqueue(
        conn,
        "voice.provision",
        {"order_id": str(order_id)},
        customer_id=cid,
        dedupe_key=f"voice.provision:{order_id}:0",
    )
    events.emit(conn, cid, "voice.order_approved", {"order_id": str(order_id), "price": price}, str(order_id))
    return get(conn, cid, order_id)


def retry(conn, cid, order_id, actor: str) -> dict:
    o = conn.execute(
        "SELECT * FROM voice_orders WHERE id = %s AND customer_id = %s FOR UPDATE", (order_id, cid)
    ).fetchone()
    if o is None:
        raise VoiceError("Order not found.", 404)
    if o["status"] != "failed":
        raise VoiceError("Only a failed order can be retried.", 409)
    n = o["retries"] + 1
    conn.execute("UPDATE voice_orders SET retries = %s, updated_at = now() WHERE id = %s", (n, order_id))
    jobs.enqueue(
        conn,
        "voice.provision",
        {"order_id": str(order_id)},
        customer_id=cid,
        dedupe_key=f"voice.provision:{order_id}:{n}",
    )
    return get(conn, cid, order_id)


def cancel(conn, cid, order_id) -> dict:
    row = conn.execute(
        """UPDATE voice_orders SET status = 'cancelled', updated_at = now()
           WHERE id = %s AND customer_id = %s AND status = 'draft' RETURNING id""",
        (order_id, cid),
    ).fetchone()
    if row is None:
        raise VoiceError("Only a draft order can be cancelled. Remove users and numbers to undo an active one.", 409)
    return get(conn, cid, order_id)


# ---- the steps -----------------------------------------------------------------------


def _ref(o, *parts) -> str:
    return ":".join(["order", str(o["id"]), *map(str, parts)])


def _tenant(conn, o, prov, calls: list) -> dict:
    cid, items = o["customer_id"], o["items"]
    ops = []
    for i, u in enumerate(items.get("users") or []):
        ref = _ref(o, "user", i)
        if conn.execute("SELECT 1 FROM voice_users WHERE order_ref = %s", (ref,)).fetchone():
            continue
        ops.append(
            {
                "op": "add_user",
                "name": u["name"],
                "email": u.get("email", ""),
                "mobile": u.get("mobile", ""),
                "extension": u.get("extension"),
                "site": u.get("site") or items.get("site"),
                "team": u.get("team"),
                "portal_email": u.get("portal_email"),
                "order_ref": ref,
            }
        )
    feats = items.get("features") or {}
    if (
        feats.get("main_ring_group")
        and not conn.execute(
            "SELECT 1 FROM voice_ring_groups WHERE customer_id = %s AND name = 'Main line'", (cid,)
        ).fetchone()
    ):
        ops.append(
            {
                "op": "save_ring_group",
                "name": "Main line",
                "extension": feats.get("main_extension", "100"),
                "members": [],
                "_all_order_users": True,
            }
        )
    if (
        feats.get("ai_after_hours")
        and not conn.execute(
            "SELECT 1 FROM voice_ai_rules WHERE customer_id = %s AND name = 'After hours to the AI agent'", (cid,)
        ).fetchone()
    ):
        if not conn.execute(
            "SELECT 1 FROM voice_hours WHERE customer_id = %s AND name = 'Office hours'", (cid,)
        ).fetchone():
            ops.append(
                {
                    "op": "save_hours",
                    "name": "Office hours",
                    "schedule": {d: [["08:00", "17:00"]] for d in ("mon", "tue", "wed", "thu", "fri")},
                }
            )
        ops.append(
            {
                "op": "save_ai_rule",
                "name": "After hours to the AI agent",
                "condition": "after_hours",
                "hours": "Office hours",
                "fallback": "voicemail",
            }
        )
    if not ops:
        return {"version": None, "note": "Already set up."}
    # Ring group members are the order's users, once they exist.
    user_ops = [op for op in ops if op["op"] == "add_user"]
    rest = [op for op in ops if op["op"] != "add_user"]
    version = None
    if user_ops:
        out = config.apply(
            conn, cid, user_ops, actor=f"order:{o['id']}", kind="order", check_price=False, summary="Order: users"
        )
        if out["errors"]:
            raise VoiceError("; ".join(f"user {e['index'] + 1}: {e['error']}" for e in out["errors"]), 422)
        version = out["version"]
    if rest:
        exts = [
            r["extension"]
            for r in conn.execute(
                "SELECT extension FROM voice_users WHERE order_ref LIKE %s AND status = 'active' ORDER BY extension",
                (_ref(o, "user", "%"),),
            ).fetchall()
        ]
        for op in rest:
            if op.pop("_all_order_users", False):
                op["members"] = exts
        out = config.apply(
            conn, cid, rest, actor=f"order:{o['id']}", kind="order", check_price=False, summary="Order: routing"
        )
        if out["errors"]:
            raise VoiceError("; ".join(e["error"] for e in out["errors"]), 422)
        version = out["version"]
    return {"version": version}


def _numbers(conn, o, prov, calls: list) -> dict:
    cid, items = o["customer_id"], o["items"]
    wanted = []  # (ref, target_type, target_id, site_id)
    for i, u in enumerate(items.get("users") or []):
        if u.get("number"):
            vu = conn.execute(
                "SELECT id, site_id FROM voice_users WHERE order_ref = %s", (_ref(o, "user", i),)
            ).fetchone()
            wanted.append((_ref(o, "number", "user", i), "user", vu["id"], vu["site_id"]))
    main = conn.execute(
        "SELECT id FROM voice_ring_groups WHERE customer_id = %s AND name = 'Main line'", (cid,)
    ).fetchone()
    for j in range(int((items.get("numbers") or {}).get("new", 0))):
        wanted.append(
            (_ref(o, "number", "new", j), "ring_group" if main else "none", main["id"] if main else None, None)
        )
    got = []
    spec = items.get("numbers") or {}
    country = spec.get("country") or None
    if country:  # checked again when it runs: a country can be switched off meanwhile
        countries.require(conn, country, cid)
    chosen = list(spec.get("choose") or [])
    for k, (ref, ttype, tid, site_id) in enumerate(wanted):
        row = conn.execute("SELECT e164 FROM voice_numbers WHERE order_ref = %s", (ref,)).fetchone()
        if row:
            got.append(row["e164"])
            continue
        key = f"{cid}:{ref}"  # same key, same number, however often it runs
        if country:
            n = prov.order_number(
                conn, cid, key, spec.get("area") or "", country=country, e164=chosen[k] if k < len(chosen) else None
            )
        else:
            n = prov.order_number(conn, cid, key)
        site = conn.execute("SELECT * FROM voice_sites WHERE id = %s", (site_id,)).fetchone() if site_id else None
        conn.execute(
            """INSERT INTO voice_numbers (customer_id, e164, source, status, target_type, target_id, site_id,
                 provider, provider_ref, emergency_address, order_ref, country)
               VALUES (%s, %s, 'new', 'pending', %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (order_ref) DO NOTHING""",
            (
                cid,
                n["e164"],
                ttype,
                tid,
                site_id,
                prov.name,
                n["ref"],
                Jsonb(config.site_address(site)),
                ref,
                country or countries.country_of(n["e164"]),
            ),
        )
        got.append(n["e164"])
    ports = []
    for k, p in enumerate((items.get("numbers") or {}).get("ported") or []):
        ref = _ref(o, "port", k)
        if conn.execute("SELECT 1 FROM voice_port_orders WHERE order_ref = %s", (ref,)).fetchone():
            continue
        e164 = config.norm_e164(p["e164"])
        num = conn.execute(
            """INSERT INTO voice_numbers (customer_id, e164, source, status, target_type, target_id, provider,
                 order_ref, country) VALUES (%s, %s, 'ported', 'porting', %s, %s, %s, %s, %s)
               ON CONFLICT (order_ref) DO UPDATE SET order_ref = EXCLUDED.order_ref RETURNING id""",
            (
                cid,
                e164,
                "ring_group" if main else "none",
                main["id"] if main else None,
                prov.name,
                ref + ":number",
                countries.country_of(e164),
            ),
        ).fetchone()
        # Its own status timeline, documents and switch-over job (ADR 0033).
        porting.create_from_order(conn, cid, o["id"], num["id"], e164, p, ref, prov)
        ports.append(e164)
    return {"numbers": got, "ports": ports}


def _devices(conn, o, prov, calls: list) -> dict:
    made = 0
    for i, u in enumerate(o["items"].get("users") or []):
        vu = conn.execute("SELECT id FROM voice_users WHERE order_ref = %s", (_ref(o, "user", i),)).fetchone()
        for kind, spec in (("desk", u.get("desk_phone")), ("softphone", u.get("softphone"))):
            if not spec:
                continue
            mac = config.norm_mac(spec.get("mac", "")) if kind == "desk" else None
            row = conn.execute(
                """INSERT INTO voice_devices (customer_id, voice_user_id, kind, mac, model, token_hash,
                     token_expires_at, order_ref)
                   VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s = 'softphone' THEN now() + interval '7 days' END, %s)
                   ON CONFLICT (order_ref) DO NOTHING RETURNING id""",
                (
                    o["customer_id"],
                    vu["id"],
                    kind,
                    mac,
                    str((spec if isinstance(spec, dict) else {}).get("model", ""))[:60],
                    token_hash(secrets.token_urlsafe(24)),
                    kind,
                    _ref(o, "device", kind, i),
                ),
            ).fetchone()
            made += 1 if row else 0
    return {"devices": made, "note": "Set-up links are issued from the order screen."}


def _confirm(conn, o, prov, calls: list) -> dict:
    rows = conn.execute(
        "SELECT id, e164 FROM voice_numbers WHERE order_ref LIKE %s AND source = 'new' AND status <> 'removed'"
        " ORDER BY e164",
        (_ref(o, "number", "%"),),
    ).fetchall()
    failed = []
    for n in rows:
        res = prov.test_call(conn, n["e164"])
        # Kept even if the step fails (written after its savepoint), so the order shows each call.
        calls.append((o["customer_id"], o["id"], n["id"], n["e164"], res["ok"], res["detail"]))
        if res["ok"]:
            conn.execute("UPDATE voice_numbers SET status = 'active' WHERE id = %s AND status = 'pending'", (n["id"],))
            emergency.activate_outbound(conn, o["customer_id"], n["id"])
        else:
            failed.append(f"{n['e164']}: {res['detail']}")
    if failed:
        raise VoiceError("Test calls failed. " + " ".join(failed), 502)
    return {"tested": [n["e164"] for n in rows]}


def _billing(conn, o, prov, calls: list) -> dict:
    cid = o["customer_id"]
    users = conn.execute(
        "UPDATE voice_users SET billing_from = now() WHERE order_ref LIKE %s AND billing_from IS NULL"
        " AND status = 'active' RETURNING id",
        (_ref(o, "user", "%"),),
    ).fetchall()
    nums = conn.execute(
        "UPDATE voice_numbers SET billing_from = now() WHERE order_ref LIKE %s AND billing_from IS NULL"
        " AND status = 'active' RETURNING id, e164",
        (_ref(o, "number", "%"),),
    ).fetchall()
    for n in nums:
        billing.one_time_charge(conn, cid, f"new_number:{n['id']}", "new_number", f"New number {n['e164']}")
    for d in conn.execute(
        "SELECT id, kind, voice_user_id FROM voice_devices WHERE order_ref LIKE %s AND status <> 'removed'",
        (_ref(o, "device", "%"),),
    ).fetchall():
        billing.one_time_charge(
            conn,
            cid,
            f"device:{d['id']}",
            "desk_phone" if d["kind"] == "desk" else "softphone",
            "Desk phone set-up" if d["kind"] == "desk" else "Softphone set-up",
            voice_user_id=d["voice_user_id"],
        )
    for p in conn.execute("SELECT id, e164 FROM voice_port_orders WHERE order_id = %s", (o["id"],)).fetchall():
        billing.one_time_charge(conn, cid, f"port:{p['id']}", "port_number", f"Number port {p['e164']}")
    return {"users_billed": len(users), "numbers_billed": len(nums)}


STEP_FN = {"tenant": _tenant, "numbers": _numbers, "devices": _devices, "confirm": _confirm, "billing": _billing}


@jobs.handler("voice.provision")
def run(conn: psycopg.Connection, job: dict):
    o = conn.execute("SELECT * FROM voice_orders WHERE id = %s FOR UPDATE", (job["payload"]["order_id"],)).fetchone()
    if o is None or o["status"] not in ("approved", "provisioning", "failed"):
        return None
    cid = o["customer_id"]
    conn.execute(
        "UPDATE voice_orders SET status = 'provisioning', failed_step = '', error = '', updated_at = now()"
        " WHERE id = %s",
        (o["id"],),
    )
    prov = providers.get()
    steps = conn.execute("SELECT * FROM voice_order_steps WHERE order_id = %s ORDER BY position", (o["id"],)).fetchall()
    for st in steps:
        if st["status"] == "done":
            continue
        calls: list = []
        try:
            with conn.transaction():
                result = STEP_FN[st["step"]](conn, o, prov, calls)
        except Exception as e:  # noqa: BLE001 - any failure is recorded on its step
            msg = str(e) or type(e).__name__
            _record_test_calls(conn, calls)
            conn.execute(
                """UPDATE voice_order_steps SET status = 'failed', attempts = attempts + 1, error = %s,
                     updated_at = now() WHERE order_id = %s AND step = %s""",
                (msg[:1000], o["id"], st["step"]),
            )
            conn.execute(
                """UPDATE voice_orders SET status = 'failed', failed_step = %s, error = %s, updated_at = now()
                   WHERE id = %s""",
                (st["step"], msg[:1000], o["id"]),
            )
            events.emit(
                conn,
                cid,
                "voice.order_failed",
                {"order_id": str(o["id"]), "step": st["step"], "error": msg[:300]},
                str(o["id"]),
            )
            return jobs.Later(f"{st['step']}: {msg}", 30)
        _record_test_calls(conn, calls)
        conn.execute(
            """UPDATE voice_order_steps SET status = 'done', attempts = attempts + 1, error = '', result = %s,
                 updated_at = now() WHERE order_id = %s AND step = %s""",
            (Jsonb(result), o["id"], st["step"]),
        )
    conn.execute(
        "UPDATE voice_orders SET status = 'active', activated_at = now(), updated_at = now() WHERE id = %s", (o["id"],)
    )
    jobs.enqueue(conn, "voice.render", {"customer_id": str(cid)}, customer_id=cid)
    events.emit(conn, cid, "voice.order_active", {"order_id": str(o["id"])}, str(o["id"]))
    audit.record(conn, "system:voice", "commai.voice.order_active", str(o["id"]), cid)
    return None


def _record_test_calls(conn, calls) -> None:
    for row in calls:
        conn.execute(
            "INSERT INTO voice_test_calls (customer_id, order_id, number_id, e164, ok, detail)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            row,
        )
    calls.clear()


@jobs.on_dead("voice.provision")
def _dead(job: dict, error: str) -> None:
    """The order already shows the failed step; nothing more to do."""


# ---- ports and devices ---------------------------------------------------------------


def set_port(conn, cid, port_id, status: str, switch_date: str | None, note: str, actor: str) -> dict:
    """ExaCarib moves a port order by hand (see porting.admin_set). On
    completion the number gets a test call; only then does it go live and
    start billing."""
    return porting.admin_set(conn, cid, port_id, status, switch_date, note, actor)


def issue_device_link(conn, cid, device_id) -> dict:
    """A fresh set-up token for a device (the old one stops working)."""
    token = secrets.token_urlsafe(24)
    row = conn.execute(
        """UPDATE voice_devices SET token_hash = %s,
             token_expires_at = CASE WHEN kind = 'softphone' THEN now() + interval '7 days' END,
             status = CASE WHEN kind = 'softphone' THEN 'waiting' ELSE status END
           WHERE id = %s AND customer_id = %s AND status <> 'removed' RETURNING id, kind, mac""",
        (token_hash(token), device_id, cid),
    ).fetchone()
    if row is None:
        raise VoiceError("Device not found.", 404)
    return {**row, "token": token}


def device_by_token(conn, token: str, mac: str | None = None) -> dict | None:
    row = conn.execute(
        """SELECT d.*, v.extension, v.sip_password, v.name AS user_name, v.status AS user_status
           FROM voice_devices d JOIN voice_users v ON v.id = d.voice_user_id
           WHERE d.token_hash = %s AND d.status <> 'removed'""",
        (token_hash(token),),
    ).fetchone()
    if row is None or row["user_status"] != "active":
        return None
    if mac is not None and row["mac"] != mac:
        return None
    if row["token_expires_at"] is not None and row["token_expires_at"] < now():
        return None
    return row
