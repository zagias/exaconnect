"""TM Forum and MEF LSO Sonata shapes, mapped onto Connect (ADR 0026).

- TMF621 Trouble Ticket v4: carriers' faults and maintenance (onto
  notices.py) and customers' tickets to ExaCarib.
- TMF622 Product Ordering v4 and MEF LSO Sonata (POQ, Quote, Product
  Order, Trouble Ticket): onto plain-English ordering (ordering.py), the
  fabric (fabric.py) and the cloud on-ramp adapters.
- TMF688 Event v4: the hub for listeners (REST hooks) and inbound events
  from carriers.

Product offerings, by id, with characteristics in camelCase:

  cloud-circuit       provider, region, site, bandwidthMbps, cloudPrefixes, className, name,
                      peerAddress, peerAsn, psk, insideCidr
  site-circuit        aSite, bSite, aVlan, bVlan, bandwidthMbps, name
  partner-connection  partner, site, bandwidthMbps
  bandwidth-change    circuit, bandwidthMbps
  internet-breakout   site, mode (pop, local or off)
  cloud-onramp        provider (aws_dx, azure_er, gcp_pi, megaport), site, bandwidthMbps, name, and the
                      provider's own fields (awsAccountId, region, circuitId, serviceKey, pairingKey,
                      bEndProductUid)
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import ordering

OFFERINGS = {
    "cloud-circuit": ("cloud_circuit", "Cloud circuit (IPsec with BGP to a cloud VPN gateway)"),
    "site-circuit": ("site_circuit", "Layer 2 circuit between two sites"),
    "partner-connection": ("partner_connection", "Private connection to a partner in the directory"),
    "bandwidth-change": ("bandwidth", "Change a circuit's bandwidth"),
    "internet-breakout": ("internet_mode", "Where a site's internet traffic leaves"),
    "cloud-onramp": (
        "onramp",
        "Dedicated cloud on-ramp (Direct Connect, ExpressRoute, Partner Interconnect, Megaport)",
    ),
}
NEEDS = ("peer_address", "peer_asn", "psk", "inside_cidr")  # given at confirmation, never stored in the order


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def iso(t: Any) -> str | None:
    if t is None:
        return None
    if isinstance(t, dt.datetime):
        return t.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    return str(t)


def parse_time(v: Any) -> dt.datetime | None:
    if not v:
        return None
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{v} is not an ISO 8601 date and time.") from None
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


class StandardsError(ValueError):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


# ---- products -------------------------------------------------------------------


def to_action(offering_id: str, config: dict, action: str = "add") -> tuple[dict, dict]:
    """One product (offering + configuration) to an ordering action and its confirmation inputs."""
    if offering_id not in OFFERINGS:
        raise StandardsError(f"Unknown product offering {offering_id}. Offerings: {', '.join(OFFERINGS)}.", 422)
    kind = OFFERINGS[offering_id][0]
    if action not in ("add", "modify") or (action == "modify") != (kind == "bandwidth"):
        raise StandardsError(f"{offering_id} is ordered with action {'modify' if kind == 'bandwidth' else 'add'}.", 422)
    a: dict[str, Any] = {"action": kind}
    inputs: dict[str, Any] = {}
    for k, v in config.items():
        if k.startswith("@"):
            continue
        key = snake(k)
        if key in NEEDS:
            inputs[key] = v
        else:
            a[key] = v
    return a, inputs


def characteristics(chars: list[dict] | None) -> dict:
    """TMF productCharacteristic [{name, value}] to a dict."""
    return {str(c.get("name")): c.get("value") for c in chars or [] if c.get("name")}


def review_one(conn: psycopg.Connection, customer_id: Any, a: dict) -> dict:
    return ordering.review(conn, customer_id, [a])


def place(
    conn: psycopg.Connection, customer_id: Any, actions: list[dict], inputs: list[dict], actor: str, text: str
) -> dict:
    """Create and confirm an order. Returns the orders row (status done, pending_partner) or raises."""
    r = ordering.review(conn, customer_id, actions)
    if r["problems"]:
        raise StandardsError("; ".join(r["problems"]), 422)
    oid = ordering.create(conn, customer_id, actions, "form", text, actor)
    order = conn.execute("SELECT * FROM orders WHERE id = %s FOR UPDATE", (oid,)).fetchone()
    try:
        ordering.confirm(conn, order, inputs, actor)
    except ordering.OrderError as e:
        raise StandardsError(str(e), 422) from None
    return conn.execute("SELECT * FROM orders WHERE id = %s", (oid,)).fetchone()


def store(
    conn, kind: str, customer_id: Any, state: str, body: dict, order_id: int | None = None, doc_id: str = ""
) -> dict:
    doc_id = doc_id or str(uuid.uuid4())
    body = {**body, "id": doc_id}
    conn.execute(
        """INSERT INTO connect_std_documents (id, kind, customer_id, state, body, order_id)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (doc_id, kind, customer_id, state, Jsonb(body), order_id),
    )
    return body


def load(conn, kind: str, customer_id: Any, doc_id: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM connect_std_documents WHERE kind = %s AND id = %s AND (%s::uuid IS NULL OR customer_id = %s)",
        (kind, doc_id, customer_id, customer_id),
    ).fetchone()


ORDER_STATE_TMF = {
    "draft": "acknowledged",
    "done": "completed",
    "pending_partner": "inProgress",
    "cancelled": "cancelled",
    "failed": "failed",
}


# ---- TMF621 ---------------------------------------------------------------------

