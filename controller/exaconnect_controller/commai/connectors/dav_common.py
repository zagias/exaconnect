"""What the CalDAV and CardDAV connectors share (ADR 0028): Basic auth with
an app password, discovery of the user's collections, WebDAV calls, and a
stand-in DAV server that behaves like iCloud, Fastmail or Nextcloud do for
the requests CommAI makes (PROPFIND, REPORT, GET, PUT with If-None-Match and
If-Match, DELETE, sync-collection).
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ..standards import dav, ical
from . import ConnectorError
from .kit import Credential, KitConnector, Request, Response, Setting, Simulator

STANDIN_SERVER = "https://dav.standin.exacarib.invalid/"


class DavConnector(KitConnector):
    auth = "credentials"
    credentials = (
        Credential("username", "User name", secret=False, pattern=r"[^\s:]{1,200}"),
        Credential("password", "App password", pattern=r"\S{4,200}"),
    )
    settings_fields = (
        Setting("server_url", "Server address", pattern=r"https://[A-Za-z0-9.-]+(:\d+)?(/[^\s]*)?", required=True),
        Setting("collection_url", "Collection address", pattern=r"https://[A-Za-z0-9.-]+(:\d+)?(/[^\s]*)?"),
    )
    home_set: tuple[str, str] = ("", "")  # (ns, name) of the home-set property
    collection_type: tuple[str, str] = ("", "")  # resourcetype of a usable collection
    setting_key = "collection_url"

    def base_url(self, conn, connection: dict) -> str:
        if self.simulated(connection):
            return STANDIN_SERVER
        url = (connection.get("settings") or {}).get("server_url") or ""
        if not url:
            raise ConnectorError(f"Set the {self.label} server address first.", "mapping")
        from .. import webhooks

        try:
            webhooks.check_url(url)  # public HTTPS servers only: never the controller's own network
        except webhooks.UnsafeURL as e:
            raise ConnectorError(str(e), "mapping") from None
        return url

    def auth_headers(self, conn, connection: dict) -> dict:
        if self.simulated(connection):
            return {"Authorization": "Basic " + base64.b64encode(b"standin:standin").decode()}
        from ..automation import oauth

        c = oauth.credentials(conn, connection)
        raw = f"{c.get('username', '')}:{c.get('password', '')}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def cause(self, status: int, body: Any) -> str:
        if status == 401:
            return "expired_signin"
        if status == 403:
            return "permission"
        if status == 404:
            return "mapping"
        if status in (400, 409, 412, 415, 422):
            return "input"
        return "provider"

    def dav(
        self,
        conn,
        connection: dict,
        method: str,
        url: str,
        body: bytes | None = None,
        *,
        depth: str | None = None,
        ctype: str = "application/xml; charset=utf-8",
        headers: dict | None = None,
        ok: tuple[int, ...] = (200, 201, 204, 207),
        allow: tuple[int, ...] = (),
    ) -> Response:
        h = {"Accept": "*/*", **(headers or {})}
        if body is not None:
            h["Content-Type"] = ctype
        if depth is not None:
            h["Depth"] = depth
        return self.call(conn, connection, method, url, content=body, headers=h, ok=ok, allow=allow)

    def collection(self, conn, connection: dict) -> str:
        """The collection's absolute URL: the setting, or found by discovery and remembered."""
        s = connection.get("settings") or {}
        if s.get(self.setting_key) and not self.simulated(connection):
            return s[self.setting_key]
        base = self.base_url(conn, connection)
        r = self.dav(conn, connection, "PROPFIND", base, dav.propfind((dav.DAV, "current-user-principal")), depth="0")
        res, _ = dav.multistatus(_text(r.body))
        principal = next((h for x in res for h in x.child_hrefs(dav.DAV, "current-user-principal")), "")
        if not principal:
            raise ConnectorError(f"{self.label}: the server did not name your account (principal).", "mapping")
        r = self.dav(conn, connection, "PROPFIND", dav.join(base, principal), dav.propfind(self.home_set), depth="0")
        res, _ = dav.multistatus(_text(r.body))
        home = next((h for x in res for h in x.child_hrefs(*self.home_set)), "")
        if not home:
            raise ConnectorError(f"{self.label}: the server did not name your collections.", "mapping")
        r = self.dav(
            conn,
            connection,
            "PROPFIND",
            dav.join(base, home),
            dav.propfind((dav.DAV, "resourcetype"), (dav.DAV, "displayname")),
            depth="1",
        )
        res, _ = dav.multistatus(_text(r.body))
        found = [x for x in res if x.is_collection(self.collection_type)]
        if not found:
            raise ConnectorError(f"{self.label}: no collection found in your account.", "mapping")
        url = dav.join(base, found[0].href)
        if not self.simulated(connection):
            conn.execute(
                "UPDATE integration_connections SET settings = settings || %s WHERE id = %s",
                (Jsonb({self.setting_key: url}), connection["id"]),
            )
        return url

    def health(self, conn, connection: dict) -> dict:
        try:
            url = self.collection(conn, connection)
            self.dav(conn, connection, "PROPFIND", url, dav.propfind((dav.DAV, "displayname")), depth="0")
        except ConnectorError as e:
            return {"ok": False, "cause": e.cause, "detail": str(e)}
        except dav.DavError as e:
            return {"ok": False, "cause": "provider", "detail": str(e)}
        return {"ok": True, "cause": "", "detail": f"{self.label} collection answering."}


