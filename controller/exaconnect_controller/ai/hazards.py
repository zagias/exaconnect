"""Disaster watch: earthquakes, tsunamis, floods, volcanoes and wildfires
near a customer's sites (ADR 0009). The hurricane watch (storms.py) stays
the source for tropical cyclones.

Feeds, each optional (an empty URL turns one off):

- USGS earthquakes (GeoJSON, magnitude 4.5 and up, past day).
- GDACS, the UN/EU multi-hazard alerts (GeoJSON): earthquakes, floods,
  volcanoes, wildfires, tsunamis, and cyclones outside NHC's waters.
- tsunami.gov message feeds (Atom) from the Pacific Tsunami Warning Center,
  which covers the Caribbean, and the National Tsunami Warning Center,
  which covers Puerto Rico and the US Virgin Islands.

No duplicate notifications. Several feeds often report one event: a
Caribbean earthquake comes from USGS and GDACS, and PTWC then sends several
tsunami messages about it. Reports are clustered into one event first
(earthquakes within 100 km and 30 minutes; a tsunami message within 300 km
and 2 hours of an earthquake, or of another message), and each event
raises one insight per customer that lists every affected site. An event
keeps its insight even when the feeds that report it change from one pass
to the next, and an insight reaches the timeline only when it is new or
gets more severe (insights.py).

Distances are straight lines from the reported epicentre or centre; each
insight links the source's own report.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from .insights import RANK, raise_insight, resolve_others
from .storms import distance_km

log = logging.getLogger("exaconnect.hazards")

USER_AGENT = "ExaConnect disaster watch (exacarib.com)"
MAX_BYTES = 30_000_000
KIND_WORD = {
    "earthquake": "Earthquake",
    "tsunami": "Tsunami",
    "cyclone": "Tropical cyclone",
    "flood": "Flood",
    "volcano": "Volcano",
    "wildfire": "Wildfire",
}
# GDACS event types we watch, and the radius (km) an event can hurt a site's links.
# Droughts are left out: they do not take links down.
GDACS_KINDS = {"EQ": "earthquake", "TC": "cyclone", "FL": "flood", "VO": "volcano", "WF": "wildfire", "TS": "tsunami"}
RADIUS_KM = {"cyclone": 500, "flood": 150, "volcano": 100, "wildfire": 50, "tsunami": 1000}
GDACS_SEVERITY = {"red": "critical", "orange": "warning", "green": "info"}
SOURCE_NAME = {"usgs": "USGS", "gdacs": "GDACS", "ptwc": "tsunami.gov"}
SOURCE_ORDER = {"usgs": 0, "gdacs": 1, "ptwc": 2}
# How long a report stays relevant after it is issued.
MAX_AGE = {"earthquake": dt.timedelta(hours=48), "tsunami": dt.timedelta(hours=24)}


@dataclass(frozen=True)
class Report:
    """One report of a hazard from one feed."""

    id: str  # "<source>:<feed id>", unique across feeds
    source: str  # usgs | gdacs | ptwc
    kind: str
    title: str
    lat: float
    lon: float
    time: dt.datetime
    url: str = ""
    magnitude: float | None = None
    alert: str = ""  # USGS PAGER or GDACS colour: green, yellow, orange, red
    severity: str | None = None  # set by the feed (GDACS colour, PTWC message type)
    tsunami_flag: bool = False

    @property
    def radius_km(self) -> float:
        if self.kind == "earthquake" and self.magnitude:
            return felt_radius_km(self.magnitude)
        return RADIUS_KM.get(self.kind, 100)


@dataclass
class Event:
    reports: list[Report] = field(default_factory=list)

    @property
    def lead(self) -> Report:
        """The report the event is named after: an earthquake over its
        tsunami messages, USGS over GDACS."""
        return min(self.reports, key=lambda r: (r.kind == "tsunami", SOURCE_ORDER[r.source], r.time))

    @property
    def kind(self) -> str:
        return self.lead.kind

    def latest_tsunami(self) -> Report | None:
        msgs = [r for r in self.reports if r.kind == "tsunami"]
        return max(msgs, key=lambda r: r.time) if msgs else None


def felt_radius_km(magnitude: float) -> float:
    """Roughly how far strong enough shaking reaches to trouble power and
    links: about 70 km at M4.5, 270 km at M6 and 720 km at M7."""
    return 10 ** (0.43 * magnitude - 0.15)


def _when(v: Any) -> dt.datetime:
    if isinstance(v, int | float):  # USGS: milliseconds since the epoch
        return dt.datetime.fromtimestamp(v / 1000, dt.UTC)
    t = dt.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


# ---- Feeds ----


def parse_usgs(doc: dict) -> list[Report]:
    out = []
    for f in doc.get("features") or []:
        try:
            p = f["properties"]
            lon, lat = f["geometry"]["coordinates"][:2]
            mag = float(p["mag"])
            out.append(
                Report(
                    id=f"usgs:{f['id']}",
                    source="usgs",
                    kind="earthquake",
                    title=f"Magnitude {mag:.1f} earthquake, {p.get('place') or 'location not named'}",
                    lat=float(lat),
                    lon=float(lon),
                    time=_when(p["time"]),
                    url=str(p.get("url") or ""),
                    magnitude=mag,
                    alert=str(p.get("alert") or "").lower(),
                    tsunami_flag=bool(p.get("tsunami")),
                )
            )
        except (KeyError, TypeError, ValueError):
            log.warning("skipped a USGS entry in an unexpected shape")
    return out


def _points(coords: Any) -> list[list[float]]:
    if isinstance(coords[0], int | float):
        return [coords]
    return [p for c in coords for p in _points(c)]


def _centre(geometry: dict) -> tuple[float, float]:
    """(lat, lon) of a GeoJSON point, or the middle of a shape's bounding box."""
    pts = _points(geometry["coordinates"])
    lons = [float(p[0]) for p in pts]
    lats = [float(p[1]) for p in pts]
    return (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2


def parse_gdacs(doc: dict) -> list[Report]:
    """GDACS lists an event's point and its impact shapes as separate
    features; one report per event, from its point when there is one."""
    best: dict[str, tuple[bool, Report]] = {}
    for f in doc.get("features") or []:
        try:
            p = f["properties"]
            kind = GDACS_KINDS.get(str(p.get("eventtype", "")).upper())
            if kind is None or str(p.get("iscurrent", "true")).lower() == "false":
                continue
            lat, lon = _centre(f["geometry"])
            colour = str(p.get("alertlevel") or "").lower()
            sev = p.get("severitydata") or {}
            mag = None
            if kind == "earthquake" and sev.get("severity") is not None:
                mag = float(sev["severity"])
            name = str(p.get("name") or p.get("eventname") or "").strip()
            title = str(p.get("description") or name or KIND_WORD[kind])
            url = p.get("url") or {}
            r = Report(
                id=f"gdacs:{p['eventtype']}{p['eventid']}",
                source="gdacs",
                kind=kind,
                title=title,
                lat=lat,
                lon=lon,
                time=_when(p.get("fromdate") or p.get("datemodified")),
                url=str(url.get("report", "") if isinstance(url, dict) else url),
                magnitude=mag,
                alert=colour,
                severity=GDACS_SEVERITY.get(colour, "info"),
            )
            is_point = f["geometry"].get("type") == "Point"
            if r.id not in best or (is_point and not best[r.id][0]):
                best[r.id] = (is_point, r)
        except (KeyError, TypeError, ValueError, IndexError):
            log.warning("skipped a GDACS entry in an unexpected shape")
    return [r for _, r in best.values()]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def tsunami_severity(text: str) -> str:
    """From the message type: warnings and threat messages are critical, a
    final message (the threat has passed) and information statements are not."""
    t = text.lower()
    if "final" in t:
        return "info"
    if "warning" in t or "threat" in t:
        return "critical"
    if "advisory" in t or "watch" in t:
        return "warning"
    return "info"


def parse_tsunami(xml_text: str, feed: str = "ptwc") -> list[Report]:
    """tsunami.gov Atom feeds: one entry per message, with geo:lat/geo:long."""
    if re.search(r"<!(DOCTYPE|ENTITY)", xml_text, re.IGNORECASE):  # never expand entities from a feed
        raise ValueError("tsunami feed carries a DTD; refusing it")
    out = []
    for entry in ET.fromstring(xml_text):
        if _local(entry.tag) != "entry":
            continue
        try:
            fields: dict[str, Any] = {}
            for child in entry:
                name = _local(child.tag)
                if name == "link" and child.get("href") and "url" not in fields:
                    fields["url"] = child.get("href")
                else:
                    fields[name] = "".join(child.itertext()).strip()
            title = fields.get("title", "Tsunami message")
            summary = re.sub(r"\s+", " ", fields.get("summary", ""))
            m = re.search(r"Category:\s*(\w+)", summary)
            category = m.group(1) if m else ""
            out.append(
                Report(
                    id=f"{feed}:{fields.get('id') or title + fields.get('updated', '')}",
                    source="ptwc",
                    kind="tsunami",
                    title=title,
                    lat=float(fields["lat"]),
                    lon=float(fields["long"]),
                    time=_when(fields.get("updated") or fields.get("published")),
                    url=str(fields.get("url", "")),
                    severity=tsunami_severity(category + " " + title),
                )
            )
        except (KeyError, TypeError, ValueError):
            log.warning("skipped a tsunami message in an unexpected shape")
    return out


def fetch(url: str, timeout_s: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, */*"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:  # noqa: S310 (fixed, configured URLs)
        body = r.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError(f"feed is larger than {MAX_BYTES // 1_000_000} MB")
    return body


# The last error per feed, for the lab check and the logs.
last_errors: dict[str, str] = {}


def read_feeds(usgs_url: str, gdacs_url: str, tsunami_urls: list[str]) -> tuple[list[Report], set[str]]:
    """Every configured feed's reports, and the sources that could not be read
    (whose open insights then stay as they are rather than clearing)."""
    reports: list[Report] = []
    failed: set[str] = set()
    jobs: list[tuple[str, str, Any]] = []
    if usgs_url:
        jobs.append(("usgs", usgs_url, lambda b: parse_usgs(json.loads(b))))
    if gdacs_url:
        jobs.append(("gdacs", gdacs_url, lambda b: parse_gdacs(json.loads(b))))
    for i, u in enumerate(tsunami_urls):
        jobs.append(("ptwc", u, lambda b, i=i: parse_tsunami(b.decode("utf-8", "replace"), f"ptwc{i}")))
    for source, url, parse in jobs:
        try:
            reports += parse(fetch(url))
        except Exception as e:  # one feed down must not hide the others
            log.warning("%s feed %s: %s", source, url, e)
            last_errors[source] = f"{type(e).__name__}: {e}"[:300]
            failed.add(source)
    return reports, failed


# ---- Clustering ----


def _joins(r: Report, other: Report) -> bool:
    """Whether two reports are the same event."""
    km = distance_km(r.lat, r.lon, other.lat, other.lon)
    gap = abs((r.time - other.time).total_seconds())
    kinds = {r.kind, other.kind}
    if kinds == {"earthquake"}:
        return r.source != other.source and km <= 100 and gap <= 1800
    if kinds == {"earthquake", "tsunami"}:
        return km <= 300 and gap <= 7200
    if kinds == {"tsunami"}:
        return km <= 300 and gap <= 6 * 3600
    # Other kinds come from GDACS alone, which already gives each event one id.
    return False


def cluster(reports: list[Report]) -> list[Event]:
    """Groups reports of the same event. Earthquakes first, so tsunami
    messages attach to the quake that caused them."""
    events: list[Event] = []
    for r in sorted(reports, key=lambda r: (r.kind == "tsunami", SOURCE_ORDER[r.source], r.time)):
        home = next((e for e in events if any(_joins(r, o) for o in e.reports)), None)
        if home is None:
            events.append(Event([r]))
        else:
            home.reports.append(r)
    return events


def nhc_covers(r: Report) -> bool:
    """NHC's waters (North Atlantic, Caribbean, eastern North Pacific), where
    the hurricane watch already reports cyclones."""
    return r.kind == "cyclone" and r.lat > 0 and -180 <= r.lon <= 0


def current(reports: list[Report], now: dt.datetime, nhc_on: bool) -> list[Report]:
    return [
        r
        for r in reports
        if now - r.time <= MAX_AGE.get(r.kind, dt.timedelta(days=14)) and not (nhc_on and nhc_covers(r))
    ]


# ---- Impact ----


def report_impact(r: Report, km: float) -> str | None:
    """The severity one report means for a site km away, or None."""
    if km > r.radius_km:
        return None
    if r.kind == "earthquake":
        pager = r.alert if r.source == "usgs" else ""
        if pager in ("orange", "red") or ((r.magnitude or 0) >= 6 and km <= r.radius_km / 2):
            sev = "critical"
        elif (r.magnitude or 0) >= 5.5 or pager == "yellow":
            sev = "warning"
        else:
            sev = "info"
        if r.source == "gdacs" and r.severity:  # GDACS's colour is its impact estimate
            sev = max(sev, r.severity, key=RANK.get)
        return sev
    return r.severity or "info"


def site_impact(event: Event, lat: float, lon: float) -> tuple[str, float] | None:
    """(severity, km from the nearest report) for one site, or None. Tsunami
    messages supersede each other, so only the latest counts."""
    latest = event.latest_tsunami()
    best: tuple[str, float] | None = None
    for r in event.reports:
        if r.kind == "tsunami" and r is not latest:
            continue
        km = distance_km(r.lat, r.lon, lat, lon)
        sev = report_impact(r, km)
        if sev and (best is None or (RANK[sev], -km) > (RANK[best[0]], -best[1])):
            best = (sev, km)
    return best


def describe(event: Event, hits: list[tuple[dict, str, float]]) -> dict:
    """The insight for one event and one customer's affected sites."""
    lead = event.lead
    hits = sorted(hits, key=lambda h: (-RANK[h[1]], h[2]))
    site, severity, km = hits[0]
    severity = max((h[1] for h in hits), key=RANK.get)
    tsunami = event.latest_tsunami()
    name = lead.title
    title = f"{name}: {km:.0f} km from {site['name']}"
    if len(hits) > 1:
        title += f", and near {len(hits) - 1} more site{'s' if len(hits) > 2 else ''}"
    sources = sorted({r.source for r in event.reports}, key=SOURCE_ORDER.get)
    detail = [f"{name}, reported by {' and '.join(SOURCE_NAME[s] for s in sources)}."]
    if tsunami is not None and tsunami is not lead:
        detail.append(f"Latest tsunami message: {tsunami.title}.")
    elif lead.tsunami_flag and tsunami is None:
        detail.append("USGS flags it as a large ocean earthquake: watch tsunami.gov for any tsunami message.")
    detail.append("Sites in range: " + ", ".join(f"{s['name']} {k:.0f} km ({sev})" for s, sev, k in hits) + ".")
    if severity == "critical":
        detail.append(
            "Consider switching Storm Mode on for the sites marked critical, so the satellite path is warm "
            "if terrestrial links fail."
        )
    detail.append("Distances are straight lines from the reported location; check the source's report.")
    return {
        "severity": severity,
        "site_id": site["id"],
        "title": title,
        "detail": " ".join(detail),
        "data": {
            "hazard": event.kind,
            "sources": sources,
            "member_ids": sorted(r.id for r in event.reports),
            "position": [lead.lat, lead.lon],
            "time": lead.time.isoformat(),
            "magnitude": lead.magnitude,
            "sites": [
                {
                    "id": str(s["id"]),
                    "name": s["name"],
                    "km": round(k, 1),
                    "severity": sev,
                    "suggest_storm_mode": sev == "critical",
                }
                for s, sev, k in hits
            ],
            "suggest_storm_mode": severity == "critical",
            "links": [
                {"label": f"{SOURCE_NAME[r.source]}: {r.title}"[:120], "url": r.url}
                for r in sorted(event.reports, key=lambda r: (SOURCE_ORDER[r.source], r.time))
                if r.url
            ][:6],
        },
    }


# ---- Pass ----


def _known_keys(conn, customer_id: Any, example: bool) -> dict[str, str]:
    """Report id -> insight key, for this customer's open and recently
    cleared disaster insights, so an event keeps its insight."""
    rows = conn.execute(
        """SELECT key, data->'member_ids' AS ids FROM insights
           WHERE customer_id = %s AND kind = 'hazard' AND example = %s
             AND (resolved_at IS NULL OR resolved_at > now() - interval '24 hours')
           ORDER BY resolved_at IS NULL, first_seen""",
        (customer_id, example),
    ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        for rid in row["ids"] or []:
            out[rid] = row["key"]
    return out


def run_once(
    conn,
    reports: list[Report],
    failed: set[str] | None = None,
    example: bool = False,
    nhc_on: bool = True,
    now: dt.datetime | None = None,
) -> int:
    """Raises or refreshes one insight per event per customer and resolves the
    rest. Returns how many are open."""
    now = now or dt.datetime.now(dt.UTC)
    events = cluster(current(reports, now, nhc_on))
    sites = conn.execute(
        """SELECT s.id, s.name, s.customer_id, s.latitude, s.longitude FROM sites s
           WHERE s.latitude IS NOT NULL AND s.longitude IS NOT NULL ORDER BY s.name"""
    ).fetchall()
    by_customer: dict[Any, list[dict]] = {}
    for s in sites:
        by_customer.setdefault(s["customer_id"], []).append(s)
    suffix = ":example" if example else ""
    total = 0
    for customer_id, csites in by_customer.items():
        known = _known_keys(conn, customer_id, example)
        seen: set[str] = set()
        for event in events:
            hits = []
            for s in csites:
                hit = site_impact(event, s["latitude"], s["longitude"])
                if hit:
                    hits.append((s, hit[0], hit[1]))
            if not hits:
                continue
            key = next((known[r.id] for r in event.reports if r.id in known and known[r.id] not in seen), None)
            key = key or f"hazard:{event.lead.id}{suffix}"
            if key in seen:  # two events claiming one key: give the second its own
                key = f"hazard:{event.lead.id}{suffix}"
            w = describe(event, hits)
            raise_insight(
                conn,
                customer_id,
                "hazard",
                key,
                w["severity"],
                ("Example data: " if example else "") + w["title"],
                w["detail"],
                w["data"],
                site_id=w["site_id"],
                example=example,
            )
            seen.add(key)
        # A feed that could not be read this pass keeps its insights open.
        if failed:
            for row in conn.execute(
                """SELECT key, data->'sources' AS sources FROM insights
                   WHERE customer_id = %s AND kind = 'hazard' AND example = %s AND resolved_at IS NULL""",
                (customer_id, example),
            ):
                if set(row["sources"] or []) & failed:
                    seen.add(row["key"])
        resolve_others(conn, customer_id, "hazard", seen, example=example)
        total += len(seen)
    return total


# ---- Example data ----


def example_reports(now: dt.datetime | None = None) -> list[Report]:
    """A made-up magnitude 6.4 earthquake off Trinidad, as USGS, GDACS and PTWC
    would each report it (one event, so one insight), and a far-away flood.
    Times are moved to just now. Marked as example data."""
    now = now or dt.datetime.now(dt.UTC)
    files = resources.files(__package__).joinpath("fixtures")
    reports = (
        parse_usgs(json.loads(files.joinpath("usgs-example.json").read_text()))
        + parse_gdacs(json.loads(files.joinpath("gdacs-example.json").read_text()))
        + parse_tsunami(files.joinpath("ptwc-example.xml").read_text())
    )
    quake = max(r.time for r in reports if r.source == "usgs")
    return [dataclasses.replace(r, time=now - dt.timedelta(minutes=50) + (r.time - quake)) for r in reports]
