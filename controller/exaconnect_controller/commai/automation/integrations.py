"""Integration setup as a product (ADR 0020).

One path for every app: choose it from the catalogue, authorise it, pick the
actions allowed (read separate from create, update, cancel, refund and
delete), map fields, test, approve, switch on, then monitor. A broken
integration names its cause (expired sign-in, missing permission, bad
mapping or a provider error), shows the evidence, and offers a safe repair.

Statuses: draft -> authorised -> testing -> live; paused and broken at any
time. Sign-in tokens are stored encrypted (vault.py) and never returned.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import actions, connectors, diagnostics, events, jobs
from ..connectors import google_calendar, hubspot, installed, kit  # noqa: F401 - registers the real connectors
from . import llm, oauth
from .redact import redact

events.register(
    "integration.connected",
    "integration.signed_in",
    "integration.tested",
    "integration.live",
    "integration.paused",
    "integration.resumed",
    "integration.repaired",
    "integration.disconnected",
)

WRITE_KINDS = ("create", "update", "cancel", "refund", "delete")

CAUSES = {
    "expired_signin": {
        "label": "Sign-in expired",
        "explain": "The sign-in to {app} has expired or was revoked, so CommAI can't reach it.",
        "steps": [("sign_in", "Sign in to {app} again"), ("recheck", "Check the connection again")],
    },
    "permission": {
        "label": "Missing permission",
        "explain": "{app} refused a request because the sign-in does not include a permission it needs.",
        "steps": [
            ("sign_in", "Sign in again and allow the permission"),
            ("remove_action", "Switch off the action that needs it"),
            ("recheck", "Check the connection again"),
        ],
    },
    "mapping": {
        "label": "Bad field mapping",
        "explain": "A field mapping or setting points at something {app} doesn't have (a property or calendar).",
        "steps": [("edit_mapping", "Fix the field mapping or setting"), ("recheck", "Check the connection again")],
    },
    "provider": {
        "label": "Provider error",
        "explain": "{app} had a problem of its own (an outage, a time-out or a rate limit). Nothing is wrong with "
        "your set-up.",
        "steps": [("recheck", "Check the connection again"), ("retry_failed", "Retry the failed actions safely")],
    },
    "input": {
        "label": "Request refused",
        "explain": "{app} refused some requests because of what was asked (a taken time, a missing record). The "
        "connection itself is fine.",
        "steps": [("review", "Review the failed actions")],
    },
}

# CommAI fields that can be mapped, per object (the inputs connectors receive).
SOURCE_FIELDS = {
    "contact": ["name", "email", "phone", "company", "notes"],
    "ticket": ["subject", "description", "priority"],
    "order": ["title", "reference", "amount", "currency", "description"],
    "appointment": ["start", "end", "reason", "location", "contact"],
    # The organisation a contact belongs to: a company in HubSpot, an account in Salesforce or Zoho.
    "company": ["company_name", "domain", "company_phone", "industry", "city", "country"],
}
# Which provider object each CommAI object maps to, when the names differ.
OBJECT_ALIASES = {
    "order": ("deal", "order"),
    "appointment": ("appointment", "event"),
    "company": ("company", "account", "organisation", "organization"),
}
SYNONYMS = {
    "name": {"firstname", "fullname", "name", "contactname"},
    "email": {"email", "emailaddress", "attendeesemail"},
    "phone": {"phone", "mobilephone", "phonenumber"},
    "company": {"company", "companyname", "organisation", "organization"},
    "notes": {"message", "notes", "note"},
    "subject": {"subject", "title", "summary"},
    "description": {"content", "description", "body", "details"},
    "priority": {"hsticketpriority", "priority"},
    "title": {"dealname", "title", "name", "summary"},
    "amount": {"amount", "value", "total"},
    "start": {"startdatetime", "start", "starttime"},
    "end": {"enddatetime", "end", "endtime"},
    "reason": {"summary", "description", "reason"},
    "location": {"location", "place"},
    "contact": {"attendeesemail", "contact"},
    "company_name": {"name", "companyname", "accountname", "organisationname", "organizationname"},
    "domain": {"domain", "website", "websiteurl", "url"},
    "company_phone": {"phone", "phonenumber", "mainphone"},
    "industry": {"industry", "sector"},
    "city": {"city", "town", "billingcity"},
    "country": {"country", "billingcountry", "countryregion"},
}


class SetupError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower().removeprefix("hs_"))


# ---- reading ---------------------------------------------------------------------


def _row(conn, customer_id: Any, app: str, lock: bool = False) -> dict | None:
    return conn.execute(
        "SELECT * FROM integration_connections WHERE customer_id = %s AND app = %s" + (" FOR UPDATE" if lock else ""),
        (customer_id, app),
    ).fetchone()


def _connector(app: str) -> connectors.Connector:
    try:
        return connectors.get(app)
    except KeyError:
        raise SetupError(f"There is no {app} integration.", 404) from None


def public(row: dict | None) -> dict | None:
    """The connection as the API shows it: never the secret reference."""
    if row is None:
        return None
    out = {k: v for k, v in row.items() if k not in ("secret_ref",)}
    out["last_error"] = redact(row.get("last_error") or "")
    out["signed_in"] = row.get("auth_status") in ("signed_in", "not_needed")
    return out


def catalogue(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    rows = {
        r["app"]: r
        for r in conn.execute("SELECT * FROM integration_connections WHERE customer_id = %s", (customer_id,))
    }
    out = []
    for c in sorted(connectors.all_connectors(), key=lambda c: c.label):
        item = kit.describe_any(c)
        row = rows.get(item["app"])
        if isinstance(c, kit.KitConnector):
            # ADR 0034: the real app needs ExaCarib's registration and go-live;
            # until then a connection runs on the app's stand-in.
            real, reasons = c.real_ready(conn, customer_id)
            ready, why = real, " ".join(reasons)
            simulated = (row is None and not real) or bool(row and row.get("auth_method") == "simulated")
        else:
            ready, why = (True, "") if c.auth == "none" else oauth.ready(item["app"])
            simulated = item["app"].startswith("sim_")
        token_entry = item["app"] == "hubspot"
        out.append(
            {
                **item,
                "simulated": simulated,
                "sign_in_ready": ready,
                "not_live_reason": why,
                "token_entry": token_entry,
                "read_actions": [a["name"] for a in item["actions"] if a["kind"] == "read"],
                "write_actions": [a["name"] for a in item["actions"] if a["kind"] in WRITE_KINDS],
                "mapping_objects": sorted(getattr(c, "mapping_targets", {}) or {}),
                "connection": public(rows.get(item["app"])),
            }
        )
    return out


# ---- setup steps -----------------------------------------------------------------


def connect(conn: psycopg.Connection, customer_id: Any, app: str, actor: str) -> dict:
    """Step 1: choose the app. Returns the connection and, for sign-in apps,
    the URL to sign in at (or why sign-in is not available yet)."""
    c = _connector(app)
    row = _row(conn, customer_id, app, lock=True)
    if row is None:
        row = conn.execute(
            "INSERT INTO integration_connections (customer_id, app) VALUES (%s, %s) RETURNING *", (customer_id, app)
        ).fetchone()
        events.emit(conn, customer_id, "integration.connected", {"app": app, "by": actor}, app)
    if c.auth == "none" and row["status"] == "draft":
        row = conn.execute(
            """UPDATE integration_connections SET status = 'authorised', auth_status = 'not_needed',
                      updated_at = now() WHERE id = %s RETURNING *""",
            (row["id"],),
        ).fetchone()
    out: dict = {"connection": public(row)}
    if isinstance(c, kit.KitConnector) and c.auth != "none":
        real, reasons = c.real_ready(conn, customer_id)
        out["sign_in_ready"], out["not_live_reason"] = real, " ".join(reasons)
        if row["status"] == "draft" and row["auth_status"] in ("none", "") and not real:
            # ADR 0034: until the real app is ready, the connection runs on its stand-in.
            row = conn.execute(
                """UPDATE integration_connections SET status = 'authorised', auth_status = 'not_needed',
                          auth_method = 'simulated', updated_at = now() WHERE id = %s RETURNING *""",
                (row["id"],),
            ).fetchone()
            out["connection"] = public(row)
        elif real and c.auth == "oauth":
            try:
                out["sign_in_url"] = oauth.start(conn, customer_id, app, actor)
            except oauth.OAuthError as e:
                out["sign_in_ready"], out["not_live_reason"] = False, str(e)
        return out
    if c.auth != "none":
        ready, why = oauth.ready(app)
        out["sign_in_ready"] = ready
        out["not_live_reason"] = why
        if ready:
            out["sign_in_url"] = oauth.start(conn, customer_id, app, actor)
    return out


def start_sign_in(conn: psycopg.Connection, customer_id: Any, app: str, actor: str) -> str:
    c = _connector(app)
    if isinstance(c, kit.KitConnector):
        real, reasons = c.real_ready(conn, customer_id)
        if not real:
            raise SetupError(" ".join(reasons), 409)
        if c.auth != "oauth":
            raise SetupError(f"{c.label} uses credentials you enter, not a sign-in page.", 409)
    if _row(conn, customer_id, app) is None:
        connect(conn, customer_id, app, actor)
    try:
        return oauth.start(conn, customer_id, app, actor)
    except oauth.OAuthError as e:
        raise SetupError(str(e), e.code) from None


def finish_sign_in(conn: psycopg.Connection, app: str, code: str, state: str) -> dict:
    try:
        row = oauth.callback(conn, app, code, state)
    except oauth.OAuthError as e:
        raise SetupError(str(e), e.code) from None
    events.emit(conn, row["customer_id"], "integration.signed_in", {"app": app, "method": "oauth"}, app)
    return row


def enter_token(conn: psycopg.Connection, customer_id: Any, app: str, token: str, actor: str) -> dict:
    """Secure entry of a private-app token (HubSpot). Checked with one read,
    then stored encrypted; the token is never shown again."""
    if app != "hubspot":
        raise SetupError("This app signs in with its own sign-in page, not a token.")
    token = (token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{20,400}", token):
        raise SetupError("That doesn't look like a HubSpot private-app token.", 422)
    row = _row(conn, customer_id, app, lock=True)
    if row is None:
        row = connect(conn, customer_id, app, actor)["connection"]
        row = _row(conn, customer_id, app, lock=True)
    from . import vault

    try:
        with conn.transaction():
            oauth.set_token(conn, row, token)
            fresh = _row(conn, customer_id, app)
            check = _connector(app).health(conn, fresh)
            if not check["ok"] and check["cause"] in ("expired_signin", "permission"):
                raise SetupError(f"HubSpot did not accept that token: {check['detail']}", 422)
    except vault.VaultError as e:
        raise SetupError(str(e), 409) from None
    events.emit(conn, customer_id, "integration.signed_in", {"app": app, "method": "token", "by": actor}, app)
    return public(_row(conn, customer_id, app))


def enter_credentials(conn: psycopg.Connection, customer_id: Any, app: str, creds: dict, actor: str) -> dict:
    """Secure entry of credentials (API key, user name and app password, client
    id and secret) for apps that use them (ADR 0034). Checked against the
    app's own fields, stored encrypted after one live check, never shown again."""
    c = _connector(app)
    fields = getattr(c, "credentials", ()) or ()
    if not fields:
        raise SetupError(f"{c.label} signs in with its own sign-in page, not credentials.")
    if isinstance(c, kit.KitConnector):
        real, reasons = c.real_ready(conn, customer_id)
        if not real:
            raise SetupError(" ".join(reasons), 409)
    clean = {}
    for f in fields:
        v = str((creds or {}).get(f.name) or "").strip()
        if not v:
            raise SetupError(f"{f.label} is required.", 422)
        if not re.fullmatch(f.pattern, v):
            raise SetupError(f"That doesn't look like a {c.label} {f.label.lower()}.", 422)
        clean[f.name] = v
    unknown = set(creds or {}) - {f.name for f in fields}
    if unknown:
        raise SetupError(f"Unknown fields: {', '.join(sorted(unknown))}.", 422)
    row = _row(conn, customer_id, app, lock=True)
    if row is None:
        connect(conn, customer_id, app, actor)
        row = _row(conn, customer_id, app, lock=True)
    from . import vault

    try:
        with conn.transaction():
            oauth.set_credentials(conn, row, clean)
            fresh = _row(conn, customer_id, app)
            check = c.health(conn, fresh)
            if not check["ok"] and check["cause"] in ("expired_signin", "permission"):
                raise SetupError(f"{c.label} did not accept those details: {check['detail']}", 422)
    except vault.VaultError as e:
        raise SetupError(str(e), 409) from None
    events.emit(conn, customer_id, "integration.signed_in", {"app": app, "method": "credentials", "by": actor}, app)
    return public(_row(conn, customer_id, app))


