"""Hurricane watch: NHC advisories near a customer's sites suggest Storm Mode.

Every 15 minutes the controller reads the National Hurricane Center's list
of active storms (CurrentStorms.json). For each storm and each site with
coordinates it projects the storm's current motion forward 72 hours (dead
reckoning, an hour at a time) and finds the closest approach. A storm
forecast to pass close suggests switching Storm Mode on; it never switches
it on by itself.

When NHC publishes its forecast track and cone for a storm (KMZ files linked
from CurrentStorms.json), the warning uses them instead: the closest approach
along the official track, and whether the site is inside the cone. Dead
reckoning stays as the fallback when those files are missing or unreadable.
Every insight links the NHC advisory.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import math
import urllib.request
import zipfile
from dataclasses import dataclass, field
from importlib import resources
from typing import Any
from xml.etree import ElementTree

from .insights import note_storm_mode, open_customers, raise_insight, resolve_others

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
    track_url: str = ""
    cone_url: str = ""

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
                    track_url=str((s.get("forecastTrack") or {}).get("kmzFile", "")),
                    cone_url=str((s.get("trackCone") or {}).get("kmzFile", "")),
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


# NHC forecast points come at these hours from the advisory (the first is the current position).
FORECAST_HOURS = [0, 12, 24, 36, 48, 60, 72, 96, 120]


@dataclass(frozen=True)
class Track:
    """NHC's forecast track (points in time order) and cone (one ring), as (lat, lon)."""

    points: list[tuple[float, float]] = field(default_factory=list)
    cone: list[tuple[float, float]] = field(default_factory=list)


def _coords(text: str) -> list[tuple[float, float]]:
    out = []
    for item in text.split():
        parts = item.split(",")
        if len(parts) >= 2:
            out.append((float(parts[1]), float(parts[0])))  # KML is lon,lat[,alt]
    return out


def read_kml(data: bytes) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """(points, first polygon ring) from a KML or KMZ file. Point placemarks are the track;
    the first polygon is the cone."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".kml")), None)
            if name is None:
                return [], []
            data = z.read(name)[:8_000_000]
    root = ElementTree.fromstring(data)  # noqa: S314 (NHC's file, size-capped, no entity expansion in ElementTree)
    points: list[tuple[float, float]] = []
    ring: list[tuple[float, float]] = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "Point":
            c = next((x for x in el.iter() if x.tag.rsplit("}", 1)[-1] == "coordinates"), None)
            if c is not None and c.text:
                points.extend(_coords(c.text))
        elif tag == "Polygon" and not ring:
            c = next((x for x in el.iter() if x.tag.rsplit("}", 1)[-1] == "coordinates"), None)
            if c is not None and c.text:
                ring = _coords(c.text)
    return points, ring


def inside(lat: float, lon: float, ring: list[tuple[float, float]]) -> bool:
    """Point in polygon (ray casting); fine at the size of a forecast cone."""
    hit = False
    for (la1, lo1), (la2, lo2) in zip(ring, ring[1:] + ring[:1], strict=True):
        if (la1 > lat) != (la2 > lat) and lon < (lo2 - lo1) * (lat - la1) / (la2 - la1) + lo1:
            hit = not hit
    return hit


def track_approach(track: Track, lat: float, lon: float) -> tuple[float, int]:
    """(distance km, hours from now) of the closest approach along NHC's forecast track,
    walking each leg in 10 km steps."""
    pts = track.points
    best = (distance_km(pts[0][0], pts[0][1], lat, lon), 0)
    for i in range(len(pts) - 1):
        (la1, lo1), (la2, lo2) = pts[i], pts[i + 1]
        h1 = FORECAST_HOURS[min(i, len(FORECAST_HOURS) - 1)]
        h2 = FORECAST_HOURS[min(i + 1, len(FORECAST_HOURS) - 1)]
        steps = max(1, int(distance_km(la1, lo1, la2, lo2) / 10))
        for k in range(1, steps + 1):
            f = k / steps
            d = distance_km(la1 + (la2 - la1) * f, lo1 + (lo2 - lo1) * f, lat, lon)
            if d < best[0]:
                best = (d, round(h1 + (h2 - h1) * f))
    return best


def fetch_bytes(url: str, timeout_s: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ExaCarib Connect hurricane watch (exacarib.com)"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:  # noqa: S310 (URLs from NHC's own feed)
        return r.read(8_000_000)


def load_tracks(doc: dict, get=fetch_bytes) -> dict[str, Track]:
    """NHC's track and cone for every storm that has them. Run outside a transaction.
    Only nhc.noaa.gov URLs are followed; a storm whose files fail keeps dead reckoning."""
    out: dict[str, Track] = {}
    for storm in parse(doc):
        if not storm.track_url.startswith("https://www.nhc.noaa.gov/"):
            continue
        try:
            points, _ = read_kml(get(storm.track_url))
            cone: list[tuple[float, float]] = []
            if storm.cone_url.startswith("https://www.nhc.noaa.gov/"):
                _, cone = read_kml(get(storm.cone_url))
        except Exception as e:  # noqa: BLE001 (any failure falls back to dead reckoning)
            log.warning("NHC track for %s unreadable (%s); using its current motion", storm.id, type(e).__name__)
            continue
        if len(points) >= 2:
            out[storm.id] = Track(points=points, cone=cone)
    return out


STORM_MODE_ADVICE = (
    "Consider switching Storm Mode on so the satellite path is warm before the terrestrial links suffer."
)


def assess(
    storm: Storm, site: str, lat: float, lon: float, policy: Policy = Policy(), track: Track | None = None
) -> dict | None:
    """The warning for one storm and one site, or None when it stays clear. With NHC's
    track it uses the official forecast and cone; without, the storm's current motion."""
    official = track is not None and len(track.points) >= 2
    if official:
        km, hours = track_approach(track, lat, lon)
        in_cone = bool(track.cone) and inside(lat, lon, track.cone)
    else:
        km, hours = closest_approach(storm, lat, lon, policy.horizon_h)
        in_cone = False
    critical = policy.critical_km_hurricane if storm.hurricane else policy.critical_km
    if km > policy.warning_km and not in_cone:
        return None
    severity = "critical" if km <= critical or in_cone else "warning"
    when = "now" if hours == 0 else f"in about {hours} h"
    if official:
        detail = (
            f"{storm.label} ({storm.intensity_kt:.0f} kt): on the NHC forecast track it passes about {km:.0f} km "
            f"from {site} {when}."
        )
        if in_cone:
            detail += f" {site} is inside the forecast cone."
    else:
        detail = (
            f"{storm.label} ({storm.intensity_kt:.0f} kt) is moving at {storm.speed_kmh:.0f} km/h on a heading of "
            f"{storm.heading_deg:.0f}°. Projecting that motion forward, it passes about {km:.0f} km from {site} {when}."
        )
    if severity == "critical":
        detail += " " + STORM_MODE_ADVICE
    if not official:
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
            "source": "nhc_track" if official else "dead_reckoning",
            "in_cone": in_cone,
        },
    }


