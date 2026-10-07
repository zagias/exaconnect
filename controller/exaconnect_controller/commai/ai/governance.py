"""AI governance (ADR 0032): evaluation suites, candidates that must pass them
before going live, and daily action limits per role.

- Evaluation cases: a question, the behaviour expected and the behaviour
  forbidden. A business keeps its own suite; ExaCarib keeps a global one that
  runs for every change.
- A change to the model or to an agent's instructions is a candidate. It is
  evaluated automatically (the durable job "ai.eval") and can be promoted only
  after its latest run passed every case. Results are kept.
  - Model: EXA_LLM_MODEL naming a model other than the live one creates a
    candidate; the live model stays in use until an ExaCarib admin promotes it.
  - Instructions: changing the customer agent's tone or instructions on the
    AI profile creates a candidate; the business's admin promotes it.
- Action limits: how many actions each role may propose per day (UTC);
  actions.propose refuses beyond the limit.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ... import db
from .. import events, inbox, jobs
from . import judging, knowledge, runtime
from .model import Model, ModelError, ModelInput, OpenAICompatibleModel

events.register("ai.candidate_created", "ai.candidate_evaluated", "ai.candidate_promoted")

INSTRUCTION_FIELDS = ("tone", "instructions")
LIMIT_ROLES = ("customer_agent", "copilot", "platform_assistant", "workflow", "person")
MAX_CASES = 200


class GovernanceError(Exception):
    def __init__(self, message: str, code: int = 422):
        super().__init__(message)
        self.code = code


# ---- evaluation cases ---------------------------------------------------------------------


def cases(conn: psycopg.Connection, customer_id: Any | None) -> list[dict]:
    """A business's own cases, or the global suite when customer_id is None."""
    return conn.execute(
        """SELECT id, customer_id, question, expected, forbidden, enabled, created_by, created_at FROM ai_eval_cases
           WHERE customer_id IS NOT DISTINCT FROM %s ORDER BY created_at, id""",
        (customer_id,),
    ).fetchall()


def add_case(conn, customer_id: Any | None, question: str, expected: str, forbidden: str, actor: str) -> dict:
    question, expected, forbidden = (" ".join((x or "").split()) for x in (question, expected, forbidden))
    if not 3 <= len(question) <= 1000:
        raise GovernanceError("Write the question in 3 to 1,000 characters.")
    if not expected and not forbidden:
        raise GovernanceError("Say what the AI should do, what it must not do, or both.")
    if len(expected) > 300 or len(forbidden) > 300:
        raise GovernanceError("Keep each behaviour under 300 characters.")
    if len(cases(conn, customer_id)) >= MAX_CASES:
        raise GovernanceError(f"A suite can hold up to {MAX_CASES} cases.")
    return conn.execute(
        """INSERT INTO ai_eval_cases (customer_id, question, expected, forbidden, created_by)
           VALUES (%s, %s, %s, %s, %s) RETURNING *""",
        (customer_id, question, expected, forbidden, actor),
    ).fetchone()


def delete_case(conn, customer_id: Any | None, case_id: Any) -> None:
    cur = conn.execute(
        "DELETE FROM ai_eval_cases WHERE id = %s AND customer_id IS NOT DISTINCT FROM %s", (case_id, customer_id)
    )
    if cur.rowcount == 0:
        raise GovernanceError("Case not found.", 404)


# ---- candidates ---------------------------------------------------------------------------


