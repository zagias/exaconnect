"""Diagnostic checks for the channels (ADR 0018), run by the platform assistant.

Each answers "why might this channel have stopped sending?" from the
business's real configuration and recent sends: the account's status and
credentials, recent failures and blocks with their errors, rejected webhooks,
opted-out counts, the WhatsApp service window and template status.
"""

from __future__ import annotations

from typing import Any

import psycopg

from .. import diagnostics
from . import providers
from .email import SENDERS, SmtpSender


def _finding(area: str, status: str, summary: str, evidence: list[str], confidence: str = "confirmed", fix=None):
    return {
        "area": area,
        "status": status,
        "confidence": confidence,
        "summary": summary,
        "evidence": evidence,
        "fix": fix,
    }


def _recent(conn: psycopg.Connection, customer_id: Any, channel: str) -> dict:
    rows = conn.execute(
        """SELECT m.status, m.error, count(*) AS n, max(m.created_at) AS last FROM messages m
           JOIN conversations c ON c.id = m.conversation_id
           WHERE m.customer_id = %s AND c.channel = %s AND m.direction = 'out'
             AND m.created_at > now() - interval '7 days'
           GROUP BY m.status, m.error ORDER BY n DESC""",
        (customer_id, channel),
    ).fetchall()
    blocked = conn.execute(
        """SELECT e.data->>'reason' AS reason, count(*) AS n FROM commai_events e
           JOIN conversations c ON c.id::text = e.subject
           WHERE e.customer_id = %s AND e.type = 'message.blocked' AND c.channel = %s
             AND e.at > now() - interval '7 days'
           GROUP BY 1 ORDER BY n DESC LIMIT 3""",
        (customer_id, channel),
    ).fetchall()
    opted = conn.execute(
        "SELECT count(*) AS n FROM contact_identities WHERE customer_id = %s AND channel = %s AND opted_out",
        (customer_id, channel),
    ).fetchone()["n"]
    return {"rows": rows, "blocked": blocked, "opted_out": opted}


def _account_findings(conn, customer_id: Any, channel: str, label: str) -> list[dict]:
    out: list[dict] = []
    accts = conn.execute(
        "SELECT * FROM channel_accounts WHERE customer_id = %s AND channel = %s ORDER BY created_at",
        (customer_id, channel),
    ).fetchall()
    if not accts:
        return [
            _finding(
                channel,
                "problem",
                f"No {label} account is set up, so nothing can be sent.",
                [],
                fix={"id": "open_channels", "label": f"Set up {label}", "params": {"channel": channel}},
            )
        ]
    for a in accts:
        ev = [f"Account {a['address']} via {a['provider']}, status {a['status']}."]
        if channel == "email":
            sender = SENDERS.get(a["provider"])
            missing = SmtpSender.missing() if a["provider"] == "smtp" else []
            simulated = bool(sender and sender.simulated)
        else:
            prov = providers.get(a["provider"])
            missing = prov.missing(a)
            simulated = prov.simulated
        if simulated:
            ev.append("This account uses the simulated provider: nothing reaches real phones or inboxes.")
        if a["last_error"]:
            ev.append(f"Last provider error ({a['last_error_at']:%d %b %H:%M} UTC): {a['last_error']}")
        rejected = conn.execute(
            """SELECT count(*) AS n, max(at) AS last FROM channel_webhook_log
               WHERE account_id = %s AND outcome = 'rejected' AND at > now() - interval '24 hours'""",
            (a["id"],),
        ).fetchone()
        if rejected["n"]:
            ev.append(f"{rejected['n']} inbound webhooks were refused in the last 24 hours (signature did not verify).")
        if a["status"] in ("paused", "setup", "broken"):
            out.append(
                _finding(
                    channel,
                    "problem",
                    f"The {label} account {a['address']} is {a['status']}, so replies are refused.",
                    ev,
                    fix={
                        "id": "set_account_status",
                        "label": "Set the account live",
                        "params": {"account_id": str(a["id"]), "status": "live"},
                    }
                    if not missing
                    else None,
                )
            )
        elif missing:
            out.append(
                _finding(
                    channel,
                    "problem",
                    f"{a['provider']} is not configured: {', '.join(missing)} not set on the server.",
                    ev,
                )
            )
        elif rejected["n"]:
            out.append(
                _finding(
                    channel,
                    "problem",
                    "Inbound messages are being refused: the webhook signature does not match the configured secret.",
                    ev,
                    "likely",
                )
            )
        elif a["last_error"]:
            out.append(
                _finding(channel, "problem", f"The provider refused a recent send: {a['last_error']}", ev, "likely")
            )
        else:
            out.append(_finding(channel, "ok", f"The {label} account {a['address']} is live.", ev))
    return out


