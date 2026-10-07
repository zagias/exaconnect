"""Admin inventory API: customers, sites, links, enrolment tokens, nodes, audit."""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .. import audit, db, inventory
from ..security import hash_password, new_token
from .deps import AdminDep, UserDep, ViewerDep, check_customer, customer_scope

router = APIRouter(tags=["admin"])


class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.get("/customers")
def list_customers(user: AdminDep) -> list[dict]:
    with db.tx() as conn:
        return conn.execute("SELECT id, name, created_at FROM customers ORDER BY name").fetchall()


@router.post("/customers", status_code=201)
def create_customer(body: CustomerIn, user: AdminDep) -> dict:
    with db.tx() as conn:
        return {"id": inventory.ensure_customer(conn, body.name, user.actor)}


class SiteIn(BaseModel):
    customer_id: str
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
    kind: Literal["site", "pop"] = "site"
    location: str = ""
    timezone: str = "UTC"
    asn: int = Field(ge=1, le=4294967294)
    lan_prefixes: list[str] = []
    overlay_host: int = Field(ge=1, le=254)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    # PoP only: the interface and address that reach cloud VPN gateways (ADR 0009).
    cloud_interface: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9._-]{1,15}$")
    cloud_address: str | None = None
    # The LAN-facing interface, where layer 2 circuits take their VLANs (default eth4).
    lan_interface: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9._-]{1,15}$")
    # PoP only: the interface and next hop toward the internet (ADR 0010).
    internet_interface: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9._-]{1,15}$")
    internet_gateway: str | None = None

    @field_validator("cloud_address")
    @classmethod
    def _cloud_address(cls, v: str | None) -> str | None:
        return None if not v else str(ipaddress.ip_interface(v))

    @field_validator("internet_gateway")
    @classmethod
    def _internet_gateway(cls, v: str | None) -> str | None:
        return None if not v else str(ipaddress.IPv4Address(v))

    @field_validator("lan_prefixes")
    @classmethod
    def _prefixes(cls, v: list[str]) -> list[str]:
        return [str(ipaddress.ip_network(p)) for p in v]


@router.post("/sites", status_code=201)
def upsert_site(body: SiteIn, user: AdminDep) -> dict:
    with db.tx() as conn:
        sid = inventory.upsert_site(conn, body.customer_id, user.actor, **body.model_dump(exclude={"customer_id"}))
    return {"id": sid}


class LinkIn(BaseModel):
    carrier: str = Field(min_length=1, max_length=120)
    path: Literal["carrier-a", "carrier-b", "sat"]
    underlay_type: Literal["fibre", "broadband", "lte", "leo", "geo"]
    underlay_interface: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,15}$")
    underlay_ip: str | None = None
    # The carrier's next hop on this link, for local internet breakout (ADR 0010).
    underlay_gateway: str | None = None
    commit_mbps: float = 0
    cost_per_mbps: float = 0
    burst_price: float = 0

    @field_validator("underlay_gateway")
    @classmethod
    def _underlay_gateway(cls, v: str | None) -> str | None:
        return None if not v else str(ipaddress.IPv4Address(v))


@router.post("/sites/{site_id}/links", status_code=201)
def upsert_link(site_id: str, body: LinkIn, user: AdminDep) -> dict:
    with db.tx() as conn:
        site = conn.execute("SELECT customer_id FROM sites WHERE id = %s", (site_id,)).fetchone()
        if site is None:
            raise HTTPException(404, "Site not found.")
        carrier_id = inventory.ensure_carrier(conn, body.carrier, user.actor)
        lid = inventory.upsert_link(
            conn,
            site["customer_id"],
            site_id,
            user.actor,
            carrier_id=carrier_id,
            **body.model_dump(exclude={"carrier"}),
        )
    return {"id": lid}


class TokenIn(BaseModel):
    site_id: str
    ttl_hours: int = Field(default=24, ge=1, le=24 * 30)


class TokenOut(BaseModel):
    token: str
    expires_at: dt.datetime
    ca_fingerprint: str
    agent_url: str
    # The agent gateway on the internet (ADR 0024); empty while it is not published.
    public_agent_url: str = ""


