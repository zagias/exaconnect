"""Data governance per business (ADR 0024): retention, legal hold, subject
requests, full business export and the processing-locations page.

Retention. Each category keeps data for a number of days, or keeps it:

  messages         customer-facing message text and attachments (not calls)
  notes            private notes
  call_recordings  the recording reference on a call record
  transcripts      the text of calls (messages on the 'voice' channel)
  ai_logs          AI run records (answers' sources, tool calls, reasons)

A durable job applies the rules once a day per business (and on request).
Message and transcript text is redacted (the message stays, so reports and
conversation history still count it); notes and AI logs are deleted; a call
keeps its record but loses its recording reference. Nothing that belongs to
a contact under legal hold is touched, and a business-wide hold stops the
job altogether. Each run records its cut-offs, what it did and what it held.

Subject requests. One contact's data can be exported (JSON, or a ZIP with
their files), anonymised or deleted. A legal hold blocks anonymising and
deleting. Every request is recorded.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import zipfile
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ... import audit
from .. import events, jobs

events.register("data.retention_applied", "data.subject_request", "data.export_ready")

CATEGORIES: dict[str, str] = {
    "messages": "Messages (text and attachments)",
    "notes": "Private notes",
    "call_recordings": "Call recordings",
    "transcripts": "Call transcripts",
    "ai_logs": "AI logs",
}

# Never leaves in an export, whichever table it is in.
SECRET_COLUMNS = [
    "secret",
    "hook_token",
    "hook_secret",
    "token_hash",
    "secret_ref",
    "password_hash",
    "totp_secret",
    "totp_pending",
    "id_token",
    "data",
]
# What a business export contains (notes only when asked for).
EXPORT_TABLES = [
    "contacts",
    "contact_identities",
    "conversations",
    "messages",
    "commai_notes",
    "conversation_log",
    "handovers",
    "contact_memory",
    "commai_settings",
    "commai_teams",
    "commai_members",
    "commai_routing_rules",
    "commai_role_tools",
    "ai_runs",
    "ai_calls",
    "knowledge_sources",
    "knowledge_gaps",
    "action_runs",
    "integration_connections",
    "webhook_endpoints",
    "widget_keys",
    "channel_accounts",
    "whatsapp_templates",
    "commai_workflows",
    "usage_records",
    "usage_limits",
    "commai_events",
    "voice_sites",
    "voice_users",
    "voice_numbers",
    "voice_cdrs",
    "commai_locations",
    "commai_brands",
    "commai_opening_hours",
    "commai_holidays",
    "commai_closures",
    "commai_roles",
    "commai_role_assignments",
    "commai_security_settings",
    "commai_security_alerts",
    "commai_retention_rules",
    "commai_retention_runs",
    "commai_subject_requests",
]


class DataError(ValueError):
    """A data request we refuse. `code` maps to an HTTP status."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str, indent=2, ensure_ascii=False)


# ---- settings ------------------------------------------------------------------------------


def data_settings(conn: psycopg.Connection, customer_id: Any) -> dict:
    row = conn.execute("SELECT * FROM commai_data_settings WHERE customer_id = %s", (customer_id,)).fetchone()
    return row or {
        "customer_id": customer_id,
        "hold_all": False,
        "hold_reason": "",
        "updated_by": "",
        "updated_at": None,
    }


def rules(conn: psycopg.Connection, customer_id: Any) -> dict[str, int | None]:
    got = {
        r["category"]: r["days"]
        for r in conn.execute(
            "SELECT category, days FROM commai_retention_rules WHERE customer_id = %s", (customer_id,)
        ).fetchall()
    }
    return {c: got.get(c) for c in CATEGORIES}