def extra_settings(conn: psycopg.Connection, customer_id: Any, app: str) -> set[str]:
    """Settings an app takes beyond the common ones: its own (tenant, subdomain,
    server address...) and, for example apps and stand-ins, simulate_failure."""
    try:
        c = connectors.get(app)
    except KeyError:
        return set()
    out = {s.name for s in getattr(c, "settings_fields", ()) or ()}
    row = _row(conn, customer_id, app)
    if app.startswith("sim_") or (row and row.get("auth_method") == "simulated"):
        out.add("simulate_failure")
    return out


def check_settings(app: str, settings: dict) -> None:
    c = _connector(app)
    mode = settings.get("simulate_failure")
    if mode not in (None, "", "expired_signin", "permission", "provider", "rate_limit"):
        raise SetupError("simulate_failure is expired_signin, permission, provider or rate_limit.", 422)
    if hasattr(c, "check_settings"):
        try:
            c.check_settings(settings)
        except ValueError as e:
            raise SetupError(str(e), 422) from None


def set_allowed(conn: psycopg.Connection, customer_id: Any, app: str, names: list[str], actor: str) -> dict:
    """Step 3: which actions CommAI may run. Read is listed apart from writes."""
    c = _connector(app)
    row = _row(conn, customer_id, app, lock=True)
    if row is None:
        raise SetupError(f"Connect {c.label} first.", 409)
    unknown = [n for n in names if n not in c.actions]
    if unknown:
        raise SetupError(f"{c.label} has no action called {', '.join(unknown)}.", 422)
    names = sorted(set(names), key=list(c.actions).index)
    row = conn.execute(
        "UPDATE integration_connections SET allowed_actions = %s, updated_at = now() WHERE id = %s RETURNING *",
        (names, row["id"]),
    ).fetchone()
    return public(row)