@router.post("/enrolment-tokens", status_code=201)
def issue_token(body: TokenIn, user: AdminDep, request: Request) -> TokenOut:
    """The token is shown once. Give it to the installer with the CA fingerprint."""
    with db.tx() as conn:
        try:
            token, expires = inventory.issue_token(conn, body.site_id, user.actor, body.ttl_hours)
        except LookupError:
            raise HTTPException(404, "Site not found.") from None
    return TokenOut(
        token=token,
        expires_at=expires,
        ca_fingerprint=request.app.state.ca.fingerprint,
        agent_url=request.app.state.settings.agent_url,
        public_agent_url=request.app.state.settings.agent_public_url,
    )


@router.get("/nodes")
def list_nodes(user: ViewerDep) -> list[dict]:
    scope = customer_scope(user)
    with db.tx() as conn:
        return conn.execute(
            """SELECT n.id, n.name, s.kind AS role, n.customer_id, n.enrolled_at, n.last_seen,
                      n.applied_version, n.apply_ok, n.apply_error, n.agent_version,
                      n.cert_serial LIKE 'revoked:%%' AS revoked,
                      (SELECT max(version) FROM desired_states d WHERE d.node_id = n.id) AS desired_version
               FROM nodes n JOIN sites s ON s.id = n.site_id
               WHERE %(c)s::uuid IS NULL OR n.customer_id = %(c)s
               ORDER BY n.name""",
            {"c": scope},
        ).fetchall()


@router.post("/nodes/{node_id}/revoke")
def revoke_node(node_id: str, user: AdminDep) -> dict:
    """Stop a node's certificate working at once (ADR 0024). The site keeps its
    inventory; a new enrolment token brings it back with a fresh certificate."""
    with db.tx() as conn:
        node = conn.execute(
            "UPDATE nodes SET cert_serial = 'revoked:' || id::text WHERE id = %s RETURNING name, customer_id",
            (node_id,),
        ).fetchone()
        if node is None:
            raise HTTPException(404, "Node not found.")
        audit.record(conn, user.actor, "node.revoke", node["name"], node["customer_id"])
    return {"revoked": True}


@router.get("/audit")
def list_audit(user: AdminDep, limit: int = 100) -> list[dict]:
    with db.tx() as conn:
        return conn.execute(
            "SELECT at, actor, action, target, detail FROM audit_log ORDER BY id DESC LIMIT %s",
            (min(limit, 1000),),
        ).fetchall()


@router.get("/carriers")
def list_carriers(user: AdminDep) -> list[dict]:
    with db.tx() as conn:
        return conn.execute("SELECT id, name FROM carriers ORDER BY name").fetchall()


@router.get("/inventory")
def inventory_view(user: AdminDep, customer_id: str) -> dict:
    """Sites with their links, for the admin screens."""
    with db.tx() as conn:
        sites = conn.execute(
            """SELECT s.id, s.name, s.kind, s.location, s.timezone, s.asn, s.lan_prefixes::text[] AS lan_prefixes,
                      s.overlay_host, s.latitude, s.longitude, s.cloud_interface,
                      s.cloud_address::text AS cloud_address,
                      s.lan_interface, s.internet_interface, host(s.internet_gateway) AS internet_gateway,
                      s.internet_mode, n.name AS node, n.last_seen
               FROM sites s LEFT JOIN nodes n ON n.site_id = s.id
               WHERE s.customer_id = %s ORDER BY s.kind DESC, s.name""",
            (customer_id,),
        ).fetchall()
        links = conn.execute(
            """SELECT l.id, l.site_id, l.path, c.name AS carrier, l.underlay_type, l.underlay_interface,
                      host(l.underlay_ip) || '/' || masklen(l.underlay_ip) AS underlay_ip,
                      host(l.underlay_gateway) AS underlay_gateway,
                      l.commit_mbps, l.cost_per_mbps, l.burst_price
               FROM links l JOIN carriers c ON c.id = l.carrier_id JOIN paths p ON p.name = l.path
               WHERE l.customer_id = %s ORDER BY p.ordinal""",
            (customer_id,),
        ).fetchall()
    for s in sites:
        s["links"] = [lk for lk in links if lk["site_id"] == s["id"]]
    return {"sites": sites}


class SlaIn(BaseModel):
    max_latency_ms: float | None = Field(default=None, gt=0, le=10_000)
    max_jitter_ms: float | None = Field(default=None, gt=0, le=10_000)
    max_loss_pct: float | None = Field(default=None, gt=0, le=100)
    allow_satellite: bool = True