def set_rules(conn: psycopg.Connection, customer_id: Any, new: dict[str, int | None], actor: str) -> dict:
    for cat, days in new.items():
        if cat not in CATEGORIES:
            raise DataError(f"Unknown category: {cat}.")
        if days is not None and not 1 <= int(days) <= 36500:
            raise DataError("Keep data for between 1 and 36,500 days, or keep it.")
        conn.execute(
            """INSERT INTO commai_retention_rules (customer_id, category, days, updated_by) VALUES (%s, %s, %s, %s)
               ON CONFLICT (customer_id, category) DO UPDATE SET days = EXCLUDED.days,
                 updated_by = EXCLUDED.updated_by, updated_at = now()""",
            (customer_id, cat, days, actor),
        )
    return rules(conn, customer_id)


# ---- retention -------------------------------------------------------------------------------

_HELD_CONV = """EXISTS (SELECT 1 FROM contacts ct WHERE ct.id = c.contact_id AND ct.legal_hold)"""


def _apply(conn: psycopg.Connection, cid: Any, cat: str, cut: dt.datetime) -> tuple[int, int]:
    """(rows changed, rows held back by a legal hold) for one category."""
    p = {"c": cid, "cut": cut}
    if cat in ("messages", "transcripts"):
        voice = "=" if cat == "transcripts" else "<>"
        base = f"""FROM conversations c WHERE m.conversation_id = c.id AND m.customer_id = %(c)s
                   AND c.channel {voice} 'voice' AND m.created_at < %(cut)s AND m.redacted_at IS NULL"""
        held = conn.execute(
            f"SELECT count(*) AS n FROM messages m, conversations c WHERE m.conversation_id = c.id"
            f" AND m.customer_id = %(c)s AND c.channel {voice} 'voice' AND m.created_at < %(cut)s"
            f" AND m.redacted_at IS NULL AND {_HELD_CONV}",
            p,
        ).fetchone()["n"]
        n = conn.execute(
            f"""UPDATE messages m SET body = '', original_body = '', attachments = '[]', redacted_at = now()
                {base} AND NOT {_HELD_CONV}""",
            p,
        ).rowcount
        if cat == "messages":
            n += conn.execute(
                f"""DELETE FROM channel_files f USING conversations c WHERE f.conversation_id = c.id
                    AND f.customer_id = %(c)s AND f.created_at < %(cut)s AND NOT {_HELD_CONV}""",
                p,
            ).rowcount
        return n, held
    if cat == "notes":
        held = conn.execute(
            f"""SELECT count(*) AS n FROM commai_notes m JOIN conversations c ON c.id = m.conversation_id
                WHERE m.customer_id = %(c)s AND m.created_at < %(cut)s AND {_HELD_CONV}""",
            p,
        ).fetchone()["n"]
        n = conn.execute(
            f"""DELETE FROM commai_notes m USING conversations c WHERE c.id = m.conversation_id
                AND m.customer_id = %(c)s AND m.created_at < %(cut)s AND NOT {_HELD_CONV}""",
            p,
        ).rowcount
        return n, held
    if cat == "ai_logs":
        held_sql = f"EXISTS (SELECT 1 FROM conversations c WHERE c.id = r.conversation_id AND {_HELD_CONV})"
        held = conn.execute(
            f"""SELECT count(*) AS n FROM ai_runs r
                WHERE r.customer_id = %(c)s AND r.created_at < %(cut)s AND {held_sql}""",
            p,
        ).fetchone()["n"]
        n = conn.execute(
            f"DELETE FROM ai_runs r WHERE r.customer_id = %(c)s AND r.created_at < %(cut)s AND NOT {held_sql}", p
        ).rowcount
        return n, held
    if cat == "call_recordings":
        held_sql = """EXISTS (SELECT 1 FROM contacts ct WHERE ct.customer_id = v.customer_id AND ct.legal_hold
                      AND ct.phone <> '' AND ct.phone IN (v.from_number, v.to_number))"""
        held = conn.execute(
            f"""SELECT count(*) AS n FROM voice_cdrs v WHERE v.customer_id = %(c)s AND v.recording_ref <> ''
                AND v.ended_at < %(cut)s AND {held_sql}""",
            p,
        ).fetchone()["n"]
        n = conn.execute(
            f"""UPDATE voice_cdrs v SET recording_ref = '' WHERE v.customer_id = %(c)s AND v.recording_ref <> ''
                AND v.ended_at < %(cut)s AND NOT {held_sql}""",
            p,
        ).rowcount
        return n, held
    raise DataError(f"Unknown category: {cat}.")


