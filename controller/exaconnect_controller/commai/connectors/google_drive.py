"""Google Drive connector (ADR 0035): folders a business picks become
knowledge sources a person approves.

Written from the Drive API v3 (files.list, files.get alt=media,
files.export, changes.getStartPageToken, changes.watch). Sign-in is Google
OAuth with ExaCarib's Google app (EXA_GOOGLE_CLIENT_ID/SECRET, shared with
Google Calendar) and the scope drive.readonly, a restricted scope: Google's
verification and security assessment are needed before going live for
everyone.

- Google Docs are exported as plain text and Sheets as CSV; text, Markdown
  and CSV files are read as they are. Other types are listed as skipped.
- Files whose owner turned off downloading or copying are skipped. With the
  ``organisation`` rule, only files shared with the business's whole domain
  (``domain`` setting) or with anyone are used.
- Pages: nextPageToken. Rate limits: 429 and 403 rateLimitExceeded.
- Changes: a Drive push channel (changes.watch) to the business's app hook;
  each notification carries X-Goog-Channel-Token, checked against the hook's
  secret, and queues a re-sync. Channels expire, so a sync renews it.
"""

from __future__ import annotations

import datetime as dt
import secrets
import urllib.parse
from typing import Any

from ..automation import oauth
from . import kit
from .knowledge_files import KnowledgeFiles, as_text
from .more_common import app_hook, hook_url, state_get, state_put

FOLDER = "application/vnd.google-apps.folder"
EXPORT = {"application/vnd.google-apps.document": "text/plain", "application/vnd.google-apps.spreadsheet": "text/csv"}
DIRECT = ("text/plain", "text/markdown", "text/csv", "text/x-markdown")
FIELDS = (
    "nextPageToken,files(id,name,mimeType,version,size,webViewLink,trashed,"
    "capabilities/canDownload,copyRequiresWriterPermission,permissions(type,domain))"
)

oauth.PROVIDERS["google_drive"] = oauth.Provider(
    "google_drive",
    "Google",
    "https://accounts.google.com/o/oauth2/v2/auth",
    "https://oauth2.googleapis.com/token",
    ("https://www.googleapis.com/auth/drive.readonly",),
    "EXA_GOOGLE",
    (("access_type", "offline"), ("prompt", "consent"), ("include_granted_scopes", "true")),
)