def permissions(app: str, allowed: list[str]) -> dict:
    c = _connector(app)
    return {
        "read": [n for n in allowed if c.actions[n].kind == "read"],
        "write": [
            {"name": n, "kind": c.actions[n].kind, "sensitive": c.actions[n].sensitive}
            for n in allowed
            if c.actions[n].kind in WRITE_KINDS
        ],
    }


def suggest_mapping(conn: psycopg.Connection, customer_id: Any, app: str, settings: Any = None) -> dict:
    """Step 4: suggested field mappings. Confident matches are filled in;
    anything unclear is left for a person. The model (when a key is set) may
    suggest targets for unclear fields, but only from the app's own list."""
    c = _connector(app)
    targets: dict[str, list[str]] = getattr(c, "mapping_targets", {}) or {}
    objects: dict[str, dict] = {}
    open_items: list[str] = []
    for obj, sources in SOURCE_FIELDS.items():
        target_obj = next((t for t in OBJECT_ALIASES.get(obj, (obj,)) if t in targets), None)
        if target_obj is None:
            continue
        fields = []
        for src in sources:
            scored = []
            for t in targets[target_obj]:
                n = _norm(t)
                if n == _norm(src):
                    s = 1.0
                elif n in SYNONYMS.get(src, set()):
                    s = 0.9
                elif _norm(src) in n or n in _norm(src):
                    s = 0.6
                else:
                    s = 0.0
                scored.append((s, t))
            scored.sort(key=lambda x: -x[0])
            best = scored[0] if scored and scored[0][0] > 0 else (0.0, "")
            tie = len(scored) > 1 and scored[1][0] == best[0] and best[0] > 0
            clear = best[0] >= 0.85 and not tie
            fields.append(
                {
                    "source": f"{obj}.{src}",
                    "target": best[1] if best[0] >= 0.5 else "",
                    "confidence": "high" if clear else ("low" if best[0] >= 0.5 else "none"),
                    "needs_person": not clear,
                    "by": "rules",
                }
            )
            if not clear:
                open_items.append(f"{obj}.{src}")
        objects[target_obj] = {"from": obj, "fields": fields, "targets": targets[target_obj]}
    if open_items and llm.available(settings):
        _model_mapping(settings, c, objects, open_items)
    return {"app": app, "objects": objects, "open": open_items, "model_used": llm.available(settings)}


