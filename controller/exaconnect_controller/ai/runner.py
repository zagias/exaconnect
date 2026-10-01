"""Background passes for the AI features: anomalies every 2 minutes, the
bill-shock forecast and application detection every 5, and the hurricane watch every 15 (configurable).
A Postgres advisory lock keeps it to one controller at a time."""

from __future__ import annotations

import asyncio
import logging
import time

from .. import db
from ..settings import Settings
from . import anomaly, billshock, detect, storms

log = logging.getLogger("exaconnect.ai")
LOCK_ID = 4245


def _locked(fn, *args) -> int | None:
    with db.tx() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (LOCK_ID,)).fetchone()["ok"]:
            return None
        return fn(conn, *args)


def storm_pass(url: str) -> int | None:
    doc = storms.fetch(url)  # outside the transaction: the network can be slow
    return _locked(storms.run_once, doc)


async def loop(settings: Settings) -> None:
    last: dict[str, float] = {}

    async def every(name: str, period_s: float, fn, *args) -> None:
        if period_s <= 0 or time.monotonic() - last.get(name, -1e12) < period_s:
            return
        last[name] = time.monotonic()
        try:
            await asyncio.to_thread(fn, *args)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never fatal; the next pass tries again
            log.warning("%s pass failed: %s", name, e)

    await asyncio.sleep(20)  # let the first telemetry arrive
    while True:
        await every("anomaly", 120, _locked, anomaly.run_once)
        await every("billshock", 300, _locked, billshock.run_once)
        await every("detect", 300, _locked, detect.run_once)
        if settings.nhc_url:
            await every("storms", settings.nhc_interval_s, storm_pass, settings.nhc_url)
        await asyncio.sleep(15)