class DriveSim(kit.Simulator):
    """Answers like the Drive API, with an example folder of help documents."""

    app = "google_drive"

    def _seed(self, conn, c) -> list[dict]:
        files = self.all(conn, c, "file")
        if files:
            return files
        seed = [
            {"id": "fld_help", "name": "Help centre (example data)", "mimeType": FOLDER, "parents": ["root"]},
            {
                "id": "doc_hours",
                "name": "Opening hours",
                "mimeType": "application/vnd.google-apps.document",
                "parents": ["fld_help"],
                "version": "1",
                "text": "We are open 8am to 4pm, Monday to Friday.",
            },
            {
                "id": "txt_fees",
                "name": "Fees.md",
                "mimeType": "text/markdown",
                "parents": ["fld_help"],
                "version": "1",
                "size": "60",
                "text": "# Fees\nA replacement card costs 25 dollars.",
            },
            {
                "id": "doc_locked",
                "name": "Board minutes",
                "mimeType": "application/vnd.google-apps.document",
                "parents": ["fld_help"],
                "version": "1",
                "copyRequiresWriterPermission": True,
                "text": "secret",
            },
            {
                "id": "pdf_form",
                "name": "Form.pdf",
                "mimeType": "application/pdf",
                "parents": ["fld_help"],
                "version": "1",
                "size": "1000",
            },
        ]
        for f in seed:
            self.put(conn, c, "file", f["id"], f)
        return seed

    @kit.Simulator.route("GET", r"/drive/v3/about$")
    def about(self, conn, c, req, m):
        return 200, {"user": {"displayName": "Example user"}}

    @kit.Simulator.route("GET", r"/drive/v3/files$")
    def list(self, conn, c, req, m):
        q = req.query.get("q", "")
        parent = q.split("'")[1] if q.startswith("'") else "root"
        out = [
            {k: v for k, v in f.items() if k != "text"}
            for f in self._seed(conn, c)
            if parent in f.get("parents", []) and not f.get("trashed")
        ]
        for f in out:
            f.setdefault("capabilities", {"canDownload": True})
            f.setdefault("webViewLink", f"https://drive.google.com/file/d/{f['id']}/view")
        return 200, {"files": out}

    @kit.Simulator.route("GET", r"/drive/v3/files/([^/]+)/export$")
    def export(self, conn, c, req, m):
        f = self.get(conn, c, "file", m.group(1))
        return (200, f["text"], {"Content-Type": "text/plain"}) if f else (404, self.error_body(404, "File not found"))

    @kit.Simulator.route("GET", r"/drive/v3/files/([^/]+)$")
    def media(self, conn, c, req, m):
        f = self.get(conn, c, "file", m.group(1))
        return (200, f.get("text", ""), {"Content-Type": f["mimeType"]}) if f else (404, self.error_body(404, "x"))

    @kit.Simulator.route("GET", r"/drive/v3/changes/startPageToken$")
    def start(self, conn, c, req, m):
        return 200, {"startPageToken": "100"}

    @kit.Simulator.route("POST", r"/drive/v3/changes/watch$")
    def watch(self, conn, c, req, m):
        b = req.body or {}
        return 200, {
            "kind": "api#channel",
            "id": b.get("id"),
            "resourceId": "res-sim",
            "expiration": b.get("expiration"),
        }


