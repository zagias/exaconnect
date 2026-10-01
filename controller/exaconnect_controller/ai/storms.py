"""Hurricane watch: NHC advisories near a customer's sites suggest Storm Mode.

Every 15 minutes the controller reads the National Hurricane Center's list
of active storms (CurrentStorms.json). For each storm and each site with
coordinates it projects the storm's current motion forward 72 hours (dead
reckoning, an hour at a time) and finds the closest approach. A storm
forecast to pass close suggests switching Storm Mode on; it never switches
it on by itself.

Dead reckoning is not the NHC forecast track: it is a simple, explainable
first warning. Every insight links the NHC advisory for the official track.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import urllib.request
from dataclasses import dataclass
from importlib import resources
from typing import Any

from .insights import raise_insight, resolve_others

log = logging.getLogger("exaconnect.storms")

EARTH_KM = 6371.0
MPH_TO_KMH = 1.609344
NAMES = {
    "TD": "Tropical Depression",
    "STD": "Subtropical Depression",
    "TS": "Tropical Storm",
    "STS": "Subtropical Storm",
    "HU": "Hurricane",
    "MH": "Major Hurricane",
    "PTC": "Potential Tropical Cyclone",
    "PC": "Post-tropical Cyclone",
}


@dataclass(frozen=True)
class Storm:
    id: str
    name: str
    classification: str
    intensity_kt: float
    lat: float
    lon: float
    heading_deg: float  # direction of motion, degrees clockwise from north
    speed_kmh: float
    updated: str
    advisory_url: str

    @property
    def label(self) -> str:
        return f"{NAMES.get(self.classification, 'Storm')} {self.name}".strip()

    @property
    def hurricane(self) -> bool:
        return self.classification in ("HU", "MH")


@dataclass(frozen=True)
class Policy:
    horizon_h: int = 72
    critical_km: float = 250  # suggest Storm Mode
    critical_km_hurricane: float = 350  # hurricanes have wider damaging winds
    warning_km: float = 600  # keep an eye on it


def coord(v: Any) -> float:
    """'33.7N' -> 33.7, '43.6W' -> -43.6; numbers pass through."""
    if isinstance(v, int | float):
        return float(v)
    s = str(v).strip().upper()
    sign = -1.0 if s.endswith(("S", "W")) else 1.0
    return sign * float(s.rstrip("NSEW"))


def parse(doc: dict) -> list[Storm]:
    out = []
    for s in doc.get("activeStorms") or []:
        try:
            lat = s.get("latitudeNumeric", s.get("latitude"))
            lon = s.get("longitudeNumeric", s.get("longitude"))
            out.append(
                Storm(
                    id=str(s["id"]),
                    name=str(s.get("name", "")),
                    classification=str(s.get("classification", "")),
                    intensity_kt=float(s.get("intensity") or 0),
                    lat=coord(lat),
                    lon=coord(lon),
                    heading_deg=float(s.get("movementDir") or 0),
                    speed_kmh=float(s.get("movementSpeed") or 0) * MPH_TO_KMH,
                    updated=str(s.get("lastUpdate", "")),
                    advisory_url=str((s.get("publicAdvisory") or {}).get("url", "")),
                )
            )
        except (KeyError, TypeError, ValueError):
            log.warning("skipped a storm entry NHC sent in an unexpected shape")
    return out


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def move(lat: float, lon: float, heading_deg: float, km: float) -> tuple[float, float]:
    """The point km along a great circle from (lat, lon) on a heading."""
    d, h = km / EARTH_KM, math.radians(heading_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(h))
    l2 = l1 + math.atan2(math.sin(h) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def closest_approach(storm: Storm, lat: float, lon: float, horizon_h: int = 72) -> tuple[float, int]:
    """(distance km, hours from now) of the storm's closest projected approach."""
    best = (distance_km(storm.lat, storm.lon, lat, lon), 0)
    for h in range(1, horizon_h + 1):
        plat, plon = move(storm.lat, storm.lon, storm.heading_deg, storm.speed_kmh * h)
        d = distance_km(plat, plon, lat, lon)
        if d < best[0]:
            best = (d, h)
    return best


def assess(storm: Storm, site: str, lat: float, lon: float, policy: Policy = Policy()) -> dict | None:
    """The warning for one storm and one site, or None when it stays clear."""
    km, hours = closest_approach(storm, lat, lon, policy.horizon_h)
    critical = policy.critical_km_hurricane if storm.hurricane else policy.critical_km
    if km > policy.warning_km:
        return None
    severity = "critical" if km <= critical else "warning"
    when = "now" if hours == 0 else f"in about {hours} h"
    detail = (
        f"{storm.label} ({storm.intensity_kt:.0f} kt) is moving at {storm.speed_kmh:.0f} km/h on a heading of "
        f"{storm.heading_deg:.0f}°. Projecting that motion forward, it passes about {km:.0f} km from {site} {when}."
    )
    if severity == "critical":
        detail += " Consider switching Storm Mode on so the satellite path is warm before the terrestrial links suffer."
    detail += " This is a straight-line projection; check the NHC advisory for the official forecast track."
    return {
        "severity": severity,
        "title": f"{storm.label} may pass {km:.0f} km from {site} {when}",
        "detail": detail,
        "data": {
            "storm_id": storm.id,
            "storm": storm.label,
            "classification": storm.classification,
            "intensity_kt": storm.intensity_kt,
            "closest_km": round(km, 1),
            "hours": hours,
            "suggest_storm_mode": severity == "critical",
            "advisory_url": storm.advisory_url,
            "storm_position": [storm.lat, storm.lon],
            "nhc_updated": storm.updated,
        },
    }


def fetch(url: str, timeout_s: float = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "ExaConnect hurricane watch (exacarib.com)"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:  # noqa: S310 (fixed, configured URL)
        return json.loads(r.read(4_000_000))


def example_feed() -> dict:
    """A made-up hurricane south-east of Jamaica, for demos. Marked as example data."""
    return json.loads(resources.files(__package__).joinpath("fixtures/nhc-example.json").read_text())


def run_once(conn, doc: dict, example: bool = False, now: dt.datetime | None = None) -> int:
    """Raises or refreshes storm warnings for every customer; resolves the rest.
    Returns how many warnings are open."""
    storms = parse(doc)
    sites = conn.execute(
        """SELECT s.id, s.name, s.customer_id, s.latitude, s.longitude, c.storm_mode
           FROM sites s JOIN customers c ON c.id = s.customer_id
           WHERE s.latitude IS NOT NULL AND s.longitude IS NOT NULL"""
    ).fetchall()
    seen: dict[Any, set[str]] = {s["customer_id"]: set() for s in sites}
    for site in sites:
        for storm in storms:
            w = assess(storm, site["name"], site["latitude"], site["longitude"])
            if w is None:
                continue
            key = f"storm:{storm.id}:{site['name']}" + (":example" if example else "")
            raise_insight(
                conn,
                site["customer_id"],
                "storm_warning",
                key,
                w["severity"],
                ("Example data: " if example else "") + w["title"],
                w["detail"],
                w["data"],
                site_id=site["id"],
                example=example,
            )
            seen[site["customer_id"]].add(key)
    for customer_id, keys in seen.items():
        resolve_others(conn, customer_id, "storm_warning", keys, example=example)
    return sum(len(k) for k in seen.values())
