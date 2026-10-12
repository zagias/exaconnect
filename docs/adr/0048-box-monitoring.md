# 0048 Monitoring the box itself: SNMP, traps, syslog and a read-only API

Date: 2026-10-12
Status: accepted

## Context

Customers run their own monitoring (Zabbix, PRTG, SolarWinds, LibreNMS, Datadog)
and expect a branch box to be watched the way a FortiGate or Cisco router is:
SNMP polling, traps, syslog and an API. The controller already sends traps,
syslog, OpenTelemetry and Prometheus metrics about the whole network
(ADR 0026), but the box itself had none of these.

## Decision

The box console gets a Monitoring page (`#/monitoring`, `GET/POST /api/monitoring`).

- **SNMP polling**: net-snmp's `snmpd`, v2c (read-only community) and/or v3
  (one user, SHA authentication, AES privacy). Standard MIBs: MIB-II, IF-MIB,
  HOST-RESOURCES, UCD (load, disk). It answers on the office side and over
  Connect tunnels; the box firewall drops it on internet ports, and snmpd only
  answers the address ranges set (default: private addresses).
  `exacarib-box` ships a systemd drop-in so snmpd reads only
  `/etc/snmp/exacarib-snmpd.conf` and keeps the link up/down trap module Debian disables.
  snmpd stays off until someone saves monitoring.
- **Traps**: up to four destinations, v2c or v3. snmpd sends link up/down,
  cold start and authentication failure (disk and load are polled through
  UCD-SNMP: Debian ships without the MIB files snmpd's default threshold
  monitors need). Every agent event
  (BFD down/up, traffic moved, controller silent/back, a change made on the box,
  and the others) also goes out as an ExaCarib trap with `snmptrap`:
  `1.3.6.1.4.1.<PEN>.2.0.<n>` with kind, detail and box name in `.2.1.1` to
  `.2.1.3`. The controller's own traps stay under `.1`. The box has a fixed engine
  ID (shown in the console) for v3 traps.
- **Syslog**: rsyslog forwards the box's log (`*.info`) to one collector over
  UDP or TCP in RFC 5424 format, queued while the collector is away.
- **Read-only API keys**: up to ten named keys, shown once, stored as SHA-256
  hashes. `Authorization: Bearer exabox_…` reads `GET /api/overview`,
  `/api/network` and `/api/monitoring` from the office side. A key never writes.
- Secrets (community, v3 passwords) stay on the box in 0600 files and are never
  sent back to the browser; saving with an empty field keeps them.
- Every change is a box console change, reported to the controller like port changes.
- "Send a test trap and log message" checks the path end to end.

## Consequences

- The image test (`deploy/box/test-image.sh check`) polls v2c and v3, walks
  IF-MIB, checks a wrong community and the internet port get nothing, receives
  a trap and a syslog message, and reads with an API key on every image build.
- Seams: setting monitoring from the portal for many boxes at once (desired
  state), an SNMP MIB file for the ExaCarib traps, path latency/loss as SNMP
  values, syslog over TLS, NetFlow/IPFIX from the box.