def fetch(url: str, timeout_s: float = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "ExaCarib Connect hurricane watch (exacarib.com)"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:  # noqa: S310 (fixed, configured URL)
        return json.loads(r.read(4_000_000))


def example_feed() -> dict:
    """A made-up hurricane south-east of Jamaica, for demos. Marked as example data."""
    return json.loads(resources.files(__package__).joinpath("fixtures/nhc-example.json").read_text())


def summarise(storm: Storm, hits: list[tuple[dict, dict]]) -> dict:
    """One warning per storm for a customer, however many of its sites are in
    range: the most exposed site leads, the others are listed with theirs."""
    hits = sorted(hits, key=lambda h: (h[1]["severity"] != "critical", h[1]["data"]["closest_km"]))
    lead_site, lead = hits[0]
    w = {**lead, "data": dict(lead["data"]), "site_id": lead_site["id"]}
    w["severity"] = "critical" if any(h["severity"] == "critical" for _, h in hits) else "warning"
    w["data"]["sites"] = [
        {
            "id": str(s["id"]),
            "name": s["name"],
            "km": h["data"]["closest_km"],
            "hours": h["data"]["hours"],
            "severity": h["severity"],
            "suggest_storm_mode": h["data"]["suggest_storm_mode"],
        }
        for s, h in hits
    ]
    if len(hits) > 1:
        others = ", ".join(f"{s['name']} ({h['data']['closest_km']:.0f} km)" for s, h in hits[1:])
        w["title"] += f", and near {len(hits) - 1} more site{'s' if len(hits) > 2 else ''}"
        w["detail"] += f" Also in range: {others}."
    return w


def run_once(
    conn, doc: dict, example: bool = False, now: dt.datetime | None = None, tracks: dict[str, Track] | None = None
) -> int:
    """Raises or refreshes storm warnings for every customer, one per storm
    (not one per site, so a customer hears about a storm once); resolves the
    rest. Returns how many warnings are open."""
    storms = parse(doc)
    sites = conn.execute(
        """SELECT s.id, s.name, s.customer_id, s.latitude, s.longitude, c.storm_mode
           FROM sites s JOIN customers c ON c.id = s.customer_id
           WHERE s.latitude IS NOT NULL AND s.longitude IS NOT NULL ORDER BY s.name"""
    ).fetchall()
    by_customer: dict[Any, list[dict]] = {}
    for site in sites:
        by_customer.setdefault(site["customer_id"], []).append(site)
    for customer_id in open_customers(conn, "storm_warning", example):
        by_customer.setdefault(customer_id, [])
    seen: dict[Any, set[str]] = {c: set() for c in by_customer}
    for customer_id, csites in by_customer.items():
        for storm in storms:
            track = (tracks or {}).get(storm.id)
            hits = [
                (s, w) for s in csites if (w := assess(storm, s["name"], s["latitude"], s["longitude"], track=track))
            ]
            if not hits:
                continue
            w = summarise(storm, hits)
            note_storm_mode(conn, w, STORM_MODE_ADVICE)
            key = f"storm:{storm.id}" + (":example" if example else "")
            raise_insight(
                conn,
                customer_id,
                "storm_warning",
                key,
                w["severity"],
                ("Example data: " if example else "") + w["title"],
                w["detail"],
                w["data"],
                site_id=w["site_id"],
                example=example,
            )
            seen[customer_id].add(key)
    for customer_id, keys in seen.items():
        resolve_others(conn, customer_id, "storm_warning", keys, example=example)
    return sum(len(k) for k in seen.values())
