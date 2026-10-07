"""Hurricane watch with NHC's forecast track and cone (KMZ), and the fallback to dead reckoning."""

from __future__ import annotations

import io
import zipfile

import pytest

from exaconnect_controller.ai import storms

KINGSTON = (17.97, -76.79)
PORT_OF_SPAIN = (10.65, -61.51)

KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
{body}
</Document></kml>"""


def _kmz(body: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("al992026.kml", KML.format(body=body))
    return buf.getvalue()


def _point(lat, lon):
    return f"<Placemark><Point><coordinates>{lon},{lat},0</coordinates></Point></Placemark>"


# A track that turns north towards Port of Spain, unlike the storm's current westward motion.
TRACK = _kmz("".join(_point(*p) for p in [(15.9, -71.8), (14.5, -66.5), (12.5, -63.5), (10.9, -61.7), (12.0, -60.0)]))
CONE = _kmz(
    "<Placemark><Polygon><outerBoundaryIs><LinearRing><coordinates>"
    "-73,17.5 -66,16.5 -60,13.5 -59,10 -63,9.5 -68,12.5 -73,14.5 -73,17.5"
    "</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
)


def _feed():
    doc = storms.example_feed()
    doc["activeStorms"][0]["forecastTrack"] = {
        "kmzFile": "https://www.nhc.noaa.gov/storm_graphics/api/AL992026_TRACK_latest.kmz"
    }
    doc["activeStorms"][0]["trackCone"] = {
        "kmzFile": "https://www.nhc.noaa.gov/storm_graphics/api/AL992026_CONE_latest.kmz"
    }
    return doc


def _get(url):
    return TRACK if "TRACK" in url else CONE


def test_read_kmz_and_cone():
    points, ring = storms.read_kml(TRACK)
    assert points[0] == (15.9, -71.8) and len(points) == 5 and ring == []
    _, ring = storms.read_kml(CONE)
    assert storms.inside(*PORT_OF_SPAIN, ring) and not storms.inside(*KINGSTON, ring)


def test_the_official_track_replaces_dead_reckoning():
    doc = _feed()
    tracks = storms.load_tracks(doc, get=_get)
    hurricane = storms.parse(doc)[0]
    track = tracks[hurricane.id]
    # Dead reckoning sends it at Kingston; the official track and cone go to Port of Spain.
    assert storms.assess(hurricane, "site-b", *PORT_OF_SPAIN) is None
    w = storms.assess(hurricane, "site-b", *PORT_OF_SPAIN, track=track)
    assert w["severity"] == "critical" and w["data"]["in_cone"] is True and w["data"]["source"] == "nhc_track"
    assert "NHC forecast track" in w["detail"] and "inside the forecast cone" in w["detail"]
    assert w["data"]["closest_km"] < 60 and 30 <= w["data"]["hours"] <= 60
    kingston = storms.assess(hurricane, "site-a", *KINGSTON, track=track)
    assert kingston is None or kingston["severity"] == "warning"


def test_failures_fall_back_and_only_nhc_urls_are_followed():
    doc = _feed()

    def broken(url):
        raise OSError("no route")

    assert storms.load_tracks(doc, get=broken) == {}
    doc["activeStorms"][0]["forecastTrack"]["kmzFile"] = "https://evil.example/track.kmz"
    calls = []
    assert storms.load_tracks(doc, get=lambda u: calls.append(u) or TRACK) == {}
    assert calls == []
    # Without a track, the warning is the straight-line projection, as before.
    w = storms.assess(storms.parse(doc)[0], "site-a", *KINGSTON)
    assert w["data"]["source"] == "dead_reckoning" and "straight-line projection" in w["detail"]


def test_bad_kml_is_rejected():
    with pytest.raises(Exception):  # noqa: B017
        storms.read_kml(b"<kml><unclosed>")