def run_retention(
    conn: psycopg.Connection, customer_id: Any, trigger: str = "schedule", now: dt.datetime | None = None
) -> dict:
    """Apply the business's retention rules once and record what was done."""
    now = now or dt.datetime.now(dt.UTC)
    r = rules(conn, customer_id)
    ds = data_settings(conn, customer_id)
    cutoffs = {c: (now - dt.timedelta(days=d)).isoformat() for c, d in r.items() if d is not None}
    counts: dict[str, int] = {}
    held: dict[str, int] = {}
    if ds["hold_all"]:
        held = {"all": 1}
    else:
        for cat, days in r.items():
            if days is None:
                continue
            counts[cat], held[cat] = _apply(conn, customer_id, cat, now - dt.timedelta(days=days))
    run = conn.execute(
        """INSERT INTO commai_retention_runs (customer_id, trigger, cutoffs, counts, held, finished_at)
           VALUES (%s, %s, %s, %s, %s, now()) RETURNING *""",
        (customer_id, trigger, Jsonb(cutoffs), Jsonb(counts), Jsonb(held)),
    ).fetchone()
    audit.record(
        conn,
        "system:retention",
        "commai.data.retention_run",
        str(run["id"]),
        customer_id,
        {"trigger": trigger, "counts": counts, "held": held, "hold_all": ds["hold_all"]},
    )
    events.emit(
        conn, customer_id, "data.retention_applied", {"run_id": str(run["id"]), "counts": counts}, str(run["id"])
    )
    return run


def schedule_retention(conn: psycopg.Connection, customer_id: Any, trigger: str = "schedule") -> int | None:
    """Queue today's retention run (once a day per business), if it has any rule."""
    if not any(d is not None for d in rules(conn, customer_id).values()):
        return None
    if trigger == "schedule":
        key = f"retention:{customer_id}:{dt.datetime.now(dt.UTC):%Y-%m-%d}"
    else:
        key = f"retention:{customer_id}:{trigger}:{dt.datetime.now(dt.UTC):%Y%m%d%H%M%S%f}"
    return jobs.enqueue(
        conn,
        "enterprise.retention",
        {"customer_id": str(customer_id), "trigger": trigger},
        customer_id=customer_id,
        dedupe_key=key,
    )


@jobs.handler("enterprise.retention")
def _retention_job(conn: psycopg.Connection, job: dict):
    run_retention(conn, job["payload"]["customer_id"], job["payload"].get("trigger", "schedule"))
    return None


# ---- subject requests -------------------------------------------------------------------------


def _contact(conn: psycopg.Connection, customer_id: Any, contact_id: Any, lock: bool = False) -> dict:
    row = conn.execute(
        "SELECT * FROM contacts WHERE id = %s AND customer_id = %s" + (" FOR UPDATE" if lock else ""),
        (contact_id, customer_id),
    ).fetchone()
    if row is None:
        raise DataError("Contact not found.", 404)
    return row


