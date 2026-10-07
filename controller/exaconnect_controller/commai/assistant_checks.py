"""AI agent and sign-in diagnostics for the platform assistant (ADR 0039).

AI agents: model failures, a high hand-over (escalation) rate, open
knowledge gaps, and tools the AI tried to use without permission.

Sign-in: single sign-on that is required but not switched on, SSO with no
approved email domain, a failed SSO test, directory sync (SCIM) tokens that
are missing or unused, and a burst of failed sign-ins for this business's
people. Evidence never includes email addresses, IP addresses or secrets.
"""

from __future__ import annotations

from typing import Any

import psycopg

from . import diagnostics, usage
from .automation.redact import redact

ESCALATION_MIN_RUNS = 10
ESCALATION_SHARE = 0.5
LOGIN_FAILURES = 5


def _f(area: str, status: str, confidence: str, summary: str, evidence=(), fix=None, ids=()) -> dict:
    out = {
        "area": area,
        "status": status,
        "confidence": confidence,
        "summary": summary,
        "evidence": [redact(str(e)) for e in evidence],
        "fix": fix,
    }
    if ids:
        out["correlation_ids"] = list(ids)
    return out


@diagnostics.register("ai_agents")
def check_ai(conn: psycopg.Connection, cid: Any) -> list[dict]:
    out = []
    r = conn.execute(
        """SELECT count(*) FILTER (WHERE outcome = 'failed' AND created_at > now() - interval '24 hours') AS failed,
                  (array_agg(reason ORDER BY created_at DESC) FILTER (WHERE outcome = 'failed'
                     AND created_at > now() - interval '24 hours'))[1:3] AS fail_reasons,
                  (array_agg(id ORDER BY created_at DESC) FILTER (WHERE outcome = 'failed'
                     AND created_at > now() - interval '24 hours'))[1:3] AS fail_ids,
                  count(*) FILTER (WHERE outcome IN ('replied', 'escalated')) AS answered,
                  count(*) FILTER (WHERE outcome = 'escalated') AS escalated
           FROM ai_runs WHERE customer_id = %s AND role = 'customer_agent'
             AND created_at > now() - interval '7 days'""",
        (cid,),
    ).fetchone()
    if r["failed"]:
        out.append(
            _f(
                "ai_agents:model",
                "problem",
                "confirmed",
                f"The AI agent failed {r['failed']} time(s) in the last 24 hours; those conversations went to people.",
                [f"Error: {x}" for x in r["fail_reasons"] or []],
                None,
                [f"ai_run:{i}" for i in r["fail_ids"] or []],
            )
        )
    if r["answered"] >= ESCALATION_MIN_RUNS and r["escalated"] / r["answered"] >= ESCALATION_SHARE:
        top = conn.execute(
            """SELECT reason, count(*) AS n FROM ai_runs WHERE customer_id = %s AND role = 'customer_agent'
                 AND outcome = 'escalated' AND created_at > now() - interval '7 days'
               GROUP BY reason ORDER BY n DESC LIMIT 3""",
            (cid,),
        ).fetchall()
        out.append(
            _f(
                "ai_agents:escalation",
                "problem",
                "likely",
                f"The AI agent handed over {r['escalated']} of {r['answered']} conversations in the last 7 days.",
                [f"{t['n']}: {t['reason']}" for t in top],
            )
        )
    gaps = conn.execute(
        """SELECT question, times FROM knowledge_gaps WHERE customer_id = %s AND status = 'open'
           ORDER BY times DESC, last_seen DESC LIMIT 3""",
        (cid,),
    ).fetchall()
    if gaps:
        n = conn.execute(
            "SELECT count(*) AS n FROM knowledge_gaps WHERE customer_id = %s AND status = 'open'", (cid,)
        ).fetchone()["n"]
        out.append(
            _f(
                "ai_agents:knowledge",
                "problem",
                "likely",
                f"{n} question(s) the AI couldn't answer from approved knowledge are waiting for an answer.",
                [f"Asked {g['times']} time(s): {g['question'][:120]}" for g in gaps]
                + ["Add the answers on the AI agents screen (Knowledge gaps)."],
            )
        )
    tools = conn.execute(
        """SELECT reason, count(*) AS n FROM ai_runs WHERE customer_id = %s AND outcome = 'escalated'
             AND reason LIKE '%%not allowed%%' AND created_at > now() - interval '7 days'
           GROUP BY reason ORDER BY n DESC LIMIT 3""",
        (cid,),
    ).fetchall()
    if tools:
        out.append(
            _f(
                "ai_agents:tools",
                "problem",
                "confirmed",
                "The AI agent tried to use an app action it is not allowed to, so it handed over instead.",
                [f"{t['n']}: {t['reason']}" for t in tools]
                + ["An admin can allow the action for the AI on the Integrations screen, if it should."],
            )
        )
    return out