def create_candidate(
    conn: psycopg.Connection, customer_id: Any | None, kind: str, change: dict, previous: dict, actor: str
) -> dict:
    # A newer candidate of the same kind replaces one still waiting.
    conn.execute(
        """UPDATE ai_candidates SET status = 'withdrawn', decided_by = %s, decided_at = now()
           WHERE customer_id IS NOT DISTINCT FROM %s AND kind = %s AND status IN ('testing', 'passed', 'failed')""",
        (actor, customer_id, kind),
    )
    cand = conn.execute(
        """INSERT INTO ai_candidates (customer_id, kind, change, previous, created_by) VALUES (%s, %s, %s, %s, %s)
           RETURNING *""",
        (customer_id, kind, Jsonb(change), Jsonb(previous), actor),
    ).fetchone()
    jobs.enqueue(
        conn,
        "ai.eval",
        {"candidate_id": str(cand["id"])},
        customer_id=customer_id,
        dedupe_key=f"ai.eval:{cand['id']}",
        max_attempts=3,
    )
    if customer_id:
        events.emit(conn, customer_id, "ai.candidate_created", {"candidate_id": str(cand["id"]), "kind": kind}, "")
    return cand


def candidates(conn: psycopg.Connection, customer_id: Any | None) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM ai_candidates WHERE customer_id IS NOT DISTINCT FROM %s ORDER BY created_at DESC LIMIT 50""",
        (customer_id,),
    ).fetchall()
    for r in rows:
        r["latest_run"] = latest_run(conn, r["id"])
    return rows


def latest_run(conn: psycopg.Connection, candidate_id: Any) -> dict | None:
    return conn.execute(
        "SELECT * FROM ai_eval_runs WHERE candidate_id = %s ORDER BY started_at DESC LIMIT 1", (candidate_id,)
    ).fetchone()


def get_candidate(conn: psycopg.Connection, candidate_id: Any, customer_id: Any | None) -> dict:
    row = conn.execute(
        "SELECT * FROM ai_candidates WHERE id = %s AND customer_id IS NOT DISTINCT FROM %s FOR UPDATE",
        (candidate_id, customer_id),
    ).fetchone()
    if row is None:
        raise GovernanceError("Candidate not found.", 404)
    return row


def rerun(conn: psycopg.Connection, candidate_id: Any, customer_id: Any | None) -> None:
    cand = get_candidate(conn, candidate_id, customer_id)
    if cand["status"] not in ("testing", "passed", "failed"):
        raise GovernanceError("This candidate is closed.", 409)
    conn.execute("UPDATE ai_candidates SET status = 'testing' WHERE id = %s", (cand["id"],))
    jobs.enqueue(conn, "ai.eval", {"candidate_id": str(cand["id"])}, customer_id=customer_id, max_attempts=3)


def promote(conn: psycopg.Connection, candidate_id: Any, customer_id: Any | None, actor: str) -> dict:
    cand = get_candidate(conn, candidate_id, customer_id)
    run = latest_run(conn, cand["id"])
    if cand["status"] != "passed" or run is None or run["status"] != "done" or run["failed"]:
        raise GovernanceError("Only a candidate whose latest evaluation passed every case can go live.", 409)
    if cand["kind"] == "model":
        conn.execute(
            """INSERT INTO ai_live_model (id, model, promoted_by) VALUES (1, %s, %s)
               ON CONFLICT (id) DO UPDATE SET model = EXCLUDED.model, promoted_by = EXCLUDED.promoted_by,
                 promoted_at = now()""",
            (cand["change"]["model"], actor),
        )
        _cache.clear()
    else:
        current = (inbox.settings(conn, customer_id)["config"] or {}).get("ai") or {}
        conn.execute(
            """UPDATE commai_settings SET config = jsonb_set(config, '{ai}', %s), updated_at = now()
               WHERE customer_id = %s""",
            (Jsonb({**current, **cand["change"]}), customer_id),
        )
    row = conn.execute(
        """UPDATE ai_candidates SET status = 'promoted', decided_by = %s, decided_at = now() WHERE id = %s
           RETURNING *""",
        (actor, cand["id"]),
    ).fetchone()
    if customer_id:
        events.emit(conn, customer_id, "ai.candidate_promoted", {"candidate_id": str(cand["id"])}, "")
    return row


def withdraw(conn: psycopg.Connection, candidate_id: Any, customer_id: Any | None, actor: str) -> dict:
    cand = get_candidate(conn, candidate_id, customer_id)
    if cand["status"] == "promoted":
        raise GovernanceError("This candidate is already live.", 409)
    return conn.execute(
        """UPDATE ai_candidates SET status = 'withdrawn', decided_by = %s, decided_at = now() WHERE id = %s
           RETURNING *""",
        (actor, cand["id"]),
    ).fetchone()


# ---- instruction changes from the AI profile ----------------------------------------------


def split_profile_change(conn: psycopg.Connection, customer_id: Any, fields: dict) -> tuple[dict, dict]:
    """(fields that apply now, instruction changes that need a candidate)."""
    current = runtime.profile(conn, customer_id)
    now, later = {}, {}
    for k, v in fields.items():
        if k in INSTRUCTION_FIELDS and v != current.get(k):
            later[k] = v
        else:
            now[k] = v
    return now, later


def pending_instructions(conn: psycopg.Connection, customer_id: Any) -> dict | None:
    return conn.execute(
        """SELECT * FROM ai_candidates WHERE customer_id = %s AND kind = 'instructions'
           AND status IN ('testing', 'passed', 'failed') ORDER BY created_at DESC LIMIT 1""",
        (customer_id,),
    ).fetchone()


# ---- the live model -----------------------------------------------------------------------

_cache: dict[str, Any] = {}
CACHE_S = 30


def sync_model(conn: psycopg.Connection, env_model: str) -> str:
    """The live model's name. The first model ever seen is the baseline; a
    different EXA_LLM_MODEL afterwards becomes a candidate."""
    live = conn.execute("SELECT model FROM ai_live_model WHERE id = 1").fetchone()
    if live is None:
        conn.execute(
            "INSERT INTO ai_live_model (id, model, promoted_by) VALUES (1, %s, 'system:first-run') "
            "ON CONFLICT (id) DO NOTHING",
            (env_model,),
        )
        return env_model
    if live["model"] != env_model:
        waiting = conn.execute(
            """SELECT 1 FROM ai_candidates WHERE customer_id IS NULL AND kind = 'model' AND change->>'model' = %s
               AND status IN ('testing', 'passed', 'failed', 'promoted')
               AND created_at >= (SELECT promoted_at FROM ai_live_model WHERE id = 1)""",
            (env_model,),
        ).fetchone()
        if not waiting:
            create_candidate(
                conn, None, "model", {"model": env_model}, {"model": live["model"]}, "system:EXA_LLM_MODEL"
            )
    return live["model"]


def live_model(env_model: str) -> str:
    hit = _cache.get(env_model)
    if hit and time.monotonic() - hit[1] < CACHE_S:
        return hit[0]
    try:
        with db.tx() as conn:
            name = sync_model(conn, env_model)
    except Exception:  # noqa: BLE001 - no database (tooling): use what the environment says
        return env_model
    _cache[env_model] = (name, time.monotonic())
    return name


# ---- running the suite --------------------------------------------------------------------

ModelFactory = Callable[[str], Model]
_factory: ModelFactory | None = None


def set_model_factory(fn: ModelFactory | None) -> None:
    """Tests: build candidate models by name."""
    global _factory
    _factory = fn


def model_named(name: str) -> Model:
    if _factory is not None:
        return _factory(name)
    if runtime._override is not None:
        return runtime._override
    from ...settings import get_settings

    s = get_settings()
    if not s.llm_api_key:
        return runtime.get_model()
    return OpenAICompatibleModel(api_key=s.llm_api_key, base_url=s.llm_base_url, model=name)


def _answer(conn, customer_id: Any | None, case: dict, model: Model, prof: dict) -> dict:
    """What the candidate says to one question, without acting or sending."""
    kb = (
        knowledge.lookup(conn, customer_id, case["question"], runtime.PROFILES["customer_agent"].max_knowledge)
        if customer_id
        else {"hits": [], "contradictory": False, "contradiction": ""}
    )
    business = runtime.business_name(conn, customer_id) if customer_id else "an example business"
    ctx = {
        "business": business,
        "business_language": prof["business_language"],
        "customer_language": prof["business_language"],
        "channel": "web",
        "conversation": [{"from": "customer", "text": case["question"], "at": ""}],
        "knowledge": kb,
        "tools": [],
        "tool_details": [],
        "verified": False,
        "contact": {},
        "memory": [],
        "actions_in_this_conversation": [],
        "last_ai_intent": "",
        "evaluation": True,
    }
    out = model.complete(
        ModelInput(
            role="customer_agent",
            task="reply",
            system=runtime.system_prompt("customer_agent", prof, business),
            context=ctx,
            max_tokens=runtime.PROFILES["customer_agent"].max_tokens,
        )
    )
    escalated = out.escalate or (not out.sources and not kb["hits"] and not out.tool_calls)
    return {"answer": out.answer, "escalated": bool(escalated), "reason": out.reason, "model": out.model}


def evaluate_case(conn, customer_id: Any | None, case: dict, model: Model, prof: dict) -> dict:
    said = _answer(conn, customer_id, case, model, prof)
    criteria = []
    if case["expected"]:
        criteria.append({"id": "expected", "text": case["expected"]})
    if case["forbidden"]:
        criteria.append({"id": "forbidden", "text": "Never " + case["forbidden"]})
    conv = [{"from": "customer", "text": case["question"]}]
    if said["answer"]:
        conv.append({"from": "ai", "text": said["answer"]})
    judge_out = runtime.get_model().complete(
        ModelInput(
            role="quality_reviewer",
            task="judge",
            system=judging.JUDGE_SYSTEM,
            context={"conversation": conv, "criteria": criteria, "contact_name": "", "escalated": said["escalated"]},
            max_tokens=600,
            temperature=0,
        )
    )
    raw = judge_out.data.get("results") if isinstance(judge_out.data, dict) else None
    if not isinstance(raw, list):
        raise ModelError("The AI service sent an answer in an unexpected shape.")
    verdicts = judging.verify_quotes(raw, conv)
    ok = all(v["verdict"] == "pass" for v in verdicts) and len(verdicts) == len(criteria)
    return {
        "case_id": str(case["id"]),
        "suite": "business" if case["customer_id"] else "global",
        "question": case["question"],
        "expected": case["expected"],
        "forbidden": case["forbidden"],
        "answer": said["answer"],
        "escalated": said["escalated"],
        "verdicts": verdicts,
        "passed": ok,
    }


@jobs.handler("ai.eval")
def _eval_job(conn: psycopg.Connection, job: dict):
    run_suite(conn, job["payload"]["candidate_id"])
    return None


def _suites(conn, cand: dict) -> list[tuple[Any, dict]]:
    """(business, case) pairs to run: the global suite, then the business's own
    (for a model change: every business's own suite)."""
    pairs: list[tuple[Any, dict]] = [(None, c) for c in cases(conn, None) if c["enabled"]]
    if cand["kind"] == "instructions":
        pairs += [(cand["customer_id"], c) for c in cases(conn, cand["customer_id"]) if c["enabled"]]
    else:
        rows = conn.execute(
            "SELECT * FROM ai_eval_cases WHERE customer_id IS NOT NULL AND enabled ORDER BY customer_id, created_at"
        ).fetchall()
        pairs += [(c["customer_id"], c) for c in rows]
    return pairs[: MAX_CASES * 2]


def run_suite(conn: psycopg.Connection, candidate_id: Any) -> dict:
    cand = conn.execute("SELECT * FROM ai_candidates WHERE id = %s FOR UPDATE", (candidate_id,)).fetchone()
    if cand is None or cand["status"] in ("promoted", "withdrawn"):
        return {"status": "skipped"}
    run = conn.execute("INSERT INTO ai_eval_runs (candidate_id) VALUES (%s) RETURNING *", (candidate_id,)).fetchone()
    model = model_named(cand["change"]["model"]) if cand["kind"] == "model" else None
    results, error = [], ""
    for cid, case in _suites(conn, cand):
        prof = runtime.profile(conn, cid) if cid else dict(runtime.DEFAULT_PROFILE)
        if cand["kind"] == "instructions" and str(cid) == str(cand["customer_id"]):
            prof = {**prof, **cand["change"]}
        elif cand["kind"] == "instructions" and cid is None:
            prof = {**runtime.profile(conn, cand["customer_id"]), **cand["change"]}
        try:
            results.append(evaluate_case(conn, cid or cand["customer_id"], case, model or runtime.get_model(), prof))
        except (ModelError, runtime.UsageLimit) as e:
            error = str(e)
            break
    passed = sum(r["passed"] for r in results)
    failed = len(results) - passed
    status = "failed" if error else "done"
    conn.execute(
        """UPDATE ai_eval_runs SET status = %s, total = %s, passed = %s, failed = %s, results = %s, error = %s,
                  finished_at = now() WHERE id = %s""",
        (status, len(results), passed, failed, Jsonb(results), error[:500], run["id"]),
    )
    verdict = "passed" if status == "done" and failed == 0 else "failed"
    conn.execute("UPDATE ai_candidates SET status = %s WHERE id = %s", (verdict, candidate_id))
    if cand["customer_id"]:
        events.emit(
            conn,
            cand["customer_id"],
            "ai.candidate_evaluated",
            {"candidate_id": str(candidate_id), "passed": passed, "failed": failed, "status": verdict},
            "",
        )
    return {"status": verdict, "total": len(results), "passed": passed, "failed": failed}


def runs(conn: psycopg.Connection, candidate_id: Any) -> list[dict]:
    return conn.execute(
        "SELECT * FROM ai_eval_runs WHERE candidate_id = %s ORDER BY started_at DESC LIMIT 20", (candidate_id,)
    ).fetchall()


# ---- action limits ------------------------------------------------------------------------


def action_limits(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    rows = {
        r["role"]: r["daily_limit"]
        for r in conn.execute(
            "SELECT role, daily_limit FROM ai_action_limits WHERE customer_id = %s", (customer_id,)
        ).fetchall()
    }
    used = {
        r["role"]: r["n"]
        for r in conn.execute(
            """SELECT role, count(*) AS n FROM action_runs WHERE customer_id = %s AND status <> 'rejected'
               AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' GROUP BY role""",
            (customer_id,),
        ).fetchall()
    }
    return [{"role": r, "daily_limit": rows.get(r), "used_today": used.get(r, 0)} for r in LIMIT_ROLES]


def set_action_limit(conn: psycopg.Connection, customer_id: Any, role: str, limit: int | None) -> None:
    if role not in LIMIT_ROLES:
        raise GovernanceError(f"Unknown role {role}.", 404)
    if limit is None:
        conn.execute("DELETE FROM ai_action_limits WHERE customer_id = %s AND role = %s", (customer_id, role))
        return
    if not 0 <= limit <= 100_000:
        raise GovernanceError("A daily limit is between 0 and 100,000.")
    conn.execute(
        """INSERT INTO ai_action_limits (customer_id, role, daily_limit) VALUES (%s, %s, %s)
           ON CONFLICT (customer_id, role) DO UPDATE SET daily_limit = EXCLUDED.daily_limit""",
        (customer_id, role, limit),
    )


def over_daily_limit(conn: psycopg.Connection, customer_id: Any, role: str) -> str:
    """A refusal message when the role has used its actions for today, else ''."""
    lim = conn.execute(
        "SELECT daily_limit FROM ai_action_limits WHERE customer_id = %s AND role = %s", (customer_id, role)
    ).fetchone()
    if lim is None:
        return ""
    n = conn.execute(
        """SELECT count(*) AS n FROM action_runs WHERE customer_id = %s AND role = %s AND status <> 'rejected'
           AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""",
        (customer_id, role),
    ).fetchone()["n"]
    if n >= lim["daily_limit"]:
        return (
            f"The {role.replace('_', ' ')} has used its {lim['daily_limit']} actions for today. "
            "A person can act, or an admin can raise the limit."
        )
    return ""
