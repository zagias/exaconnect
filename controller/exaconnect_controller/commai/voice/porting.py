"""Number porting (ADR 0033): a port order with its own status timeline.

    draft --submit--> submitted --provider--> documents_needed --upload--> submitted
                                  \\--> rejected --correct and resubmit--> submitted
                                  \\--> scheduled (firm order commitment date)
    scheduled --on the date, durable job--> cutting_over --> completed
                                                         \\--> rolled_back (test call failed)
    rolled_back / scheduled --reschedule--> scheduled;  anything before completion --cancel--> cancelled

- The business uploads the documents the losing carrier needs (a signed
  letter of authorisation and a recent bill; ID or others when asked). Each
  file is checked (type by its first bytes, size) and stored outside the
  database under the data directory with a random name, readable only through
  the API by that business's voice admins and ExaCarib.
- The provider answers later; a durable job polls it. Every change is a line
  on the port's timeline, an event and a notice for the voice admins.
- On the agreed date a durable job cuts the number over, places test calls,
  and only then makes the number live and starts billing. If the test calls
  fail, it hands the number back to the losing carrier and says so.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from ... import audit
from .. import events, jobs
from . import billing, countries, emergency
from . import provider as providers
from .common import VoiceError, now

events.register(
    "voice.port_submitted",
    "voice.port_documents_needed",
    "voice.port_rejected",
    "voice.port_scheduled",
    "voice.port_completed",
    "voice.port_rolled_back",
    "voice.port_cancelled",
)

DOC_TYPES = {"loa": "Letter of authorisation", "bill": "Recent bill", "id": "Proof of identity", "other": "Other"}
REQUIRED_DOCS = ("loa", "bill")
MAX_DOC_BYTES = 10 * 1024 * 1024
MAGIC = (
    (b"%PDF-", "application/pdf", (".pdf",)),
    (b"\x89PNG\r\n\x1a\n", "image/png", (".png",)),
    (b"\xff\xd8\xff", "image/jpeg", (".jpg", ".jpeg")),
)
CUTOVER_HOUR = 10  # local time on the agreed date
OPEN = ("draft", "submitted", "documents_needed", "rejected", "scheduled", "rolled_back")
TEST_CALL_TRIES = 2


# ---- reading --------------------------------------------------------------------------


def _get(conn, cid, port_id, lock: bool = False) -> dict:
    p = conn.execute(
        f"SELECT * FROM voice_port_orders WHERE id = %s AND customer_id = %s{' FOR UPDATE' if lock else ''}",
        (port_id, cid),
    ).fetchone()
    if p is None:
        raise VoiceError("Port order not found.", 404)
    return p


def get(conn: psycopg.Connection, cid: Any, port_id: Any) -> dict:
    p = _get(conn, cid, port_id)
    p["timeline"] = conn.execute(
        "SELECT status, detail, actor, at FROM voice_port_events WHERE port_id = %s ORDER BY id", (port_id,)
    ).fetchall()
    p["documents"] = conn.execute(
        """SELECT id, doc_type, filename, content_type, size_bytes, sha256, uploaded_by, uploaded_at
           FROM voice_port_documents WHERE port_id = %s AND removed_at IS NULL ORDER BY uploaded_at""",
        (port_id,),
    ).fetchall()
    have = {d["doc_type"] for d in p["documents"]}
    p["missing_documents"] = [DOC_TYPES[t] for t in REQUIRED_DOCS if t not in have]
    p["test_calls"] = conn.execute(
        "SELECT e164, ok, detail, at FROM voice_test_calls WHERE number_id = %s ORDER BY id", (p["number_id"],)
    ).fetchall()
    p["price"] = billing.price_impact(billing.card_at(conn, cid), {"numbers": 1, "ported_numbers": 1})
    return p


def list_ports(conn: psycopg.Connection, cid: Any) -> list[dict]:
    return conn.execute(
        """SELECT p.*, (SELECT count(*) FROM voice_port_documents d WHERE d.port_id = p.id AND d.removed_at IS NULL)
                  AS documents
           FROM voice_port_orders p WHERE p.customer_id = %s ORDER BY p.created_at DESC""",
        (cid,),
    ).fetchall()


def _event(conn, p: dict, status: str, detail: str, actor: str, *, notify: bool = True, etype: str = "") -> None:
    conn.execute(
        "INSERT INTO voice_port_events (port_id, customer_id, status, detail, actor) VALUES (%s, %s, %s, %s, %s)",
        (p["id"], p["customer_id"], status, detail[:1000], actor),
    )
    events.emit(
        conn,
        p["customer_id"],
        etype or "voice.port_updated",
        {"e164": p["e164"], "status": status, "detail": detail[:300]},
        str(p["id"]),
    )
    if notify:
        emergency.notify(
            conn, p["customer_id"], "port", f"Port {p['e164']}: {status.replace('_', ' ')}", detail, str(p["id"])
        )


def _set(conn, p: dict, status: str, **cols) -> dict:
    sets = ", ".join(["status = %s", "updated_at = now()", *[f"{k} = %s" for k in cols]])
    return conn.execute(
        f"UPDATE voice_port_orders SET {sets} WHERE id = %s RETURNING *", (status, *cols.values(), p["id"])
    ).fetchone()


# ---- creating and submitting -----------------------------------------------------------


def create(conn: psycopg.Connection, cid: Any, body: dict, actor: str) -> dict:
    """A port order in draft. The number is reserved on our side as 'porting'."""
    from . import config

    try:
        e164 = config.norm_e164(body.get("e164", ""))
        target_type = body.get("target_type") or "none"
        target_id = config._target(conn, cid, target_type, body.get("target"))
        site = config._site(conn, cid, body.get("site"), required=False)
    except config.OpError as e:
        raise VoiceError(str(e), 422) from e
    cc = countries.country_of(e164)
    countries.require(conn, cc, cid, "porting")
    requested = _date(body.get("requested_date"))
    if not str(body.get("losing_carrier", "")).strip():
        raise VoiceError("Say which carrier the number is with now.", 422)
    try:
        num = conn.execute(
            """INSERT INTO voice_numbers (customer_id, e164, source, status, target_type, target_id, site_id, country,
                 provider, emergency_address)
               VALUES (%s, %s, 'ported', 'porting', %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                cid,
                e164,
                target_type,
                target_id,
                site["id"] if site else None,
                cc,
                providers.get().name,
                Jsonb(config.site_address(site)),
            ),
        ).fetchone()
    except psycopg.errors.UniqueViolation as e:
        raise VoiceError(f"{e164} is already on ExaCarib Connect or being ported.", 409) from e
    p = conn.execute(
        """INSERT INTO voice_port_orders (customer_id, number_id, e164, losing_carrier, status, country, account_name,
             account_number, service_address, requested_date, created_by, target_type, target_id, site_id)
           VALUES (%s, %s, %s, %s, 'draft', %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (
            cid,
            num["id"],
            e164,
            str(body["losing_carrier"]).strip()[:120],
            cc,
            str(body.get("account_name") or "").strip()[:200],
            str(body.get("account_number") or "").strip()[:60],
            Jsonb(body.get("service_address") or config.site_address(site)),
            requested,
            actor,
            target_type,
            target_id,
            site["id"] if site else None,
        ),
    ).fetchone()
    _event(conn, p, "draft", "Port order started. Add the documents, then submit it.", actor, notify=False)
    return get(conn, cid, p["id"])


def create_from_order(conn, cid, order_id, number_id, e164: str, spec: dict, ref: str, prov) -> None:
    """A port inside a stage 3 order: submitted to the provider straight away."""
    if conn.execute("SELECT 1 FROM voice_port_orders WHERE order_ref = %s", (ref,)).fetchone():
        return
    details = {
        "account_name": spec.get("account_name", ""),
        "account_number": spec.get("account_number", ""),
        "requested_date": spec.get("switch_date") or None,
        "documents": [],
    }
    sub = prov.submit_port(conn, cid, e164, spec.get("losing_carrier", ""), f"{cid}:{ref}", details)
    p = conn.execute(
        """INSERT INTO voice_port_orders (customer_id, order_id, number_id, e164, losing_carrier, status, switch_date,
             requested_date, provider_ref, order_ref, country, account_name, account_number, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (order_ref) DO NOTHING RETURNING *""",
        (
            cid,
            order_id,
            number_id,
            e164,
            str(spec.get("losing_carrier", ""))[:120],
            sub["status"],
            spec.get("switch_date") or None,
            spec.get("switch_date") or None,
            sub["ref"],
            ref,
            countries.country_of(e164),
            str(spec.get("account_name", ""))[:200],
            str(spec.get("account_number", ""))[:60],
            f"order:{order_id}",
        ),
    ).fetchone()
    if p:
        _event(
            conn, p, "submitted", "Sent to the losing carrier with the order.", "system", etype="voice.port_submitted"
        )
        _queue_poll(conn, p)


def _date(v) -> dt.date | None:
    if not v:
        return None
    try:
        return dt.date.fromisoformat(str(v))
    except ValueError as e:
        raise VoiceError("Dates look like 2026-11-30.", 422) from e


def update_details(conn, cid, port_id, body: dict, actor: str) -> dict:
    p = _get(conn, cid, port_id, lock=True)
    if p["status"] not in ("draft", "rejected", "documents_needed"):
        raise VoiceError("The details can be changed only before the port is accepted.", 409)
    vals = {}
    for k, n in (("account_name", 200), ("account_number", 60), ("losing_carrier", 120)):
        if k in body and body[k] is not None:
            vals[k] = str(body[k]).strip()[:n]
    if "requested_date" in body:
        vals["requested_date"] = _date(body["requested_date"])
    if vals:
        conn.execute(
            f"UPDATE voice_port_orders SET {', '.join(f'{k} = %s' for k in vals)}, updated_at = now() WHERE id = %s",
            (*vals.values(), p["id"]),
        )
        _event(
            conn,
            p,
            p["status"],
            "Details corrected: " + ", ".join(k.replace("_", " ") for k in vals) + ".",
            actor,
            notify=False,
        )
    return get(conn, cid, port_id)


def _doc_types(conn, port_id) -> list[str]:
    return sorted(
        {
            r["doc_type"]
            for r in conn.execute(
                "SELECT doc_type FROM voice_port_documents WHERE port_id = %s AND removed_at IS NULL", (port_id,)
            ).fetchall()
        }
    )


def submit(conn, cid, port_id, actor: str, *, can_spend: bool, accepted_price: dict | None) -> dict:
    """Send a draft (or a corrected rejected) port to the provider."""
    p = _get(conn, cid, port_id, lock=True)
    if p["status"] not in ("draft", "rejected"):
        raise VoiceError("This port has already been submitted.", 409)
    countries.require(conn, p["country"], cid, "porting")
    if not can_spend:
        raise VoiceError("Only someone with spend permission can submit a port (it has a one-time fee).", 403)
    price = billing.price_impact(billing.card_at(conn, cid), {"numbers": 1, "ported_numbers": 1})
    seen = accepted_price or {}
    if (seen.get("monthly_delta"), seen.get("one_time")) != (price["monthly_delta"], price["one_time"]):
        raise VoiceError(f"Check the price first: {price['monthly_delta']} a month and {price['one_time']} once.", 409)
    docs = _doc_types(conn, port_id)
    missing = [DOC_TYPES[t] for t in REQUIRED_DOCS if t not in docs]
    if missing:
        raise VoiceError("Add these documents first: " + ", ".join(missing).lower() + ".", 422)
    if not p["account_number"] or not p["account_name"]:
        raise VoiceError("Give the account name and number the losing carrier has for this line.", 422)
    attempt = conn.execute(
        "SELECT count(*) AS n FROM voice_port_events WHERE port_id = %s AND status = 'submitted'", (p["id"],)
    ).fetchone()["n"]
    details = {
        "account_name": p["account_name"],
        "account_number": p["account_number"],
        "requested_date": p["requested_date"].isoformat() if p["requested_date"] else None,
        "documents": docs,
    }
    sub = providers.get().submit_port(
        conn, cid, p["e164"], p["losing_carrier"], f"{cid}:port:{p['id']}:{attempt}", details
    )
    p = _set(conn, p, "submitted", provider_ref=sub["ref"], rejection_code="", rejection_reason="")
    _event(
        conn,
        p,
        "submitted",
        "Sent to the losing carrier with " + ", ".join(DOC_TYPES[t].lower() for t in docs) + ".",
        actor,
        etype="voice.port_submitted",
    )
    _queue_poll(conn, p)
    audit.record(conn, actor, "commai.voice.port_submit", str(p["id"]), cid, {"price": price})
    return get(conn, cid, port_id)


def _queue_poll(conn, p: dict) -> None:
    n = conn.execute("SELECT count(*) AS n FROM voice_port_events WHERE port_id = %s", (p["id"],)).fetchone()["n"]
    jobs.enqueue(
        conn,
        "voice.port_poll",
        {"port_id": str(p["id"])},
        customer_id=p["customer_id"],
        dedupe_key=f"voice.port_poll:{p['id']}:{n}",
        delay_s=min(providers.SIM_DELAYS.get("port", 0), 60),
        max_attempts=200,
    )


def _cutover_at(conn, p: dict, day: dt.date) -> dt.datetime:
    tz = "America/Port_of_Spain"
    if p.get("site_id"):
        row = conn.execute("SELECT timezone FROM voice_sites WHERE id = %s", (p["site_id"],)).fetchone()
        tz = row["timezone"] if row else tz
    return dt.datetime.combine(day, dt.time(CUTOVER_HOUR), ZoneInfo(tz))


def _schedule(conn, p: dict, foc: dt.date, actor: str, detail: str) -> dict:
    at = _cutover_at(conn, p, foc)
    p = _set(conn, p, "scheduled", foc_date=foc, switch_date=foc, cutover_at=at)
    _event(conn, p, "scheduled", detail, actor, etype="voice.port_scheduled")
    n = conn.execute("SELECT count(*) AS n FROM voice_port_events WHERE port_id = %s", (p["id"],)).fetchone()["n"]
    jobs.enqueue(
        conn,
        "voice.port_cutover",
        {"port_id": str(p["id"]), "foc": foc.isoformat()},
        customer_id=p["customer_id"],
        dedupe_key=f"voice.port_cutover:{p['id']}:{foc.isoformat()}:{n}",
        delay_s=max(0.0, (at - now()).total_seconds()),
        max_attempts=50,
    )
    return p


@jobs.handler("voice.port_poll")
def _poll(conn: psycopg.Connection, job: dict):
    p = conn.execute(
        "SELECT * FROM voice_port_orders WHERE id = %s FOR UPDATE", (job["payload"]["port_id"],)
    ).fetchone()
    if p is None or p["status"] != "submitted":
        return None
    res = providers.get().port_status(conn, p["provider_ref"])
    st = res["status"]
    if st == "submitted":
        return jobs.Later("The losing carrier has not answered yet.", 60)
    if st == "documents_needed":
        p = _set(conn, p, "documents_needed")
        _event(
            conn,
            p,
            "documents_needed",
            res.get("reason") or "The losing carrier needs more documents.",
            "provider",
            etype="voice.port_documents_needed",
        )
    elif st == "rejected":
        p = _set(conn, p, "rejected", rejection_code=res.get("code", ""), rejection_reason=res.get("reason", ""))
        _event(
            conn,
            p,
            "rejected",
            f"Rejected by the losing carrier: {res.get('reason') or 'no reason given'} "
            "Correct the details and submit again, or cancel.",
            "provider",
            etype="voice.port_rejected",
        )
    elif st == "accepted":
        foc = dt.date.fromisoformat(res["foc_date"])
        _schedule(
            conn, p, foc, "provider", f"Accepted. Firm order commitment: the number switches over on {foc.isoformat()}."
        )
    elif st == "cancelled":
        _cancelled(conn, p, "provider", "Cancelled by the provider.")
    return None


# ---- documents ------------------------------------------------------------------------


def _doc_root(data_dir: str, cid: Any) -> Path:
    root = Path(data_dir) / "commai" / "port-documents" / str(uuid.UUID(str(cid)))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _safe_name(name: str) -> str:
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .")[:100]
    return base or "document"


def check_file(filename: str, content_type: str, data: bytes) -> tuple[str, str]:
    """-> (safe filename, content type). Refuses anything but PDF, PNG and JPEG
    whose first bytes, declared type and file extension all agree."""
    if not data:
        raise VoiceError("The file is empty.", 422)
    if len(data) > MAX_DOC_BYTES:
        raise VoiceError("Files can be up to 10 MB.", 413)
    name = _safe_name(filename)
    for magic, ctype, exts in MAGIC:
        if data.startswith(magic):
            if content_type and content_type.split(";")[0].strip().lower() != ctype:
                raise VoiceError("The file's type does not match its contents.", 415)
            if not name.lower().endswith(exts):
                name = os.path.splitext(name)[0][:90] + exts[0]
            return name, ctype
    raise VoiceError("Send a PDF, PNG or JPEG file.", 415)


def add_document(
    conn, cid, port_id, doc_type: str, filename: str, content_type: str, data: bytes, actor: str, data_dir: str
) -> dict:
    p = _get(conn, cid, port_id, lock=True)
    if doc_type not in DOC_TYPES:
        raise VoiceError(f"Document types are {', '.join(DOC_TYPES)}.", 422)
    if p["status"] not in ("draft", "documents_needed", "rejected", "submitted"):
        raise VoiceError("This port no longer takes documents.", 409)
    name, ctype = check_file(filename, content_type, data)
    key = uuid.uuid4().hex
    path = _doc_root(data_dir, cid) / key
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    digest = hashlib.sha256(data).hexdigest()
    row = conn.execute(
        """INSERT INTO voice_port_documents (customer_id, port_id, doc_type, filename, content_type, size_bytes,
             sha256, storage_key, uploaded_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (cid, p["id"], doc_type, name, ctype, len(data), digest, key, actor),
    ).fetchone()
    _event(conn, p, p["status"], f"{DOC_TYPES[doc_type]} added ({name}).", actor, notify=False)
    if p["status"] in ("documents_needed", "submitted") and p["provider_ref"]:
        docs = [{"type": t} for t in _doc_types(conn, p["id"])]
        res = providers.get().send_port_documents(conn, p["provider_ref"], docs)
        if p["status"] == "documents_needed" and res.get("status") == "submitted":
            p = _set(conn, p, "submitted")
            _event(conn, p, "submitted", "Documents sent to the losing carrier.", actor, etype="voice.port_submitted")
            _queue_poll(conn, p)
    audit.record(conn, actor, "commai.voice.port_document", str(row["id"]), cid, {"type": doc_type, "bytes": len(data)})
    return {"id": row["id"], "filename": name, "content_type": ctype, "size_bytes": len(data), "sha256": digest}


def read_document(conn, cid, port_id, doc_id, data_dir: str) -> tuple[dict, bytes]:
    d = conn.execute(
        """SELECT * FROM voice_port_documents WHERE id = %s AND port_id = %s AND customer_id = %s
           AND removed_at IS NULL""",
        (doc_id, port_id, cid),
    ).fetchone()
    if d is None:
        raise VoiceError("Document not found.", 404)
    data = (_doc_root(data_dir, cid) / d["storage_key"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != d["sha256"]:
        raise VoiceError("The stored file does not match what was uploaded.", 500)
    return d, data


def remove_document(conn, cid, port_id, doc_id, actor: str, data_dir: str) -> None:
    p = _get(conn, cid, port_id, lock=True)
    if p["status"] not in ("draft", "documents_needed", "rejected"):
        raise VoiceError("Documents can be removed only before the port is with the losing carrier.", 409)
    d = conn.execute(
        """UPDATE voice_port_documents SET removed_at = now() WHERE id = %s AND port_id = %s AND customer_id = %s
           AND removed_at IS NULL RETURNING *""",
        (doc_id, port_id, cid),
    ).fetchone()
    if d is None:
        raise VoiceError("Document not found.", 404)
    try:
        (_doc_root(data_dir, cid) / d["storage_key"]).unlink()
    except FileNotFoundError:
        pass
    _event(conn, p, p["status"], f"{DOC_TYPES[d['doc_type']]} removed ({d['filename']}).", actor, notify=False)


# ---- reschedule, cancel, cut-over ------------------------------------------------------------


def reschedule(conn, cid, port_id, day: str, actor: str) -> dict:
    p = _get(conn, cid, port_id, lock=True)
    if p["status"] not in ("scheduled", "rolled_back"):
        raise VoiceError("Only a scheduled or rolled-back port can be given a new date.", 409)
    new = _date(day)
    if new is None:
        raise VoiceError("Give the new date.", 422)
    res = providers.get().reschedule_port(conn, p["provider_ref"], new)
    foc = dt.date.fromisoformat(res["foc_date"])
    _schedule(conn, p, foc, actor, f"Switch-over moved to {foc.isoformat()}.")
    return get(conn, cid, port_id)


def _cancelled(conn, p, actor, detail) -> dict:
    p = _set(conn, p, "cancelled")
    conn.execute(
        "UPDATE voice_numbers SET status = 'removed', removed_at = now() WHERE id = %s AND status = 'porting'",
        (p["number_id"],),
    )
    _event(conn, p, "cancelled", detail, actor, etype="voice.port_cancelled")
    return p


def cancel(conn, cid, port_id, actor: str) -> dict:
    p = _get(conn, cid, port_id, lock=True)
    if p["status"] not in OPEN:
        raise VoiceError("This port can't be cancelled now.", 409)
    if p["provider_ref"]:
        providers.get().cancel_port(conn, p["provider_ref"])
    _cancelled(conn, p, actor, "Cancelled. The number stays with the losing carrier.")
    return get(conn, cid, port_id)


def _test_calls(conn, prov, p) -> tuple[bool, str]:
    detail = ""
    for _ in range(TEST_CALL_TRIES):
        res = prov.test_call(conn, p["e164"])
        conn.execute(
            "INSERT INTO voice_test_calls (customer_id, order_id, number_id, e164, ok, detail)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (p["customer_id"], p["order_id"], p["number_id"], p["e164"], res["ok"], res["detail"]),
        )
        if res["ok"]:
            return True, res["detail"]
        detail = res["detail"]
    return False, detail


def go_live(conn, p: dict, actor: str) -> dict:
    """The number is with ExaCarib and answered its test call: make it live."""
    cid = p["customer_id"]
    conn.execute(
        """UPDATE voice_numbers SET status = 'active', billing_from = coalesce(billing_from, now()), provider = %s,
             provider_ref = %s WHERE id = %s""",
        (providers.get().name, p["provider_ref"], p["number_id"]),
    )
    p = _set(conn, p, "completed")
    if not p["order_id"]:
        billing.one_time_charge(conn, cid, f"port:{p['id']}", "port_number", f"Number port {p['e164']}")
    out = emergency.activate_outbound(conn, cid, p["number_id"])
    note = "" if out["outbound_enabled"] else f" Outbound calls wait: {out['outbound_reason']}"
    _event(
        conn,
        p,
        "completed",
        f"Switched over and the test call worked. The number is live.{note}",
        actor,
        etype="voice.port_completed",
    )
    jobs.enqueue(conn, "voice.render", {"customer_id": str(cid)}, customer_id=cid)
    return p


@jobs.handler("voice.port_cutover")
def _cutover(conn: psycopg.Connection, job: dict):
    pl = job["payload"]
    p = conn.execute("SELECT * FROM voice_port_orders WHERE id = %s FOR UPDATE", (pl["port_id"],)).fetchone()
    if (
        p is None
        or p["status"] not in ("scheduled", "cutting_over")
        or (p["foc_date"] and p["foc_date"].isoformat() != pl["foc"])
    ):
        return None  # cancelled, rescheduled or done
    if p["cutover_at"] and p["cutover_at"] > now():
        return jobs.Later("Waiting for the switch-over date.", max(60.0, (p["cutover_at"] - now()).total_seconds()))
    prov = providers.get()
    if p["status"] == "scheduled":
        p = _set(conn, p, "cutting_over")
        _event(conn, p, "cutting_over", "Switching the number over now.", "system", notify=False)
    try:
        prov.activate_port(conn, p["provider_ref"])
    except providers.ProviderError as e:
        _event(
            conn,
            p,
            "cutting_over",
            f"The provider could not switch over yet ({e}). Trying again shortly.",
            "system",
            notify=False,
        )
        return jobs.Later(str(e), 60)
    ok, detail = _test_calls(conn, prov, p)
    if ok:
        go_live(conn, p, "system")
        return None
    prov.rollback_port(conn, p["provider_ref"])
    conn.execute("UPDATE voice_numbers SET status = 'porting' WHERE id = %s AND status <> 'removed'", (p["number_id"],))
    p = _set(conn, p, "rolled_back")
    _event(
        conn,
        p,
        "rolled_back",
        f"Test calls failed after the switch-over ({detail}). The number was handed back to "
        f"{p['losing_carrier'] or 'the losing carrier'} and works as before. Pick a new date.",
        "system",
        etype="voice.port_rolled_back",
    )
    return None


def admin_set(conn, cid, port_id, status: str, switch_date: str | None, note: str, actor: str) -> dict:
    """ExaCarib moves a port by hand (while a provider's API is not wired, or
    to correct one). Completion still needs a working test call."""
    allowed = ("submitted", "documents_needed", "accepted", "scheduled", "completed", "rejected", "cancelled")
    if status not in allowed:
        raise VoiceError(f"Port status is one of {', '.join(allowed)}.", 422)
    p = _get(conn, cid, port_id, lock=True)
    if p["status"] in ("completed", "cancelled"):
        raise VoiceError("This port order is finished.", 409)
    detail = note or "Updated by ExaCarib."
    if status == "completed":
        if switch_date:
            p = _set(conn, p, p["status"], switch_date=_date(switch_date))
        ok, res = _test_calls(conn, providers.get(), p)
        if not ok:
            raise VoiceError(f"The number ported but the test call failed: {res}", 502)
        p = go_live(conn, p, actor)
    elif status == "cancelled":
        p = _cancelled(conn, p, actor, detail)
    elif status in ("accepted", "scheduled"):
        day = _date(switch_date) or p["foc_date"] or p["switch_date"]
        if day is None:
            p = _set(conn, p, "submitted" if status == "accepted" else status)
            _event(conn, p, p["status"], detail, actor)
        else:
            p = _schedule(conn, p, day, actor, f"{detail} Switch-over on {day.isoformat()}.")
    else:
        p = _set(conn, p, status, switch_date=_date(switch_date) or p["switch_date"])
        if status == "rejected":
            conn.execute("UPDATE voice_port_orders SET rejection_reason = %s WHERE id = %s", (detail[:300], p["id"]))
        _event(conn, p, status, detail, actor)
    conn.execute("UPDATE voice_port_orders SET note = %s WHERE id = %s", (note[:300], p["id"]))
    audit.record(conn, actor, "commai.voice.port", str(port_id), cid, {"status": status})
    return conn.execute("SELECT * FROM voice_port_orders WHERE id = %s", (port_id,)).fetchone()