class GoogleDrive(KnowledgeFiles):
    app = "google_drive"
    label = "Google Drive"
    description = "Turn documents in chosen Google Drive folders into knowledge a person approves."
    auth = "oauth"
    simulator = DriveSim()
    health_path = "/drive/v3/about?fields=user"
    needs_from_exacarib = (
        "The Google app used for Calendar (EXA_GOOGLE_CLIENT_ID/SECRET) with the Drive API switched on and the "
        "drive.readonly scope added; Google's restricted-scope verification before going live for everyone."
    )
    webhooks = "Drive push notifications (changes.watch), checked with the channel token."
    docs_url = "https://developers.google.com/drive/api/reference/rest/v3"
    settings_fields = (
        kit.Setting("folders", "Folders to sync (folder ids, comma separated)", KnowledgeFiles.folder_pattern),
        kit.Setting(
            "sharing",
            "Which files: folder (all in the folders) or organisation (shared with everyone)",
            "folder|organisation",
        ),
        kit.Setting("domain", "Your organisation's Google domain", r"[a-z0-9.-]{3,120}"),
    )

    def base_url(self, conn, connection: dict) -> str:
        return "https://www.googleapis.com"

    def cause(self, status: int, body: Any) -> str:
        err = body.get("error") if isinstance(body, dict) else None
        reasons = [e.get("reason") for e in (err.get("errors") or [])] if isinstance(err, dict) else []
        if status == 403 and {"rateLimitExceeded", "userRateLimitExceeded"} & set(reasons):
            return "provider"
        if status == 404:
            return "mapping"
        return super().cause(status, body)

    def children(self, conn, connection: dict, folder: str) -> list[dict]:
        folder = folder or "root"
        if not all(ch.isalnum() or ch in "_-" for ch in folder):
            raise kit.ConnectorError("That is not a Google Drive folder id.", "input")
        items = self.paginate(
            conn,
            connection,
            "/drive/v3/files",
            params={
                "q": f"'{folder}' in parents and trashed=false",
                "fields": FIELDS,
                "pageSize": 100,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
            items=lambda b: (b or {}).get("files") or [],
            next_page=lambda r: {"pageToken": r.body["nextPageToken"]} if (r.body or {}).get("nextPageToken") else None,
            max_items=500,
        )
        out = []
        for f in items:
            mt = f.get("mimeType", "")
            out.append(
                {
                    "id": f["id"],
                    "name": f.get("name", ""),
                    "folder": mt == FOLDER,
                    "version": str(f.get("version", "")),
                    "url": f.get("webViewLink", ""),
                    "kind": "export" if mt in EXPORT else ("media" if mt in DIRECT else ""),
                    "size": f.get("size") or 0,
                    "meta": f,
                }
            )
        return out

    def rule(self, connection: dict, item: dict) -> str:
        f = item.get("meta") or {}
        if f.get("copyRequiresWriterPermission") or (f.get("capabilities") or {}).get("canDownload") is False:
            return "The owner turned off downloading or copying."
        s = self.settings(connection)
        if s.get("sharing") == "organisation":
            perms = f.get("permissions") or []
            ok = any(
                p.get("type") == "anyone" or (p.get("type") == "domain" and p.get("domain") == s.get("domain"))
                for p in perms
            )
            if not ok:
                return "Not shared with the whole organisation."
        return ""

    def text(self, conn, connection: dict, item: dict) -> str | None:
        fid = urllib.parse.quote(item["id"], safe="")
        if item["kind"] == "export":
            r = self.call(
                conn,
                connection,
                "GET",
                f"/drive/v3/files/{fid}/export",
                params={"mimeType": EXPORT[item["meta"]["mimeType"]]},
            )
        else:
            r = self.call(
                conn, connection, "GET", f"/drive/v3/files/{fid}", params={"alt": "media", "supportsAllDrives": "true"}
            )
        return as_text(r.body)

    def sync(self, conn, connection: dict) -> dict:
        out = super().sync(conn, connection)
        ch = state_get(conn, connection["customer_id"], self.app, "channel")
        if ch and ch.get("expires", 0) < (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).timestamp():
            self.register_webhooks(conn, connection, "commai:renew")
        return out

    # ---- push notifications ------------------------------------------------------------

    def register_webhooks(self, conn, connection: dict, actor: str) -> dict:
        hook = app_hook(conn, connection["customer_id"], self.app, actor)
        token = self.call(
            conn, connection, "GET", "/drive/v3/changes/startPageToken", params={"supportsAllDrives": "true"}
        ).body["startPageToken"]
        expires = dt.datetime.now(dt.UTC) + dt.timedelta(days=6)
        body = {
            "id": secrets.token_hex(16),
            "type": "web_hook",
            "address": hook_url(hook),
            "token": hook["secret"],
            "expiration": str(int(expires.timestamp() * 1000)),
        }
        ch = self.call(
            conn,
            connection,
            "POST",
            "/drive/v3/changes/watch",
            params={"pageToken": token, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"},
            json_body=body,
        ).body
        state_put(
            conn,
            connection["customer_id"],
            self.app,
            "channel",
            {"id": ch.get("id"), "resource": ch.get("resourceId"), "expires": expires.timestamp()},
        )
        return {"manual": False, "address": hook_url(hook), "channel": ch.get("id"), "expires": expires.isoformat()}

    def verify_webhook(self, conn, connection, hook, headers, body, query) -> bool:
        return kit.same(kit.header(headers, "X-Goog-Channel-Token"), hook.get("secret", ""))

    def webhook_events(self, body: bytes, headers: dict) -> list[dict]:
        state = kit.header(headers, "X-Goog-Resource-State")
        if state in ("", "sync"):
            return []
        msg = kit.header(headers, "X-Goog-Message-Number")
        return [
            {
                "type": "knowledge.changed",
                "id": f"{kit.header(headers, 'X-Goog-Channel-ID')}:{msg}",
                "data": {"state": state},
            }
        ]


kit.register(GoogleDrive())
