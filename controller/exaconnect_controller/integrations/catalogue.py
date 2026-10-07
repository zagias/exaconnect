"""The Connect event catalogue (ADR 0026).

Every event Connect publishes has a CloudEvents type of
``com.exacarib.connect.<name>``, a default severity, and an action:

- ``trigger`` opens a problem (a path down, a node offline, an SLA breach),
- ``resolve`` closes the problem with the same dedup key (the path is back),
- ``notify`` is news that opens nothing (a routing move, Storm Mode on).

Alerting and ITSM connectors (PagerDuty, Opsgenie, ServiceNow, Jira) open
and close incidents on trigger and resolve; chat, webhooks and SIEMs get
everything they subscribe to. The AsyncAPI document and the portal's list
of event kinds are built from this table, so they cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass

TYPE_PREFIX = "com.exacarib.connect."
SOURCE = "/exacarib/connect"
SEVERITIES = ("info", "warning", "critical")
RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass(frozen=True)
class Kind:
    name: str
    title: str
    description: str
    severity: str
    action: str
    origin: str  # where it happens in the code, for the docs
    example: dict
    carrier: bool = False  # also sent to the carrier whose link it concerns
    in_wildcard: bool = True  # "*" subscriptions include it


KINDS: dict[str, Kind] = {
    k.name: k
    for k in [
        Kind(
            "path.down",
            "Path down",
            "BFD on a site's tunnel over one carrier link went down. Classes move off it within a second.",
            "critical",
            "trigger",
            "agent telemetry event bfd_down (api/agent.py)",
            {"site": "kingston", "path": "carrier-a", "path_label": "Carrier A", "carrier": "Carrier A Ltd"},
            carrier=True,
        ),
        Kind(
            "path.up",
            "Path up",
            "BFD on the tunnel came back. Resolves the matching path.down.",
            "info",
            "resolve",
            "agent telemetry event bfd_up (api/agent.py)",
            {"site": "kingston", "path": "carrier-a", "path_label": "Carrier A", "carrier": "Carrier A Ltd"},
            carrier=True,
        ),
        Kind(
            "sla.breach_forecast",
            "SLA breach forecast",
            "The routing engine forecast a class's SLA breach on its path and moved it before it happened.",
            "warning",
            "notify",
            "routing decision with a forecast reason (routing/runner.py)",
            {
                "site": "kingston",
                "class": "voice",
                "path": "carrier-a",
                "metric": "loss",
                "now": 0.6,
                "forecast": 1.3,
                "limit": 1.0,
                "reason": "Moved voice from Carrier A to Carrier B: loss on Carrier A rising 0.4% a minute, "
                "forecast 1.3% in 60 s against a 1% SLA.",
            },
        ),
        Kind(
            "sla.breach",
            "SLA breach",
            "A class is outside its SLA now, either while moving or because no other path is within SLA.",
            "critical",
            "trigger",
            "routing decision with a breach now, or a hold with no path left (routing/runner.py)",
            {"site": "kingston", "class": "voice", "path": "carrier-a", "metric": "loss", "now": 1.4, "limit": 1.0},
            carrier=True,
        ),
        Kind(
            "routing.moved",
            "Routing move",
            "The engine moved a class to another path (move, move back or failover), with the plain-English reason. "
            "Resolves an open SLA breach for that class at that site.",
            "info",
            "resolve",
            "decisions table (routing/runner.py)",
            {
                "site": "kingston",
                "class": "voice",
                "kind": "move",
                "from_path": "carrier-a",
                "to_path": "carrier-b",
                "shadow": False,
                "reason": "Moved voice from Carrier A to Carrier B: loss on Carrier A rising 0.4% a minute, "
                "forecast 1.3% in 60 s against a 1% SLA.",
            },
        ),
        Kind(
            "storm.on",
            "Storm Mode on",
            "Storm Mode was switched on for a site: satellite warm, thresholds tightened.",
            "warning",
            "notify",
            "storm/service.py",
            {"site": "kingston", "by": "user:ops@example.org"},
        ),
        Kind(
            "storm.off",
            "Storm Mode off",
            "Storm Mode was switched off for a site.",
            "info",
            "notify",
            "storm/service.py",
            {"site": "kingston", "by": "user:ops@example.org"},
        ),
        Kind(
            "hazard.alert",
            "Hazard alert",
            "A hurricane, earthquake, tsunami or other hazard threatens one of your sites (NHC, USGS, GDACS).",
            "warning",
            "trigger",
            "insights of kind hazard or storm_warning (ai/hazards.py, ai/storms.py)",
            {"insight": 41, "kind": "hazard", "title": "Tropical Storm Ana: Kingston inside the cone", "site": "kingston"},
        ),
        Kind(
            "insight.raised",
            "Insight",
            "An AI insight: a bill-shock forecast or a carrier anomaly.",
            "info",
            "notify",
            "insights of kind bill_shock or anomaly (ai/billshock.py, ai/anomaly.py)",
            {"insight": 42, "kind": "anomaly", "title": "Carrier B latency is unusual at Montego Bay"},
        ),
        Kind(
            "node.enrolled",
            "Node enrolled",
            "An edge agent enrolled with a one-time token and got its certificate.",
            "info",
            "notify",
            "api/agent.py enrol",
            {"node": "kingston", "site": "kingston"},
        ),
        Kind(
            "node.revoked",
            "Node revoked",
            "An admin revoked a node's certificate; it can no longer reach the controller.",
            "warning",
            "notify",
            "api/admin.py revoke",
            {"node": "kingston", "site": "kingston", "by": "user:admin@example.org"},
        ),
        Kind(
            "node.offline",
            "Node offline",
            "An edge agent has not reported for 90 seconds. It keeps forwarding on its last map.",
            "critical",
            "trigger",
            "integrations tick (integrations/publish.py)",
            {"node": "kingston", "site": "kingston", "last_seen": "2026-10-07T12:00:00Z"},
        ),
        Kind(
            "node.online",
            "Node back online",
            "The edge agent is reporting again. Resolves node.offline.",
            "info",
            "resolve",
            "integrations tick (integrations/publish.py)",
            {"node": "kingston", "site": "kingston", "offline_s": 340},
        ),
        Kind(
            "config.apply_failed",
            "Desired state failed to apply",
            "An agent could not apply a new desired state and rolled back to the last good one.",
            "warning",
            "notify",
            "api/agent.py status",
            {"node": "kingston", "site": "kingston", "version": 12, "error": "frr-reload: syntax error"},
        ),
        Kind(
            "ddos.blocked",
            "DDoS source blocked",
            "The PoP blocked a flooding source automatically.",
            "warning",
            "notify",
            "internet.py record (PoP telemetry)",
            {"node": "pop-miami", "address": "198.51.100.7", "expires_s": 600},
        ),
        Kind(
            "carrier.fault",
            "Carrier fault notice",
            "A carrier reported a fault on links you use.",
            "critical",
            "trigger",
            "integrations/notices.py",
            {
                "notice": 7,
                "carrier": "Carrier A Ltd",
                "title": "Fibre cut near Spanish Town",
                "site": "kingston",
                "links": [{"id": "6f1c...", "path": "carrier-a"}],
            },
            carrier=True,
        ),
        Kind(
            "carrier.maintenance",
            "Carrier maintenance notice",
            "A carrier scheduled planned maintenance on links you use. Connect moves classes off them when it starts.",
            "warning",
            "notify",
            "integrations/notices.py",
            {
                "notice": 8,
                "carrier": "Carrier A Ltd",
                "title": "Core router upgrade",
                "site": "kingston",
                "starts_at": "2026-10-10T02:00:00Z",
                "ends_at": "2026-10-10T04:00:00Z",
            },
            carrier=True,
        ),
        Kind(
            "carrier.notice_resolved",
            "Carrier notice resolved",
            "The carrier resolved, closed or cancelled its notice. Resolves carrier.fault.",
            "info",
            "resolve",
            "integrations/notices.py",
            {"notice": 7, "carrier": "Carrier A Ltd", "status": "resolved", "site": "kingston"},
            carrier=True,
        ),
        Kind(
            "maintenance.started",
            "Maintenance window started",
            "A planned maintenance window started; classes were moved off the affected links beforehand.",
            "warning",
            "trigger",
            "integrations tick (integrations/publish.py)",
            {"notice": 8, "carrier": "Carrier A Ltd", "site": "kingston", "ends_at": "2026-10-10T04:00:00Z"},
            carrier=True,
        ),
        Kind(
            "maintenance.ended",
            "Maintenance window ended",
            "The maintenance window ended. Classes move back after the usual hold time.",
            "info",
            "resolve",
            "integrations tick (integrations/publish.py)",
            {"notice": 8, "carrier": "Carrier A Ltd", "site": "kingston"},
            carrier=True,
        ),
        Kind(
            "audit.recorded",
            "Audit record",
            "Every write through the API, for SIEM and syslog. Only sent when subscribed to by name.",
            "info",
            "notify",
            "audit_log (audit.py)",
            {"actor": "user:ops@example.org", "action": "class.upsert", "target": "voice", "detail": {}},
            in_wildcard=False,
        ),
        Kind(
            "test.ping",
            "Test",
            "Sent by Test send in the portal or the API. Safe to ignore.",
            "info",
            "notify",
            "integrations API",
            {"message": "This is a test from ExaCarib Connect."},
        ),
    ]
}


def type_of(name: str) -> str:
    return TYPE_PREFIX + name


def name_of(ce_type: str) -> str:
    return ce_type.removeprefix(TYPE_PREFIX)


def matches(patterns: list[str] | tuple[str, ...], name: str) -> bool:
    """`*` (everything but audit), an exact name, or a prefix like `path.*`."""
    kind = KINDS.get(name)
    for p in patterns:
        if p == "*" and (kind is None or kind.in_wildcard):
            return True
        if p == name or (p.endswith(".*") and name.startswith(p[:-1])):
            return True
    return False


def valid_patterns(patterns: list[str]) -> list[str]:
    """Checks subscription patterns; returns the bad ones."""
    groups = {n.split(".")[0] for n in KINDS}
    bad = []
    for p in patterns:
        if p == "*" or p in KINDS:
            continue
        if p.endswith(".*") and p[:-2] in groups:
            continue
        bad.append(p)
    return bad


def public() -> list[dict]:
    return [
        {
            "name": k.name,
            "type": type_of(k.name),
            "title": k.title,
            "description": k.description,
            "severity": k.severity,
            "action": k.action,
            "sent_to_carriers": k.carrier,
            "in_wildcard": k.in_wildcard,
            "example": k.example,
        }
        for k in KINDS.values()
    ]
