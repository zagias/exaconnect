"""Workflow engine on the durable job queue (ADR 0020).

A workflow is a trigger (an event type from commai_events plus conditions)
and a list of steps. Definitions are versioned; one version is live at a
time; every run records every step. Temporal remains the plan at scale:
this engine keeps the same concepts (versioned definitions, durable timers,
signals, activities with idempotency keys) behind `Engine`, so it can be
swapped without changing definitions.

Steps:
  condition     stop (or jump forward) unless conditions hold
  action        a connector action through commai.actions (role "workflow"):
                checked, approved where sensitive, executed once
  send_message  a reply on the triggering conversation; to many
                conversations only after an approval step
  assign        to a team (by name) or a person (by email)
  add_note      a private note on the conversation
  wait          a duration, or until an event (with a time-out)
  approval      waits for a person to approve or reject
  remind        if nobody has replied after a time, remind the owner
  escalate      if nobody has replied after a time, move it to a team and raise priority
  collect       ask the customer a question and wait for the answer

Triggers are dispatched by one job ("workflow.dispatch") that reads
commai_events after a stored cursor and re-enqueues itself, so events.emit
needs no changes. A live run happens once per (workflow version, event).

A trigger can also be a schedule ({"type": "schedule", "schedule":
{"every_minutes": 60}} or {"at": "09:00", "days": ["mon", ...]} in the
business's time zone) or manual ({"type": "manual"}: started by a person or
through POST /workflows/{id}/trigger). Each start is recorded as an event
(workflow.scheduled, workflow.triggered) so a run still happens once per event.

Exceptions (ADR 0039): a step's on_failure is "stop", "continue" or the id
of a later step to jump to; steps marked only_on_failure run only after such
a jump, with {{failure.step}} and {{failure.error}} filled in. A workflow's
notify_on_failure lists people told when a run fails.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import secrets
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import actions, connectors, events, inbox, jobs, usage

log = logging.getLogger("exaconnect.commai.workflows")

events.register(
    "workflow.published",
    "workflow.paused",
    "workflow.resumed",
    "workflow.run_started",
    "workflow.run_finished",
    "workflow.run_failed",
    "workflow.approval_requested",
    "workflow.reminder",
    "workflow.escalated",
    "workflow.scheduled",
    "workflow.triggered",
    "workflow.exception",
)

STEP_TYPES = (
    "condition",
    "action",
    "send_message",
    "assign",
    "add_note",
    "wait",
    "approval",
    "remind",
    "escalate",
    "collect",
)
OPS = ("eq", "ne", "contains", "not_contains", "in", "exists", "gt", "lt")
MAX_STEPS = 30
MAX_WAIT_S = 30 * 86400
DISPATCH_S = 2.0  # seconds between dispatcher passes
GAP_WAIT_S = 5.0  # how long a gap in event numbers is waited on (an uncommitted event)
MAX_RUNS_PER_CONVERSATION_HOUR = 20
# Keys the AI (or anyone) cannot put in a definition to skip rules or widen permissions.
FORBIDDEN_KEYS = {
    "approved",
    "approved_by",
    "approve",
    "skip_checks",
    "skip_approval",
    "grant",
    "grant_tools",
    "role",
    "tools",
    "permissions",
    "allowed_actions",
    "sensitive",
}
TRIGGER_BLOCKLIST_PREFIXES = ("workflow.", "webhook.")
TRIGGER_TYPES = ("event", "schedule", "manual")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MIN_EVERY_MINUTES = 5
FAILURE_STEPS = ("action", "send_message", "assign", "collect")


class WorkflowError(Exception):
    def __init__(self, message: str, code: int = 400, problems: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.problems = problems or []


class Engine:
    """The seam for Temporal: start a run for an event, signal a waiting
    run, and fire a timer. This Postgres engine implements it with jobs."""

    def start(self, conn, workflow: dict, version: dict, event: dict) -> dict | None:
        return start_run(conn, workflow, version, event)

    def signal(self, conn, run_id: Any, token: str, kind: str, data: dict) -> None:
        jobs.enqueue(
            conn,
            "workflow.step",
            {"run_id": str(run_id), "resume": {"kind": kind, "token": token, **data}},
            dedupe_key=f"wf:{run_id}:{token}:{kind}",
        )


engine = Engine()


# ---- definitions -----------------------------------------------------------------


def _dur(v: Any, name: str, problems: list[str], default: int | None = None) -> int | None:
    if v in (None, ""):
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        problems.append(f"{name} must be a number of seconds.")
        return default
    if not 1 <= n <= MAX_WAIT_S:
        problems.append(f"{name} must be between 1 second and 30 days.")
    return n


def _strip_forbidden(obj: Any, problems: list[str], path: str = "") -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in FORBIDDEN_KEYS:
                problems.append(
                    f"'{k}' is not allowed in a workflow{(' at ' + path) if path else ''}: a workflow can't approve "
                    "its own actions, skip business rules or change permissions."
                )
                continue
            out[k] = _strip_forbidden(v, problems, f"{path}.{k}" if path else k)
        return out
    if isinstance(obj, list):
        return [_strip_forbidden(x, problems, path) for x in obj]
    return obj


def _conditions(raw: Any, where: str, problems: list[str]) -> list[dict]:
    out = []
    for c in raw or []:
        if not isinstance(c, dict) or not c.get("field"):
            problems.append(f"{where}: each condition needs a field.")
            continue
        op = c.get("op", "eq")
        if op not in OPS:
            problems.append(f"{where}: unknown comparison {op}.")
            continue
        out.append({"field": str(c["field"])[:80], "op": op, "value": c.get("value")})
    return out


def validate(conn: psycopg.Connection | None, customer_id: Any, definition: Any, *, strict: bool = False) -> dict:
    """Check a definition against the schema and the business's set-up.
    Returns {"definition", "problems", "warnings"}. With strict (publishing),
    set-up gaps (a missing team, an app not switched on) are problems."""
    problems: list[str] = []
    warnings: list[str] = []
    if not isinstance(definition, dict):
        return {
            "definition": {},
            "problems": ["A workflow must be an object with a trigger and steps."],
            "warnings": [],
        }
    d = _strip_forbidden(definition, problems)
    name = str(d.get("name") or "").strip()[:120]
    if not name:
        problems.append("Give the workflow a name.")
    trig = d.get("trigger") or {}
    kind = str(trig.get("type") or ("schedule" if trig.get("schedule") else "event"))
    ev = str(trig.get("event") or "")
    if kind not in TRIGGER_TYPES:
        problems.append(f"Unknown trigger type {kind}.")
        kind = "event"
    if kind == "event":
        if not ev:
            problems.append("Choose the event that starts the workflow.")
        elif ev not in events.TYPES:
            problems.append(f"There is no event called {ev}.")
        elif ev.startswith(TRIGGER_BLOCKLIST_PREFIXES):
            problems.append(f"A workflow can't start from {ev} (it could start itself in a loop).")
        clean_trigger = {"event": ev, "conditions": _conditions(trig.get("conditions"), "Trigger", problems)}
    elif kind == "schedule":
        ev = ""
        clean_trigger = {"type": "schedule", "event": "", "conditions": [], "schedule": _schedule(trig, problems)}
    else:
        ev = ""
        clean_trigger = {"type": "manual", "event": "", "conditions": []}
    raw_steps = d.get("steps") or []
    if not isinstance(raw_steps, list) or not raw_steps:
        problems.append("Add at least one step.")
        raw_steps = []
    if len(raw_steps) > MAX_STEPS:
        problems.append(f"A workflow can have up to {MAX_STEPS} steps.")
        raw_steps = raw_steps[:MAX_STEPS]
    steps: list[dict] = []
    ids: list[str] = []
    for i, s in enumerate(raw_steps):
        if not isinstance(s, dict):
            problems.append(f"Step {i + 1} is not a step.")
            continue
        sid = re.sub(r"[^a-z0-9_]", "", str(s.get("id") or f"s{i + 1}").lower())[:30] or f"s{i + 1}"
        if sid in ids:
            sid = f"s{i + 1}"
        ids.append(sid)
        t = s.get("type")
        where = f"Step {i + 1}"
        if t not in STEP_TYPES:
            problems.append(f"{where}: unknown step type {t!r}.")
            continue
        st: dict = {"id": sid, "type": t}
        if s.get("label"):
            st["label"] = str(s["label"])[:120]
        if s.get("only_on_failure"):
            st["only_on_failure"] = True
        if t in FAILURE_STEPS and t != "action" and s.get("on_failure") not in (None, ""):
            st["on_failure"] = re.sub(r"[^a-z0-9_]", "", str(s["on_failure"]).lower())[:30] or "continue"
        if t == "condition":
            st["if"] = _conditions(s.get("if"), where, problems)
            if not st["if"]:
                problems.append(f"{where}: a condition step needs at least one condition.")
            st["else"] = str(s.get("else") or "end")
        elif t == "action":
            app, action = str(s.get("app") or ""), str(s.get("action") or "")
            st.update(
                app=app,
                action=action,
                inputs={k: str(v)[:2000] for k, v in (s.get("inputs") or {}).items()},
                on_failure=re.sub(r"[^a-z0-9_]", "", str(s.get("on_failure") or "stop").lower())[:30] or "stop",
            )
            try:
                c = connectors.get(app)
                spec = c.actions.get(action)
                if spec is None:
                    problems.append(f"{where}: {c.label} has no action called {action}.")
                else:
                    missing = [f.label for f in spec.fields if f.required and f.name not in st["inputs"]]
                    if missing:
                        problems.append(f"{where}: {spec.label} needs {', '.join(missing)}.")
                    if spec.sensitive or spec.kind in ("refund", "delete"):
                        warnings.append(
                            f"{where}: {spec.label} is sensitive, so each run waits for a person to approve it."
                        )
                    if conn is not None:
                        _setup_checks(conn, customer_id, app, action, c, spec, where, problems if strict else warnings)
            except KeyError:
                problems.append(f"{where}: there is no {app or 'such'} integration.")
        elif t == "send_message":
            st["body"] = str(s.get("body") or "").strip()[:2000]
            if not st["body"]:
                problems.append(f"{where}: write the message.")
            aud = s.get("audience") or "conversation"
            st["audience"] = aud if aud in ("conversation", "matching") else "conversation"
            if st["audience"] == "matching":
                f = s.get("filter") or {}
                st["filter"] = {k: str(f[k]) for k in ("channel", "state", "tag") if f.get(k)}
                if not any(x["type"] == "approval" for x in steps):
                    problems.append(f"{where}: sending to many customers needs an approval step before it.")
        elif t == "assign":
            st["team"] = str(s.get("team") or "").strip()[:80]
            st["user"] = str(s.get("user") or "").strip()[:200]
            if not st["team"] and not st["user"]:
                problems.append(f"{where}: say which team or person to assign to.")
            if st["team"] and conn is not None:
                ok = conn.execute(
                    "SELECT 1 FROM commai_teams WHERE customer_id = %s AND lower(name) = lower(%s)",
                    (customer_id, st["team"]),
                ).fetchone()
                if not ok:
                    (problems if strict else warnings).append(f"{where}: there is no team called {st['team']} yet.")
        elif t == "add_note":
            st["body"] = str(s.get("body") or "").strip()[:2000]
            if not st["body"]:
                problems.append(f"{where}: write the note.")
        elif t == "wait":
            if s.get("until_event"):
                ue = str(s["until_event"])
                if ue not in events.TYPES:
                    problems.append(f"{where}: there is no event called {ue}.")
                st["until_event"] = ue
                st["same_conversation"] = s.get("same_conversation", True) is not False
                st["timeout_s"] = _dur(s.get("timeout_s"), f"{where} time-out", problems, 86400)
                st["on_timeout"] = "end" if s.get("on_timeout") == "end" else "continue"
            else:
                st["duration_s"] = _dur(s.get("duration_s"), f"{where} duration", problems)
                if st["duration_s"] is None:
                    problems.append(f"{where}: say how long to wait, or which event to wait for.")
        elif t == "approval":
            st["prompt"] = str(s.get("prompt") or "Approve this step?").strip()[:500]
            st["timeout_s"] = _dur(s.get("timeout_s"), f"{where} time-out", problems, 7 * 86400)
        elif t in ("remind", "escalate"):
            st["after_s"] = _dur(s.get("after_s"), f"{where} time", problems, 3600)
            st["body"] = str(s.get("body") or "").strip()[:500]
            if t == "remind":
                st["to"] = str(s.get("to") or "assignee")[:200]
            else:
                st["team"] = str(s.get("team") or "").strip()[:80]
                pr = s.get("priority") or "high"
                st["priority"] = pr if pr in ("low", "normal", "high", "urgent") else "high"
        elif t == "collect":
            st["question"] = str(s.get("question") or "").strip()[:1000]
            if not st["question"]:
                problems.append(f"{where}: write the question to ask.")
            st["save_as"] = re.sub(r"[^a-z0-9_]", "_", str(s.get("save_as") or "answer").lower())[:40]
            st["timeout_s"] = _dur(s.get("timeout_s"), f"{where} time-out", problems, 86400)
            st["on_timeout"] = "end" if s.get("on_timeout") == "end" else "continue"
        steps.append(st)
    # Jumps go forward only, so a workflow can never loop.
    for i, st in enumerate(steps):
        later = [x["id"] for x in steps[i + 1 :]]
        if st["type"] == "condition" and st["else"] not in ("end", "continue"):
            if st["else"] not in later:
                problems.append(f"Step {i + 1}: 'else' must name a later step, 'continue' or 'end'.")
        of = st.get("on_failure")
        if of and of not in ("stop", "continue") and of not in later:
            problems.append(f"Step {i + 1}: 'on failure' must be stop, continue or a later step.")
    if steps and steps[0].get("only_on_failure"):
        problems.append("The first step can't be one that runs only after a failure.")
    conv_types = ("send_message", "collect", "assign", "add_note", "remind", "escalate")
    conv_steps = any(x["type"] in conv_types for x in steps)
    if conv_steps and clean_trigger.get("type") in ("schedule", "manual"):
        warnings.append(
            "Some steps work on a conversation. A scheduled run has none, and a manual run has one only when it is "
            "started for a conversation; otherwise they are skipped."
        )
    elif conv_steps and ev and not _has_conversation(ev):
        warnings.append("Some steps work on a conversation, but this trigger may not have one; they will be skipped.")
    notify = []
    for who in (d.get("notify_on_failure") or [])[:10]:
        w = str(who).strip().lower()[:200]
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", w):
            problems.append(f"{who} is not an email address to notify.")
        elif (
            conn is not None
            and not conn.execute(
                "SELECT 1 FROM users WHERE lower(email) = %s AND (customer_id = %s OR role = 'admin')", (w, customer_id)
            ).fetchone()
        ):
            (problems if strict else warnings).append(f"There is nobody called {w} to notify.")
        else:
            notify.append(w)
    clean = {
        "name": name,
        "description": str(d.get("description") or "")[:500],
        "trigger": clean_trigger,
        "steps": steps,
    }
    if notify:
        clean["notify_on_failure"] = notify
    return {"definition": clean, "problems": problems, "warnings": warnings}


def _has_conversation(ev: str) -> bool:
    return ev.startswith(("conversation.", "message.", "note.", "booking.", "action.", "ai."))


def trigger_type(definition: dict) -> str:
    return (definition.get("trigger") or {}).get("type") or "event"


def start_event_type(definition: dict) -> str:
    """The event a run of this workflow starts from."""
    t = trigger_type(definition)
    if t == "schedule":
        return "workflow.scheduled"
    if t == "manual":
        return "workflow.triggered"
    return definition["trigger"]["event"]


def _schedule(trig: dict, problems: list[str]) -> dict:
    sc = trig.get("schedule") or {}
    if not isinstance(sc, dict):
        problems.append("The schedule must say how often the workflow runs.")
        return {}
    if sc.get("every_minutes") not in (None, ""):
        try:
            n = int(sc["every_minutes"])
        except (TypeError, ValueError):
            problems.append("Run every: a number of minutes.")
            return {}
        if not MIN_EVERY_MINUTES <= n <= 30 * 1440:
            problems.append(f"Run every {MIN_EVERY_MINUTES} minutes to 30 days.")
        return {"every_minutes": n}
    at = str(sc.get("at") or "")
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", at)
    if not m:
        problems.append("Say when the workflow runs: every so many minutes, or at a time like 09:00.")
        return {}
    days = [str(x).lower()[:3] for x in (sc.get("days") or DAYS)]
    bad = [x for x in days if x not in DAYS]
    if bad or not days:
        problems.append("Days must be mon, tue, wed, thu, fri, sat or sun.")
        days = [x for x in days if x in DAYS] or list(DAYS)
    return {"at": f"{int(m.group(1)):02d}:{m.group(2)}", "days": [x for x in DAYS if x in days]}


def next_fire(schedule: dict, tz: str, after: dt.datetime) -> dt.datetime:
    """The next time a schedule fires, strictly after `after` (UTC)."""
    from zoneinfo import ZoneInfo

    if schedule.get("every_minutes"):
        return after + dt.timedelta(minutes=int(schedule["every_minutes"]))
    zone = ZoneInfo(tz or "UTC")
    hh, mm = (int(x) for x in schedule["at"].split(":"))
    local = after.astimezone(zone)
    for add in range(0, 8):
        day = local.date() + dt.timedelta(days=add)
        cand = dt.datetime.combine(day, dt.time(hh, mm), tzinfo=zone)
        if cand > local and DAYS[day.weekday()] in schedule["days"]:
            return cand.astimezone(dt.UTC)
    raise WorkflowError("The schedule never fires.", 422)


def _setup_checks(conn, customer_id, app, action, c, spec, where, sink: list[str]) -> None:
    row = connectors.connection(conn, customer_id, app)
    if row is None:
        sink.append(f"{where}: {c.label} is not connected yet.")
    elif row["status"] != "live":
        sink.append(f"{where}: {c.label} is not switched on (it is {row['status']}).")
    elif action not in row["allowed_actions"]:
        sink.append(f"{where}: {spec.label} is not an allowed action for {c.label}.")
    if not actions.role_may(conn, customer_id, "workflow", app, action):
        sink.append(f"{where}: workflows are not allowed to {spec.label.lower()} ({app}.{action}).")


def tools_needed(definition: dict) -> list[str]:
    return sorted({f"{s['app']}.{s['action']}" for s in definition.get("steps", []) if s["type"] == "action"})


# ---- preview ---------------------------------------------------------------------


def _fmt_s(n: int | None) -> str:
    if not n:
        return "a while"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if n >= size and n % size == 0:
            k = n // size
            return f"{k} {unit}{'s' if k != 1 else ''}"
    return f"{n} seconds"


def _fmt_cond(c: dict) -> str:
    v = c.get("value")
    v = " or ".join(f'"{x}"' for x in v) if isinstance(v, list) else f'"{v}"'
    return {
        "eq": f"{c['field']} is {v}",
        "ne": f"{c['field']} is not {v}",
        "contains": f"{c['field']} contains {v}",
        "not_contains": f"{c['field']} does not contain {v}",
        "in": f"{c['field']} is one of {v}",
        "exists": f"{c['field']} is set",
        "gt": f"{c['field']} is more than {v}",
        "lt": f"{c['field']} is less than {v}",
    }[c["op"]]


def preview(definition: dict) -> list[str]:
    """The workflow in plain words, one line per step."""
    t = definition.get("trigger") or {}
    kind = t.get("type") or "event"
    if kind == "schedule":
        sc = t.get("schedule") or {}
        if sc.get("every_minutes"):
            first = f"Every {_fmt_s(int(sc['every_minutes']) * 60)}"
        else:
            days = sc.get("days") or list(DAYS)
            which = (
                "every day"
                if len(days) == 7
                else "every weekday"
                if days == list(DAYS[:5])
                else "on " + ", ".join(d.capitalize() for d in days)
            )
            first = f"At {sc.get('at', '?')} {which} (business time zone)"
        lines = [first]
    elif kind == "manual":
        lines = ["When started by a person or through the API"]
    else:
        lines = [
            f"When {t.get('event', '?')}"
            + (" and " + " and ".join(_fmt_cond(c) for c in t.get("conditions") or []) if t.get("conditions") else "")
        ]
    for i, s in enumerate(definition.get("steps") or [], 1):
        k = s["type"]
        if k == "condition":
            line = (
                "Continue only if "
                + " and ".join(_fmt_cond(c) for c in s["if"])
                + ("" if s["else"] == "end" else f", otherwise go to {s['else']}")
            )
        elif k == "action":
            try:
                c = connectors.get(s["app"])
                label = f"{c.actions[s['action']].label} in {c.label}"
            except KeyError:
                label = f"{s['app']}.{s['action']}"
            line = f"{label} (checked by the action service; sensitive actions wait for a person)"
        elif k == "send_message":
            line = f'Send "{s["body"][:80]}"' + (
                " to every matching conversation, after approval"
                if s.get("audience") == "matching"
                else " on the conversation"
            )
        elif k == "assign":
            line = f"Assign to {s.get('team') or s.get('user')}"
        elif k == "add_note":
            line = f'Add a private note: "{s["body"][:80]}"'
        elif k == "wait":
            line = (
                f"Wait for {s['until_event']} (up to {_fmt_s(s.get('timeout_s'))})"
                if s.get("until_event")
                else f"Wait {_fmt_s(s.get('duration_s'))}"
            )
        elif k == "approval":
            line = f'Wait for a person to approve: "{s["prompt"][:80]}"'
        elif k == "remind":
            line = f"If nobody has replied after {_fmt_s(s['after_s'])}, remind the {s.get('to', 'assignee')}"
        elif k == "escalate":
            line = f"If nobody has replied after {_fmt_s(s['after_s'])}, raise priority to {s['priority']}" + (
                f" and move to {s['team']}" if s.get("team") else ""
            )
        elif k == "collect":
            line = f'Ask "{s["question"][:80]}" and wait up to {_fmt_s(s["timeout_s"])} for the answer'
        else:
            line = k
        if s.get("only_on_failure"):
            line = "Only after a failure: " + line
        of = s.get("on_failure")
        if of and of not in ("stop", "continue"):
            line += f"; if it fails, go to {of}"
        elif of == "continue" and k == "action":
            line += "; if it fails, carry on"
        lines.append(f"{i}. {line}")
    if definition.get("notify_on_failure"):
        lines.append("If a run fails, tell " + ", ".join(definition["notify_on_failure"]))
    return lines


# ---- storing workflows ------------------------------------------------------------


def create(conn, customer_id: Any, definition: dict, *, actor: str, source: str = "person", pack: str = "") -> dict:
    v = validate(conn, customer_id, definition)
    if not v["definition"].get("name"):
        raise WorkflowError("Give the workflow a name.", 422, v["problems"])
    wf = conn.execute(
        """INSERT INTO commai_workflows (customer_id, name, description, pack, created_by)
           VALUES (%s, %s, %s, %s, %s) RETURNING *""",
        (customer_id, v["definition"]["name"], v["definition"]["description"], pack, actor),
    ).fetchone()
    ver = _add_version(conn, wf, v["definition"], source, actor)
    return {"workflow": wf, "version": ver, **{k: v[k] for k in ("problems", "warnings")}}


def _add_version(conn, wf: dict, definition: dict, source: str, actor: str, note: str = "") -> dict:
    n = conn.execute(
        "SELECT coalesce(max(version), 0) + 1 AS n FROM commai_workflow_versions WHERE workflow_id = %s", (wf["id"],)
    ).fetchone()["n"]
    return conn.execute(
        """INSERT INTO commai_workflow_versions
             (workflow_id, customer_id, version, definition, source, note, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (wf["id"], wf["customer_id"], n, Jsonb(definition), source, note, actor),
    ).fetchone()


