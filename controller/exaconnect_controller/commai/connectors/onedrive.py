"""OneDrive and SharePoint connector (ADR 0035): folders a business picks
become knowledge sources a person approves.

Written from Microsoft Graph v1.0 (drives, driveItem children and content,
sites search, subscriptions). Sign-in is Microsoft identity platform OAuth
with ExaCarib's multi-tenant Entra ID app (the Microsoft 365 app,
EXA_MS365_CLIENT_ID/SECRET) and the delegated permissions Files.Read.All
and Sites.Read.All, which need an organisation admin's consent.

- Folders are named "drive id/item id" (from list_folders or
  find_libraries). Text, Markdown and CSV files are read; Word and PDF files
  are listed as skipped (Graph has no plain-text conversion).
- With the ``organisation`` rule, only files shared with the whole
  organisation (or by link with anyone) are used.
- Pages: @odata.nextLink. Rate limits: 429 and 503 with Retry-After.
- Changes: a Graph subscription on each drive's root to the business's app
  hook; Graph first checks the address (validationToken, echoed back), then
  each notification carries clientState, checked against the hook's secret.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import urllib.parse
from typing import Any

from ..automation import oauth
from . import ActionSpec, ConnectorError, Field, kit
from .knowledge_files import KnowledgeFiles, as_text
from .more_common import app_hook, hook_url

TEXT = (".txt", ".md", ".markdown", ".csv")
SELECT = "id,name,file,folder,cTag,eTag,size,webUrl,shared,parentReference"
GRAPH = "https://graph.microsoft.com/v1.0"
_MS = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0"

oauth.PROVIDERS["onedrive"] = oauth.Provider(
    "onedrive",
    "Microsoft",
    _MS + "/authorize",
    _MS + "/token",
    ("offline_access", "User.Read", "Files.Read.All", "Sites.Read.All"),
    "EXA_MS365",
    (("response_mode", "query"),),
    defaults=(("tenant", "common"),),
)


def _ref(item: dict) -> str:
    drive = (item.get("parentReference") or {}).get("driveId", "")
    return f"{drive}/{item['id']}"


class GraphSim(kit.Simulator):
    """Answers like Microsoft Graph, with an example SharePoint library."""

    app = "onedrive"

    def error_body(self, status: int, message: str) -> Any:
        return {
            "error": {
                "code": {401: "InvalidAuthenticationToken", 403: "accessDenied"}.get(status, "generalException"),
                "message": message,
            }
        }

    def _seed(self, conn, c) -> list[dict]:
        items = self.all(conn, c, "item")
        if items:
            return items
        d = "b!sim"
        seed = [
            {"id": "root", "name": "root", "folder": {}, "parent": ""},
            {"id": "F1", "name": "Policies (example data)", "folder": {"childCount": 3}, "parent": "root"},
            {
                "id": "I1",
                "name": "Returns.md",
                "file": {"mimeType": "text/markdown"},
                "parent": "F1",
                "cTag": "c1",
                "size": 80,
                "shared": {"scope": "organization"},
                "text": "Returns are accepted within 30 days with a receipt.",
            },
            {
                "id": "I2",
                "name": "Private notes.txt",
                "file": {"mimeType": "text/plain"},
                "parent": "F1",
                "cTag": "c1",
                "size": 20,
                "text": "Only for the manager.",
            },
            {
                "id": "I3",
                "name": "Handbook.docx",
                "file": {"mimeType": "application/msword"},
                "parent": "F1",
                "cTag": "c1",
                "size": 9000,
            },
        ]
        for i in seed:
            i["parentReference"] = {"driveId": d}
            i["webUrl"] = f"https://example.sharepoint.com/{i['name']}"
            self.put(conn, c, "item", i["id"], i)
        return seed

    @kit.Simulator.route("GET", r"/v1\.0/me/drive$")
    def me(self, conn, c, req, m):
        return 200, {"id": "b!sim", "driveType": "business"}

    @kit.Simulator.route("GET", r"/v1\.0/(?:me/drive/root|drives/[^/]+/items/([^/]+))/children$")
    def children(self, conn, c, req, m):
        parent = m.group(1) or "root"
        return 200, {
            "value": [
                {k: v for k, v in i.items() if k not in ("text", "parent")}
                for i in self._seed(conn, c)
                if i["parent"] == parent
            ]
        }

    @kit.Simulator.route("GET", r"/v1\.0/drives/[^/]+/items/([^/]+)/content$")
    def content(self, conn, c, req, m):
        i = self.get(conn, c, "item", m.group(1))
        return (200, i.get("text", ""), {"Content-Type": "text/plain"}) if i else (404, self.error_body(404, "x"))

    @kit.Simulator.route("GET", r"/v1\.0/sites$")
    def sites(self, conn, c, req, m):
        return 200, {"value": [{"id": "example.sharepoint.com,1,2", "displayName": "Customer service"}]}

    @kit.Simulator.route("GET", r"/v1\.0/sites/([^/]+)/drives$")
    def drives(self, conn, c, req, m):
        return 200, {"value": [{"id": "b!sim", "name": "Documents"}]}

    @kit.Simulator.route("POST", r"/v1\.0/subscriptions$")
    def subscribe(self, conn, c, req, m):
        return 201, {"id": f"sub-{self.new_id(conn, c, 'sub')}", **(req.body or {})}


class OneDrive(KnowledgeFiles):
    app = "onedrive"
    label = "OneDrive and SharePoint"
    description = "Turn files in chosen OneDrive or SharePoint folders into knowledge a person approves."
    auth = "oauth"
    simulator = GraphSim()
    health_path = "/me/drive"
    needs_from_exacarib = (
        "The multi-tenant Entra ID app used for Microsoft 365 (EXA_MS365_CLIENT_ID/SECRET) with the delegated "
        "permissions Files.Read.All and Sites.Read.All; each organisation's admin consents once."
    )
    webhooks = "Microsoft Graph change notifications, checked with clientState."
    docs_url = "https://learn.microsoft.com/graph/api/resources/onedrive"
    settings_fields = (
        kit.Setting("tenant", "Microsoft tenant (domain or id; default common)", r"[A-Za-z0-9.-]{1,120}"),
        kit.Setting("folders", "Folders to sync (drive id/item id, comma separated)", KnowledgeFiles.folder_pattern),
        kit.Setting(
            "sharing",
            "Which files: folder (all in the folders) or organisation (shared with everyone)",
            "folder|organisation",
        ),
    )
    actions = {
        **KnowledgeFiles.actions,
        "find_libraries": ActionSpec(
            "find_libraries", "Find SharePoint document libraries", "read", fields=(Field("search", "Site name"),)
        ),
    }

    def base_url(self, conn, connection: dict) -> str:
        return GRAPH

    def message(self, status: int, body: Any) -> str:
        return super().message(status, body).replace("OneDrive and SharePoint", "Microsoft Graph")

    def _path(self, folder: str) -> str:
        if not folder:
            return "/me/drive/root/children"
        drive, _, item = folder.partition("/")
        if not (re.fullmatch(r"[A-Za-z0-9!_.-]{1,200}", drive) and re.fullmatch(r"[A-Za-z0-9!_.-]{1,200}", item)):
            raise ConnectorError("A folder is named 'drive id/item id'.", "input")
        return f"/drives/{urllib.parse.quote(drive)}/items/{urllib.parse.quote(item)}/children"

    def children(self, conn, connection: dict, folder: str) -> list[dict]:
        items = self.paginate(
            conn,
            connection,
            self._path(folder),
            params={"$select": SELECT, "$top": 200},
            items=lambda b: (b or {}).get("value") or [],
            next_page=lambda r: (r.body or {}).get("@odata.nextLink"),
            max_items=500,
        )
        out = []
        for i in items:
            name = i.get("name", "")
            out.append(
                {
                    "id": _ref(i),
                    "name": name,
                    "folder": "folder" in i,
                    "version": str(i.get("cTag") or i.get("eTag") or ""),
                    "url": i.get("webUrl", ""),
                    "kind": "content" if name.lower().endswith(TEXT) else "",
                    "size": i.get("size") or 0,
                    "meta": i,
                }
            )
        return out

    def rule(self, connection: dict, item: dict) -> str:
        if self.settings(connection).get("sharing") == "organisation":
            scope = ((item.get("meta") or {}).get("shared") or {}).get("scope")
            if scope not in ("organization", "anonymous"):
                return "Not shared with the whole organisation."
        return ""

    def text(self, conn, connection: dict, item: dict) -> str | None:
        drive, _, iid = item["id"].partition("/")
        r = self.call(
            conn, connection, "GET", f"/drives/{urllib.parse.quote(drive)}/items/{urllib.parse.quote(iid)}/content"
        )
        return as_text(r.body)

    def execute(self, conn, connection: dict, action: str, inputs: dict, key: str) -> dict:
        if action == "find_libraries":
            sites = self.call(conn, connection, "GET", "/sites", params={"search": inputs["search"]}).body
            out = []
            for s in (sites.get("value") or [])[:10]:
                ds = self.call(conn, connection, "GET", f"/sites/{urllib.parse.quote(s['id'], safe=',.')}/drives").body
                for d in ds.get("value") or []:
                    out.append(
                        {"site": s.get("displayName", ""), "library": d.get("name", ""), "folder": f"{d['id']}/root"}
                    )
            return {"libraries": out}
        return super().execute(conn, connection, action, inputs, key)

    # ---- change notifications ----------------------------------------------------------

    def register_webhooks(self, conn, connection: dict, actor: str) -> dict:
        hook = app_hook(conn, connection["customer_id"], self.app, actor)
        drives = sorted({f.partition("/")[0] for f in self.folders(connection)})
        if not drives:
            raise ConnectorError("Choose the folders to sync first.", "mapping")
        expires = (dt.datetime.now(dt.UTC) + dt.timedelta(days=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
        subs = []
        for d in drives:
            r = self.call(
                conn,
                connection,
                "POST",
                "/subscriptions",
                json_body={
                    "changeType": "updated",
                    "notificationUrl": hook_url(hook),
                    "resource": f"/drives/{d}/root",
                    "expirationDateTime": expires,
                    "clientState": hook["secret"],
                },
            )
            subs.append(r.body.get("id"))
        return {"manual": False, "address": hook_url(hook), "subscriptions": subs, "expires": expires}

    def webhook_handshake(self, headers: dict, body: bytes, query: dict) -> tuple[int, str, str] | None:
        token = query.get("validationToken")
        if token is not None:
            return 200, "text/plain", str(token)[:1000]
        return None

    def verify_webhook(self, conn, connection, hook, headers, body, query) -> bool:
        try:
            values = json.loads(body).get("value") or []
        except (ValueError, AttributeError):
            return False
        return bool(values) and all(kit.same(str(v.get("clientState", "")), hook.get("secret", "")) for v in values)

    def webhook_events(self, body: bytes, headers: dict) -> list[dict]:
        try:
            values = json.loads(body).get("value") or []
        except (ValueError, AttributeError):
            return []
        if not values:
            return []
        v = values[0]
        digest = hashlib.sha256(body).hexdigest()[:32]  # every notification is its own delivery
        return [
            {
                "type": "knowledge.changed",
                "id": f"{v.get('subscriptionId', '')}:{digest}",
                "data": {"resource": v.get("resource", "")},
            }
        ]


kit.register(OneDrive())