def _record_request(conn, customer_id, contact_id, kind, actor, counts=None, status="done", reason="") -> dict:
    row = conn.execute(
        """INSERT INTO commai_subject_requests (customer_id, contact_id, kind, status, reason, counts, requested_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (customer_id, contact_id, kind, status, reason, Jsonb(counts or {}), actor),
    ).fetchone()
    audit.record(
        conn,
        actor,
        f"commai.data.subject_{kind}",
        str(contact_id),
        customer_id,
        {"status": status, "reason": reason, "counts": counts or {}},
    )
    events.emit(
        conn,
        customer_id,
        "data.subject_request",
        {"request_id": str(row["id"]), "contact_id": str(contact_id), "kind": kind, "status": status},
        str(contact_id),
    )
    return row


def subject_export(
    conn: psycopg.Connection, customer_id: Any, contact_id: Any, actor: str, include_notes: bool = False
) -> dict:
    """Everything this business holds about one contact. Every query is keyed
    on both the business and the contact (or that contact's conversations)."""
    contact = _contact(conn, customer_id, contact_id)
    p = {"c": customer_id, "ct": contact_id}
    convs = conn.execute(
        "SELECT * FROM conversations WHERE customer_id = %(c)s AND contact_id = %(ct)s ORDER BY created_at", p
    ).fetchall()
    ids = [c["id"] for c in convs]
    q = {**p, "ids": ids}
    phones = [x for x in {contact["phone"]} if x]
    out = {
        "exported_at": dt.datetime.now(dt.UTC).isoformat(),
        "contact": contact,
        "identities": conn.execute(
            "SELECT * FROM contact_identities WHERE customer_id = %(c)s AND contact_id = %(ct)s", p
        ).fetchall(),
        "conversations": convs,
        "messages": conn.execute(
            """SELECT id, conversation_id, direction, author_kind, body, original_body, original_language, template,
                      attachments, status, created_at, redacted_at
               FROM messages WHERE customer_id = %(c)s AND conversation_id = ANY(%(ids)s) ORDER BY created_at""",
            q,
        ).fetchall(),
        "files": conn.execute(
            """SELECT id, conversation_id, name, content_type, size, created_at FROM channel_files
               WHERE customer_id = %(c)s AND conversation_id = ANY(%(ids)s)""",
            q,
        ).fetchall(),
        "ai_memory": conn.execute(
            "SELECT fact, source, created_at FROM contact_memory WHERE customer_id = %(c)s AND contact_id = %(ct)s", p
        ).fetchall(),
        "ai_runs": conn.execute(
            """SELECT id, conversation_id, role, task, outcome, intent, reason, language, created_at
               FROM ai_runs WHERE customer_id = %(c)s AND conversation_id = ANY(%(ids)s) ORDER BY created_at""",
            q,
        ).fetchall(),
        "calls": conn.execute(
            """SELECT call_id, direction, from_number, to_number, started_at, ended_at, seconds,
                      (recording_ref <> '') AS has_recording
               FROM voice_cdrs WHERE customer_id = %(c)s AND (from_number = ANY(%(ph)s) OR to_number = ANY(%(ph)s))""",
            {**p, "ph": phones},
        ).fetchall(),
        "requests": conn.execute(
            """SELECT kind, status, reason, created_at FROM commai_subject_requests
               WHERE customer_id = %(c)s AND contact_id = %(ct)s ORDER BY created_at""",
            p,
        ).fetchall(),
        "notes_included": include_notes,
    }
    if include_notes:
        out["notes"] = conn.execute(
            """SELECT id, conversation_id, author, body, created_at FROM commai_notes
               WHERE customer_id = %(c)s AND conversation_id = ANY(%(ids)s) ORDER BY created_at""",
            q,
        ).fetchall()
    _record_request(
        conn,
        customer_id,
        contact_id,
        "export",
        actor,
        {"conversations": len(convs), "messages": len(out["messages"]), "notes_included": include_notes},
    )
    return out


def subject_zip(conn: psycopg.Connection, customer_id: Any, data: dict) -> bytes:
    """The export as a ZIP: subject.json and the contact's files."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("subject.json", _json(data))
        for f in data["files"]:
            row = conn.execute(
                "SELECT data FROM channel_files WHERE id = %s AND customer_id = %s", (f["id"], customer_id)
            ).fetchone()
            if row:
                safe = "".join(ch for ch in f["name"] if ch.isalnum() or ch in "._-")[:80] or "file"
                z.writestr(f"files/{f['id']}-{safe}", bytes(row["data"]))
    return buf.getvalue()


def _hold_reason(conn, customer_id, contact: dict) -> str:
    if contact["legal_hold"]:
        return (
            "This contact is under legal hold"
            + (f" ({contact['legal_hold_reason']})" if contact["legal_hold_reason"] else "")
            + "."
        )
    ds = data_settings(conn, customer_id)
    if ds["hold_all"]:
        return "Your organisation has a legal hold on all data."
    return ""


def subject_erase(conn: psycopg.Connection, customer_id: Any, contact_id: Any, mode: str, actor: str) -> dict:
    """Anonymise (keep the conversation shells, remove what identifies the
    person) or delete (remove the contact and their conversations)."""
    if mode not in ("anonymise", "delete"):
        raise DataError("Choose anonymise or delete.")
    contact = _contact(conn, customer_id, contact_id, lock=True)
    reason = _hold_reason(conn, customer_id, contact)
    if reason:
        req = _record_request(conn, customer_id, contact_id, mode, actor, status="refused", reason=reason)
        return {"refused": True, "reason": reason, "request": req}
    p = {"c": customer_id, "ct": contact_id}
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM conversations WHERE customer_id = %(c)s AND contact_id = %(ct)s", p
        ).fetchall()
    ]
    q = {**p, "ids": ids, "phone": contact["phone"]}
    counts: dict[str, int] = {"conversations": len(ids)}
    if contact["phone"]:
        counts["calls"] = conn.execute(
            """UPDATE voice_cdrs
               SET from_number = CASE WHEN from_number = %(phone)s THEN 'removed' ELSE from_number END,
                      to_number = CASE WHEN to_number = %(phone)s THEN 'removed' ELSE to_number END, recording_ref = ''
               WHERE customer_id = %(c)s AND %(phone)s IN (from_number, to_number)""",
            q,
        ).rowcount
    conn.execute(
        """UPDATE action_runs SET inputs = '{}', result = '{}' WHERE customer_id = %(c)s
           AND conversation_id = ANY(%(ids)s)""",
        q,
    )
    if mode == "delete":
        counts["messages"] = conn.execute(
            "SELECT count(*) AS n FROM messages WHERE customer_id = %(c)s AND conversation_id = ANY(%(ids)s)", q
        ).fetchone()["n"]
        conn.execute("DELETE FROM conversations WHERE customer_id = %(c)s AND id = ANY(%(ids)s)", q)
        conn.execute("DELETE FROM contacts WHERE customer_id = %(c)s AND id = %(ct)s", p)
    else:
        counts["messages"] = conn.execute(
            """UPDATE messages SET body = '', original_body = '', attachments = '[]', redacted_at = now()
               WHERE customer_id = %(c)s AND conversation_id = ANY(%(ids)s)""",
            q,
        ).rowcount
        counts["notes"] = conn.execute(
            "DELETE FROM commai_notes WHERE customer_id = %(c)s AND conversation_id = ANY(%(ids)s)", q
        ).rowcount
        counts["files"] = conn.execute(
            "DELETE FROM channel_files WHERE customer_id = %(c)s AND conversation_id = ANY(%(ids)s)", q
        ).rowcount
        counts["ai_runs"] = conn.execute(
            "DELETE FROM ai_runs WHERE customer_id = %(c)s AND conversation_id = ANY(%(ids)s)", q
        ).rowcount
        conn.execute("DELETE FROM handovers WHERE customer_id = %(c)s AND conversation_id = ANY(%(ids)s)", q)
        conn.execute("UPDATE ai_calls SET caller = '' WHERE customer_id = %(c)s AND conversation_id = ANY(%(ids)s)", q)
        conn.execute("UPDATE conversations SET subject = '' WHERE customer_id = %(c)s AND id = ANY(%(ids)s)", q)
        counts["memory"] = conn.execute(
            "DELETE FROM contact_memory WHERE customer_id = %(c)s AND contact_id = %(ct)s", p
        ).rowcount
        counts["identities"] = conn.execute(
            "DELETE FROM contact_identities WHERE customer_id = %(c)s AND contact_id = %(ct)s", p
        ).rowcount
        conn.execute(
            """UPDATE contacts SET name = '', email = '', phone = '', external_ref = '', anonymised_at = now()
               WHERE customer_id = %(c)s AND id = %(ct)s""",
            p,
        )
    req = _record_request(conn, customer_id, contact_id, mode, actor, counts)
    return {"refused": False, "counts": counts, "request": req}


def set_hold(conn: psycopg.Connection, customer_id: Any, contact_id: Any, on: bool, reason: str) -> dict:
    _contact(conn, customer_id, contact_id)
    return conn.execute(
        """UPDATE contacts SET legal_hold = %s, legal_hold_reason = %s WHERE id = %s AND customer_id = %s
           RETURNING id, name, legal_hold, legal_hold_reason""",
        (on, reason if on else "", contact_id, customer_id),
    ).fetchone()


# ---- business export ---------------------------------------------------------------------------


def request_export(conn: psycopg.Connection, customer_id: Any, actor: str, include_notes: bool) -> dict:
    row = conn.execute(
        "INSERT INTO commai_exports (customer_id, requested_by, include_notes) VALUES (%s, %s, %s) RETURNING *",
        (customer_id, actor, include_notes),
    ).fetchone()
    jobs.enqueue(
        conn,
        "enterprise.export",
        {"export_id": str(row["id"])},
        customer_id=customer_id,
        dedupe_key=f"export:{row['id']}",
    )
    return row


def build_export(conn: psycopg.Connection, customer_id: Any, include_notes: bool) -> tuple[bytes, dict]:
    buf = io.BytesIO()
    counts: dict[str, int] = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for table in EXPORT_TABLES:
            if table == "commai_notes" and not include_notes:
                continue
            has = conn.execute(
                """SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
                   AND table_name = %s AND column_name = 'customer_id'""",
                (table,),
            ).fetchone()
            if has is None:
                continue
            rows = [
                r["row"]
                for r in conn.execute(
                    f"SELECT to_jsonb(t) - %s::text[] AS row FROM {table} t WHERE t.customer_id = %s",  # noqa: S608
                    (SECRET_COLUMNS, customer_id),
                ).fetchall()
            ]
            counts[table] = len(rows)
            z.writestr(f"{table}.json", _json(rows))
        people = conn.execute(
            """SELECT id, email, role, display_name, given_name, family_name, disabled_at, created_at
               FROM users WHERE customer_id = %s""",
            (customer_id,),
        ).fetchall()
        counts["users"] = len(people)
        z.writestr("users.json", _json(people))
        z.writestr(
            "README.txt",
            "ExaCarib Connect business export. One JSON file per table, rows for your organisation only.\n"
            "Secrets (keys, webhook secrets, password hashes) are never included."
            + ("" if include_notes else "\nPrivate notes were left out of this export.")
            + "\n",
        )
    return buf.getvalue(), counts


@jobs.handler("enterprise.export")
def _export_job(conn: psycopg.Connection, job: dict):
    row = conn.execute(
        "SELECT * FROM commai_exports WHERE id = %s FOR UPDATE", (job["payload"]["export_id"],)
    ).fetchone()
    if row is None or row["status"] != "queued":
        return None
    data, counts = build_export(conn, row["customer_id"], row["include_notes"])
    conn.execute(
        """UPDATE commai_exports SET status = 'ready', data = %s, size = %s, counts = %s, ready_at = now()
           WHERE id = %s""",
        (data, len(data), Jsonb(counts), row["id"]),
    )
    events.emit(conn, row["customer_id"], "data.export_ready", {"export_id": str(row["id"])}, str(row["id"]))
    return None


@jobs.on_dead("enterprise.export")
def _export_dead(job: dict, error: str) -> None:
    from ... import db

    with db.tx() as conn:
        conn.execute(
            "UPDATE commai_exports SET status = 'failed', error = %s WHERE id = %s",
            (error[:500], job["payload"]["export_id"]),
        )


# ---- processing locations and subprocessors -------------------------------------------------------

# What the providers publish about where they process data. Not verified by ExaCarib.
PROVIDER_FACTS = {
    "twilio": ("Twilio Inc.", "United States (as Twilio publishes; not verified by ExaCarib)"),
    "360dialog": ("360dialog GmbH", "Germany, with Meta's WhatsApp servers (as 360dialog publishes)"),
    "smtp": ("Your configured mail server", "Wherever that server runs"),
    "deepinfra": ("DeepInfra Inc.", "United States (as DeepInfra publishes; not verified by ExaCarib)"),
}


def processing(conn: psycopg.Connection, settings: Any, customer_id: Any) -> dict:
    """Where this business's data is processed and by whom, generated from the
    controller's configuration and this business's own accounts."""
    hosting = os.environ.get("EXA_HOSTING_REGION", "")
    rows: list[dict] = [
        {
            "what": "Application and database (conversations, contacts, notes, settings)",
            "who": "ExaCarib (self-hosted controller and PostgreSQL)",
            "where": hosting or "Not recorded yet: set EXA_HOSTING_REGION on the server",
            "status": "live",
        },
        {
            "what": "Sign-in gateway",
            "who": "Keycloak, run by ExaCarib next to the controller",
            "where": hosting or "Same host as the controller",
            "status": "live" if getattr(settings, "oidc_issuer", "") else "not_configured",
        },
    ]
    if getattr(settings, "llm_api_key", ""):
        who, where = PROVIDER_FACTS["deepinfra"]
        base = getattr(settings, "llm_base_url", "")
        if "deepinfra" not in base:
            who, where = f"The AI provider at {base}", "As that provider publishes"
        rows.append({"what": "AI answers and summaries", "who": who, "where": where, "status": "live"})
    else:
        rows.append(
            {
                "what": "AI answers and summaries",
                "who": "Simulated model inside the controller",
                "where": "No data leaves ExaCarib",
                "status": "simulated",
            }
        )
    if os.environ.get("EXA_COMMAI_SERVER_SPEECH", "").lower() in ("on", "1", "true", "yes"):
        who, where = PROVIDER_FACTS["deepinfra"]
        rows.append(
            {"what": "Call speech (speech to text, text to speech)", "who": who, "where": where, "status": "live"}
        )
    else:
        rows.append(
            {
                "what": "Call speech",
                "who": "The caller's own browser",
                "where": "On the caller's device",
                "status": "live",
            }
        )
    for acct in conn.execute(
        "SELECT channel, provider, address FROM channel_accounts WHERE customer_id = %s ORDER BY channel",
        (customer_id,),
    ).fetchall():
        sim = acct["provider"] == "simulated"
        who, where = PROVIDER_FACTS.get(acct["provider"], (acct["provider"], "As that provider publishes"))
        rows.append(
            {
                "what": f"{acct['channel'].capitalize()} messages ({acct['address']})",
                "who": "Simulated provider inside the controller" if sim else who,
                "where": "No data leaves ExaCarib" if sim else where,
                "status": "simulated" if sim else "live",
            }
        )
    for c in conn.execute(
        "SELECT app, test_mode, status FROM integration_connections WHERE customer_id = %s ORDER BY app", (customer_id,)
    ).fetchall():
        rows.append(
            {
                "what": f"Integration: {c['app']}",
                "who": "Simulated connector" if c["test_mode"] else f"{c['app']} (your own account)",
                "where": "No data leaves ExaCarib" if c["test_mode"] else "Where your account with them is hosted",
                "status": "simulated" if c["test_mode"] else c["status"],
            }
        )
    rows.append(
        {
            "what": "Phone numbers and calls (SIP)",
            "who": "Simulated SIP provider",
            "where": "No calls leave ExaCarib yet",
            "status": "simulated",
        }
    )
    remote = os.environ.get("EXA_BACKUP_RCLONE_REMOTE", "")
    rows.append(
        {
            "what": "Backups (encrypted, kept 14 days)",
            "who": "ExaCarib",
            "where": (
                "Controller host and an off-site copy" if remote else "Controller host only; no off-site copy yet"
            ),
            "status": "live",
        }
    )
    return {
        "generated_at": dt.datetime.now(dt.UTC),
        "rows": rows,
        "note": "Generated from this server's configuration and your accounts. Where a provider says where it "
        "processes data, we repeat what it publishes; ExaCarib has not audited it. Simulated means the "
        "data never leaves ExaCarib.",
    }
