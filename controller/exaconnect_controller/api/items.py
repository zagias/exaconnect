"""Single-item reads for resources that could only be listed (ADR 0026).

Each one reuses its list endpoint's query and access check, so a single
GET never shows more than the list would: a customer sees only its own
items, a carrier only its own links, and admin-only lists stay admin-only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit, db, internet, traffic
from . import admin, circuits, ordering, sso
from .deps import AdminDep, UserDep, ViewerDep, check_customer, customer_scope

router = APIRouter(tags=["items"])


def _one(rows: list[dict], key: str, value: Any, what: str) -> dict:
    for r in rows:
        if str(r.get(key)) == str(value):
            return r
    raise HTTPException(404, f"{what} not found.")


# ---- inventory ---------------------------------------------------------------------


@router.get("/customers/{customer_id}")
def get_customer(customer_id: str, user: UserDep) -> dict:
    """An organisation: admins any, customer users their own."""
    check_customer(user, customer_id)
    with db.tx() as conn:
        row = conn.execute(
            """SELECT c.id, c.name, c.created_at, c.shadow_mode, c.storm_mode, c.storm_since,
                      (SELECT count(*) FROM sites s WHERE s.customer_id = c.id AND s.kind = 'site') AS sites,
                      (SELECT count(*) FROM links l WHERE l.customer_id = c.id) AS links
               FROM customers c WHERE c.id::text = %s""",
            (customer_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Customer not found.")
    return row


class CustomerPatch(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.patch("/customers/{customer_id}")
def rename_customer(customer_id: str, body: CustomerPatch, user: AdminDep) -> dict:
    with db.tx() as conn:
        if conn.execute(
            "SELECT 1 FROM customers WHERE lower(name) = lower(%s) AND id::text <> %s", (body.name, customer_id)
        ).fetchone():
            raise HTTPException(409, "There is already an organisation with that name.")
        row = conn.execute(
            "UPDATE customers SET name = %s WHERE id::text = %s RETURNING id, name, created_at",
            (body.name, customer_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Customer not found.")
        audit.record(conn, user.actor, "customer.rename", body.name, row["id"])
    return row


@router.get("/nodes/{node_id}")
def get_node(node_id: str, user: ViewerDep) -> dict:
    return _one(admin.list_nodes(user), "id", node_id, "Node")


@router.get("/carriers/{carrier_id}")
def get_carrier(carrier_id: str, user: UserDep) -> dict:
    """A carrier: admins any, a carrier account its own."""
    if user.role == "carrier":
        if str(user.carrier_id) != carrier_id:
            raise HTTPException(404, "Carrier not found.")
    elif user.role != "admin":
        raise HTTPException(403, "Not available for this account.")
    with db.tx() as conn:
        row = conn.execute(
            """SELECT c.id, c.name, (SELECT count(*) FROM links l WHERE l.carrier_id = c.id) AS links
               FROM carriers c WHERE c.id::text = %s""",
            (carrier_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Carrier not found.")
    return row


LINK_SQL = """SELECT l.id, l.customer_id, l.site_id, s.name AS site, l.path, p.label AS path_label, l.carrier_id,
                     c.name AS carrier, l.underlay_type, l.underlay_interface,
                     host(l.underlay_ip) || '/' || masklen(l.underlay_ip) AS underlay_ip,
                     host(l.underlay_gateway) AS underlay_gateway, l.commit_mbps, l.cost_per_mbps, l.burst_price,
                     l.shape_mbps
              FROM links l JOIN sites s ON s.id = l.site_id JOIN carriers c ON c.id = l.carrier_id
              JOIN paths p ON p.name = l.path
              WHERE (%(c)s::uuid IS NULL OR l.customer_id = %(c)s) AND (%(k)s::uuid IS NULL OR l.carrier_id = %(k)s)"""


def _link_scope(user) -> dict:
    if user.role == "admin":
        return {"c": None, "k": None}
    if user.role == "customer":
        return {"c": user.customer_id, "k": None}
    if user.role == "carrier" and user.carrier_id:
        return {"c": None, "k": user.carrier_id}
    raise HTTPException(403, "Not available for this account.")


@router.get("/sites/{site_id}/links")
def site_links(site_id: str, user: UserDep) -> list[dict]:
    with db.tx() as conn:
        return conn.execute(
            LINK_SQL + " AND l.site_id::text = %(s)s ORDER BY p.ordinal", {**_link_scope(user), "s": site_id}
        ).fetchall()


@router.get("/links/{link_id}")
def get_link(link_id: str, user: UserDep) -> dict:
    """A carrier link: admins any, customers their own, carriers theirs."""
    with db.tx() as conn:
        row = conn.execute(LINK_SQL + " AND l.id::text = %(id)s", {**_link_scope(user), "id": link_id}).fetchone()
    if row is None:
        raise HTTPException(404, "Link not found.")
    return row


@router.get("/customers/{customer_id}/classes/{name}")
def get_class(customer_id: str, name: str, user: UserDep) -> dict:
    check_customer(user, customer_id)
    rows = [c for c in admin.list_classes(user, customer_id) if str(c["customer_id"]) == customer_id]
    return _one(rows, "name", name, "Class")


@router.get("/users/{user_id}")
def get_user(user_id: str, user: AdminDep) -> dict:
    return _one(admin.list_users(user), "id", user_id, "User")


@router.get("/auth/api-keys/{key_id}")
def get_key(key_id: int, user: UserDep) -> dict:
    from .auth import list_keys

    return _one(list_keys(user), "id", key_id, "Key")


# ---- routing and insights -------------------------------------------------------------


@router.get("/decisions/{decision_id}")
def get_decision(decision_id: int, user: ViewerDep) -> dict:
    scope = customer_scope(user)
    with db.tx() as conn:
        row = conn.execute(
            """SELECT d.id, d.time, d.customer_id, d.site_id, s.name AS site, d.class_name, d.kind,
                      d.from_path, fp.label AS from_label, d.to_path, tp.label AS to_label,
                      d.shadow, d.engine, d.reason, d.inputs
               FROM decisions d JOIN sites s ON s.id = d.site_id
               LEFT JOIN paths fp ON fp.name = d.from_path LEFT JOIN paths tp ON tp.name = d.to_path
               WHERE d.id = %(id)s AND (%(c)s::uuid IS NULL OR d.customer_id = %(c)s)""",
            {"id": decision_id, "c": scope},
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Decision not found.")
    return row


@router.get("/insights/{insight_id}")
def get_insight(insight_id: int, user: UserDep) -> dict:
    """One insight, open or resolved. Carriers see anomalies on their own links only."""
    args: dict[str, Any] = {"id": insight_id, "c": None, "k": None, "carrier": None}
    if user.role == "customer":
        args["c"] = user.customer_id
    elif user.role == "carrier":
        if not user.carrier_id:
            raise HTTPException(403, "Not available for this account.")
        args.update(k="anomaly", carrier=user.carrier_id)
    elif user.role != "admin":
        raise HTTPException(403, "Not available for this account.")
    with db.tx() as conn:
        row = conn.execute(
            """SELECT i.id, i.customer_id, i.kind, i.severity, i.title, i.detail, i.data, i.example,
                      i.first_seen, i.last_seen, i.resolved_at, i.acknowledged_by, i.acknowledged_at,
                      s.name AS site, l.path, ca.name AS carrier
               FROM insights i LEFT JOIN sites s ON s.id = i.site_id LEFT JOIN links l ON l.id = i.link_id
               LEFT JOIN carriers ca ON ca.id = i.carrier_id
               WHERE i.id = %(id)s AND (%(c)s::uuid IS NULL OR i.customer_id = %(c)s)
                 AND (%(k)s::text IS NULL OR i.kind = %(k)s)
                 AND (%(carrier)s::uuid IS NULL OR i.carrier_id = %(carrier)s)""",
            args,
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Insight not found.")
    return row


# ---- traffic, circuits, internet ------------------------------------------------------


@router.get("/customers/{customer_id}/rules/{rule_id}")
def get_traffic_rule(customer_id: str, rule_id: int, user: UserDep) -> dict:
    check_customer(user, customer_id)
    with db.tx() as conn:
        return _one(traffic.load_rules(conn, customer_id), "id", rule_id, "Rule")


@router.get("/applications/{detection_id}")
def get_detection(detection_id: int, user: UserDep) -> dict:
    with db.tx() as conn:
        det = conn.execute(
            "SELECT d.*, s.name AS site FROM app_detections d JOIN sites s ON s.id = d.site_id WHERE d.id = %s",
            (detection_id,),
        ).fetchone()
    if det is None:
        raise HTTPException(404, "Detection not found.")
    try:
        check_customer(user, det["customer_id"])
    except HTTPException:
        raise HTTPException(404, "Detection not found.") from None
    return det


@router.get("/customers/{customer_id}/circuits/{circuit_id}")
def get_circuit(customer_id: str, circuit_id: int, user: UserDep) -> dict:
    check_customer(user, customer_id)
    with db.tx() as conn:
        rows = circuits.views(conn, customer_id, circuit_id)
    if not rows:
        raise HTTPException(404, "Circuit not found.")
    return rows[0]


@router.get("/customers/{customer_id}/firewall/rules")
def list_firewall_rules(customer_id: str, user: UserDep) -> list[dict]:
    check_customer(user, customer_id)
    with db.tx() as conn:
        return internet.view(conn, customer_id)["rules"]


@router.get("/customers/{customer_id}/firewall/rules/{rule_id}")
def get_firewall_rule(customer_id: str, rule_id: int, user: UserDep) -> dict:
    return _one(list_firewall_rules(customer_id, user), "id", rule_id, "Rule")


@router.get("/customers/{customer_id}/port-forwards")
def list_port_forwards(customer_id: str, user: UserDep) -> list[dict]:
    check_customer(user, customer_id)
    with db.tx() as conn:
        return internet.view(conn, customer_id)["forwards"]


@router.get("/customers/{customer_id}/port-forwards/{forward_id}")
def get_port_forward(customer_id: str, forward_id: int, user: UserDep) -> dict:
    return _one(list_port_forwards(customer_id, user), "id", forward_id, "Port forward")


# ---- admin ---------------------------------------------------------------------------


@router.get("/admin/partners/{partner_id}")
def get_admin_partner(partner_id: int, user: AdminDep) -> dict:
    return _one(ordering.admin_partners(user), "id", partner_id, "Partner")


@router.get("/admin/protection/blocklist")
def list_blocklist(user: AdminDep) -> list[dict]:
    with db.tx() as conn:
        return conn.execute(
            """SELECT id, prefix::text AS prefix, reason, created_by, created_at, expires_at, lifted
               FROM blocked_sources ORDER BY id DESC"""
        ).fetchall()


@router.get("/admin/protection/blocklist/{block_id}")
def get_block(block_id: int, user: AdminDep) -> dict:
    return _one(list_blocklist(user), "id", block_id, "Block list entry")


@router.get("/releases/{release_id}")
def get_release(release_id: int, user: AdminDep) -> dict:
    with db.tx() as conn:
        row = conn.execute(
            """SELECT id, commit, previous, started_at, finished_at, status, kind, backup IS NOT NULL AS backed_up,
                      detail FROM releases WHERE id = %s""",
            (release_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Release not found.")
    return row


@router.get("/customers/{customer_id}/sso-connections/{connection_id}")
def get_sso_connection(customer_id: str, connection_id: str, user: UserDep, request: Request) -> dict:
    return _one(sso.list_connections(customer_id, user, request)["items"], "id", connection_id, "Connection")


@router.get("/customers/{customer_id}/scim-tokens/{token_id}")
def get_scim_token(customer_id: str, token_id: str, user: UserDep, request: Request) -> dict:
    return _one(sso.list_scim_tokens(customer_id, user, request)["items"], "id", token_id, "Token")


@router.get("/customers/{customer_id}/directory-groups/{group_id}")
def get_directory_group(customer_id: str, group_id: str, user: UserDep) -> dict:
    return _one(sso.list_groups(customer_id, user), "id", group_id, "Group")