def _model_mapping(settings: Any, c: connectors.Connector, objects: dict, open_items: list[str]) -> None:
    import json

    ask = {
        o: {"open": [f["source"] for f in v["fields"] if f["needs_person"]], "targets": v["targets"]}
        for o, v in objects.items()
    }
    got = llm.complete_json(
        settings,
        "You map CommAI fields to fields of a business app. Answer JSON only: "
        '{"<source>": {"target": "<one of the targets or empty>", "confidence": "high|low"}}. '
        "Never invent a target that is not listed. Leave target empty when unsure.",
        f"App: {c.label}\n" + json.dumps(ask),
    )
    if not isinstance(got, dict):
        return
    for v in objects.values():
        for f in v["fields"]:
            g = got.get(f["source"])
            if not f["needs_person"] or not isinstance(g, dict):
                continue
            t = g.get("target")
            if t in v["targets"]:
                f.update(target=t, by="model", confidence="model")
                # The model's suggestion is shown, but a person still confirms it.


def save_mapping(
    conn: psycopg.Connection, customer_id: Any, app: str, mapping: dict, actor: str, settings: Any = None
) -> dict:
    """A person saves the mapping. Every field they set (a target, or "" for
    "don't send") is confirmed; anything still unclear stays open."""
    c = _connector(app)
    targets: dict[str, list[str]] = getattr(c, "mapping_targets", {}) or {}
    row = _row(conn, customer_id, app, lock=True)
    if row is None:
        raise SetupError(f"Connect {c.label} first.", 409)
    clean: dict[str, dict[str, str]] = {}
    for obj, fields in (mapping or {}).items():
        if obj not in targets or not isinstance(fields, dict):
            raise SetupError(f"{c.label} has nothing called {obj} to map.", 422)
        for src, tgt in fields.items():
            if tgt and tgt not in targets[obj]:
                raise SetupError(f"{c.label} has no {obj} field called {tgt}.", 422)
            clean.setdefault(obj, {})[src] = tgt or ""
    suggestion = suggest_mapping(conn, customer_id, app, None)
    confirmed = {f"{v['from']}.{s}" for o, v in suggestion["objects"].items() for s in clean.get(o, {})}
    still_open = [x for x in suggestion["open"] if x not in confirmed]
    row = conn.execute(
        "UPDATE integration_connections SET mapping = %s, mapping_open = %s, updated_at = now() WHERE id = %s"
        " RETURNING *",
        (Jsonb(clean), still_open, row["id"]),
    ).fetchone()
    return public(row)