def _recent_findings(conn, customer_id: Any, channel: str, label: str) -> list[dict]:
    r = _recent(conn, customer_id, channel)
    out: list[dict] = []
    failed = [x for x in r["rows"] if x["status"] == "failed"]
    if failed:
        out.append(
            _finding(
                channel,
                "problem",
                f"{sum(x['n'] for x in failed)} {label} messages failed in the last 7 days.",
                [f"{x['n']} × {x['error'] or 'no reason given'}" for x in failed[:3]],
            )
        )
    # Blocked when written (a message.blocked event) or when the queued send ran (status 'blocked').
    blocked = [(x["n"], x["reason"]) for x in r["blocked"]]
    blocked += [(x["n"], x["error"]) for x in r["rows"] if x["status"] == "blocked"]
    if blocked:
        out.append(
            _finding(
                channel,
                "problem",
                f"{sum(n for n, _ in blocked)} {label} replies were blocked by the channel's rules in the last 7 days.",
                [f"{n} × {why}" for n, why in blocked[:4]],
            )
        )
    if r["opted_out"]:
        out.append(
            _finding(
                channel,
                "ok",
                f"{r['opted_out']} contacts have opted out of {label}; they are never sent to.",
                [],
                "confirmed",
            )
        )
    return out


@diagnostics.register("whatsapp")
def check_whatsapp(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    out = _account_findings(conn, customer_id, "whatsapp", "WhatsApp")
    out += _recent_findings(conn, customer_id, "whatsapp", "WhatsApp")
    closed = conn.execute(
        """SELECT count(*) AS n FROM conversations WHERE customer_id = %s AND channel = 'whatsapp'
           AND state <> 'resolved' AND (last_inbound_at IS NULL OR last_inbound_at < now() - interval '24 hours')""",
        (customer_id,),
    ).fetchone()["n"]
    if closed:
        out.append(
            _finding(
                "whatsapp",
                "ok",
                f"{closed} open WhatsApp conversations are outside the 24-hour "
                "service window: only an approved template can be sent to them.",
                [],
            )
        )
    tpls = conn.execute(
        "SELECT status, count(*) AS n FROM whatsapp_templates WHERE customer_id = %s GROUP BY status",
        (customer_id,),
    ).fetchall()
    counts = {t["status"]: t["n"] for t in tpls}
    if not counts.get("approved"):
        out.append(
            _finding(
                "whatsapp",
                "problem",
                "There is no approved WhatsApp template, so customers who "
                "haven't written in 24 hours can't be reached.",
                [f"{n} {s}" for s, n in counts.items()],
            )
        )
    return out


@diagnostics.register("sms")
def check_sms(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    return _account_findings(conn, customer_id, "sms", "SMS") + _recent_findings(conn, customer_id, "sms", "SMS")


@diagnostics.register("email")
def check_email(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    return _account_findings(conn, customer_id, "email", "email") + _recent_findings(
        conn, customer_id, "email", "email"
    )


@diagnostics.register("website_chat")
def check_widget(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    keys = conn.execute("SELECT * FROM widget_keys WHERE customer_id = %s AND active", (customer_id,)).fetchall()
    if not keys:
        return [
            _finding(
                "website_chat",
                "problem",
                "Website chat has no key yet, so the widget can't start.",
                [],
                fix={"id": "open_channels", "label": "Set up website chat", "params": {"channel": "web"}},
            )
        ]
    out = []
    for k in keys:
        installs = conn.execute(
            "SELECT * FROM widget_installs WHERE key_id = %s ORDER BY last_seen_at DESC LIMIT 5", (k["id"],)
        ).fetchall()
        ev = [f"Allowed origins: {', '.join(k['allowed_origins']) or 'none'}."]
        ev += [
            f"Seen on {i['origin']} at {i['last_seen_at']:%d %b %H:%M} UTC"
            + ("" if i["allowed"] else " (not on the allowed list)")
            for i in installs
        ]
        if not k["allowed_origins"]:
            out.append(
                _finding(
                    "website_chat",
                    "problem",
                    f"The key {k['name']} has no allowed origins, so every website is refused.",
                    ev,
                )
            )
        elif any(not i["allowed"] for i in installs):
            out.append(
                _finding(
                    "website_chat",
                    "problem",
                    "The widget is installed on a website that isn't on "
                    "the allowed list, so visitors there can't chat.",
                    ev,
                    "likely",
                )
            )
        elif not installs:
            out.append(
                _finding(
                    "website_chat", "unknown", "The widget has not reported in from any website yet.", ev, "unknown"
                )
            )
        else:
            out.append(_finding("website_chat", "ok", f"The widget {k['name']} is installed and allowed.", ev))
    return out