def get(conn, customer_id: Any, workflow_id: Any, lock: bool = False) -> dict:
    wf = conn.execute(
        "SELECT * FROM commai_workflows WHERE id = %s AND customer_id = %s" + (" FOR UPDATE" if lock else ""),
        (workflow_id, customer_id),
    ).fetchone()
    if wf is None:
        raise WorkflowError("Workflow not found.", 404)
    return wf


def version(conn, wf: dict, n: int | None = None) -> dict:
    if n is None:
        row = conn.execute(
            "SELECT * FROM commai_workflow_versions WHERE workflow_id = %s ORDER BY version DESC LIMIT 1", (wf["id"],)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM commai_workflow_versions WHERE workflow_id = %s AND version = %s", (wf["id"], n)
        ).fetchone()
    if row is None:
        raise WorkflowError("Version not found.", 404)
    return row


def update(conn, customer_id: Any, workflow_id: Any, definition: dict, *, actor: str, source: str = "person") -> dict:
    """Editing saves a new version; the live version keeps running until the new one is published."""
    wf = get(conn, customer_id, workflow_id, lock=True)
    v = validate(conn, customer_id, definition)
    if not v["definition"].get("name"):
        raise WorkflowError("Give the workflow a name.", 422, v["problems"])
    ver = _add_version(conn, wf, v["definition"], source, actor)
    wf = conn.execute(
        "UPDATE commai_workflows SET name = %s, description = %s, updated_at = now() WHERE id = %s RETURNING *",
        (v["definition"]["name"], v["definition"]["description"], wf["id"]),
    ).fetchone()
    return {"workflow": wf, "version": ver, "problems": v["problems"], "warnings": v["warnings"]}