def accept_suggestions(conn, customer_id: Any, app: str, actor: str) -> dict:
    """Save every confident suggestion as the mapping (unclear ones stay open)."""
    s = suggest_mapping(conn, customer_id, app, None)
    mapping = {
        o: {f["source"].split(".", 1)[1]: f["target"] for f in v["fields"] if not f["needs_person"]}
        for o, v in s["objects"].items()
    }
    return save_mapping(conn, customer_id, app, mapping, actor)


def run_test(conn: psycopg.Connection, customer_id: Any, app: str, actor: str) -> dict:
    """Step 5: sample lookups for every allowed read action, run now against
    the app; nothing is written. Controlled test writes go through
    test_action(). Moves the connection to 'testing'."""
    c = _connector(app)
    row = _row(conn, customer_id, app, lock=True)
    if row is None or row["status"] == "draft":
        raise SetupError(f"Connect and sign in to {c.label} first.", 409)
    if row["status"] == "paused":
        raise SetupError(f"{c.label} is paused. Resume it first.", 409)
    results = []
    reads = [n for n in row["allowed_actions"] if c.actions[n].kind == "read"]
    for name in reads:
        sample = getattr(c, "sample_inputs", lambda a: {})(name) or _default_sample(c, name)
        try:
            with conn.transaction():
                clean = c.validate(name, sample)
                out = c.execute(conn, {**row, "test": True}, name, clean, f"test:{row['id']}:{name}")
            results.append({"action": name, "ok": True, "inputs": clean, "result": _trim(out)})
        except connectors.ConnectorError as e:
            results.append({"action": name, "ok": False, "cause": e.cause, "error": redact(str(e))})
        except ValueError as e:
            results.append({"action": name, "ok": False, "cause": "input", "error": str(e)})
    check = None
    if not reads:
        with conn.transaction():
            check = c.health(conn, row)
        results.append(
            {
                "action": "connection check",
                "ok": check["ok"],
                "cause": check.get("cause", ""),
                "error": redact(check.get("detail", "")) if not check["ok"] else "",
            }
        )
    ok = all(r["ok"] for r in results)
    cause = next((r["cause"] for r in results if not r["ok"]), "")
    result = {"ok": ok, "at": dt.datetime.now(dt.UTC).isoformat(), "by": actor, "results": results}
    row = conn.execute(
        """UPDATE integration_connections SET last_test_at = now(), last_test_result = %s,
                  status = CASE WHEN status IN ('authorised', 'broken') AND %s THEN 'testing' ELSE status END,
                  last_cause = %s, updated_at = now()
           WHERE id = %s RETURNING *""",
        (Jsonb(result), ok, cause, row["id"]),
    ).fetchone()
    events.emit(conn, customer_id, "integration.tested", {"app": app, "ok": ok, "cause": cause}, app)
    return {"connection": public(row), "test": result}


