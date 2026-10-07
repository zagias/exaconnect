"""Pieces shared by the helpdesk, team chat, commerce, payments and knowledge
connectors (ADR 0035), on top of the connector kit (ADR 0034).

- Ticket links: a helpdesk ticket linked to a CommAI conversation, its status
  synced back from the helpdesk's webhook (a note on the conversation and a
  ``ticket.updated`` event).
- Staff messages for Slack and Teams that never carry more customer data
  than the business allows (``share`` setting: none, names or summary).
- Chat identities: a Slack or Teams user linked to a CommAI person, so a
  button press there approves as that person, through the action service.
- App hooks: the per-business inbound address a provider sends its change
  notifications to (``commai_inbound_hooks``, kind 'app').
- Knowledge sync: files from chosen folders become knowledge sources that a
  person approves (``knowledge.add_source``); a changed file needs approving
  again; a file the business can no longer see is removed.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import Callable, Iterable
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import events, inbox, jobs
from . import kit


class MoreConnector(kit.KitConnector):
    """The base for this group's connectors: a kit connector that sends no
    credentials to its stand-in (a stand-in connection has none) and can
    apply a verified change notification."""

    def auth_headers(self, conn, connection: dict) -> dict:
        if self.simulated(connection):
            return {"Authorization": "Bearer simulated"}
        return self.live_auth_headers(conn, connection)

    def live_auth_headers(self, conn, connection: dict) -> dict:
        return super().auth_headers(conn, connection)

    def on_webhook_event(self, conn, connection: dict, event: dict) -> None:
        """Apply one verified change notification (after verify_webhook)."""


events.register(
    "ticket.linked",
    "ticket.updated",
    "commerce.order_updated",
    "payment.succeeded",
    "payment.failed",
    "knowledge.file_synced",
    "knowledge.file_removed",
)

SHARE_LEVELS = ("none", "names", "summary")


def luhn_card(text: str) -> bool:
    """True when the text holds something shaped like a payment card number.
    Card data never passes through CommAI, so such inputs are refused."""
    for m in re.finditer(r"(?:\d[ -]?){13,19}", text or ""):
        digits = [int(c) for c in re.sub(r"\D", "", m.group(0))]
        if not 13 <= len(digits) <= 19:
            continue
        total = 0
        for i, d in enumerate(reversed(digits)):
            if i % 2:
                d *= 2
                if d > 9:
                    d -= 9
            total += d
        if total % 10 == 0:
            return True
    return False


# ---- per-connection state --------------------------------------------------------------


def state_get(conn, customer_id: Any, app: str, key: str) -> dict:
    row = conn.execute(
        "SELECT value FROM commai_connector_state WHERE customer_id = %s AND app = %s AND key = %s",
        (customer_id, app, key),
    ).fetchone()
    return row["value"] if row else {}


def state_put(conn, customer_id: Any, app: str, key: str, value: dict) -> None:
    conn.execute(
        """INSERT INTO commai_connector_state (customer_id, app, key, value) VALUES (%s, %s, %s, %s)
           ON CONFLICT (customer_id, app, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        (customer_id, app, key, Jsonb(value)),
    )


def customer_for(conn, app: str, key: str, ident: str) -> list[Any]:
    """The businesses whose provider identity (Slack team, Teams tenant) is ident."""
    if not ident:
        return []
    return [
        r["customer_id"]
        for r in conn.execute(
            "SELECT customer_id FROM commai_connector_state WHERE app = %s AND key = %s AND value->>'id' = %s",
            (app, key, ident),
        ).fetchall()
    ]


# ---- app hooks (change notifications from a provider) ------------------------------------


def app_hook(conn, customer_id: Any, app: str, actor: str = "") -> dict:
    """The business's inbound address for this app's change notifications,
    made on first use. The generic receiver (ADR 0034) checks each delivery
    with the connector's verify_webhook."""
    row = conn.execute(
        "SELECT * FROM commai_inbound_hooks WHERE customer_id = %s AND app = %s AND kind = 'app'",
        (customer_id, app),
    ).fetchone()
    if row:
        return row
    return conn.execute(
        """INSERT INTO commai_inbound_hooks (customer_id, kind, app, name, token, secret, created_by)
           VALUES (%s, 'app', %s, %s, %s, %s, %s) RETURNING *""",
        (customer_id, app, f"{app} changes", secrets.token_urlsafe(24), secrets.token_urlsafe(32), actor),
    ).fetchone()


def hook_url(hook: dict) -> str:
    return f"{os.environ.get('EXA_PUBLIC_URL', '').rstrip('/')}/api/v1/commai/integration-hooks/{hook['token']}"