TT_STATUS = {
    "scheduled": "pending",
    "open": "acknowledged",
    "in_progress": "inProgress",
    "resolved": "resolved",
    "closed": "closed",
    "cancelled": "cancelled",
}
TT_STATUS_IN = {v: k for k, v in TT_STATUS.items()} | {"held": "in_progress", "rejected": "cancelled"}
SEV_IN = {"critical": "critical", "major": "critical", "minor": "warning", "warning": "warning", "info": "info"}
SEV_OUT = {"critical": "Critical", "warning": "Minor", "info": "Info"}


def ticket_type(kind: str) -> str:
    return {"maintenance": "Maintenance", "fault": "Fault", "trouble": "Incident"}[kind]


def ticket(n: dict, base: str) -> dict:
    """A notice or customer ticket as a TMF621 TroubleTicket."""
    out: dict[str, Any] = {
        "id": str(n["id"]),
        "href": f"{base}/{n['id']}",
        "name": n["title"],
        "description": n["description"],
        "severity": SEV_OUT.get(n["severity"], "Minor"),
        "ticketType": ticket_type(n["kind"]),
        "status": TT_STATUS.get(n["status"], "acknowledged"),
        "creationDate": iso(n["created_at"]),
        "lastUpdate": iso(n["updated_at"]),
        "resolutionDate": iso(n["resolved_at"]),
        "externalIdentifier": [{"id": n["external_id"], "owner": "carrier"}] if n["external_id"] else [],
        "relatedEntity": [
            {"id": str(x), "role": "affectedLink", "@referredType": "Link", "name": str(x)} for x in n["link_ids"]
        ],
        "@type": "TroubleTicket",
    }
    if n["kind"] == "maintenance":
        out["@type"] = "MaintenanceTroubleTicket"
        out["@baseType"] = "TroubleTicket"
        out["plannedStartDate"] = iso(n["starts_at"])
        out["plannedEndDate"] = iso(n["ends_at"])
        out["moveTraffic"] = bool(n["move_traffic"])
    return out


def from_ticket(body: dict) -> dict:
    """TMF621 TroubleTicket (create) to notice fields."""
    tt = str(body.get("ticketType") or "").lower()
    kind = "maintenance" if "maint" in tt or body.get("@type") == "MaintenanceTroubleTicket" else "fault"
    links = [
        str(e.get("id"))
        for e in body.get("relatedEntity") or []
        if e.get("@referredType") in ("Link", "Circuit", None) or e.get("role") in ("affectedLink", "link")
    ]
    ext = body.get("externalIdentifier") or []
    return {
        "kind": kind,
        "title": str(body.get("name") or body.get("description") or "Carrier notice")[:200],
        "description": str(body.get("description") or ""),
        "severity": SEV_IN.get(str(body.get("severity") or "").lower()),
        "link_ids": [x for x in links if x and x != "None"],
        "starts_at": parse_time(body.get("plannedStartDate")),
        "ends_at": parse_time(body.get("plannedEndDate") or body.get("expectedResolutionDate")),
        "external_id": str((ext[0] or {}).get("id") if ext else body.get("externalId") or "")[:120],
        "move_traffic": body.get("moveTraffic", True) is not False,
    }


def ticket_changes(body: dict) -> dict:
    out: dict[str, Any] = {}
    if body.get("status"):
        st = TT_STATUS_IN.get(str(body["status"]))
        if st is None:
            raise StandardsError(f"Unknown status {body['status']}.", 422)
        out["status"] = st
    if "name" in body:
        out["title"] = str(body["name"])[:200]
    if "description" in body:
        out["description"] = str(body["description"])[:4000]
    if body.get("severity"):
        out["severity"] = SEV_IN.get(str(body["severity"]).lower())
    for src, dst in (("plannedStartDate", "starts_at"), ("plannedEndDate", "ends_at")):
        if body.get(src):
            out[dst] = parse_time(body[src])
    return out


# ---- TMF622 ---------------------------------------------------------------------


def tmf622_items(body: dict) -> tuple[list[dict], list[dict]]:
    actions, inputs = [], []
    items = body.get("productOrderItem") or []
    if not items:
        raise StandardsError("A product order needs at least one productOrderItem.", 422)
    for it in items:
        off = ((it.get("productOffering") or {}).get("id")) or ""
        prod = it.get("product") or {}
        a, i = to_action(off, characteristics(prod.get("productCharacteristic")), it.get("action") or "add")
        actions.append(a)
        inputs.append(i)
    return actions, inputs


def tmf622_view(doc: dict, order: dict | None, base: str) -> dict:
    body = dict(doc["body"])
    state = doc["state"]
    if order is not None:
        state = ORDER_STATE_TMF.get(order["status"], state)
        body["completionDate"] = iso(order["confirmed_at"]) if order["status"] == "done" else None
        results = {r.get("action"): r for r in order["results"] or []}
        for n, it in enumerate(body.get("productOrderItem") or []):
            r = results.get(n) or {}
            it["state"] = "completed" if r.get("ok") and not r.get("pending") else ("inProgress" if r else state)
            if r.get("message"):
                it.setdefault("note", [{"text": r["message"]}])
    body["state"] = state
    body["href"] = f"{base}/{doc['id']}"
    body["@type"] = "ProductOrder"
    return body


# ---- MEF LSO Sonata ---------------------------------------------------------------


def sonata_item(it: dict) -> tuple[dict, dict]:
    prod = it.get("product") or {}
    off = ((prod.get("productOffering") or {}).get("id")) or ""
    return to_action(off, dict(prod.get("productConfiguration") or {}), it.get("action") or "add")


def money(v: float | Decimal) -> dict:
    return {"unit": "USD", "value": float(Decimal(str(v)).quantize(Decimal("0.01")))}
