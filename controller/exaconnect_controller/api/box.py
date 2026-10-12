"""Cloud management of ExaCarib boxes (ADR 0049): where each box is managed from."""

from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import audit, db
from .. import box as boxes
from .deps import UserDep, check_customer, require_org_manager

router = APIRouter(tags=["box"])
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class ManagedIn(BaseModel):
    mode: Literal["site", "cloud", "both"]


class PortIn(BaseModel):
    iface: str = Field(max_length=15)
    path: str = Field(default="", max_length=16)
    mode: Literal["dhcp", "static", "pppoe", "keep"]
    address: str = Field(default="", max_length=64)
    gateway: str = Field(default="", max_length=64)
    dns: list[str] = Field(default=[], max_length=4)
    pppoe_user: str = Field(default="", max_length=128)
    pppoe_password: str = Field(default="", max_length=128)


class NetworkIn(BaseModel):
    wan: list[PortIn] = Field(max_length=3)
    lan: PortIn | None = None
    lan_dhcp: bool = False


def _node(conn, site_id: str) -> dict:
    if not _UUID.match(site_id):
        raise HTTPException(404, "No agent at this site.")
    row = conn.execute(
        "SELECT id, name, customer_id, box, box_managed, box_network FROM nodes WHERE site_id = %s FOR UPDATE",
        (site_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "No agent at this site.")
    return row


@router.get("/sites/{site_id}/box/managed")
def get_managed(site_id: str, user: UserDep) -> dict:
    with db.tx() as conn:
        n = _node(conn, site_id)
    check_customer(user, n["customer_id"])
    return {"console": bool((n["box"] or {}).get("console")), "managed": boxes.managed(n)}


@router.post("/sites/{site_id}/box/managed")
def set_managed(site_id: str, body: ManagedIn, user: UserDep) -> dict:
    """Switch where a box is managed from: on site (its console only), the cloud
    (the portal only; the console reads) or both (latest change wins). The box
    keeps its settings as they are. For the organisation's owners and admins."""
    with db.tx() as conn:
        n = _node(conn, site_id)
        require_org_manager(user, n["customer_id"])
        if not (n["box"] or {}).get("console"):
            raise HTTPException(409, "This site's agent does not run the box console.")
        m = boxes.set_from_portal(conn, n, body.mode, user.actor)
    return {"console": True, "managed": {"mode": m["mode"], "at": m["at"], "by": m["by"]}}


@router.get("/sites/{site_id}/box/network")
def get_network(site_id: str, user: UserDep) -> dict:
    with db.tx() as conn:
        n = _node(conn, site_id)
    check_customer(user, n["customer_id"])
    box = n["box"] or {}
    return {
        "console": bool(box.get("console")),
        "managed": boxes.managed(n),
        "box": box,
        "portal": boxes.public_network(n["box_network"]),
    }


@router.post("/sites/{site_id}/box/network")
def set_network(site_id: str, body: NetworkIn, user: UserDep) -> dict:
    """Set a box's ports from the portal (ADR 0051). The box takes them while it
    is managed from the cloud or both, and keeps them only once it can still
    reach ExaCarib with them; otherwise it puts its earlier ports back after two
    minutes. For the organisation's owners and admins."""
    with db.tx() as conn:
        n = _node(conn, site_id)
        require_org_manager(user, n["customer_id"])
        if not (n["box"] or {}).get("console"):
            raise HTTPException(409, "This site's agent does not run the box console.")
        if boxes.managed(n)["mode"] == "site":
            raise HTTPException(409, "This box is managed on site. Switch Managed from to Cloud or both first.")
        try:
            bn = boxes.set_network(conn, n, body.model_dump(), user.actor)
        except boxes.NetworkError as e:
            raise HTTPException(400, str(e)) from None
    return {"portal": boxes.public_network(bn)}


# Templates (ADR 0053)


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    network: NetworkIn | None = None
    from_site_id: str | None = None


class TemplateSitesIn(BaseModel):
    site_ids: list[str] = Field(max_length=500)


def _org(user, customer_id: str | None) -> str:
    """The organisation a template call acts for: the user's own, or the one
    ExaCarib staff name."""
    cid = customer_id or (str(user.customer_id) if user.customer_id else None)
    if not cid or not _UUID.match(cid):
        raise HTTPException(400, "Choose an organisation.")
    check_customer(user, cid)
    return cid


def _template(conn, template_id: str) -> dict:
    if not _UUID.match(template_id):
        raise HTTPException(404, "No such template.")
    t = conn.execute("SELECT * FROM box_templates WHERE id = %s FOR UPDATE", (template_id,)).fetchone()
    if t is None:
        raise HTTPException(404, "No such template.")
    return t


def _bad(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


@router.get("/box-templates")
def list_templates(user: UserDep, customer_id: str | None = None) -> dict:
    """An organisation's box templates, and its boxes with the template each follows."""
    with db.tx() as conn:
        cid = _org(user, customer_id)
        ts = conn.execute(
            "SELECT id, name, network, updated_by, updated_at FROM box_templates WHERE customer_id = %s ORDER BY name",
            (cid,),
        ).fetchall()
        rows = conn.execute(
            """SELECT s.id AS site_id, s.name AS site, n.box, n.box_managed, n.box_network, n.box_template_id
               FROM sites s JOIN nodes n ON n.site_id = s.id
               WHERE s.customer_id = %s AND (n.box->>'console')::boolean ORDER BY s.name""",
            (cid,),
        ).fetchall()
    boxes_out = []
    for r in rows:
        box = r["box"] or {}
        portal = box.get("portal") or {}
        same = bool(r["box_network"] and portal.get("at")) and boxes._at(portal) == boxes._at(r["box_network"])
        boxes_out.append(
            {
                "site_id": str(r["site_id"]),
                "site": r["site"],
                "managed": boxes.managed(r)["mode"],
                "template_id": str(r["box_template_id"]) if r["box_template_id"] else None,
                "ports": [p.get("name") for p in box.get("ports") or []],
                "state": portal.get("state") if same else ("sent" if r["box_network"] else None),
            }
        )
    return {
        "templates": [{**t, "id": str(t["id"])} for t in ts],
        "boxes": boxes_out,
    }


@router.post("/box-templates", status_code=201)
def create_template(body: TemplateIn, user: UserDep, customer_id: str | None = None) -> dict:
    """A new template, from a set-up or from one location's box as it is now."""
    with db.tx() as conn:
        cid = _org(user, customer_id)
        require_org_manager(user, cid)
        net = body.network.model_dump() if body.network else None
        if net is None and body.from_site_id:
            n = _node(conn, body.from_site_id)
            if str(n["customer_id"]) != cid or not (n["box"] or {}).get("console"):
                raise HTTPException(400, "That location has no ExaCarib box in this organisation.")
            b = n["box"]
            net = {"wan": b.get("wan") or [], "lan": b.get("lan"), "lan_dhcp": bool(b.get("lan_dhcp"))}
        if net is None:
            raise HTTPException(400, "Give the ports, or the location to copy them from.")
        try:
            clean = boxes.check_template(net)
        except boxes.NetworkError as e:
            raise _bad(e) from None
        if conn.execute(
            "SELECT 1 FROM box_templates WHERE customer_id = %s AND name = %s", (cid, body.name.strip())
        ).fetchone():
            raise HTTPException(409, "There is already a template with that name.")
        t = conn.execute(
            """INSERT INTO box_templates (customer_id, name, network, updated_by) VALUES (%s, %s, %s, %s)
               RETURNING id, name, network, updated_by, updated_at""",
            (cid, body.name.strip(), Jsonb(clean), user.actor.removeprefix("user:")),
        ).fetchone()
        audit.record(conn, user.actor, "box.template.create", body.name.strip(), cid, clean)
    return {**t, "id": str(t["id"])}


@router.put("/box-templates/{template_id}")
def update_template(template_id: str, body: TemplateIn, user: UserDep) -> dict:
    """Change a template. Every box that follows it gets the new ports."""
    with db.tx() as conn:
        t = _template(conn, template_id)
        require_org_manager(user, t["customer_id"])
        net = t["network"]
        if body.network is not None:
            try:
                net = boxes.check_template(body.network.model_dump())
            except boxes.NetworkError as e:
                raise _bad(e) from None
        name = body.name.strip()
        if (
            name != t["name"]
            and conn.execute(
                "SELECT 1 FROM box_templates WHERE customer_id = %s AND name = %s", (t["customer_id"], name)
            ).fetchone()
        ):
            raise HTTPException(409, "There is already a template with that name.")
        t = conn.execute(
            """UPDATE box_templates SET name = %s, network = %s, updated_by = %s, updated_at = now()
               WHERE id = %s RETURNING *""",
            (name, Jsonb(net), user.actor.removeprefix("user:"), t["id"]),
        ).fetchone()
        audit.record(conn, user.actor, "box.template.update", name, t["customer_id"], net)
        sites = [
            str(r["site_id"])
            for r in conn.execute("SELECT site_id FROM nodes WHERE box_template_id = %s", (t["id"],)).fetchall()
        ]
        results = boxes.apply_template(conn, t, sites, user.actor) if body.network is not None else []
    return {"template": {**t, "id": str(t["id"])}, "results": results}


@router.post("/box-templates/{template_id}/sites")
def template_sites(template_id: str, body: TemplateSitesIn, user: UserDep) -> dict:
    """Make boxes follow a template: each gets its ports now and on every change."""
    with db.tx() as conn:
        t = _template(conn, template_id)
        require_org_manager(user, t["customer_id"])
        for s in body.site_ids:
            if not _UUID.match(s):
                raise HTTPException(400, "Unknown location.")
        results = boxes.apply_template(conn, t, body.site_ids, user.actor)
    return {"results": results}


@router.delete("/box-templates/{template_id}/sites/{site_id}")
def template_unfollow(template_id: str, site_id: str, user: UserDep) -> dict:
    """A box stops following a template. It keeps the ports it has."""
    with db.tx() as conn:
        t = _template(conn, template_id)
        require_org_manager(user, t["customer_id"])
        if not _UUID.match(site_id):
            raise HTTPException(404, "Unknown location.")
        conn.execute(
            "UPDATE nodes SET box_template_id = NULL WHERE site_id = %s AND box_template_id = %s", (site_id, t["id"])
        )
        audit.record(conn, user.actor, "box.template.unfollow", t["name"], t["customer_id"], {"site_id": site_id})
    return {"ok": True}


@router.delete("/box-templates/{template_id}")
def delete_template(template_id: str, user: UserDep) -> dict:
    """Remove a template. Its boxes keep the ports they have."""
    with db.tx() as conn:
        t = _template(conn, template_id)
        require_org_manager(user, t["customer_id"])
        conn.execute("DELETE FROM box_templates WHERE id = %s", (t["id"],))
        audit.record(conn, user.actor, "box.template.delete", t["name"], t["customer_id"], {})
    return {"ok": True}


# Fleet software updates (ADR 0054)


class SoftwareIn(BaseModel):
    site_ids: list[str] = Field(min_length=1, max_length=500)
    version: str = Field(max_length=48)


@router.get("/box-software/versions")
def software_versions(user: UserDep) -> dict:
    """Box software versions there are to install, newest first."""
    try:
        versions = boxes.releases()
    except Exception:  # noqa: BLE001 (GitHub unreachable or answering oddly)
        raise HTTPException(502, "Could not get the list of box software versions from GitHub.") from None
    return {"versions": versions, "latest": versions[0] if versions else None}


@router.get("/box-software")
def list_software(user: UserDep, customer_id: str | None = None) -> dict:
    """An organisation's boxes with the software each runs and what was asked of it."""
    with db.tx() as conn:
        cid = _org(user, customer_id)
        rows = conn.execute(
            """SELECT s.id AS site_id, s.name AS site, n.box, n.box_managed, n.box_software
               FROM sites s JOIN nodes n ON n.site_id = s.id
               WHERE s.customer_id = %s AND (n.box->>'console')::boolean ORDER BY s.name""",
            (cid,),
        ).fetchall()
    return {
        "boxes": [
            {
                "site_id": str(r["site_id"]),
                "site": r["site"],
                "managed": boxes.managed(r)["mode"],
                **boxes.software_state(r),
            }
            for r in rows
        ]
    }


@router.post("/box-software")
def send_software(body: SoftwareIn, user: UserDep, customer_id: str | None = None) -> dict:
    """Ask boxes for a software version: a newer one to update, an earlier one to
    go back. Each box restarts into it and keeps it only if it comes up healthy."""
    if not boxes.VERSION.match(body.version):
        raise HTTPException(400, "That is not a version number.")
    with db.tx() as conn:
        cid = _org(user, customer_id)
        require_org_manager(user, cid)
        results = []
        for sid in body.site_ids:
            res: dict = {"site_id": sid, "ok": False}
            row = None
            if _UUID.match(sid):
                row = conn.execute(
                    """SELECT n.id, n.name, n.customer_id, n.box, n.box_managed, n.box_software, s.name AS site
                       FROM sites s LEFT JOIN nodes n ON n.site_id = s.id
                       WHERE s.id = %s AND s.customer_id = %s FOR UPDATE OF s""",
                    (sid, cid),
                ).fetchone()
            if row is None:
                res["error"] = "No such location in this organisation."
            else:
                res["site"] = row["site"]
                if row["id"] is None or not (row["box"] or {}).get("console"):
                    res["error"] = "No ExaCarib box at this location."
                elif boxes.managed(row)["mode"] == "site":
                    res["error"] = "This box is managed on site. Switch Managed from to Cloud or both first."
                else:
                    boxes.set_software(conn, row, body.version, user.actor)
                    res["ok"] = True
            results.append(res)
        if any(r["ok"] for r in results):
            boxes.desired.refresh(conn, cid)
    return {"results": results}