def set_hook_secret(conn, hook: dict, secret: str) -> None:
    """Some providers choose the signing secret themselves (Stripe, Zendesk)."""
    conn.execute("UPDATE commai_inbound_hooks SET secret = %s WHERE id = %s", (secret, hook["id"]))


# ---- ticket links ------------------------------------------------------------------------


def link_ticket(
    conn,
    customer_id: Any,
    app: str,
    ticket_id: str,
    *,
    conversation_id: Any = None,
    number: str = "",
    status: str = "",
    url: str = "",
) -> dict:
    conv = None
    if conversation_id:
        conv = conn.execute(
            "SELECT id FROM conversations WHERE id::text = %s AND customer_id = %s", (str(conversation_id), customer_id)
        ).fetchone()
    row = conn.execute(
        """INSERT INTO commai_ticket_links (customer_id, app, ticket_id, conversation_id, number, status, url)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (customer_id, app, ticket_id) DO UPDATE SET
             conversation_id = COALESCE(EXCLUDED.conversation_id, commai_ticket_links.conversation_id),
             number = CASE WHEN EXCLUDED.number <> '' THEN EXCLUDED.number ELSE commai_ticket_links.number END,
             status = CASE WHEN EXCLUDED.status <> '' THEN EXCLUDED.status ELSE commai_ticket_links.status END,
             url = CASE WHEN EXCLUDED.url <> '' THEN EXCLUDED.url ELSE commai_ticket_links.url END,
             updated_at = now()
           RETURNING *, (xmax = 0) AS inserted""",
        (customer_id, app, str(ticket_id), conv["id"] if conv else None, number, status, url),
    ).fetchone()
    if row["inserted"]:
        events.emit(
            conn,
            customer_id,
            "ticket.linked",
            {"app": app, "ticket_id": str(ticket_id), "conversation_id": str(row["conversation_id"] or "")},
            f"{app}:{ticket_id}",
        )
    return row


def sync_ticket_status(conn, customer_id: Any, app: str, label: str, ticket_id: str, status: str) -> dict | None:
    """A helpdesk reported a ticket's new status: record it, note it on the
    linked conversation and emit ticket.updated. Unknown tickets are ignored."""
    row = conn.execute(
        "SELECT * FROM commai_ticket_links WHERE customer_id = %s AND app = %s AND ticket_id = %s FOR UPDATE",
        (customer_id, app, str(ticket_id)),
    ).fetchone()
    if row is None or not status or row["status"] == status:
        return row
    row = conn.execute(
        """UPDATE commai_ticket_links SET status = %s, updated_at = now()
           WHERE customer_id = %s AND app = %s AND ticket_id = %s RETURNING *""",
        (status, customer_id, app, str(ticket_id)),
    ).fetchone()
    if row["conversation_id"]:
        inbox.add_note(
            conn,
            customer_id,
            row["conversation_id"],
            author="CommAI",
            body=f"{label} ticket {row['number'] or ticket_id} is now {status}.",
        )
    events.emit(
        conn,
        customer_id,
        "ticket.updated",
        {
            "app": app,
            "ticket_id": str(ticket_id),
            "status": status,
            "conversation_id": str(row["conversation_id"] or ""),
        },
        f"{app}:{ticket_id}",
    )
    return row


def tickets_for(conn, customer_id: Any, conversation_id: Any) -> list[dict]:
    return conn.execute(
        """SELECT app, ticket_id, number, status, url, created_at, updated_at FROM commai_ticket_links
           WHERE customer_id = %s AND conversation_id::text = %s ORDER BY created_at""",
        (customer_id, str(conversation_id)),
    ).fetchall()


# ---- staff messages (Slack, Teams) -------------------------------------------------------


def portal_link(path: str) -> str:
    base = os.environ.get("EXA_PORTAL_URL", "").rstrip("/")
    return f"{base}{path}" if base else path