@diagnostics.register("signin")
def check_signin(conn: psycopg.Connection, cid: Any) -> list[dict]:
    out = []
    conns = conn.execute(
        """SELECT c.*, (SELECT count(*) FROM sso_domains d WHERE d.connection_id = c.id AND d.status = 'approved')
                  AS domains
           FROM sso_connections c WHERE c.customer_id = %s ORDER BY c.created_at""",
        (cid,),
    ).fetchall()
    for c in conns:
        name = c["display_name"]
        if c["require_sso"] and c["status"] != "enabled":
            out.append(
                _f(
                    "signin:sso",
                    "problem",
                    "confirmed",
                    f"Single sign-on through {name} is required, but the connection is {c['status']}, so people "
                    "may be unable to sign in.",
                    ["Test and enable the connection, or stop requiring it (Settings, Sign-in)."],
                )
            )
        if c["status"] == "enabled" and not c["domains"]:
            out.append(
                _f(
                    "signin:sso",
                    "problem",
                    "confirmed",
                    f"{name} is switched on but no email domain is approved, so nobody is sent to it.",
                    ["Ask ExaCarib to approve your email domain (Settings, Sign-in)."],
                )
            )
        test = c["last_test"] or {}
        if test and test.get("ok") is False:
            out.append(
                _f(
                    "signin:sso",
                    "problem",
                    "confirmed",
                    f"The last test of {name} failed.",
                    [f"Result: {test.get('detail') or test.get('error') or 'no detail'}"],
                )
            )
    scim = conn.execute(
        """SELECT count(*) FILTER (WHERE revoked_at IS NULL) AS live,
                  max(last_used_at) FILTER (WHERE revoked_at IS NULL) AS last_used,
                  coalesce(max(last_used_at) FILTER (WHERE revoked_at IS NULL), '-infinity')
                    < now() - interval '7 days' AS stale,
                  (SELECT count(*) FROM scim_groups g WHERE g.customer_id = %s) AS groups
           FROM scim_tokens WHERE customer_id = %s""",
        (cid, cid),
    ).fetchone()
    if scim["groups"] and not scim["live"]:
        out.append(
            _f(
                "signin:scim",
                "problem",
                "confirmed",
                "Directory groups are set up but there is no live directory sync token, so changes in your "
                "directory no longer reach Connect.",
                ["Create a new token (Settings, Sign-in, Directory sync) and give it to your identity provider."],
            )
        )
    elif scim["live"] and scim["stale"]:
        out.append(
            _f(
                "signin:scim",
                "problem",
                "likely",
                "Your identity provider has not synced the directory for more than 7 days.",
                [f"Last sync: {scim['last_used'] or 'never'}"],
            )
        )
    fails = conn.execute(
        """SELECT a.detail->>'reason' AS reason, count(*) AS n, count(DISTINCT lower(a.target)) AS people
           FROM audit_log a JOIN users u ON lower(u.email) = lower(a.target)
           WHERE a.action = 'login_failed' AND a.at > now() - interval '24 hours' AND u.customer_id = %s
           GROUP BY 1 ORDER BY n DESC""",
        (cid,),
    ).fetchall()
    total = sum(f["n"] for f in fails)
    if total >= LOGIN_FAILURES:
        out.append(
            _f(
                "signin:failures",
                "problem",
                "likely",
                f"{total} sign-ins failed for {sum(f['people'] for f in fails)} of your people in the last 24 hours.",
                [f"{f['n']}: {f['reason'] or 'wrong password'}" for f in fails[:4]]
                + ["After 10 failures in 15 minutes, sign-in pauses for that person for 15 minutes."],
            )
        )
    return out


@diagnostics.register("budgets")
def check_budgets(conn: psycopg.Connection, cid: Any) -> list[dict]:
    out = []
    for b in usage.budgets(conn, cid):
        if b["state"] == "ok":
            continue
        stopped = b["state"] == "stopped"
        out.append(
            _f(
                f"usage:budget:{b['scope']}",
                "problem" if stopped else "ok",
                "confirmed",
                f"The monthly budget for {b['label']} is used up, so that work has stopped."
                if stopped
                else f"The monthly budget for {b['label']} has passed its alert level.",
                [
                    f"Spent {b['spent_this_month']} this month; alert at {b['monthly_alert'] or 'none'}, "
                    f"limit {b['monthly_hard'] or 'none'}.",
                    "An admin can change the budget on the Usage and bill screen.",
                ],
            )
        )
    return out