def _default_sample(c: connectors.Connector, name: str) -> dict:
    out = {}
    for f in c.actions[name].fields:
        out[f.name] = {
            "email": "test@example.com",
            "date": (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).date().isoformat(),
        }.get(f.type, "test")
    return out


def _trim(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _trim(x) for k, x in list(v.items())[:20]}
    if isinstance(v, list):
        return [_trim(x) for x in v[:10]]
    return v


def test_action(conn, customer_id: Any, app: str, action: str, inputs: dict, actor: str, key: str) -> dict:
    """A controlled test action through the normal action service (test=True).
    Real connectors only write in test mode to a test calendar or sandbox."""
    return actions.propose(
        conn,
        customer_id,
        role="person",
        app=app,
        action=action,
        inputs=inputs,
        actor=actor,
        test=True,
        idempotency_key=key,
    )


def approve(conn: psycopg.Connection, customer_id: Any, app: str, actor: str) -> dict:
    """Step 6: a person approves and switches it on."""
    if not actor.startswith("user:"):
        raise SetupError("Only a person can switch an integration on.", 403)
    c = _connector(app)
    row = _row(conn, customer_id, app, lock=True)
    if row is None:
        raise SetupError(f"Connect {c.label} first.", 409)
    missing = []
    if row["auth_status"] not in ("signed_in", "not_needed"):
        missing.append("sign in")
    if not row["allowed_actions"]:
        missing.append("choose at least one allowed action")
    if row["mapping_open"]:
        missing.append(f"finish the field mapping ({', '.join(row['mapping_open'])})")
    if not (row["last_test_result"] or {}).get("ok"):
        missing.append("run a test that passes")
    if missing:
        raise SetupError(f"Before switching {c.label} on: {'; '.join(missing)}.", 409)
    row = conn.execute(
        """UPDATE integration_connections SET status = 'live', test_mode = false, approved_by = %s,
                  approved_at = now(), last_cause = '', updated_at = now() WHERE id = %s RETURNING *""",
        (actor, row["id"]),
    ).fetchone()
    events.emit(conn, customer_id, "integration.live", {"app": app, "by": actor}, app)
    return public(row)


def pause(conn: psycopg.Connection, customer_id: Any, app: str, actor: str) -> dict:
    row = _row(conn, customer_id, app, lock=True)
    if row is None:
        raise SetupError("Not connected.", 404)
    if row["status"] == "paused":
        return public(row)
    row = conn.execute(
        """UPDATE integration_connections SET paused_from = status, status = 'paused', updated_at = now()
           WHERE id = %s RETURNING *""",
        (row["id"],),
    ).fetchone()
    events.emit(conn, customer_id, "integration.paused", {"app": app, "by": actor}, app)
    return public(row)


def resume(conn: psycopg.Connection, customer_id: Any, app: str, actor: str) -> dict:
    row = _row(conn, customer_id, app, lock=True)
    if row is None or row["status"] != "paused":
        raise SetupError("This integration is not paused.", 409)
    back = row["paused_from"] or "authorised"
    if back == "live" and not row["approved_at"]:
        back = "authorised"
    row = conn.execute(
        "UPDATE integration_connections SET status = %s, paused_from = '', updated_at = now() WHERE id = %s"
        " RETURNING *",
        (back, row["id"]),
    ).fetchone()
    events.emit(conn, customer_id, "integration.resumed", {"app": app, "by": actor}, app)
    return public(row)


def disconnect(conn: psycopg.Connection, customer_id: Any, app: str, actor: str) -> None:
    row = _row(conn, customer_id, app, lock=True)
    if row is None:
        raise SetupError("Not connected.", 404)
    oauth.sign_out(conn, row)
    conn.execute("DELETE FROM integration_connections WHERE id = %s", (row["id"],))
    events.emit(conn, customer_id, "integration.disconnected", {"app": app, "by": actor}, app)


# ---- health and repair -----------------------------------------------------------

_CAUSE_RE = re.compile(r"\((expired_signin|permission|mapping|provider|input)\)$")


def affected_workflows(conn, customer_id: Any, app: str) -> list[dict]:
    rows = conn.execute(
        """SELECT w.id, w.name, w.status, v.definition FROM commai_workflows w
           JOIN commai_workflow_versions v ON v.workflow_id = w.id AND v.version = w.live_version
           WHERE w.customer_id = %s AND w.status IN ('live', 'paused')""",
        (customer_id,),
    ).fetchall()
    out = []
    for r in rows:
        uses = [
            s for s in (r["definition"] or {}).get("steps", []) if s.get("type") == "action" and s.get("app") == app
        ]
        if uses:
            out.append(
                {
                    "id": str(r["id"]),
                    "name": r["name"],
                    "status": r["status"],
                    "actions": sorted({s.get("action", "") for s in uses}),
                }
            )
    return out