def conversation_brief(conn, customer_id: Any, conversation_id: Any, share: str) -> dict:
    """What a staff message may say about a conversation. 'none': only that
    something needs a person, with a link; 'names': plus the customer's name
    and the channel; 'summary': plus the subject and handover reason. Never
    message text, email addresses or phone numbers."""
    share = share if share in SHARE_LEVELS else "none"
    conv = conn.execute(
        """SELECT c.id, c.channel, c.subject, c.priority, ct.name AS contact_name FROM conversations c
           LEFT JOIN contacts ct ON ct.id = c.contact_id WHERE c.id::text = %s AND c.customer_id = %s""",
        (str(conversation_id), customer_id),
    ).fetchone()
    if conv is None:
        raise ValueError("No such conversation.")
    lines: list[str] = []
    if share in ("names", "summary"):
        who = conv["contact_name"] or "A customer"
        lines.append(f"{who}, on {conv['channel']}")
    if share == "summary":
        if conv["subject"]:
            lines.append(f"Subject: {conv['subject'][:120]}")
        h = conn.execute(
            "SELECT reason FROM handovers WHERE conversation_id = %s ORDER BY at DESC LIMIT 1", (conv["id"],)
        ).fetchone()
        if h and h["reason"]:
            lines.append(f"Reason: {h['reason'][:200]}")
    return {
        "id": str(conv["id"]),
        "priority": conv["priority"],
        "lines": lines,
        "link": portal_link(f"/commai/inbox/{conv['id']}"),
    }


def approval_brief(conn, customer_id: Any, run_id: Any, share: str) -> dict:
    """What an approval request may say: the app and action always; the
    inputs only when the business shares summaries."""
    from . import get

    run = conn.execute(
        "SELECT * FROM action_runs WHERE id::text = %s AND customer_id = %s", (str(run_id), customer_id)
    ).fetchone()
    if run is None:
        raise ValueError("No such action.")
    if run["status"] != "awaiting_approval":
        raise ValueError(f"That action is {run['status'].replace('_', ' ')}, not waiting for approval.")
    try:
        c = get(run["app"])
        spec = c.actions.get(run["action"])
        what = f"{c.label}: {spec.label if spec else run['action']}"
    except KeyError:
        what = f"{run['app']}: {run['action']}"
    lines = []
    if share == "summary":
        for k, v in list((run["inputs"] or {}).items())[:6]:
            if isinstance(v, str | int | float) and not re.search(r"@|\+?\d[\d ()-]{7,}", str(v)):
                lines.append(f"{k}: {str(v)[:80]}")
    return {
        "run_id": str(run["id"]),
        "what": what,
        "lines": lines,
        "link": portal_link(f"/commai/actions/{run['id']}"),
    }


def person_for(conn, customer_id: Any, app: str, external_user: str) -> str | None:
    """The CommAI person (as an actor, user:<email>) linked to a Slack or Teams
    user, if they may approve for this business (a member with a reply seat)."""
    row = conn.execute(
        """SELECT i.user_email FROM commai_chat_identities i
           JOIN users u ON lower(u.email) = lower(i.user_email)
           JOIN commai_members m ON m.user_id = u.id AND m.customer_id = i.customer_id
           WHERE i.customer_id = %s AND i.app = %s AND i.external_user = %s AND m.seat <> 'internal'""",
        (customer_id, app, external_user),
    ).fetchone()
    return f"user:{row['user_email']}" if row else None


def decide(conn, customer_id: Any, run_id: str, decision: str, approver: str) -> dict:
    """Approve or reject through the action service (one rule set for every surface)."""
    from .. import actions

    if decision == "approve":
        return actions.approve(conn, customer_id, run_id, approver=approver)
    return actions.reject(conn, customer_id, run_id, actor=approver, reason="Rejected in team chat.")


# ---- knowledge sync (Google Drive, OneDrive/SharePoint) ----------------------------------

MAX_BODY = 200_000


