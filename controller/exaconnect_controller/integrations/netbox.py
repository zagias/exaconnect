"""NetBox inventory sync (ADR 0026): sites, carrier links and LAN prefixes.

Direction is chosen per integration:

- ``push``: Connect is the source; NetBox is updated to match (dcim/sites,
  circuits/providers, circuits/circuit-types, circuits/circuits, ipam/prefixes).
- ``pull``: NetBox is the source for what it knows better: each Connect
  site's location, coordinates and LAN prefixes are read from the NetBox
  site with the same slug. Sites are never created or deleted from NetBox
  (they need an ASN and an overlay address), so unmatched ones are reported.

Matching is by slug (sites, providers), cid (circuits) and prefix.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

import psycopg

from .. import audit
from .providers import Field, Provider, ProviderError, raise_for, register
from .transport import Http, Transport, sim_ok

TYPE_SLUG = "exacarib-underlay"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")[:100] or "unnamed"


@register
class NetBox(Provider):
    key = "netbox"
    name = "NetBox"
    category = "inventory"
    docs = "https://netboxlabs.com/docs/netbox/en/stable/integrations/rest-api/"
    api = "NetBox REST API v4: dcim/sites, circuits/providers, circuits/circuits, ipam/prefixes"
    live_needs = "Your NetBox address and an API token (write access for push, read for pull)."
    receives_events = False
    owners = ("customer",)
    fields = (
        Field("url", "NetBox address", required=True, kind="url"),
        Field("token", "API token", secret=True, required=True),
        Field("direction", "Source of truth", default="push", kind="choice", choices=("push", "pull"),
              help="push: Connect updates NetBox. pull: NetBox updates Connect's site details and prefixes."),
        Field("interval_minutes", "Sync every (minutes)", default=60, kind="int"),
    )

    def simulate(self, method: str, url: str, headers: dict, body: bytes) -> Http:
        import json

        if method == "GET":
            return sim_ok(200, {"count": 0, "next": None, "previous": None, "results": []})
        data = json.loads(body or b"{}")
        return sim_ok(201 if method == "POST" else 200, {"id": abs(hash(url + str(data))) % 100000 + 1, **data})


class Api:
    def __init__(self, http: Transport, base: str, token: str):
        self.http, self.base = http, base.rstrip("/")
        self.headers = {"Authorization": f"Token {token}", "Accept": "application/json"}

    def get(self, path: str, **params: Any) -> list[dict]:
        import urllib.parse

        url = f"{self.base}/api/{path}/?{urllib.parse.urlencode(params)}"
        out: list[dict] = []
        for _ in range(20):
            resp = self.http.request("GET", url, headers=self.headers)
            raise_for(resp, "NetBox")
            body = resp.json()
            out += body.get("results") or []
            url = body.get("next")
            if not url:
                break
        return out

    def upsert(self, path: str, match: dict, body: dict) -> tuple[dict, str]:
        found = self.get(path, **match)
        if found:
            cur = found[0]
            changed = {k: v for k, v in body.items() if _differs(cur.get(k), v)}
            if not changed:
                return cur, "unchanged"
            resp = self.http.request("PATCH", f"{self.base}/api/{path}/{cur['id']}/", headers=self.headers, json_body=changed)
            raise_for(resp, "NetBox")
            return resp.json(), "updated"
        resp = self.http.request("POST", f"{self.base}/api/{path}/", headers=self.headers, json_body=body)
        raise_for(resp, "NetBox")
        return resp.json(), "created"


def _differs(cur: Any, want: Any) -> bool:
    if isinstance(cur, dict):  # nested object or choice: compare its id / value
        cur = cur.get("id", cur.get("value"))
    if isinstance(cur, float) or isinstance(want, float):
        try:
            return abs(float(cur) - float(want)) > 1e-6
        except (TypeError, ValueError):
            return True
    return cur != want


def push(conn: psycopg.Connection, api: Api, customer_id: Any) -> dict:
    counts = {"created": 0, "updated": 0, "unchanged": 0}

    def tally(what: str) -> None:
        counts[what] += 1

    sites = conn.execute(
        "SELECT * FROM sites WHERE customer_id = %s ORDER BY kind DESC, name", (customer_id,)
    ).fetchall()
    site_ids: dict[Any, int] = {}
    for s in sites:
        body = {
            "name": s["name"],
            "slug": slug(s["name"]),
            "status": "active",
            "description": (s["location"] or "")[:200],
            "time_zone": s["timezone"] or "UTC",
            "comments": f"Managed by ExaCarib Connect ({s['kind']}).",
        }
        if s["latitude"] is not None:
            body["latitude"] = round(float(s["latitude"]), 6)
            body["longitude"] = round(float(s["longitude"]), 6)
        obj, what = api.upsert("dcim/sites", {"slug": body["slug"]}, body)
        site_ids[s["id"]] = obj["id"]
        tally(what)
    ctype, what = api.upsert(
        "circuits/circuit-types", {"slug": TYPE_SLUG}, {"name": "ExaCarib underlay", "slug": TYPE_SLUG}
    )
    tally(what)
    links = conn.execute(
        """SELECT l.*, c.name AS carrier, s.name AS site FROM links l JOIN carriers c ON c.id = l.carrier_id
           JOIN sites s ON s.id = l.site_id WHERE l.customer_id = %s ORDER BY s.name, l.path""",
        (customer_id,),
    ).fetchall()
    providers: dict[str, int] = {}
    for lk in links:
        if lk["carrier"] not in providers:
            obj, what = api.upsert(
                "circuits/providers", {"slug": slug(lk["carrier"])}, {"name": lk["carrier"], "slug": slug(lk["carrier"])}
            )
            providers[lk["carrier"]] = obj["id"]
            tally(what)
        cid = f"{lk['site']}-{lk['path']}"
        obj, what = api.upsert(
            "circuits/circuits",
            {"cid": cid},
            {
                "cid": cid,
                "provider": providers[lk["carrier"]],
                "type": ctype["id"],
                "status": "active",
                "commit_rate": int(float(lk["commit_mbps"] or 0) * 1000),  # kbps
                "description": f"{lk['underlay_type']} link on path {lk['path']} at {lk['site']}",
                "comments": f"ExaCarib Connect link {lk['id']}",
            },
        )
        tally(what)
    for s in sites:
        for p in s["lan_prefixes"] or []:
            obj, what = api.upsert(
                "ipam/prefixes",
                {"prefix": str(p)},
                {
                    "prefix": str(p),
                    "status": "active",
                    "scope_type": "dcim.site",
                    "scope_id": site_ids[s["id"]],
                    "description": f"LAN at {s['name']} (ExaCarib Connect)",
                },
            )
            tally(what)
    return counts


def pull(conn: psycopg.Connection, api: Api, customer_id: Any, actor: str) -> dict:
    from .. import desired

    changed: list[str] = []
    unmatched: list[str] = []
    for s in conn.execute("SELECT * FROM sites WHERE customer_id = %s ORDER BY name", (customer_id,)).fetchall():
        found = api.get("dcim/sites", slug=slug(s["name"]))
        if not found:
            unmatched.append(s["name"])
            continue
        nb = found[0]
        prefixes = sorted(
            {
                str(ipaddress.ip_network(p["prefix"], strict=False))
                for p in api.get("ipam/prefixes", site_id=nb["id"])
                if p.get("prefix") and ((p.get("status") or {}).get("value", "active") == "active")
            }
        )
        updates: dict[str, Any] = {}
        if nb.get("description") and nb["description"] != s["location"]:
            updates["location"] = nb["description"][:200]
        if nb.get("latitude") is not None and nb.get("longitude") is not None:
            if s["latitude"] is None or abs(float(nb["latitude"]) - float(s["latitude"])) > 1e-6 or abs(
                float(nb["longitude"]) - float(s["longitude"])
            ) > 1e-6:
                updates["latitude"], updates["longitude"] = float(nb["latitude"]), float(nb["longitude"])
        if prefixes and sorted(str(p) for p in s["lan_prefixes"]) != prefixes:
            updates["lan_prefixes"] = prefixes
        if updates:
            sets = ", ".join(f"{k} = %({k})s" + ("::cidr[]" if k == "lan_prefixes" else "") for k in updates)
            conn.execute(f"UPDATE sites SET {sets} WHERE id = %(id)s", {**updates, "id": s["id"]})
            audit.record(conn, actor, "site.netbox_pull", s["name"], customer_id, {"fields": sorted(updates)})
            changed.append(s["name"])
    if changed:
        desired.refresh(conn, customer_id)
    return {"changed": changed, "unmatched": unmatched}


def sync(conn: psycopg.Connection, integ: dict, http: Transport, secrets: dict, actor: str = "system:netbox") -> dict:
    cfg = integ["config"] or {}
    if not integ["customer_id"]:
        raise ProviderError("NetBox sync belongs to an organisation.", retry=False)
    api = Api(http, cfg["url"], secrets.get("token", ""))
    if cfg.get("direction") == "pull":
        out = {"direction": "pull", **pull(conn, api, integ["customer_id"], actor)}
    else:
        out = {"direction": "push", **push(conn, api, integ["customer_id"])}
    return out

