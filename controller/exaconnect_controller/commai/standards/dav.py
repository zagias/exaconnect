"""WebDAV (RFC 4918) with CalDAV (RFC 4791) and CardDAV (RFC 6352): request
bodies and multistatus parsing, shared by the CalDAV and CardDAV connectors
and their stand-ins.

Discovery follows RFC 6764 and 4791/6352: the current-user-principal, then
its calendar-home-set or addressbook-home-set, then the collections in it.
Documents with a DOCTYPE or entity declarations are refused before parsing.
"""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CARDDAV = "urn:ietf:params:xml:ns:carddav"
CS = "http://calendarserver.org/ns/"
NS = {"d": DAV, "c": CALDAV, "card": CARDDAV, "cs": CS}

for _p, _u in NS.items():
    ET.register_namespace(_p, _u)


def q(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}"


class DavError(ValueError):
    pass


def _xml(text: str) -> ET.Element:
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise DavError("The server sent an XML document with a DOCTYPE; refused.")
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise DavError(f"The server's answer is not valid XML ({e}).") from None


@dataclass
class Resource:
    href: str
    status: int = 200
    props: dict[str, ET.Element] = field(default_factory=dict)

    def text(self, ns: str, name: str) -> str:
        el = self.props.get(q(ns, name))
        return (el.text or "").strip() if el is not None else ""

    def has(self, ns: str, name: str) -> bool:
        return q(ns, name) in self.props

    def child_hrefs(self, ns: str, name: str) -> list[str]:
        el = self.props.get(q(ns, name))
        if el is None:
            return []
        return [(h.text or "").strip() for h in el.iter(q(DAV, "href"))]

    def is_collection(self, kind: tuple[str, str]) -> bool:
        rt = self.props.get(q(DAV, "resourcetype"))
        return rt is not None and rt.find(q(*kind)) is not None


def _status(text: str) -> int:
    parts = (text or "").split()
    try:
        return int(parts[1]) if len(parts) > 1 else 200
    except ValueError:
        return 200


def multistatus(text: str) -> tuple[list[Resource], str]:
    """Resources from a 207 Multi-Status, and the sync-token if one is given."""
    root = _xml(text)
    out = []
    for resp in root.findall(q(DAV, "response")):
        href = urllib.parse.unquote((resp.findtext(q(DAV, "href")) or "").strip())
        res = Resource(href, _status(resp.findtext(q(DAV, "status")) or ""))
        for ps in resp.findall(q(DAV, "propstat")):
            if _status(ps.findtext(q(DAV, "status")) or "HTTP/1.1 200 OK") != 200:
                continue
            prop = ps.find(q(DAV, "prop"))
            for child in list(prop) if prop is not None else []:
                res.props[child.tag] = child
        out.append(res)
    return out, (root.findtext(q(DAV, "sync-token")) or "").strip()


def _doc(root: ET.Element) -> bytes:
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root)


def propfind(*props: tuple[str, str]) -> bytes:
    root = ET.Element(q(DAV, "propfind"))
    prop = ET.SubElement(root, q(DAV, "prop"))
    for ns, name in props:
        ET.SubElement(prop, q(ns, name))
    return _doc(root)


def calendar_query(start: str, end: str) -> bytes:
    """REPORT calendar-query: events overlapping [start, end) (UTC, 20261007T090000Z)."""
    root = ET.Element(q(CALDAV, "calendar-query"))
    prop = ET.SubElement(root, q(DAV, "prop"))
    ET.SubElement(prop, q(DAV, "getetag"))
    ET.SubElement(prop, q(CALDAV, "calendar-data"))
    flt = ET.SubElement(root, q(CALDAV, "filter"))
    cal = ET.SubElement(flt, q(CALDAV, "comp-filter"), {"name": "VCALENDAR"})
    ev = ET.SubElement(cal, q(CALDAV, "comp-filter"), {"name": "VEVENT"})
    ET.SubElement(ev, q(CALDAV, "time-range"), {"start": start, "end": end})
    return _doc(root)


def addressbook_query(prop_name: str, value: str) -> bytes:
    """REPORT addressbook-query: cards whose property (EMAIL, TEL, FN) equals ``value``."""
    root = ET.Element(q(CARDDAV, "addressbook-query"))
    prop = ET.SubElement(root, q(DAV, "prop"))
    ET.SubElement(prop, q(DAV, "getetag"))
    ET.SubElement(prop, q(CARDDAV, "address-data"))
    flt = ET.SubElement(root, q(CARDDAV, "filter"))
    pf = ET.SubElement(flt, q(CARDDAV, "prop-filter"), {"name": prop_name})
    tm = ET.SubElement(pf, q(CARDDAV, "text-match"), {"collation": "i;unicode-casemap", "match-type": "equals"})
    tm.text = value
    return _doc(root)


def sync_collection(token: str, data: tuple[str, str]) -> bytes:
    """REPORT sync-collection (RFC 6578): what changed since ``token`` ("" for everything)."""
    root = ET.Element(q(DAV, "sync-collection"))
    ET.SubElement(root, q(DAV, "sync-token")).text = token
    ET.SubElement(root, q(DAV, "sync-level")).text = "1"
    prop = ET.SubElement(root, q(DAV, "prop"))
    ET.SubElement(prop, q(DAV, "getetag"))
    ET.SubElement(prop, q(*data))
    return _doc(root)


def parse_request(body: bytes | str) -> ET.Element:
    """For stand-ins: the request body as XML."""
    text = body.decode() if isinstance(body, bytes) else (body or "")
    return _xml(text) if text.strip() else ET.Element(q(DAV, "propfind"))


def response_xml(items: list[tuple[str, dict[str, object], int]], sync_token: str = "") -> str:
    """For stand-ins: a 207 multistatus. Each item is (href, {"{ns}name": text | Element | list}, status)."""
    root = ET.Element(q(DAV, "multistatus"))
    for href, props, status in items:
        r = ET.SubElement(root, q(DAV, "response"))
        ET.SubElement(r, q(DAV, "href")).text = href
        if status != 200:
            ET.SubElement(r, q(DAV, "status")).text = f"HTTP/1.1 {status} Not Found"
            continue
        ps = ET.SubElement(r, q(DAV, "propstat"))
        p = ET.SubElement(ps, q(DAV, "prop"))
        for tag, val in props.items():
            el = ET.SubElement(p, tag)
            if isinstance(val, ET.Element):
                el.append(val)
            elif isinstance(val, list):
                for sub in val:
                    el.append(sub)
            elif val is not None:
                el.text = str(val)
        ET.SubElement(ps, q(DAV, "status")).text = "HTTP/1.1 200 OK"
    if sync_token:
        ET.SubElement(root, q(DAV, "sync-token")).text = sync_token
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")


def el(ns: str, name: str, text: str = "", **attrs: str) -> ET.Element:
    e = ET.Element(q(ns, name), attrs)
    if text:
        e.text = text
    return e


def href_el(href: str) -> ET.Element:
    return el(DAV, "href", href)


def join(base: str, href: str) -> str:
    """An href from the server (often just a path) made absolute against the server's address."""
    return urllib.parse.urljoin(base, href)