def publish(
    conn, customer_id: Any, workflow_id: Any, *, actor: str, version_n: int | None = None, grant_tools: bool = False
) -> dict:
    """A person makes a version live. Every check must pass. The workflow role
    may only use actions a person allows: with grant_tools, the publishing
    person allows exactly the actions this version uses."""
    if not actor.startswith("user:"):
        raise WorkflowError("Only a person can publish a workflow.", 403)
    wf = get(conn, customer_id, workflow_id, lock=True)
    ver = version(conn, wf, version_n)
    if grant_tools:
        for tool in tools_needed(ver["definition"]):
            conn.execute(
                "INSERT INTO commai_role_tools (customer_id, role, tool) VALUES (%s, 'workflow', %s)"
                " ON CONFLICT DO NOTHING",
                (customer_id, tool),
            )
    v = validate(conn, customer_id, ver["definition"], strict=True)
    if v["problems"]:
        raise WorkflowError("This version can't go live yet: " + " ".join(v["problems"]), 409, v["problems"])
    seq = conn.execute("SELECT coalesce(max(seq), 0) AS s FROM commai_events").fetchone()["s"]
    wf = conn.execute(
        """UPDATE commai_workflows SET status = 'live', live_version = %s, live_from_seq = %s, updated_at = now()
           WHERE id = %s RETURNING *""",
        (ver["version"], seq, wf["id"]),
    ).fetchone()
    events.emit(
        conn,
        customer_id,
        "workflow.published",
        {"workflow_id": str(wf["id"]), "version": ver["version"], "by": actor},
        wf["id"],
    )
    ensure_dispatcher(conn)
    arm_schedule(conn, wf, ver["definition"])
    return {"workflow": wf, "version": ver, "warnings": v["warnings"]}


def pause(conn, customer_id: Any, workflow_id: Any, *, actor: str) -> dict:
    wf = get(conn, customer_id, workflow_id, lock=True)
    if wf["status"] != "live":
        raise WorkflowError("Only a live workflow can be paused.", 409)
    wf = conn.execute(
        "UPDATE commai_workflows SET status = 'paused', updated_at = now() WHERE id = %s RETURNING *", (wf["id"],)
    ).fetchone()
    events.emit(conn, customer_id, "workflow.paused", {"workflow_id": str(wf["id"]), "by": actor}, wf["id"])
    conn.execute("DELETE FROM commai_workflow_schedules WHERE workflow_id = %s", (wf["id"],))
    return wf


def resume(conn, customer_id: Any, workflow_id: Any, *, actor: str) -> dict:
    wf = get(conn, customer_id, workflow_id, lock=True)
    if wf["status"] != "paused":
        raise WorkflowError("This workflow is not paused.", 409)
    seq = conn.execute("SELECT coalesce(max(seq), 0) AS s FROM commai_events").fetchone()["s"]
    wf = conn.execute(
        "UPDATE commai_workflows SET status = 'live', live_from_seq = %s, updated_at = now() WHERE id = %s RETURNING *",
        (seq, wf["id"]),
    ).fetchone()
    # Runs held while paused carry on from where they stopped.
    for r in conn.execute(
        "UPDATE commai_workflow_runs SET status = 'running', updated_at = now() WHERE workflow_id = %s"
        " AND status = 'held' RETURNING id, step_index",
        (wf["id"],),
    ).fetchall():
        jobs.enqueue(
            conn,
            "workflow.step",
            {"run_id": str(r["id"])},
            customer_id=customer_id,
            dedupe_key=f"wf:{r['id']}:resume:{secrets.token_hex(6)}",
        )
    events.emit(conn, customer_id, "workflow.resumed", {"workflow_id": str(wf["id"]), "by": actor}, wf["id"])
    ensure_dispatcher(conn)
    arm_schedule(conn, wf, version(conn, wf, wf["live_version"])["definition"])
    return wf


