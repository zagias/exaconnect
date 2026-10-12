"""Zero-touch installation (ADR 0050): boxes call home; ExaCarib staff assign them."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit, db, ztp
from ..commai.ratelimit import hit
from .auth import _client_ip
from .deps import AdminDep

router = APIRouter(tags=["zero-touch"])
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class HelloIn(BaseModel):
    serial: str = Field(min_length=1, max_length=64)
    public_key: str = Field(min_length=40, max_length=64)
    at: str = Field(max_length=40)
    signature: str = Field(min_length=80, max_length=100)
    mac: str | None = Field(default=None, max_length=17, pattern=r"^[0-9a-fA-F:]{0,17}$")
    model: str | None = Field(default=None, max_length=80)
    version: str | None = Field(default=None, max_length=60)


@router.post("/ztp/hello")
def hello(body: HelloIn, request: Request) -> dict:
    """A box that has not joined Connect calls here with its serial number and
    key. It is told to wait until staff assign it, then gets what it needs to join."""
    ip = _client_ip(request)
    s = request.app.state.settings
    agent_url = s.agent_public_url or f"https://{request.url.hostname}:8443"
    with db.tx() as conn:
        allowed, _, reset = hit(conn, f"ztp:{ip}", 60)
        if not allowed:
            raise HTTPException(429, "Too many calls from this address.", headers={"Retry-After": str(reset)})
    with db.tx() as conn:
        try:
            return ztp.hello(conn, body.model_dump(), ip, agent_url, request.app.state.ca.fingerprint)
        except ztp.ZTPError as e:
            err = e  # caught inside the transaction, so a recorded key conflict is kept
    raise HTTPException(err.status, err.detail)


# ---- ExaCarib staff: the device list -------------------------------------------------------


@router.get("/admin/boxes")
def list_boxes(user: AdminDep) -> list[dict]:
    with db.tx() as conn:
        return ztp.listing(conn)


@router.get("/admin/boxes/sites")
def assignable_sites(user: AdminDep) -> list[dict]:
    """Every customer site a box can be assigned to, with its organisation."""
    with db.tx() as conn:
        return conn.execute(
            """SELECT s.id, s.name, c.name AS organisation, l.name AS location, n.id IS NOT NULL AS enrolled
               FROM sites s JOIN customers c ON c.id = s.customer_id
               LEFT JOIN org_locations l ON l.id = s.org_location_id
               LEFT JOIN nodes n ON n.site_id = s.id
               WHERE s.kind = 'site' ORDER BY c.name, coalesce(l.name, s.name)"""
        ).fetchall()


class BoxIn(BaseModel):
    serial: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=200)
    site_id: str | None = None


class BoxPatch(BaseModel):
    note: str | None = Field(default=None, max_length=200)
    # A site to assign the box to, or "" to unassign it.
    site_id: str | None = None


def _site(conn, site_id: str) -> dict:
    if not _UUID.match(site_id):
        raise HTTPException(404, "Site not found.")
    site = conn.execute(
        "SELECT id, name, customer_id FROM sites WHERE id = %s AND kind = 'site'", (site_id,)
    ).fetchone()
    if site is None:
        raise HTTPException(404, "Site not found.")
    return site


def _assign(conn, serial: str, site_id: str | None, actor: str) -> None:
    if site_id:
        site = _site(conn, site_id)
        other = conn.execute(
            "SELECT serial FROM boxes WHERE site_id = %s AND serial <> %s AND node_id IS NULL", (site["id"], serial)
        ).fetchone()
        if other is not None:
            raise HTTPException(409, f"Box {other['serial']} is already waiting to join {site['name']}.")
        conn.execute(
            """UPDATE boxes SET site_id = %s, customer_id = %s, assigned_by = %s, assigned_at = now(), token_at = NULL
               WHERE serial = %s""",
            (site["id"], site["customer_id"], actor, serial),
        )
        audit.record(conn, actor, "box.assign", serial, site["customer_id"], {"site": site["name"]})
    else:
        row = conn.execute("SELECT customer_id FROM boxes WHERE serial = %s", (serial,)).fetchone()
        conn.execute(
            """UPDATE boxes SET site_id = NULL, customer_id = NULL, assigned_by = %s, assigned_at = now()
               WHERE serial = %s""",
            (actor, serial),
        )
        audit.record(conn, actor, "box.unassign", serial, row["customer_id"] if row else None)


@router.post("/admin/boxes", status_code=201)
def add_box(body: BoxIn, user: AdminDep) -> dict:
    """Add a box by its serial number (from its label or the delivery note) before it
    calls home, optionally assigned to a site straight away."""
    serial = ztp.normalise(body.serial)
    if not serial:
        raise HTTPException(400, "Type the box's serial number.")
    with db.tx() as conn:
        row = conn.execute(
            """INSERT INTO boxes (serial, note, added_by) VALUES (%s, %s, %s)
               ON CONFLICT (serial) DO NOTHING RETURNING serial""",
            (serial, body.note, user.actor),
        ).fetchone()
        if row is None:
            raise HTTPException(409, f"Box {serial} is already on the list.")
        audit.record(conn, user.actor, "box.add", serial)
        if body.site_id:
            _assign(conn, serial, body.site_id, user.actor)
        return ztp.listing(conn, serial)[0]


@router.patch("/admin/boxes/{serial}")
def update_box(serial: str, body: BoxPatch, user: AdminDep) -> dict:
    """Assign a box to a site (it joins by itself when it next calls home), move it,
    unassign it, or change its note."""
    serial = ztp.normalise(serial)
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM boxes WHERE serial = %s FOR UPDATE", (serial,)).fetchone() is None:
            raise HTTPException(404, "Box not found.")
        if body.note is not None:
            conn.execute("UPDATE boxes SET note = %s WHERE serial = %s", (body.note, serial))
        if body.site_id is not None:
            _assign(conn, serial, body.site_id or None, user.actor)
        return ztp.listing(conn, serial)[0]


@router.post("/admin/boxes/{serial}/release")
def release_box(serial: str, user: AdminDep) -> dict:
    """Forget the key a box called home with, after a conflict or when its hardware
    was replaced: the next box to call with this serial is trusted."""
    serial = ztp.normalise(serial)
    with db.tx() as conn:
        row = conn.execute(
            "UPDATE boxes SET public_key = NULL, key_conflict = NULL WHERE serial = %s RETURNING customer_id",
            (serial,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Box not found.")
        audit.record(conn, user.actor, "box.release", serial, row["customer_id"])
        return ztp.listing(conn, serial)[0]


@router.delete("/admin/boxes/{serial}", status_code=204)
def delete_box(serial: str, user: AdminDep) -> None:
    serial = ztp.normalise(serial)
    with db.tx() as conn:
        row = conn.execute("DELETE FROM boxes WHERE serial = %s RETURNING customer_id", (serial,)).fetchone()
        if row is None:
            raise HTTPException(404, "Box not found.")
        audit.record(conn, user.actor, "box.delete", serial, row["customer_id"])