def _text(body: Any) -> str:
    return body if isinstance(body, str) else ""


def etag(body: str) -> str:
    return '"' + hashlib.sha256(body.encode()).hexdigest()[:16] + '"'


class DavStandIn(Simulator):
    """A DAV server stand-in with one calendar and one address book."""

    PRINCIPAL = "/principals/me/"
    CAL_HOME = "/calendars/me/"
    CALENDAR = "/calendars/me/bookings/"
    CARD_HOME = "/addressbooks/me/"
    BOOK = "/addressbooks/me/contacts/"

    def __init__(self, app: str):
        self.app = app
        super().__init__()

    def error_body(self, status: int, message: str) -> Any:
        return f'<?xml version="1.0"?><d:error xmlns:d="DAV:"><d:message>{message}</d:message></d:error>'

    # ---- storage of resources ------------------------------------------------------

    def _resources(self, conn, connection, coll: str) -> list[dict]:
        return [r for r in self.all(conn, connection, "dav") if r["href"].startswith(coll) and not r.get("deleted")]

    def _sync(self, conn, connection) -> dict:
        return self.get(conn, connection, "davsync", "state") or {"n": 1, "log": []}

    def _changed(self, conn, connection, href: str, deleted: bool) -> None:
        s = self._sync(conn, connection)
        s["n"] += 1
        s["log"] = [x for x in s["log"] if x["href"] != href] + [{"href": href, "n": s["n"], "deleted": deleted}]
        self.put(conn, connection, "davsync", "state", s)

    # ---- methods -------------------------------------------------------------------

    @Simulator.route("PROPFIND", r".*")
    def propfind(self, conn, connection, req: Request, m) -> tuple:
        path = req.path
        wanted = [
            c.tag
            for c in dav.parse_request(req.raw or b"").iter()
            if c.tag.startswith("{") and c.tag != "{DAV:}propfind" and c.tag != "{DAV:}prop"
        ]
        depth = dav_header(req.headers, "Depth") or "0"

        def props_for(href: str) -> dict:
            p: dict[str, object] = {}
            for tag in wanted:
                if tag == dav.q(dav.DAV, "current-user-principal"):
                    p[tag] = dav.href_el(self.PRINCIPAL)
                elif tag == dav.q(dav.CALDAV, "calendar-home-set"):
                    p[tag] = dav.href_el(self.CAL_HOME)
                elif tag == dav.q(dav.CARDDAV, "addressbook-home-set"):
                    p[tag] = dav.href_el(self.CARD_HOME)
                elif tag == dav.q(dav.DAV, "resourcetype"):
                    kids = [dav.el(dav.DAV, "collection")] if href.endswith("/") else []
                    if href == self.CALENDAR:
                        kids.append(dav.el(dav.CALDAV, "calendar"))
                    if href == self.BOOK:
                        kids.append(dav.el(dav.CARDDAV, "addressbook"))
                    p[tag] = kids
                elif tag == dav.q(dav.DAV, "displayname"):
                    p[tag] = {self.CALENDAR: "Bookings", self.BOOK: "Contacts"}.get(href, "")
                elif tag == dav.q(dav.DAV, "sync-token"):
                    p[tag] = f"standin-sync-{self._sync(conn, connection)['n']}"
            return p

        items = [(path, props_for(path), 200)]
        if depth == "1":
            if path == self.CAL_HOME:
                items.append((self.CALENDAR, props_for(self.CALENDAR), 200))
            elif path == self.CARD_HOME:
                items.append((self.BOOK, props_for(self.BOOK), 200))
        return 207, dav.response_xml(items), {"Content-Type": "application/xml; charset=utf-8"}

    @Simulator.route("REPORT", r".*")
    def report(self, conn, connection, req: Request, m) -> tuple:
        root = dav.parse_request(req.raw or b"")
        coll = req.path
        items = []
        if root.tag == dav.q(dav.CALDAV, "calendar-query"):
            tr = root.find(".//" + dav.q(dav.CALDAV, "time-range"))
            start = ical.to_time(ical.Prop("X", {}, tr.get("start"))) if tr is not None else None
            end = ical.to_time(ical.Prop("X", {}, tr.get("end"))) if tr is not None else None
            for r in self._resources(conn, connection, coll):
                evs = ical.events(r["body"])
                if start and end and not any(e["start"] < end and start < e["end"] for e in evs):
                    continue
                items.append(
                    (
                        r["href"],
                        {dav.q(dav.DAV, "getetag"): r["etag"], dav.q(dav.CALDAV, "calendar-data"): r["body"]},
                        200,
                    )
                )
        elif root.tag == dav.q(dav.CARDDAV, "addressbook-query"):
            pf = root.find(".//" + dav.q(dav.CARDDAV, "prop-filter"))
            tm = root.find(".//" + dav.q(dav.CARDDAV, "text-match"))
            name = (pf.get("name") if pf is not None else "EMAIL").upper()
            want = (tm.text or "").strip().lower() if tm is not None else ""
            for r in self._resources(conn, connection, coll):
                comp = next(ical.parse(r["body"]).walk("VCARD"), None)
                vals = [
                    p.value.strip().lower().removeprefix("mailto:").removeprefix("tel:")
                    for p in (comp.all(name) if comp else [])
                ]
                if want in vals:
                    items.append(
                        (
                            r["href"],
                            {dav.q(dav.DAV, "getetag"): r["etag"], dav.q(dav.CARDDAV, "address-data"): r["body"]},
                            200,
                        )
                    )
        elif root.tag == dav.q(dav.DAV, "sync-collection"):
            tok = (root.findtext(dav.q(dav.DAV, "sync-token")) or "").strip()
            since = int(tok.rsplit("-", 1)[-1]) if re.fullmatch(r"standin-sync-\d+", tok) else 0
            if tok and not since:
                return 403, self.error_body(403, "valid-sync-token"), {"Content-Type": "application/xml"}
            s = self._sync(conn, connection)
            for x in s["log"]:
                if x["n"] > since and x["href"].startswith(coll):
                    if x["deleted"]:
                        items.append((x["href"], {}, 404))
                    else:
                        r = self.get(conn, connection, "dav", x["href"])
                        items.append(
                            (
                                x["href"],
                                {dav.q(dav.DAV, "getetag"): r["etag"], dav.q(dav.CARDDAV, "address-data"): r["body"]},
                                200,
                            )
                        )
            return 207, dav.response_xml(items, f"standin-sync-{s['n']}"), {"Content-Type": "application/xml"}
        else:
            return 400, self.error_body(400, "Unsupported REPORT"), {"Content-Type": "application/xml"}
        return 207, dav.response_xml(items), {"Content-Type": "application/xml; charset=utf-8"}

    @Simulator.route("GET", r".*")
    def get_resource(self, conn, connection, req: Request, m) -> tuple:
        r = self.get(conn, connection, "dav", req.path)
        if not r or r.get("deleted"):
            return 404, self.error_body(404, "Not found"), {"Content-Type": "application/xml"}
        return 200, r["body"], {"ETag": r["etag"], "Content-Type": r["ctype"]}

    @Simulator.route("PUT", r".*")
    def put_resource(self, conn, connection, req: Request, m) -> tuple:
        cur = self.get(conn, connection, "dav", req.path)
        exists = bool(cur and not cur.get("deleted"))
        if dav_header(req.headers, "If-None-Match") == "*" and exists:
            return 412, self.error_body(412, "Precondition failed"), {"Content-Type": "application/xml"}
        im = dav_header(req.headers, "If-Match")
        if im and (not exists or im != cur["etag"]):
            return 412, self.error_body(412, "Precondition failed"), {"Content-Type": "application/xml"}
        body = req.body if isinstance(req.body, str) else ""
        try:
            ical.parse(body)
        except ical.ICalError:
            return 415, self.error_body(415, "Unsupported media"), {"Content-Type": "application/xml"}
        tag = etag(body)
        ctype = dav_header(req.headers, "Content-Type") or "text/plain"
        self.put(conn, connection, "dav", req.path, {"href": req.path, "body": body, "etag": tag, "ctype": ctype})
        self._changed(conn, connection, req.path, False)
        return (204 if exists else 201), "", {"ETag": tag}

    @Simulator.route("DELETE", r".*")
    def delete_resource(self, conn, connection, req: Request, m) -> tuple:
        cur = self.get(conn, connection, "dav", req.path)
        if not cur or cur.get("deleted"):
            return 404, self.error_body(404, "Not found"), {"Content-Type": "application/xml"}
        im = dav_header(req.headers, "If-Match")
        if im and im != cur["etag"]:
            return 412, self.error_body(412, "Precondition failed"), {"Content-Type": "application/xml"}
        self.put(conn, connection, "dav", req.path, {**cur, "deleted": True})
        self._changed(conn, connection, req.path, True)
        return 204, ""


def dav_header(headers: dict, name: str) -> str:
    n = name.lower()
    return next((str(v) for k, v in (headers or {}).items() if k.lower() == n), "")


def remember_setting(conn: psycopg.Connection, connection: dict, values: dict) -> None:
    conn.execute(
        "UPDATE integration_connections SET settings = settings || %s WHERE id = %s",
        (Jsonb(values), connection["id"]),
    )