# ---- context, conditions and templates --------------------------------------------


def build_context(conn, customer_id: Any, event: dict) -> dict:
    data = event.get("data") or {}
    ctx: dict = {
        "event": {"id": str(event.get("id") or ""), "type": event.get("type", ""), "data": data},
        "vars": {},
        "steps": {},
    }
    conv_id = data.get("conversation_id") or None
    if conv_id:
        conv = conn.execute(
            """SELECT c.id, c.channel, c.subject, c.state, c.priority, c.tags, c.language, c.contact_id,
                      t.name AS team, u.email AS assignee
               FROM conversations c LEFT JOIN commai_teams t ON t.id = c.team_id
               LEFT JOIN users u ON u.id = c.assignee_id
               WHERE c.id::text = %s AND c.customer_id = %s""",
            (str(conv_id), customer_id),
        ).fetchone()
        if conv:
            ctx["conversation"] = {k: (str(v) if k in ("id", "contact_id") and v else v) for k, v in conv.items()}
            if conv["contact_id"]:
                ct = conn.execute(
                    "SELECT id, name, email, phone, language FROM contacts WHERE id = %s", (conv["contact_id"],)
                ).fetchone()
                if ct:
                    ctx["contact"] = {**ct, "id": str(ct["id"])}
    mid = data.get("message_id")
    if mid:
        m = conn.execute(
            "SELECT id, conversation_id, direction, body, created_at FROM messages WHERE id::text = %s"
            " AND customer_id = %s",
            (str(mid), customer_id),
        ).fetchone()
        if m:
            first = not conn.execute(
                "SELECT 1 FROM messages WHERE conversation_id = %s AND direction = 'in' AND created_at < %s LIMIT 1",
                (m["conversation_id"], m["created_at"]),
            ).fetchone()
            ctx["message"] = {"id": str(m["id"]), "text": m["body"], "direction": m["direction"], "first": first}
    return ctx


def resolve(ctx: dict, path: str) -> Any:
    aliases = {
        "channel": ("conversation.channel", "event.data.channel"),
        "text": ("message.text", "conversation.subject"),
        "first_message": ("message.first",),
    }
    for p in aliases.get(path, (path,)):
        cur: Any = ctx
        for part in p.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur not in (None, ""):
            return cur
    return None


def check(ctx: dict, cond: dict) -> bool:
    v = resolve(ctx, cond["field"])
    want = cond.get("value")
    op = cond["op"]
    if op == "exists":
        return v not in (None, "", [], False)
    if op in ("contains", "not_contains"):
        hay = (" ".join(map(str, v)) if isinstance(v, list) else str(v or "")).lower()
        needles = want if isinstance(want, list) else [want]
        hit = any(str(n).lower() in hay for n in needles if n not in (None, ""))
        return hit if op == "contains" else not hit
    if op == "in":
        return str(v) in [str(x) for x in (want if isinstance(want, list) else [want])]
    if op in ("gt", "lt"):
        try:
            a, b = float(v), float(want)
        except (TypeError, ValueError):
            return False
        return a > b if op == "gt" else a < b
    same = str(v).lower() == str(want).lower() if not isinstance(want, bool) else bool(v) == want
    return same if op == "eq" else not same


_TPL = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def render(text: str, ctx: dict) -> str:
    def sub(m: re.Match) -> str:
        v = resolve(ctx, m.group(1))
        if v is None:
            return ""
        return json.dumps(v, default=str) if isinstance(v, dict | list) else str(v)

    return _TPL.sub(sub, text or "")


# ---- runs ------------------------------------------------------------------------