class ClassIn(BaseModel):
    description: str = Field(default="", max_length=200)
    dscp: list[int] = Field(default=[], max_length=64)
    ports: str = Field(default="", max_length=500)
    subnets: list[str] = Field(default=[], max_length=64)
    ordinal: int = Field(default=100, ge=1, le=1000)
    priority: Literal["realtime", "interactive", "normal", "bulk"] = "normal"
    preferred_path: str | None = Field(default=None, max_length=16)
    sla: SlaIn

    @field_validator("dscp")
    @classmethod
    def _dscp(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 63 for d in v):
            raise ValueError("DSCP values are 0 to 63")
        return sorted(set(v))

    @field_validator("subnets")
    @classmethod
    def _subnets(cls, v: list[str]) -> list[str]:
        return [str(ipaddress.ip_network(p, strict=False)) for p in v]

    @field_validator("ports")
    @classmethod
    def _ports(cls, v: str) -> str:
        from ..ports import parse_ports

        parse_ports(v)
        return v


@router.get("/classes")
def list_classes(user: ViewerDep, customer_id: str | None = None) -> list[dict]:
    scope = customer_scope(user) or customer_id
    with db.tx() as conn:
        return conn.execute(
            """SELECT c.customer_id, c.name, c.description, c.dscp, c.ports, c.subnets::text[] AS subnets, c.ordinal,
                      c.priority, c.preferred_path, c.builtin, s.max_latency_ms, s.max_jitter_ms, s.max_loss_pct,
                      coalesce(s.allow_satellite, true) AS allow_satellite
               FROM app_classes c LEFT JOIN sla_policies s ON s.customer_id = c.customer_id AND s.class_name = c.name
               WHERE %(c)s::uuid IS NULL OR c.customer_id = %(c)s ORDER BY c.ordinal, c.name""",
            {"c": scope},
        ).fetchall()


MAX_CLASSES = 12


@router.put("/customers/{customer_id}/classes/{name}")
def put_class(customer_id: str, name: str, body: ClassIn, user: UserDep) -> dict:
    """Create or update an application class and its SLA policy. Customers
    manage their own classes. Agents get the new classifier and steering map
    on their next poll."""
    check_customer(user, customer_id)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,19}", name):
        raise HTTPException(400, "Class names are lower case letters, digits and dashes, up to 20 characters.")
    from .. import desired

    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM customers WHERE id = %s", (customer_id,)).fetchone() is None:
            raise HTTPException(404, "Customer not found.")
        if (
            body.preferred_path
            and not conn.execute(
                "SELECT 1 FROM links WHERE customer_id = %s AND path = %s LIMIT 1", (customer_id, body.preferred_path)
            ).fetchone()
        ):
            raise HTTPException(400, f"There is no path called '{body.preferred_path}'.")
        count = conn.execute(
            "SELECT count(*) FILTER (WHERE name <> %s) AS n FROM app_classes WHERE customer_id = %s",
            (name, customer_id),
        ).fetchone()["n"]
        if count >= MAX_CLASSES:
            raise HTTPException(400, f"An organisation can have up to {MAX_CLASSES} classes.")
        conn.execute(
            """INSERT INTO app_classes (customer_id, name, description, dscp, ports, subnets, ordinal, priority,
                                        preferred_path)
               VALUES (%s, %s, %s, %s, %s, %s::cidr[], %s, %s, %s)
               ON CONFLICT (customer_id, name) DO UPDATE SET description = EXCLUDED.description,
                 dscp = EXCLUDED.dscp, ports = EXCLUDED.ports, subnets = EXCLUDED.subnets,
                 ordinal = EXCLUDED.ordinal, priority = EXCLUDED.priority,
                 preferred_path = EXCLUDED.preferred_path""",
            (
                customer_id,
                name,
                body.description,
                body.dscp,
                body.ports,
                body.subnets,
                body.ordinal,
                body.priority,
                body.preferred_path or None,
            ),
        )
        conn.execute(
            """INSERT INTO sla_policies (customer_id, class_name, max_latency_ms, max_jitter_ms, max_loss_pct,
                                        allow_satellite)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (customer_id, class_name) DO UPDATE SET max_latency_ms = EXCLUDED.max_latency_ms,
                 max_jitter_ms = EXCLUDED.max_jitter_ms, max_loss_pct = EXCLUDED.max_loss_pct,
                 allow_satellite = EXCLUDED.allow_satellite""",
            (
                customer_id,
                name,
                body.sla.max_latency_ms,
                body.sla.max_jitter_ms,
                body.sla.max_loss_pct,
                body.sla.allow_satellite,
            ),
        )
        audit.record(conn, user.actor, "class.upsert", name, customer_id, body.model_dump(mode="json"))
        desired.refresh(conn, customer_id)
    return {"name": name}


