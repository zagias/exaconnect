"""Background work for integrations: the tick (nodes offline, maintenance
windows), OTLP metrics export, NetBox syncs and on-ramp status checks.
One process at a time (advisory lock); errors are logged, never fatal."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

from .. import db

log = logging.getLogger("exaconnect.integrations")

LOCK_ID = 4251
TICK_S = 15
EXPORT_S = 60
ONRAMP_S = 300


def tick_once(now: dt.datetime | None = None) -> dict | None:
    from . import publish

    with db.tx() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (LOCK_ID,)).fetchone()["ok"]:
            return None
        return publish.tick(conn, now)


def export_metrics() -> int:
    """Push the metric families to every enabled OTLP integration that exports metrics."""
    from . import delivery, metrics, providers, transport
    from .providers import Context, ProviderError

    sent = 0
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT * FROM connect_integrations WHERE enabled AND provider IN ('otlp', 'grafana_cloud')"
            " AND customer_id IS NOT NULL AND coalesce((config->>'export_metrics')::boolean, true)"
        ).fetchall()
        for r in rows:
            p = providers.get(r["provider"])
            try:
                secrets = transport.decrypt(r["secret_ciphertext"])
                live = delivery.mode_of(r, secrets) == "live"
                http = transport.Transport(conn, r["id"], r["provider"], live, secrets, p.simulate)
                p.export_metrics(
                    Context(conn, r, r["config"] or {}, secrets, http), metrics.collect(conn, r["customer_id"])
                )
                sent += 1
            except (ProviderError, transport.Unreachable, transport.vault.VaultError) as e:
                conn.execute(
                    "UPDATE connect_integrations SET last_status = %s WHERE id = %s",
                    (f"metrics export failed: {str(e)[:180]}", r["id"]),
                )
    return sent


def sync_inventory(now: float | None = None) -> int:
    from .api import run_sync

    n = 0
    with db.tx() as conn:
        rows = conn.execute(
            """SELECT * FROM connect_integrations WHERE enabled AND provider = 'netbox'
                 AND (last_delivery_at IS NULL OR last_delivery_at
                      < now() - make_interval(mins => coalesce((config->>'interval_minutes')::int, 60)))"""
        ).fetchall()
        for r in rows:
            try:
                run_sync(conn, r, "system:netbox")
                n += 1
            except Exception:  # noqa: BLE001
                log.exception("NetBox sync %s failed", r["id"])
    return n


def refresh_onramps() -> int:
    from . import onramps

    with db.tx() as conn:
        return onramps.refresh_all(conn)


async def loop() -> None:
    last = {"export": 0.0, "sync": 0.0, "onramp": 0.0}
    while True:
        for name, fn, every in (
            ("tick", tick_once, 0),
            ("export", export_metrics, EXPORT_S),
            ("sync", sync_inventory, EXPORT_S),
            ("onramp", refresh_onramps, ONRAMP_S),
        ):
            if every and time.monotonic() - last[name] < every:
                continue
            if every:
                last[name] = time.monotonic()
            try:
                await asyncio.to_thread(fn)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("integrations %s failed", name)
        await asyncio.sleep(TICK_S)