def _log_step(conn, run: dict, i: int, step: dict, status: str, summary: str, detail: dict | None = None) -> None:
    conn.execute(
        """INSERT INTO commai_workflow_run_steps
             (run_id, customer_id, step_index, step_id, type, status, summary, detail)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            run["id"],
            run["customer_id"],
            i,
            step.get("id", "trigger"),
            step.get("type", "trigger"),
            status,
            summary[:500],
            Jsonb(detail or {}),
        ),
    )


def start_run(conn, wf: dict, ver: dict, event: dict) -> dict | None:
    """Start a live run for this event, at most once per (version, event)."""
    ctx = build_context(conn, wf["customer_id"], event)
    conv_id = (ctx.get("conversation") or {}).get("id")
    if conv_id:
        recent = conn.execute(
            """SELECT count(*) AS n FROM commai_workflow_runs WHERE workflow_id = %s AND conversation_id = %s
               AND NOT test AND started_at > now() - interval '1 hour'""",
            (wf["id"], conv_id),
        ).fetchone()["n"]
        if recent >= MAX_RUNS_PER_CONVERSATION_HOUR:
            log.warning("workflow %s: run limit reached for conversation %s", wf["id"], conv_id)
            return None
    from .. import entitlements

    if not entitlements.enabled(conn, wf["customer_id"], "automation"):
        return None
    if not usage.allowed(conn, wf["customer_id"], "workflow_run", workflow_id=wf["id"]):
        return None
    run = conn.execute(
        """INSERT INTO commai_workflow_runs (customer_id, workflow_id, version_id, version, event_id, conversation_id,
                                             context)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (version_id, event_id) WHERE event_id IS NOT NULL AND NOT test DO NOTHING RETURNING *""",
        (wf["customer_id"], wf["id"], ver["id"], ver["version"], event["id"], conv_id, Jsonb(ctx)),
    ).fetchone()
    if run is None:
        return None
    usage.record(
        conn,
        wf["customer_id"],
        "workflow_run",
        1,
        ref=str(run["id"]),
        detail={"workflow_id": str(wf["id"]), "version": ver["version"]},
    )
    _log_step(
        conn,
        run,
        -1,
        {"id": "trigger", "type": "trigger"},
        "done",
        f"Started by {event['type']}",
        {"event_id": str(event["id"])},
    )
    events.emit(
        conn,
        wf["customer_id"],
        "workflow.run_started",
        {
            "workflow_id": str(wf["id"]),
            "run_id": str(run["id"]),
            "version": ver["version"],
            "event_id": str(event["id"]),
            "conversation_id": str(conv_id or ""),
        },
        run["id"],
    )
    jobs.enqueue(
        conn,
        "workflow.step",
        {"run_id": str(run["id"])},
        customer_id=wf["customer_id"],
        dedupe_key=f"wf:{run['id']}:start",
    )
    return run


def _finish(conn, run: dict, status: str, error: str = "") -> None:
    conn.execute(
        """UPDATE commai_workflow_runs SET status = %s, error = %s, wait = '{}', finished_at = now(), updated_at = now()
           WHERE id = %s""",
        (status, error[:500], run["id"]),
    )
    if run["test"]:
        return
    kind = "workflow.run_failed" if status == "failed" else "workflow.run_finished"
    events.emit(
        conn,
        run["customer_id"],
        kind,
        {
            "workflow_id": str(run["workflow_id"]),
            "run_id": str(run["id"]),
            "status": status,
            "conversation_id": str(run["conversation_id"] or ""),
        },
        run["id"],
    )
    if status != "failed":
        return
    name = conn.execute("SELECT name FROM commai_workflows WHERE id = %s", (run["workflow_id"],)).fetchone()
    ver = conn.execute("SELECT definition FROM commai_workflow_versions WHERE id = %s", (run["version_id"],)).fetchone()
    notify = list((ver["definition"] if ver else {}).get("notify_on_failure") or [])
    if notify:
        events.emit(
            conn,
            run["customer_id"],
            "workflow.exception",
            {
                "workflow_id": str(run["workflow_id"]),
                "run_id": str(run["id"]),
                "error": error[:500],
                "notify": notify,
                "conversation_id": str(run["conversation_id"] or ""),
            },
            run["id"],
        )
    if run["conversation_id"]:
        try:
            inbox.add_note(
                conn,
                run["customer_id"],
                run["conversation_id"],
                author="Jibsy workflows",
                body="".join(f"@{w} " for w in notify) + f'Workflow "{name["name"] if name else ""}" stopped: {error}',
                mentions=notify,
            )
        except inbox.InboxError:
            pass


def _wait(
    conn, run: dict, i: int, ctx: dict, wait: dict, status: str = "waiting", timeout_s: float | None = None
) -> None:
    token = secrets.token_hex(8)
    wait = {**wait, "token": token, "step_index": i, "since": dt.datetime.now(dt.UTC).isoformat()}
    if timeout_s:
        wait["until"] = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=timeout_s)).isoformat()
    conn.execute(
        """UPDATE commai_workflow_runs SET status = %s, step_index = %s, context = %s, wait = %s, updated_at = now()
           WHERE id = %s""",
        (status, i, Jsonb(ctx), Jsonb(wait), run["id"]),
    )
    if timeout_s:
        jobs.enqueue(
            conn,
            "workflow.step",
            {"run_id": str(run["id"]), "resume": {"kind": "timeout", "token": token}},
            customer_id=run["customer_id"],
            dedupe_key=f"wf:{run['id']}:{token}:timeout",
            delay_s=timeout_s,
        )


def _conversation(ctx: dict) -> str | None:
    return (ctx.get("conversation") or {}).get("id")


def _replied_since(conn, conv_id: str, since: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM messages WHERE conversation_id = %s AND direction = 'out' AND author_kind IN ('user', 'ai')"
            " AND created_at > %s LIMIT 1",
            (conv_id, since),
        ).fetchone()
    )


class _Stop(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status


class _Jump(Exception):
    def __init__(self, target: str):
        super().__init__(target)
        self.target = target


def _on_fail(step: dict, ctx: dict, reason: str, default: str = "stop") -> str:
    """A step failed: carry on, stop the run, or take the exception path."""
    how = step.get("on_failure") or default
    if how == "continue":
        return "next"
    ctx["failure"] = {"step": step["id"], "error": reason[:500]}
    if how == "stop":
        raise _Stop("failed", reason)
    return f"goto:{how}"


def _resume(conn, run: dict, wf: dict, step: dict, i: int, ctx: dict, resume: dict) -> bool:
    """Handle a signal for the waiting step. True to move on to the next step."""
    kind = resume.get("kind")
    t = step["type"]
    wait = run["wait"] or {}
    if t == "action":
        ar = conn.execute(
            "SELECT status, result, error FROM action_runs WHERE id = %s", (wait.get("action_run_id"),)
        ).fetchone()
        if ar and ar["status"] == "succeeded":
            ctx["steps"][step["id"]] = {"result": ar["result"]}
            _log_step(
                conn,
                run,
                i,
                step,
                "done",
                f"{step['app']}.{step['action']} succeeded",
                {"action_run_id": wait.get("action_run_id")},
            )
            return True
        if ar and ar["status"] in ("failed", "rejected") or kind == "timeout":
            why = (ar["error"] if ar else "") or ("timed out waiting for approval" if kind == "timeout" else "failed")
            _log_step(
                conn,
                run,
                i,
                step,
                "failed",
                f"{step['app']}.{step['action']}: {why}",
                {"action_run_id": wait.get("action_run_id")},
            )
            out = _on_fail(step, ctx, f"{step['app']}.{step['action']} {ar['status'] if ar else 'failed'}: {why}")
            if out.startswith("goto:"):
                raise _Jump(out[5:])
            return True
        return False  # not finished yet: keep waiting
    if t == "wait":
        if kind == "event":
            _log_step(conn, run, i, step, "done", f"{step.get('until_event')} happened")
            return True
        if step.get("until_event") and step.get("on_timeout") == "end":
            _log_step(conn, run, i, step, "done", "Timed out waiting; stopping as set")
            raise _Stop("done", "timed out")
        _log_step(conn, run, i, step, "done", "Wait over")
        return True
    if t == "collect":
        if kind == "event":
            m = conn.execute(
                "SELECT body FROM messages WHERE id::text = %s", (resume.get("message_id", ""),)
            ).fetchone()
            ctx["vars"][step["save_as"]] = m["body"] if m else ""
            _log_step(conn, run, i, step, "done", f"Answer saved as {step['save_as']}")
            return True
        _log_step(conn, run, i, step, "done", "No answer in time")
        if step.get("on_timeout") == "end":
            raise _Stop("done", "no answer")
        return True
    if t == "approval":
        if kind == "approved":
            _log_step(conn, run, i, step, "done", f"Approved by {resume.get('by', '')}")
            return True
        why = "rejected by " + resume.get("by", "") if kind == "rejected" else "no decision in time"
        _log_step(conn, run, i, step, "failed", f"Not approved: {why}")
        raise _Stop("cancelled", f"Not approved: {why}")
    if t in ("remind", "escalate"):
        conv_id = _conversation(ctx)
        if conv_id and _replied_since(conn, conv_id, wait.get("since")):
            _log_step(conn, run, i, step, "done", "Someone replied in time; nothing to do")
            return True
        if not conv_id:
            _log_step(conn, run, i, step, "skipped", "No conversation")
            return True
        name = wf["name"]
        if t == "remind":
            owner = (ctx.get("conversation") or {}).get("assignee") or ""
            cur = conn.execute(
                "SELECT u.email FROM conversations c LEFT JOIN users u ON u.id = c.assignee_id WHERE c.id = %s",
                (conv_id,),
            ).fetchone()
            owner = (
                (cur["email"] if cur and cur["email"] else owner)
                if step.get("to", "assignee") == "assignee"
                else step.get("to")
            )
            body = render(step.get("body") or "", ctx) or (
                f'Reminder from "{name}": nobody has replied for {_fmt_s(step["after_s"])}.'
            )
            inbox.add_note(
                conn,
                run["customer_id"],
                conv_id,
                author="Jibsy workflows",
                body=(f"@{owner} " if owner else "") + body,
                mentions=[owner] if owner else [],
            )
            events.emit(
                conn,
                run["customer_id"],
                "workflow.reminder",
                {"workflow_id": str(wf["id"]), "run_id": str(run["id"]), "conversation_id": conv_id, "to": owner},
                conv_id,
            )
            _log_step(conn, run, i, step, "done", f"Reminded {owner or 'the team'}")
        else:
            team = None
            if step.get("team"):
                team = conn.execute(
                    "SELECT id FROM commai_teams WHERE customer_id = %s AND lower(name) = lower(%s)",
                    (run["customer_id"], step["team"]),
                ).fetchone()
            if team:
                inbox.assign(
                    conn,
                    run["customer_id"],
                    conv_id,
                    actor=f"workflow:{wf['id']}",
                    team_id=team["id"],
                    reason=f"escalated by workflow {name}",
                )
            inbox.set_fields(conn, run["customer_id"], conv_id, actor=f"workflow:{wf['id']}", priority=step["priority"])
            inbox.add_note(
                conn,
                run["customer_id"],
                conv_id,
                author="Jibsy workflows",
                body=render(step.get("body") or "", ctx)
                or f'Escalated by "{name}": nobody replied for {_fmt_s(step["after_s"])}.',
            )
            events.emit(
                conn,
                run["customer_id"],
                "workflow.escalated",
                {"workflow_id": str(wf["id"]), "run_id": str(run["id"]), "conversation_id": conv_id},
                conv_id,
            )
            _log_step(conn, run, i, step, "done", f"Escalated (priority {step['priority']})")
        return True
    return True


def _execute(conn, run: dict, wf: dict, step: dict, i: int, ctx: dict) -> str:
    """Run one step. Returns 'next', 'goto:<id>', or 'wait' (the run now waits)."""
    t = step["type"]
    conv_id = _conversation(ctx)
    actor = f"workflow:{wf['id']}"
    needs_conv = t in ("send_message", "assign", "add_note", "remind", "escalate", "collect") and not (
        t == "send_message" and step.get("audience") == "matching"
    )
    if needs_conv and not conv_id:
        _log_step(conn, run, i, step, "skipped", "No conversation for this step")
        return "next"
    if t == "condition":
        ok = all(check(ctx, c) for c in step["if"])
        _log_step(
            conn,
            run,
            i,
            step,
            "done",
            "Conditions met" if ok else "Conditions not met",
            {"values": {c["field"]: resolve(ctx, c["field"]) for c in step["if"]}},
        )
        if ok or step["else"] == "continue":
            return "next"
        if step["else"] == "end":
            raise _Stop("skipped", "Conditions not met")
        return f"goto:{step['else']}"
    if t == "action":
        inputs = {k: render(v, ctx) for k, v in step["inputs"].items()}
        inputs = {k: v for k, v in inputs.items() if v != ""}
        try:
            with conn.transaction():
                ar = actions.propose(
                    conn,
                    run["customer_id"],
                    role="workflow",
                    app=step["app"],
                    action=step["action"],
                    inputs=inputs,
                    actor=actor,
                    conversation_id=conv_id,
                    idempotency_key=f"wf:{run['id']}:{step['id']}",
                )
        except actions.ActionRefused as e:
            _log_step(conn, run, i, step, "failed", f"Refused: {e}", {"inputs": inputs})
            return _on_fail(step, ctx, f"{step['app']}.{step['action']} refused: {e}")
        if ar["status"] == "rejected":
            _log_step(conn, run, i, step, "failed", f"Refused: {ar['error']}")
            return _on_fail(step, ctx, f"{step['app']}.{step['action']} refused: {ar['error']}")
        if ar["status"] == "succeeded":
            ctx["steps"][step["id"]] = {"result": ar["result"]}
            _log_step(conn, run, i, step, "done", f"{step['app']}.{step['action']} succeeded")
            return "next"
        _log_step(
            conn,
            run,
            i,
            step,
            "waiting",
            "Waiting for a person to approve"
            if ar["status"] == "awaiting_approval"
            else f"{step['app']}.{step['action']} queued",
            {"action_run_id": str(ar["id"]), "inputs": inputs},
        )
        _wait(
            conn,
            run,
            i,
            ctx,
            {
                "kind": "action",
                "action_run_id": str(ar["id"]),
                "match": {"types": ["action.succeeded", "action.failed"], "key": "run_id", "value": str(ar["id"])},
            },
            timeout_s=7 * 86400,
        )
        return "wait"
    if t == "send_message":
        body = render(step["body"], ctx)
        if step.get("audience") == "matching":
            f = step.get("filter") or {}
            rows = conn.execute(
                """SELECT id FROM conversations WHERE customer_id = %s AND state <> 'resolved'
                   AND (%s::text IS NULL OR channel = %s) AND (%s::text IS NULL OR state = %s)
                   AND (%s::text IS NULL OR %s = ANY(tags)) ORDER BY last_message_at DESC NULLS LAST LIMIT 500""",
                (
                    run["customer_id"],
                    f.get("channel"),
                    f.get("channel"),
                    f.get("state"),
                    f.get("state"),
                    f.get("tag"),
                    f.get("tag"),
                ),
            ).fetchall()
            sent = blocked = 0
            for r in rows:
                try:
                    with conn.transaction():
                        msg = _send(conn, run, wf, r["id"], body)
                    sent += 1
                except inbox.InboxError:
                    blocked += 1
            _log_step(conn, run, i, step, "done", f"Sent to {sent} conversation(s); {blocked} blocked by channel rules")
            return "next"
        try:
            with conn.transaction():
                msg = _send(conn, run, wf, conv_id, body)
        except inbox.InboxError as e:
            _log_step(conn, run, i, step, "failed", f"Not sent: {e}")
            return _on_fail(step, ctx, f"Message not sent: {e}", "continue")
        _log_step(conn, run, i, step, "done", "Message sent", {"message_id": str(msg["id"])})
        return "next"
    if t == "assign":
        try:
            if step.get("team"):
                team = conn.execute(
                    "SELECT id FROM commai_teams WHERE customer_id = %s AND lower(name) = lower(%s)",
                    (run["customer_id"], step["team"]),
                ).fetchone()
                if team is None:
                    raise inbox.InboxError(f"There is no team called {step['team']}.")
                with conn.transaction():
                    inbox.assign(
                        conn,
                        run["customer_id"],
                        conv_id,
                        actor=actor,
                        team_id=team["id"],
                        reason=f"workflow {wf['name']}",
                    )
            else:
                u = conn.execute("SELECT id FROM users WHERE lower(email) = lower(%s)", (step["user"],)).fetchone()
                if u is None:
                    raise inbox.InboxError(f"There is no one called {step['user']}.")
                with conn.transaction():
                    inbox.assign(
                        conn,
                        run["customer_id"],
                        conv_id,
                        actor=actor,
                        assignee_id=u["id"],
                        reason=f"workflow {wf['name']}",
                    )
        except inbox.InboxError as e:
            _log_step(conn, run, i, step, "failed", f"Not assigned: {e}")
            return _on_fail(step, ctx, f"Not assigned: {e}", "continue")
        cur = conn.execute(
            "SELECT t.name AS team, u.email AS assignee FROM conversations c"
            " LEFT JOIN commai_teams t ON t.id = c.team_id"
            " LEFT JOIN users u ON u.id = c.assignee_id WHERE c.id = %s",
            (conv_id,),
        ).fetchone()
        ctx.setdefault("conversation", {}).update(team=cur["team"], assignee=cur["assignee"])
        _log_step(
            conn,
            run,
            i,
            step,
            "done",
            f"Assigned to {step.get('team') or step.get('user')}"
            + (f" ({cur['assignee']})" if cur["assignee"] else ""),
        )
        return "next"
    if t == "add_note":
        inbox.add_note(conn, run["customer_id"], conv_id, author="Jibsy workflows", body=render(step["body"], ctx))
        _log_step(conn, run, i, step, "done", "Note added")
        return "next"
    if t == "wait":
        if step.get("until_event"):
            match = {"types": [step["until_event"]]}
            if step.get("same_conversation") and conv_id:
                match.update(key="conversation_id", value=conv_id)
            _log_step(conn, run, i, step, "waiting", f"Waiting for {step['until_event']}")
            _wait(conn, run, i, ctx, {"kind": "event", "match": match}, timeout_s=step["timeout_s"])
        else:
            _log_step(conn, run, i, step, "waiting", f"Waiting {_fmt_s(step['duration_s'])}")
            _wait(conn, run, i, ctx, {"kind": "timer"}, timeout_s=step["duration_s"])
        return "wait"
    if t == "approval":
        prompt = render(step["prompt"], ctx)
        _log_step(conn, run, i, step, "waiting", f"Waiting for a person: {prompt}")
        _wait(
            conn,
            run,
            i,
            ctx,
            {"kind": "approval", "prompt": prompt},
            status="awaiting_approval",
            timeout_s=step["timeout_s"],
        )
        events.emit(
            conn,
            run["customer_id"],
            "workflow.approval_requested",
            {"workflow_id": str(wf["id"]), "run_id": str(run["id"]), "conversation_id": str(conv_id or "")},
            run["id"],
        )
        return "wait"
    if t in ("remind", "escalate"):
        _log_step(conn, run, i, step, "waiting", f"Checking for a reply in {_fmt_s(step['after_s'])}")
        _wait(conn, run, i, ctx, {"kind": t}, timeout_s=step["after_s"])
        return "wait"
    if t == "collect":
        q = render(step["question"], ctx)
        try:
            with conn.transaction():
                _send(conn, run, wf, conv_id, q)
        except inbox.InboxError as e:
            _log_step(conn, run, i, step, "failed", f"Question not sent: {e}")
            return _on_fail(step, ctx, f"Question not sent: {e}", "continue")
        _log_step(conn, run, i, step, "waiting", "Asked; waiting for the answer")
        _wait(
            conn,
            run,
            i,
            ctx,
            {"kind": "event", "match": {"types": ["message.received"], "key": "conversation_id", "value": conv_id}},
            timeout_s=step["timeout_s"],
        )
        return "wait"
    raise _Stop("failed", f"Unknown step type {t}.")


def _send(conn, run: dict, wf: dict, conv_id: Any, body: str) -> dict:
    """A workflow's message, within the workflow's own budget (ADR 0039)."""
    ch = conn.execute("SELECT channel FROM conversations WHERE id = %s", (conv_id,)).fetchone()
    if ch and not usage.allowed(conn, run["customer_id"], f"message_out:{ch['channel']}", workflow_id=wf["id"]):
        raise inbox.InboxError("This workflow has reached its monthly budget.", 422)
    msg = inbox.send(conn, run["customer_id"], conv_id, body, author_kind="workflow", author=f"Workflow: {wf['name']}")
    conn.execute(
        "INSERT INTO commai_message_sources (message_id, customer_id, workflow_id, run_id) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT DO NOTHING",
        (msg["id"], run["customer_id"], wf["id"], run["id"]),
    )
    return msg


def _index(steps: list[dict], target: str) -> int:
    return next(k for k, s in enumerate(steps) if s["id"] == target)


def advance(conn, run_id: Any, resume: dict | None = None) -> None:
    run = conn.execute("SELECT * FROM commai_workflow_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
    if run is None or run["status"] in ("done", "skipped", "failed", "cancelled"):
        return
    wf = conn.execute("SELECT * FROM commai_workflows WHERE id = %s", (run["workflow_id"],)).fetchone()
    if resume and (run["wait"] or {}).get("token") != resume.get("token"):
        return  # a stale signal: the run has already moved on
    if wf["status"] == "paused" and not run["test"]:
        conn.execute(
            "UPDATE commai_workflow_runs SET status = 'held', updated_at = now() WHERE id = %s AND status = 'running'",
            (run["id"],),
        )
        if not resume:
            return
    ver = conn.execute("SELECT definition FROM commai_workflow_versions WHERE id = %s", (run["version_id"],)).fetchone()
    steps = ver["definition"]["steps"]
    ctx = run["context"]
    i = run["step_index"]
    try:
        if resume:
            try:
                if not _resume(conn, run, wf, steps[i], i, ctx, resume):
                    return
                i += 1
            except _Jump as j:
                i = _index(steps, j.target)
            conn.execute(
                "UPDATE commai_workflow_runs SET status = 'running', wait = '{}', step_index = %s,"
                " context = %s WHERE id = %s",
                (i, Jsonb(ctx), run["id"]),
            )
            if wf["status"] == "paused":
                conn.execute("UPDATE commai_workflow_runs SET status = 'held' WHERE id = %s", (run["id"],))
                return
        for _ in range(MAX_STEPS + 1):
            if i >= len(steps):
                conn.execute("UPDATE commai_workflow_runs SET context = %s WHERE id = %s", (Jsonb(ctx), run["id"]))
                _finish(conn, run, "done")
                return
            if steps[i].get("only_on_failure") and not ctx.get("failure"):
                _log_step(conn, run, i, steps[i], "skipped", "Runs only after a failure")
                out = "next"
            else:
                out = _execute(conn, run, wf, steps[i], i, ctx)
            if out == "wait":
                return
            if out.startswith("goto:"):
                i = _index(steps, out[5:])
            else:
                i += 1
            conn.execute(
                "UPDATE commai_workflow_runs SET step_index = %s, context = %s, updated_at = now() WHERE id = %s",
                (i, Jsonb(ctx), run["id"]),
            )
    except _Stop as s:
        conn.execute("UPDATE commai_workflow_runs SET context = %s WHERE id = %s", (Jsonb(ctx), run["id"]))
        _finish(conn, run, s.status, str(s) if s.status in ("failed", "cancelled") else "")


@jobs.handler("workflow.step")
def _step_job(conn: psycopg.Connection, job: dict):
    advance(conn, job["payload"]["run_id"], job["payload"].get("resume"))


def decide(conn, customer_id: Any, run_id: Any, *, approve: bool, actor: str, note: str = "") -> dict:
    """A person approves or rejects a workflow's approval step."""
    if not actor.startswith("user:"):
        raise WorkflowError("Only a person can approve a workflow step.", 403)
    run = conn.execute(
        "SELECT * FROM commai_workflow_runs WHERE id = %s AND customer_id = %s FOR UPDATE", (run_id, customer_id)
    ).fetchone()
    if run is None:
        raise WorkflowError("Run not found.", 404)
    if run["status"] != "awaiting_approval":
        raise WorkflowError("This run is not waiting for approval.", 409)
    advance(
        conn,
        run["id"],
        {
            "kind": "approved" if approve else "rejected",
            "token": run["wait"]["token"],
            "by": actor.removeprefix("user:"),
            "note": note,
        },
    )
    return conn.execute("SELECT * FROM commai_workflow_runs WHERE id = %s", (run["id"],)).fetchone()


def cancel_run(conn, customer_id: Any, run_id: Any, *, actor: str) -> dict:
    run = conn.execute(
        "SELECT * FROM commai_workflow_runs WHERE id = %s AND customer_id = %s FOR UPDATE", (run_id, customer_id)
    ).fetchone()
    if run is None:
        raise WorkflowError("Run not found.", 404)
    if run["status"] in ("done", "skipped", "failed", "cancelled"):
        return run
    _finish(conn, run, "cancelled", f"Cancelled by {actor}")
    return conn.execute("SELECT * FROM commai_workflow_runs WHERE id = %s", (run["id"],)).fetchone()


# ---- test mode (dry run) ----------------------------------------------------------

SAMPLE_EVENT = {
    "id": None,
    "type": "message.received",
    "data": {"conversation_id": "", "message_id": "", "channel": "web"},
}


def dry_run(
    conn,
    customer_id: Any,
    workflow_id: Any,
    *,
    actor: str,
    version_n: int | None = None,
    event_id: Any = None,
    sample: dict | None = None,
) -> dict:
    """Walk the workflow against a recent event or a sample, with no external
    effects: actions are checked, not proposed; messages and notes are shown,
    not sent; waits are assumed to pass. The result is stored as a test run."""
    wf = get(conn, customer_id, workflow_id)
    ver = version(conn, wf, version_n)
    d = ver["definition"]
    if event_id:
        ev = conn.execute(
            "SELECT * FROM commai_events WHERE id::text = %s AND customer_id = %s", (str(event_id), customer_id)
        ).fetchone()
        if ev is None:
            raise WorkflowError("Event not found.", 404)
    elif sample:
        ev = {"id": None, "type": start_event_type(d), "data": {}}
    else:
        ev = conn.execute(
            "SELECT * FROM commai_events WHERE customer_id = %s AND type = %s ORDER BY seq DESC LIMIT 1",
            (customer_id, start_event_type(d)),
        ).fetchone() or {"id": None, "type": start_event_type(d), "data": {}}
    ctx = build_context(conn, customer_id, ev)
    for k, v in (sample or {}).items():  # a sample fills or overrides what the event lacks
        if isinstance(v, dict):
            ctx.setdefault(k, {}).update(v)
    lines: list[dict] = []
    trig_ok = all(check(ctx, c) for c in d["trigger"]["conditions"])
    lines.append(
        {
            "step": "trigger",
            "status": "match" if trig_ok else "no_match",
            "summary": (
                "The trigger matches this event."
                if trig_ok
                else "The trigger conditions do not match this event, so a live run would not start."
            ),
            "values": {c["field"]: resolve(ctx, c["field"]) for c in d["trigger"]["conditions"]},
        }
    )
    run = conn.execute(
        """INSERT INTO commai_workflow_runs (customer_id, workflow_id, version_id, version, event_id, conversation_id,
                                             test, status, context)
           VALUES (%s, %s, %s, %s, %s, %s, true, 'done', %s) RETURNING *""",
        (customer_id, wf["id"], ver["id"], ver["version"], ev.get("id"), _conversation(ctx), Jsonb(ctx)),
    ).fetchone()
    steps = d["steps"]
    i = 0
    guard = 0
    while i < len(steps) and guard <= MAX_STEPS:
        guard += 1
        s = steps[i]
        t = s["type"]
        status, summary, detail = "would_run", "", {}
        if t == "condition":
            ok = all(check(ctx, c) for c in s["if"])
            summary = "Conditions met" if ok else "Conditions not met"
            detail = {"values": {c["field"]: resolve(ctx, c["field"]) for c in s["if"]}}
            if not ok and s["else"] == "end":
                lines.append(
                    {"step": s["id"], "type": t, "status": "stop", "summary": summary + "; the run stops.", **detail}
                )
                break
            if not ok and s["else"] not in ("continue",):
                lines.append(
                    {"step": s["id"], "type": t, "status": "jump", "summary": f"{summary}; go to {s['else']}", **detail}
                )
                i = next(k for k, x in enumerate(steps) if x["id"] == s["else"])
                continue
        elif t == "action":
            inputs = {k: render(v, ctx) for k, v in s["inputs"].items()}
            inputs = {k: v for k, v in inputs.items() if v != ""}
            issues: list[str] = []
            try:
                c = connectors.get(s["app"])
                spec = c.actions[s["action"]]
                try:
                    c.validate(s["action"], inputs)
                except ValueError as e:
                    issues.append(str(e))
                _setup_checks(conn, customer_id, s["app"], s["action"], c, spec, "", issues)
                sens = spec.sensitive or spec.kind in ("refund", "delete")
                summary = f"Would ask the action service to {spec.label.lower()} in {c.label}" + (
                    " (waits for a person's approval)" if sens else ""
                )
            except KeyError:
                issues.append("Unknown app or action.")
                summary = f"{s['app']}.{s['action']}"
            status = "would_fail" if issues else "would_run"
            detail = {"inputs": inputs, "issues": [x.lstrip(": ") for x in issues]}
        elif t == "send_message":
            summary = f'Would send: "{render(s["body"], ctx)}"' + (
                " to every matching conversation" if s.get("audience") == "matching" else ""
            )
        elif t == "assign":
            summary = f"Would assign to {s.get('team') or s.get('user')}"
        elif t == "add_note":
            summary = f'Would add a private note: "{render(s["body"], ctx)}"'
        elif t == "wait":
            summary = (
                f"Would wait for {s['until_event']}"
                if s.get("until_event")
                else f"Would wait {_fmt_s(s['duration_s'])}"
            )
        elif t == "approval":
            summary = f'Would wait for a person to approve: "{render(s["prompt"], ctx)}"'
        elif t == "remind":
            summary = f"If nobody replies within {_fmt_s(s['after_s'])}, would remind the {s.get('to', 'assignee')}"
        elif t == "escalate":
            summary = f"If nobody replies within {_fmt_s(s['after_s'])}, would escalate"
        elif t == "collect":
            summary = f'Would ask: "{render(s["question"], ctx)}" and save the answer as {s["save_as"]}'
            ctx["vars"][s["save_as"]] = f"<answer to: {s['question'][:40]}>"
        if (
            t in ("send_message", "assign", "add_note", "remind", "escalate", "collect")
            and not _conversation(ctx)
            and not (t == "send_message" and s.get("audience") == "matching")
        ):
            status, summary = "skipped", "Skipped: this event has no conversation"
        if s.get("only_on_failure"):
            status, summary = "skipped", "Runs only after a failure: " + summary
        lines.append({"step": s["id"], "type": t, "status": status, "summary": summary, **detail})
        i += 1
    for n, line in enumerate(lines):
        _log_step(
            conn,
            run,
            n - 1,
            {"id": line["step"], "type": line.get("type", "trigger")},
            line["status"],
            line["summary"],
            {k: v for k, v in line.items() if k not in ("step", "type", "status", "summary")},
        )
    conn.execute("UPDATE commai_workflow_runs SET finished_at = now() WHERE id = %s", (run["id"],))
    return {
        "run_id": str(run["id"]),
        "event": {"id": str(ev.get("id") or ""), "type": ev.get("type")},
        "trigger_matches": trig_ok,
        "steps": lines,
        "external_effects": False,
    }


# ---- trigger dispatch ------------------------------------------------------------


def ensure_dispatcher(conn) -> None:
    """Make sure the dispatcher chain is running (idempotent)."""
    cur = conn.execute("SELECT tick FROM commai_workflow_cursor WHERE id = 1").fetchone()
    if cur is None:
        conn.execute("INSERT INTO commai_workflow_cursor (id) VALUES (1) ON CONFLICT DO NOTHING")
        cur = {"tick": 0}
    jobs.enqueue(
        conn, "workflow.dispatch", {"tick": cur["tick"]}, dedupe_key=f"wf.dispatch:{cur['tick']}", max_attempts=20
    )


def _match_waits(conn, ev: dict) -> None:
    runs = conn.execute(
        """SELECT id, wait FROM commai_workflow_runs WHERE customer_id = %s AND status = 'waiting'
           AND wait->'match'->'types' ? %s""",
        (ev["customer_id"], ev["type"]),
    ).fetchall()
    data = ev["data"] or {}
    for r in runs:
        m = r["wait"]["match"]
        if m.get("key") and str(data.get(m["key"], "")) != str(m.get("value")):
            continue
        if ev["type"] == "message.received" and r["wait"].get("since") and ev["at"].isoformat() < r["wait"]["since"]:
            continue
        engine.signal(
            conn,
            r["id"],
            r["wait"]["token"],
            "event",
            {"event_id": str(ev["id"]), "message_id": str(data.get("message_id", ""))},
        )


def dispatch_pass(conn, limit: int = 500) -> int:
    """Read new events after the cursor; start runs and wake waiting runs.
    Returns how many events were read."""
    cur = conn.execute("SELECT * FROM commai_workflow_cursor WHERE id = 1 FOR UPDATE").fetchone()
    evs = conn.execute(
        "SELECT * FROM commai_events WHERE seq > %s ORDER BY seq LIMIT %s", (cur["last_seq"], limit)
    ).fetchall()
    last = cur["last_seq"]
    gap_seq, gap_since = cur["gap_seq"], cur["gap_since"]
    now = dt.datetime.now(dt.UTC)
    live: dict[str, list[tuple[dict, dict]]] = {}
    for ev in evs:
        if ev["seq"] != last + 1:
            # A missing number is usually a rolled-back event, but may be one
            # still being committed: wait a little before passing it.
            if gap_seq != last + 1:
                gap_seq, gap_since = last + 1, now
            if gap_since and (now - gap_since).total_seconds() < GAP_WAIT_S:
                break
        gap_seq, gap_since = None, None
        cid = str(ev["customer_id"])
        if cid not in live:
            live[cid] = [
                (w, {"id": w["version_id"], "version": w["live_version"], "definition": w["definition"]})
                for w in conn.execute(
                    """SELECT w.*, v.id AS version_id, v.definition FROM commai_workflows w
                       JOIN commai_workflow_versions v ON v.workflow_id = w.id AND v.version = w.live_version
                       WHERE w.customer_id = %s AND w.status = 'live'""",
                    (ev["customer_id"],),
                ).fetchall()
            ]
        try:
            with conn.transaction():
                for wf, ver in live[cid]:
                    trig = ver["definition"]["trigger"]
                    if trig["event"] != ev["type"] or ev["seq"] <= wf["live_from_seq"]:
                        continue
                    if (ev["data"] or {}).get("author_kind") == "workflow":
                        continue  # never trigger on a workflow's own messages
                    ctx = build_context(conn, ev["customer_id"], ev)
                    if all(check(ctx, c) for c in trig["conditions"]):
                        engine.start(conn, wf, ver, ev)
                _match_waits(conn, ev)
        except Exception:  # noqa: BLE001 - one bad event must not stop every workflow
            log.exception("workflow dispatch failed for event %s", ev["id"])
        last = ev["seq"]
    conn.execute(
        "UPDATE commai_workflow_cursor SET last_seq = %s, gap_seq = %s, gap_since = %s WHERE id = 1",
        (last, gap_seq, gap_since),
    )
    return len(evs)


@jobs.handler("workflow.dispatch")
def _dispatch_job(conn: psycopg.Connection, job: dict):
    cur = conn.execute("SELECT tick FROM commai_workflow_cursor WHERE id = 1 FOR UPDATE").fetchone()
    if cur is None or job["payload"].get("tick") != cur["tick"]:
        return None  # another dispatcher already moved on
    dispatch_pass(conn)
    tick = cur["tick"] + 1
    conn.execute("UPDATE commai_workflow_cursor SET tick = %s WHERE id = 1", (tick,))
    conn.execute("DELETE FROM jobs WHERE kind = 'workflow.dispatch' AND status = 'done'")
    busy = conn.execute(
        """SELECT EXISTS (SELECT 1 FROM commai_workflows WHERE status = 'live')
               OR EXISTS (SELECT 1 FROM commai_workflow_runs WHERE status = 'waiting' AND wait->>'kind' IN
                          ('event', 'action')) AS busy"""
    ).fetchone()["busy"]
    if busy:
        jobs.enqueue(
            conn,
            "workflow.dispatch",
            {"tick": tick},
            dedupe_key=f"wf.dispatch:{tick}",
            delay_s=DISPATCH_S,
            max_attempts=20,
        )
    return None


@jobs.on_dead("workflow.dispatch")
def _dispatch_dead(job: dict, error: str) -> None:
    from ... import db

    with db.tx() as conn:
        conn.execute("DELETE FROM jobs WHERE id = %s", (job["id"],))
        ensure_dispatcher(conn)


# ---- schedules and manual starts (ADR 0039) ------------------------------------------


def _tz(conn, customer_id: Any) -> str:
    row = conn.execute("SELECT timezone FROM commai_settings WHERE customer_id = %s", (customer_id,)).fetchone()
    return (row["timezone"] if row else "") or "UTC"


def arm_schedule(conn, wf: dict, definition: dict) -> None:
    """(Re)start the timer for a live scheduled workflow; remove it otherwise.
    A new token makes any timer queued for the old schedule do nothing."""
    if trigger_type(definition) != "schedule" or wf["status"] != "live":
        conn.execute("DELETE FROM commai_workflow_schedules WHERE workflow_id = %s", (wf["id"],))
        return
    at = next_fire(definition["trigger"]["schedule"], _tz(conn, wf["customer_id"]), dt.datetime.now(dt.UTC))
    token = secrets.token_hex(8)
    conn.execute(
        """INSERT INTO commai_workflow_schedules (workflow_id, customer_id, token, next_at) VALUES (%s, %s, %s, %s)
           ON CONFLICT (workflow_id) DO UPDATE SET token = EXCLUDED.token, next_at = EXCLUDED.next_at,
             updated_at = now()""",
        (wf["id"], wf["customer_id"], token, at),
    )
    _queue_tick(conn, wf, token, at)


def _queue_tick(conn, wf: dict, token: str, at: dt.datetime) -> None:
    jobs.enqueue(
        conn,
        "workflow.schedule",
        {"workflow_id": str(wf["id"]), "token": token, "at": at.isoformat()},
        customer_id=wf["customer_id"],
        dedupe_key=f"wf.sched:{wf['id']}:{token}:{at.isoformat()}",
        delay_s=max(0.0, (at - dt.datetime.now(dt.UTC)).total_seconds()),
    )


def _live(conn, customer_id: Any, workflow_id: Any) -> tuple[dict, dict]:
    wf = get(conn, customer_id, workflow_id, lock=True)
    if wf["status"] != "live":
        raise WorkflowError("Only a live workflow can be started.", 409)
    v = version(conn, wf, wf["live_version"])
    return wf, {"id": v["id"], "version": v["version"], "definition": v["definition"]}


@jobs.handler("workflow.schedule")
def _schedule_job(conn: psycopg.Connection, job: dict):
    p = job["payload"]
    row = conn.execute(
        "SELECT * FROM commai_workflow_schedules WHERE workflow_id = %s FOR UPDATE", (p["workflow_id"],)
    ).fetchone()
    if row is None or row["token"] != p["token"] or row["next_at"].isoformat() != p["at"]:
        return None  # paused, republished or already fired
    wf = conn.execute("SELECT * FROM commai_workflows WHERE id = %s", (p["workflow_id"],)).fetchone()
    if wf is None or wf["status"] != "live":
        return None
    v = version(conn, wf, wf["live_version"])
    if trigger_type(v["definition"]) != "schedule":
        conn.execute("DELETE FROM commai_workflow_schedules WHERE workflow_id = %s", (wf["id"],))
        return None
    ev_id = events.emit(
        conn,
        wf["customer_id"],
        "workflow.scheduled",
        {"workflow_id": str(wf["id"]), "scheduled_for": p["at"]},
        wf["id"],
    )
    ev = conn.execute("SELECT * FROM commai_events WHERE id = %s", (ev_id,)).fetchone()
    engine.start(conn, wf, {"id": v["id"], "version": v["version"], "definition": v["definition"]}, ev)
    nxt = next_fire(v["definition"]["trigger"]["schedule"], _tz(conn, wf["customer_id"]), row["next_at"])
    now = dt.datetime.now(dt.UTC)
    while nxt <= now:  # the controller was down: fire once now, not once per missed slot
        nxt = next_fire(v["definition"]["trigger"]["schedule"], _tz(conn, wf["customer_id"]), nxt)
    conn.execute(
        "UPDATE commai_workflow_schedules SET last_at = %s, next_at = %s, updated_at = now() WHERE workflow_id = %s",
        (row["next_at"], nxt, wf["id"]),
    )
    _queue_tick(conn, wf, row["token"], nxt)
    return None


def trigger(
    conn,
    customer_id: Any,
    workflow_id: Any,
    *,
    actor: str,
    conversation_id: Any = None,
    data: dict | None = None,
) -> dict:
    """Start a live workflow now, by a person or through the API. The start is
    recorded as a workflow.triggered event; trigger conditions (which describe
    events) are not checked, the steps' own conditions are."""
    wf, ver = _live(conn, customer_id, workflow_id)
    if conversation_id:
        ok = conn.execute(
            "SELECT 1 FROM conversations WHERE id::text = %s AND customer_id = %s", (str(conversation_id), customer_id)
        ).fetchone()
        if not ok:
            raise WorkflowError("Conversation not found.", 404)
    clean = {str(k)[:40]: (v if isinstance(v, int | float | bool) else str(v)[:500]) for k, v in (data or {}).items()}
    if len(clean) > 20:
        raise WorkflowError("Pass up to 20 values.", 422)
    ev_id = events.emit(
        conn,
        customer_id,
        "workflow.triggered",
        {
            "workflow_id": str(wf["id"]),
            "by": actor,
            "conversation_id": str(conversation_id or ""),
            "input": clean,
        },
        wf["id"],
    )
    ev = conn.execute("SELECT * FROM commai_events WHERE id = %s", (ev_id,)).fetchone()
    run = engine.start(conn, wf, ver, ev)
    if run is None:
        raise WorkflowError(
            "The workflow did not start: a usage limit, its budget or the per-conversation run limit stopped it.", 409
        )
    return run


def schedule_of(conn, workflow_id: Any) -> dict | None:
    return conn.execute(
        "SELECT next_at, last_at FROM commai_workflow_schedules WHERE workflow_id = %s", (workflow_id,)
    ).fetchone()


# ---- reading for the API -----------------------------------------------------------


def summary(conn, wf: dict) -> dict:
    latest = version(conn, wf)
    stats = conn.execute(
        """SELECT count(*) FILTER (WHERE NOT test) AS runs,
                  count(*) FILTER (WHERE NOT test AND status = 'failed') AS failed,
                  count(*) FILTER (WHERE NOT test AND status IN ('waiting', 'awaiting_approval', 'running', 'held'))
                    AS active,
                  max(started_at) FILTER (WHERE NOT test) AS last_run_at
           FROM commai_workflow_runs WHERE workflow_id = %s""",
        (wf["id"],),
    ).fetchone()
    return {
        **wf,
        "latest_version": latest["version"],
        "draft_pending": latest["version"] != wf["live_version"],
        "trigger": latest["definition"].get("trigger"),
        **stats,
    }
