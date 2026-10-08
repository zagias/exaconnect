# ADR 0008: Disaster watch beyond hurricanes, with one alert per event

Date: 2026-10-03. Status: accepted.

## Context

The hurricane watch (ADR 0006) reads NHC only. Caribbean sites are also at
risk from earthquakes, tsunamis, volcanoes, floods and wildfires. Dudley
listed eight sources and asked that there be no duplicate notifications.

## Decision

Read three machine feeds every 10 minutes (`EXA_HAZARD_INTERVAL_S`), each
optional:

| Feed | What it gives | Used for |
| --- | --- | --- |
| USGS earthquakes, M4.5+ past day (GeoJSON) | magnitude, depth, PAGER alert, tsunami flag | earthquakes |
| GDACS event list (GeoJSON) | earthquakes, cyclones, floods, volcanoes, wildfires, tsunamis, with a green/orange/red impact colour | multi-hazard |
| tsunami.gov Atom: PTWC (`PHEB`) and NTWC (`PAAQ`) | tsunami messages with position and message type | tsunami |

Impact on a site, by straight-line distance from the reported location:

- Earthquake: within the felt radius 10^(0.43·M − 0.15) km (about 60 km at
  M4.5, 270 km at M6, 720 km at M7). Critical within half of it at M6+, or
  anywhere in it when USGS's PAGER alert is orange or red; warning at
  M5.5+ or PAGER yellow; otherwise a note. GDACS's colour can raise it.
- Tsunami: within 1,000 km. Warnings and threat messages are critical,
  advisories and watches a warning, information statements and final
  messages a note. Each message supersedes the one before.
- GDACS floods 150 km, volcanoes 100 km, wildfires 50 km, cyclones 500 km,
  at GDACS's colour (red critical, orange warning, green note).
- Droughts are left out: they do not take links down.

**No duplicates.** Reports are clustered into events before anything is
raised: earthquakes from different feeds within 100 km and 30 minutes; a
tsunami message within 300 km and 2 hours of an earthquake, or 6 hours of
another message. Cyclones in NHC's waters come from the hurricane watch
only. Each event raises one insight per customer that lists every site in
range, and the hurricane watch now does the same per storm (it was one per
storm per site). An insight remembers the report ids it covers, so it
carries on when the feeds reporting an event change. It reaches the event
timeline once, and again only if it gets more severe (which also clears
its acknowledgement). If an event drops out and comes back within a day,
the same insight reopens. A feed that cannot be read leaves its insights
as they are rather than clearing them.

Critical events suggest Storm Mode for the sites marked critical. Nothing
switches it on by itself.

## Not wired, and why

- **UWI Seismic Research Centre** and **CDEMA**: no documented machine
  feed. SRC's Eastern Caribbean earthquakes reach USGS and GDACS within
  minutes; CDEMA situation reports are for people, not polling.
- **NASA FIRMS**: needs a registered map key; GDACS already reports
  wildfires with an impact estimate. Can be added behind `EXA_FIRMS_KEY`.
- **Global Flood Monitoring**: GDACS floods draw on it.
- **ReliefWeb / UN OCHA**: needs a registered app name and reports after
  the event. Useful for context in "Ask your network" later.

## Consequences

The watch is a first warning, not an official one: every insight links the
source's report. Lab check m8 reads the live feeds from the lab host.