def health(conn: psycopg.Connection, customer_id: Any, app: str, *, check: bool = False) -> dict:
    """Sign-in status, last success, failures, affected workflows, the cause
    in plain words, the evidence and the safe way to recover."""
    c = _connector(app)
    row = _row(conn, customer_id, app)
    if row is None:
        raise SetupError(f"{c.label} is not connected.", 404)
    failed = conn.execute(
        """SELECT id, action, error, created_at, finished_at, conversation_id, test FROM action_runs
           WHERE customer_id = %s AND app = %s AND status = 'failed' AND created_at > now() - interval '7 days'
           ORDER BY created_at DESC""",
        (customer_id, app),
    ).fetchall()
    since_success = [f for f in failed if row["last_success_at"] is None or f["created_at"] > row["last_success_at"]]
    cause = row["last_cause"] or ""
    if not cause and since_success:
        m = _CAUSE_RE.search(since_success[0]["error"] or "")
        cause = m.group(1) if m else "provider"
    if row["auth_status"] == "expired":
        cause = "expired_signin"
    live_check = None
    if check:
        with conn.transaction():
            live_check = c.health(conn, row)
        if not live_check["ok"]:
            cause = live_check["cause"] or cause or "provider"
    if row["status"] == "broken" and not cause:
        cause = "provider"
    problem = (
        row["status"] == "broken"
        or row["auth_status"] == "expired"
        or bool(since_success)
        or (live_check is not None and not live_check["ok"])
    )
    if not problem:
        cause = ""
    if cause == "input" and row["status"] != "broken":
        level = "attention"
    elif problem:
        level = (
            "broken"
            if row["status"] == "broken" or cause in ("expired_signin", "permission", "mapping")
            else ("degraded")
        )
    else:
        level = "healthy"
    evidence = [
        {
            "run_id": str(f["id"]),
            "action": f["action"],
            "error": redact(f["error"]),
            "at": f["created_at"].isoformat(),
            "test": f["test"],
        }
        for f in failed[:5]
    ]
    if live_check:
        evidence.insert(0, {"check": "live", "ok": live_check["ok"], "detail": redact(live_check["detail"])})
    repair = None
    if problem and cause in CAUSES:
        spec = CAUSES[cause]
        repair = {
            "cause": cause,
            "label": spec["label"],
            "explain": spec["explain"].format(app=c.label),
            "steps": [{"id": sid, "label": label.format(app=c.label)} for sid, label in spec["steps"]],
        }
    return {
        "app": app,
        "label": c.label,
        "status": row["status"],
        "level": level,
        "signed_in": row["auth_status"] in ("signed_in", "not_needed"),
        "auth_status": row["auth_status"],
        "token_expires_at": row["token_expires_at"],
        "last_success_at": row["last_success_at"],
        "last_failure_at": row["last_failure_at"],
        "last_error": redact(row["last_error"]),
        "failures_7d": len(failed),
        "failures_since_success": len(since_success),
        "cause": cause,
        "evidence": evidence,
        "affected_workflows": affected_workflows(conn, customer_id, app),
        "repair": repair,
        "checked": live_check is not None,
    }


