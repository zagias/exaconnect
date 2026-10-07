"""Regional hosting (ADR 0025).

Regions are capabilities of kind "region" in the go-live registry (ADR 0022).
Each declares every dependency it needs. A region can be switched on only when
each dependency has a provider in that region recorded: each dependency is a
go-live criterion, and a database trigger (sql/62_partners.sql) keeps it unmet
until the provider is recorded here, through whichever API marks it.

The only real region is the current server ("primary"). The others are
declared and off; no cloud resources are created for them.

Every business has a home region (default "primary"); data-location reports
read from it.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

import psycopg

from . import golive

DEPENDENCIES: dict[str, str] = {
    "database": "Database (PostgreSQL with TimescaleDB): conversations, contacts, settings, usage",
    "backups": "Backups location (encrypted copies of the database)",
    "llm": "AI model provider and the region it runs in",
    "channels": "Messaging channel providers (WhatsApp, SMS, email)",
    "speech": "Speech-to-text and text-to-speech for calls",
}
DEFAULT_REGION = "primary"

REGIONS: dict[str, dict[str, Any]] = {
    "primary": {
        "name": "Primary (the current server)",
        "location": os.environ.get("EXA_REGION_LOCATION", "The server Connect runs on today"),
        "real": True,
    },
    "caribbean": {"name": "Caribbean", "location": "A data centre in the Caribbean (not chosen yet)", "real": False},
    "us-east": {"name": "United States (east)", "location": "Not provisioned", "real": False},
    "eu-west": {"name": "Europe (west)", "location": "Not provisioned", "real": False},
}

for _key, _r in REGIONS.items():
    golive.declare(
        "region",
        _key,
        _r["name"],
        {f"dependency-{d}": f"{label}: a provider in this region is recorded." for d, label in DEPENDENCIES.items()},
        {"dependencies": list(DEPENDENCIES), "location": _r["location"], "real": _r["real"]},
    )


class RegionError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def _check(key: str, dependency: str | None = None) -> None:
    if key not in REGIONS:
        raise RegionError("No such region.", 404)
    if dependency is not None and dependency not in DEPENDENCIES:
        raise RegionError(f"Dependencies are {', '.join(DEPENDENCIES)}.", 404)


def providers(conn: psycopg.Connection, key: str) -> dict[str, dict]:
    return {
        r["dependency"]: r
        for r in conn.execute("SELECT * FROM commai_region_providers WHERE region = %s", (key,)).fetchall()
    }


def describe(conn: psycopg.Connection, key: str) -> dict:
    _check(key)
    cap = golive.get(conn, "region", key) or {"status": "off", "pilots": []}
    have = providers(conn, key)
    deps = [
        {
            "dependency": d,
            "label": label,
            "provider": have[d]["provider"] if d in have else None,
            "provider_region": have[d]["provider_region"] if d in have else None,
            "notes": have[d]["notes"] if d in have else "",
            "recorded_by": have[d]["recorded_by"] if d in have else None,
            "recorded_at": have[d]["recorded_at"] if d in have else None,
        }
        for d, label in DEPENDENCIES.items()
    ]
    return {
        "key": key,
        "name": REGIONS[key]["name"],
        "location": REGIONS[key]["location"],
        "real": REGIONS[key]["real"],
        "status": cap["status"],
        "pilots": cap.get("pilots", []),
        "dependencies": deps,
        "missing": [d["dependency"] for d in deps if not d["provider"]],
        "customers": conn.execute("SELECT count(*) AS n FROM customers WHERE home_region = %s", (key,)).fetchone()["n"],
    }


def record(
    conn: psycopg.Connection, key: str, dependency: str, provider: str, provider_region: str, notes: str, actor: str
) -> dict:
    _check(key, dependency)
    provider, provider_region = provider.strip(), provider_region.strip()
    if not provider or not provider_region:
        raise RegionError("Name the provider and the region it runs in.", 422)
    conn.execute(
        """INSERT INTO commai_region_providers (region, dependency, provider, provider_region, notes, recorded_by)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (region, dependency) DO UPDATE SET provider = EXCLUDED.provider,
             provider_region = EXCLUDED.provider_region, notes = EXCLUDED.notes,
             recorded_by = EXCLUDED.recorded_by, recorded_at = now()""",
        (key, dependency, provider, provider_region, notes.strip(), actor),
    )
    golive.check(
        conn,
        "region",
        key,
        f"dependency-{dependency}",
        True,
        f"{provider} in {provider_region} recorded. {notes}",
        actor,
    )
    return describe(conn, key)


def remove(conn: psycopg.Connection, key: str, dependency: str, actor: str) -> dict:
    """Removing a provider unmeets its criterion, which switches the region off."""
    _check(key, dependency)
    conn.execute("DELETE FROM commai_region_providers WHERE region = %s AND dependency = %s", (key, dependency))
    golive.check(conn, "region", key, f"dependency-{dependency}", False, "Provider removed.", actor)
    return describe(conn, key)


def set_home(conn: psycopg.Connection, customer_id: Any, key: str) -> None:
    _check(key)
    if key != DEFAULT_REGION and not golive.enabled(conn, "region", key, customer_id):
        raise RegionError(f"{REGIONS[key]['name']} is not switched on for this business.", 409)
    conn.execute("UPDATE customers SET home_region = %s WHERE id = %s", (key, customer_id))


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or url


def data_location(conn: psycopg.Connection, customer_id: Any) -> dict:
    """Where this business's data lives, read from its home region.

    A recorded provider is shown as recorded. For the primary region, a
    dependency with nothing recorded yet is described from the server's
    configuration and marked so."""
    row = conn.execute("SELECT home_region FROM customers WHERE id = %s", (customer_id,)).fetchone()
    key = row["home_region"] if row and row["home_region"] in REGIONS else DEFAULT_REGION
    have = providers(conn, key)
    channel_providers = sorted(
        {
            r["provider"]
            for r in conn.execute(
                "SELECT DISTINCT provider FROM channel_accounts WHERE customer_id = %s", (customer_id,)
            ).fetchall()
        }
    )
    configured = {
        "database": ("PostgreSQL on the controller server", REGIONS[DEFAULT_REGION]["location"]),
        "backups": (
            "Off-site target in deploy/backup" if os.environ.get("EXA_BACKUP_TARGET") else "Not set up yet",
            os.environ.get("EXA_BACKUP_REGION", "unknown"),
        ),
        "llm": (
            _host(os.environ.get("EXA_LLM_BASE_URL", "https://api.deepinfra.com"))
            if os.environ.get("EXA_LLM_API_KEY")
            else "Built-in simulated model (nothing leaves the server)",
            os.environ.get("EXA_LLM_REGION", "unknown") if os.environ.get("EXA_LLM_API_KEY") else "the server",
        ),
        "channels": (
            ", ".join(channel_providers) or "none set up",
            "the server" if set(channel_providers) <= {"simulated"} else "the provider's region",
        ),
        "speech": ("The caller's browser (speech stays on their device)", "the caller's device"),
    }
    items = []
    for d, label in DEPENDENCIES.items():
        if d in have:
            items.append(
                {"dependency": d, "label": label, "provider": have[d]["provider"],
                 "provider_region": have[d]["provider_region"], "source": "recorded"}
            )  # fmt: skip
        elif key == DEFAULT_REGION:
            p, r = configured[d]
            items.append(
                {"dependency": d, "label": label, "provider": p, "provider_region": r, "source": "configuration"}
            )
        else:
            items.append(
                {"dependency": d, "label": label, "provider": None, "provider_region": None, "source": "not recorded"}
            )
    return {
        "home_region": key,
        "region_name": REGIONS[key]["name"],
        "location": REGIONS[key]["location"],
        "items": items,
    }
