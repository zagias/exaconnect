"""Background passes for the AI features: anomalies every 2 minutes, the
bill-shock forecast and application detection every 5, the disaster watch every 10
and the hurricane watch every 15 (both configurable).
A Postgres advisory lock keeps it to one controller at a time."""

from __future__ import annotations

import asyncio
import logging
import time

from .. import db
from ..settings import Settings
from . import anomaly, billshock, detect, hazards, storms
from .insights import resolve_kind

log = logging.getLogger("exaconnect.ai")
LOCK_ID = 4245


def _locked(fn, *args) -> int | None:
    with db.tx() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (LOCK_ID,)).fetchone()["ok"]:
            return None
        return fn(conn, *args)


def storm_pass(url: str) -> int | None:
    doc = storms.fetch(url)  # outside the transaction: the network can be slow
    tracks = storms.load_tracks(doc)
    return _locked(storms.run_once, doc, False, None, tracks)


def hazard_pass(settings: Settings) -> int | None:
    urls = [u.strip() for u in settings.tsunami_urls.split(",") if u.strip()]
    reports, failed = hazards.read_feeds(settings.usgs_url, settings.gdacs_url, urls)  # outside the transaction
    return _locked(hazards.run_once, reports, failed, False, bool(settings.nhc_url))


def hazard_watch_on(settings: Settings) -> bool:
    return bool(settings.usgs_url or settings.gdacs_url or settings.tsunami_urls.strip(", "))


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
        else:  # a watch switched off leaves nothing to refresh its warnings, so close them
            await every("storms-off", 300, _locked, resolve_kind, "storm_warning")
        if hazard_watch_on(settings):
            await every("hazards", settings.hazard_interval_s, hazard_pass, settings)
        else:
            await every("hazards-off", 300, _locked, resolve_kind, "hazard")
        await asyncio.sleep(15)