@router.delete("/customers/{customer_id}/classes/{name}", status_code=204)
def delete_class(customer_id: str, name: str, user: UserDep) -> None:
    check_customer(user, customer_id)
    from .. import desired

    with db.tx() as conn:
        row = conn.execute(
            "SELECT builtin FROM app_classes WHERE customer_id = %s AND name = %s", (customer_id, name)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Class not found.")
        if row["builtin"]:
            raise HTTPException(400, "The built-in classes can be changed but not deleted.")
        rules = conn.execute(
            "SELECT count(*) AS n FROM traffic_rules WHERE customer_id = %s AND class_name = %s", (customer_id, name)
        ).fetchone()["n"]
        if rules:
            raise HTTPException(400, f"{rules} traffic rule(s) use this class. Change or delete them first.")
        conn.execute("DELETE FROM app_classes WHERE customer_id = %s AND name = %s", (customer_id, name))
        conn.execute("DELETE FROM sla_policies WHERE customer_id = %s AND class_name = %s", (customer_id, name))
        conn.execute("DELETE FROM steering WHERE customer_id = %s AND class_name = %s", (customer_id, name))
        audit.record(conn, user.actor, "class.delete", name, customer_id)
        desired.refresh(conn, customer_id)


# ---- Users ----

ROLES = ("admin", "customer", "carrier")


class UserIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]{1,64}@[^@\s]{1,190}$")
    role: Literal["admin", "customer", "carrier"]
    customer_id: str | None = None
    carrier_id: str | None = None


@router.get("/users")
def list_users(user: AdminDep) -> list[dict]:
    with db.tx() as conn:
        return conn.execute(
            """SELECT u.id, u.email, u.role, u.customer_id, cu.name AS customer, u.carrier_id, ca.name AS carrier,
                      u.created_at,
                      (SELECT max(at) FROM audit_log a WHERE a.actor = 'user:' || u.email AND a.action = 'login')
                        AS last_login
               FROM users u
               LEFT JOIN customers cu ON cu.id = u.customer_id
               LEFT JOIN carriers ca ON ca.id = u.carrier_id
               ORDER BY u.role, u.email"""
        ).fetchall()


def _one_time_password() -> str:
    return new_token()[:20]


@router.post("/users", status_code=201)
def create_user(body: UserIn, user: AdminDep) -> dict:
    """Creates an account with a one-time password, shown once. The user
    should change it after signing in."""
    if body.role == "customer" and not body.customer_id:
        raise HTTPException(400, "A customer account needs a customer.")
    if body.role == "carrier" and not body.carrier_id:
        raise HTTPException(400, "A carrier account needs a carrier.")
    password = _one_time_password()
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM users WHERE lower(email) = lower(%s)", (body.email,)).fetchone():
            raise HTTPException(409, "There is already an account with that email.")
        row = conn.execute(
            """INSERT INTO users (email, password_hash, role, customer_id, carrier_id)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (
                body.email,
                hash_password(password),
                body.role,
                body.customer_id if body.role == "customer" else None,
                body.carrier_id if body.role == "carrier" else None,
            ),
        ).fetchone()
        audit.record(conn, user.actor, "user.create", body.email, body.customer_id, {"role": body.role})
    return {"id": row["id"], "email": body.email, "password": password}


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: str, user: AdminDep) -> dict:
    password = _one_time_password()
    with db.tx() as conn:
        row = conn.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s RETURNING email", (hash_password(password), user_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "User not found.")
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        audit.record(conn, user.actor, "user.reset_password", row["email"])
    return {"email": row["email"], "password": password}


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: str, user: AdminDep) -> None:
    with db.tx() as conn:
        row = conn.execute("SELECT email, role FROM users WHERE id = %s", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "User not found.")
        if row["email"] == user.email:
            raise HTTPException(400, "You can't delete your own account.")
        if (
            row["role"] == "admin"
            and conn.execute("SELECT count(*) AS n FROM users WHERE role = 'admin'").fetchone()["n"] <= 1
        ):
            raise HTTPException(400, "This is the last admin account.")
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        audit.record(conn, user.actor, "user.delete", row["email"])