def repair(conn: psycopg.Connection, customer_id: Any, app: str, step: str, actor: str) -> dict:
    """Run one safe repair step. Steps that need a person elsewhere (sign in,
    edit the mapping, switch an action off) are pointed to, not done here."""
    c = _connector(app)
    row = _row(conn, customer_id, app, lock=True)
    if row is None:
        raise SetupError(f"{c.label} is not connected.", 404)
    if step == "recheck":
        with conn.transaction():
            result = c.health(conn, row)
        if result["ok"]:
            back = "live" if row["approved_at"] else ("testing" if row["last_test_at"] else "authorised")
            new_status = back if row["status"] == "broken" else row["status"]
            conn.execute(
                """UPDATE integration_connections SET status = %s, last_cause = '', last_error = '',
                          auth_status = CASE WHEN auth_status = 'expired' THEN 'signed_in' ELSE auth_status END,
                          last_success_at = now(), updated_at = now() WHERE id = %s""",
                (new_status, row["id"]),
            )
            events.emit(conn, customer_id, "integration.repaired", {"app": app, "by": actor, "step": step}, app)
        else:
            conn.execute(
                "UPDATE integration_connections SET last_cause = %s, last_error = %s, last_failure_at = now()"
                " WHERE id = %s",
                (result["cause"], result["detail"][:500], row["id"]),
            )
        return {
            "step": step,
            "ok": result["ok"],
            "detail": redact(result["detail"]),
            "health": health(conn, customer_id, app),
        }
    if step == "retry_failed":
        if row["status"] not in ("live", "testing"):
            raise SetupError(
                f"{c.label} must be working again before failed actions are retried. Check the connection first.", 409
            )
        runs = conn.execute(
            """SELECT id, test FROM action_runs WHERE customer_id = %s AND app = %s AND status = 'failed'
               AND created_at > now() - interval '24 hours' AND error LIKE %s FOR UPDATE""",
            (customer_id, app, "%(provider)"),
        ).fetchall()
        n = 0
        for r in runs:
            conn.execute("UPDATE action_runs SET status = 'approved', finished_at = NULL WHERE id = %s", (r["id"],))
            attempt = conn.execute(
                "SELECT count(*) AS n FROM jobs WHERE dedupe_key LIKE %s", (f"action:{r['id']}%",)
            ).fetchone()["n"]
            # The same idempotency key is reused, so a retry never books or creates twice.
            jobs.enqueue(
                conn,
                "action.execute",
                {"run_id": str(r["id"])},
                customer_id=customer_id,
                dedupe_key=f"action:{r['id']}:retry:{attempt}",
            )
            n += 1
        return {"step": step, "ok": True, "detail": f"{n} failed action(s) queued again with their original keys."}
    if step in ("sign_in", "edit_mapping", "remove_action", "review"):
        where = {
            "sign_in": "Sign in again from the Integrations screen. CommAI never asks for passwords or keys in chat.",
            "edit_mapping": "Open the field mapping for this integration and fix the field it names.",
            "remove_action": "Switch off the action that needs the missing permission under Allowed actions.",
            "review": "Open the failed actions listed as evidence.",
        }[step]
        return {"step": step, "ok": True, "detail": where, "needs_person": True}
    raise SetupError(f"Unknown repair step {step}.", 422)


# ---- diagnostics for the platform assistant ---------------------------------------


@diagnostics.register("integrations")
def _check_integrations(conn, customer_id: Any) -> list[dict]:
    out = []
    for row in conn.execute(
        "SELECT app FROM integration_connections WHERE customer_id = %s ORDER BY app", (customer_id,)
    ).fetchall():
        try:
            h = health(conn, customer_id, row["app"])
        except SetupError:
            continue
        if h["level"] == "healthy":
            out.append(
                {
                    "area": f"integration:{h['app']}",
                    "status": "ok",
                    "confidence": "confirmed",
                    "summary": f"{h['label']} is {h['status']} with no recent failures.",
                    "evidence": [f"Last success: {h['last_success_at'] or 'never'}"],
                    "fix": None,
                }
            )
            continue
        ev = [
            f"Status: {h['status']}",
            f"Sign-in: {h['auth_status']}",
            f"Failures since last success: {h['failures_since_success']}",
        ]
        ev += [f"{e['at'][:16]} {e['action']}: {e['error']}" for e in h["evidence"] if "run_id" in e][:3]
        if h["affected_workflows"]:
            ev.append("Workflows affected: " + ", ".join(w["name"] for w in h["affected_workflows"]))
        confirmed = h["status"] == "broken" or h["auth_status"] == "expired"
        fix = None
        if h["cause"] in ("provider", "mapping", "permission", "expired_signin"):
            fix = {
                "id": "recheck_integration",
                "label": f"Check {h['label']} again and switch it back on if it works",
                "params": {"app": h["app"]},
            }
        if h["cause"] == "provider" and h["status"] in ("live", "testing"):
            fix = {
                "id": "retry_failed_actions",
                "label": f"Retry the failed {h['label']} actions safely",
                "params": {"app": h["app"]},
            }
        out.append(
            {
                "area": f"integration:{h['app']}",
                "status": "problem",
                "confidence": "confirmed" if confirmed else "likely",
                "summary": f"{h['label']}: {CAUSES.get(h['cause'], {}).get('label', 'failing')}. "
                + (h["repair"]["explain"] if h["repair"] else ""),
                "evidence": ev,
                "fix": fix,
                "needs_person": h["cause"] in ("expired_signin", "permission", "mapping"),
                "correlation_ids": [e["run_id"] for e in h["evidence"] if "run_id" in e],
            }
        )
    return out