def sync_knowledge(
    conn: psycopg.Connection,
    customer_id: Any,
    app: str,
    label: str,
    files: Iterable[dict],
    fetch: Callable[[dict], str | None],
    *,
    complete: bool = True,
) -> dict:
    """Turn files into knowledge sources a person approves.

    ``files``: {"id", "name", "version", "url", "allowed": bool, "reason": str}.
    ``fetch(file)`` returns the file's text, or None when it has none.
    With ``complete`` (every chosen folder was listed), files no longer seen
    are removed: the business can no longer see them, so CommAI must not keep them.
    """
    from ..ai import knowledge

    known = {
        r["file_id"]: r
        for r in conn.execute(
            "SELECT * FROM commai_knowledge_files WHERE customer_id = %s AND app = %s", (customer_id, app)
        ).fetchall()
    }
    seen: set[str] = set()
    counts = {"added": 0, "changed": 0, "unchanged": 0, "skipped": 0, "removed": 0}

    def drop_source(row: dict | None) -> None:
        if row and row.get("source_id"):
            knowledge.delete_source(conn, customer_id, row["source_id"])

    def save(f: dict, status: str, reason: str, source_id: Any) -> None:
        conn.execute(
            """INSERT INTO commai_knowledge_files (customer_id, app, file_id, source_id, name, version, url, status,
                                                   reason, synced_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
               ON CONFLICT (customer_id, app, file_id) DO UPDATE SET source_id = EXCLUDED.source_id,
                 name = EXCLUDED.name, version = EXCLUDED.version, url = EXCLUDED.url, status = EXCLUDED.status,
                 reason = EXCLUDED.reason, synced_at = now()""",
            (
                customer_id,
                app,
                f["id"],
                source_id,
                f["name"][:300],
                str(f.get("version", "")),
                f.get("url", ""),
                status,
                reason,
            ),
        )

    for f in files:
        fid = str(f["id"])
        seen.add(fid)
        prev = known.get(fid)
        if not f.get("allowed", True):
            drop_source(prev)
            save(f, "skipped", f.get("reason") or "Not allowed by the business's file rules.", None)
            counts["skipped"] += 1
            continue
        if prev and prev["status"] == "synced" and prev["source_id"] and prev["version"] == str(f.get("version", "")):
            counts["unchanged"] += 1
            continue
        text = fetch(f)
        if not text or not text.strip():
            drop_source(prev)
            save(f, "skipped", "The file has no text CommAI can read.", None)
            counts["skipped"] += 1
            continue
        if len(text) > MAX_BODY:
            drop_source(prev)
            save(f, "skipped", "The file is too long (over 200,000 characters); split it.", None)
            counts["skipped"] += 1
            continue
        title = f"{f['name']}"[:200] or "Untitled"
        url = f.get("url", "") if str(f.get("url", "")).startswith("http") else ""
        if prev and prev["source_id"]:
            src = knowledge.update_source(conn, customer_id, prev["source_id"], title=title, body=text, source_url=url)
            if src is None:
                src = knowledge.add_source(
                    conn, customer_id, title=title, body=text, source_url=url, approved=False, created_by=f"{app}:sync"
                )
            counts["changed"] += 1
        else:
            src = knowledge.add_source(
                conn, customer_id, title=title, body=text, source_url=url, approved=False, created_by=f"{app}:sync"
            )
            counts["added"] += 1
        save(f, "synced", "Waiting for a person to approve it in Knowledge.", src["id"])
        events.emit(
            conn,
            customer_id,
            "knowledge.file_synced",
            {"app": app, "file_id": fid, "source_id": str(src["id"]), "name": f["name"][:200]},
            f"{app}:{fid}",
        )
    if complete:
        for fid, row in known.items():
            if fid not in seen and row["status"] != "removed":
                drop_source(row)
                conn.execute(
                    """UPDATE commai_knowledge_files SET status = 'removed', source_id = NULL, synced_at = now(),
                              reason = 'No longer in the chosen folders, or no longer shared with the business.'
                       WHERE customer_id = %s AND app = %s AND file_id = %s""",
                    (customer_id, app, fid),
                )
                events.emit(conn, customer_id, "knowledge.file_removed", {"app": app, "file_id": fid}, f"{app}:{fid}")
                counts["removed"] += 1
    return counts


def queue_sync(conn, customer_id: Any, app: str, delay_s: float = 0) -> None:
    """Queue a knowledge re-sync (at most one waiting per business and app)."""
    jobs.enqueue(
        conn,
        "connector.knowledge_sync",
        {"app": app},
        customer_id=customer_id,
        dedupe_key=f"ksync:{customer_id}:{app}:"
        + (f"at:{int((time.time() + delay_s) // delay_s)}" if delay_s else "now"),
        delay_s=delay_s,
    )


@jobs.handler("connector.knowledge_sync")
def _knowledge_sync(conn: psycopg.Connection, job: dict):
    from .. import connectors

    app = job["payload"]["app"]
    c = connectors.get(app)
    row = connectors.connection(conn, job["customer_id"], app)
    # The 'now' key is freed once this runs, so the next change queues again.
    conn.execute(
        "UPDATE jobs SET dedupe_key = dedupe_key || ':' || id WHERE id = %s AND dedupe_key LIKE '%%:now'",
        (job["id"],),
    )
    if row is None or row["status"] not in ("authorised", "testing", "live"):
        return None
    try:
        with conn.transaction():
            c.sync(conn, {**row, "test": False})
    except connectors.ConnectorError as e:
        conn.execute(
            "UPDATE integration_connections SET last_failure_at = now(), last_error = %s, last_cause = %s"
            " WHERE id = %s",
            (str(e)[:500], e.cause, row["id"]),
        )
        if e.cause == "provider" and job["attempts"] < job["max_attempts"]:
            return jobs.Later(str(e), delay_s=60)
    return None
