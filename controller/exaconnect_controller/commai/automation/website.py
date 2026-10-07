"""Reading a business's website for onboarding, and its opening hours (ADR 0033).

The page is untrusted input. It is fetched only from a public address:

- http or https only, no user name or password in the address, ports 80
  and 443 only;
- every address the host name resolves to must be public (no loopback,
  private, link-local, carrier-grade NAT or reserved ranges), and the
  connection is made to the address that was checked, so a host name can't
  be switched to a private address between the check and the request;
- at most three redirects, each checked the same way;
- at most 1 MB, 10 seconds, HTML or plain text only.

The text then goes through onboarding's own filter (instructions to an AI
are dropped and listed) and only ever becomes draft knowledge for a person
to review.
"""

from __future__ import annotations

import html
import http.client
import ipaddress
import re
import socket
import ssl
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

MAX_BYTES = 1_000_000
TIMEOUT_S = 10
MAX_REDIRECTS = 3
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class FetchError(Exception):
    pass


@dataclass
class Page:
    url: str
    status: int
    content_type: str
    body: bytes
    location: str = ""


def resolve(host: str, port: int) -> list[str]:
    try:
        return sorted({i[4][0] for i in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})
    except socket.gaierror as e:
        raise FetchError(f"{host} does not resolve.") from e


def check(url: str) -> tuple[urllib.parse.SplitResult, str]:
    """The URL and the public address to connect to. Raises FetchError."""
    p = urllib.parse.urlsplit(url.strip())
    if p.scheme not in ("http", "https"):
        raise FetchError("The website address must start with https:// or http://.")
    if not p.hostname:
        raise FetchError("That address has no host name.")
    if p.username or p.password:
        raise FetchError("The address must not contain a user name or password.")
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError as e:
        raise FetchError("That address has a bad port.") from e
    if port not in (80, 443):
        raise FetchError("Only the standard web ports (80 and 443) are fetched.")
    addrs = resolve(p.hostname, port)
    if not addrs:
        raise FetchError(f"{p.hostname} does not resolve.")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise FetchError(f"{p.hostname} points to a private address, so it is not fetched.")
    return p, addrs[0]


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _get(parts: urllib.parse.SplitResult, ip: str) -> Page:
    port = parts.port or (443 if parts.scheme == "https" else 80)
    cls = _PinnedHTTPS if parts.scheme == "https" else _PinnedHTTP
    c = cls(parts.hostname or "", ip, port, TIMEOUT_S)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    try:
        c.request("GET", path, headers={"User-Agent": "ExaCarib-CommAI-onboarding/1", "Accept": "text/html,text/plain"})
        r = c.getresponse()
        body = r.read(MAX_BYTES + 1)
        return Page(parts.geturl(), r.status, r.getheader("Content-Type") or "", body, r.getheader("Location") or "")
    except (OSError, http.client.HTTPException) as e:
        raise FetchError(f"{parts.hostname} could not be reached ({type(e).__name__}).") from None
    finally:
        c.close()


# Tests replace this with a fake; it receives the checked URL parts and the address.
getter: Callable[[urllib.parse.SplitResult, str], Page] = _get


def fetch(url: str) -> Page:
    for _ in range(MAX_REDIRECTS + 1):
        parts, ip = check(url)
        page = getter(parts, ip)
        if page.status in (301, 302, 303, 307, 308) and page.location:
            url = urllib.parse.urljoin(parts.geturl(), page.location)
            continue
        if page.status != 200:
            raise FetchError(f"The website answered {page.status}.")
        ctype = page.content_type.split(";")[0].strip().lower()
        if ctype not in ("text/html", "text/plain", "application/xhtml+xml", ""):
            raise FetchError(f"That page is {ctype}, not a web page.")
        if len(page.body) > MAX_BYTES:
            raise FetchError("That page is larger than 1 MB.")
        return page
    raise FetchError("Too many redirects.")


BLOCK = re.compile(r"(?is)<(script|style|noscript|template|svg|iframe)[^>]*>.*?</\1\s*>")
BREAK = re.compile(r"(?i)<\s*(br|/p|/div|/li|/h[1-6]|/tr|/section|/article|/header|/footer)\b[^>]*>")
HEADING = re.compile(r"(?i)<\s*h[1-6]\b[^>]*>")


def to_text(page: Page) -> str:
    raw = page.body[:MAX_BYTES].decode("utf-8", "replace")
    if "html" not in page.content_type.lower() and "<html" not in raw[:2000].lower():
        return raw
    raw = re.sub(r"(?s)<!--.*?-->", " ", raw)
    raw = BLOCK.sub(" ", raw)
    raw = HEADING.sub("\n\n", raw)
    raw = BREAK.sub("\n", raw)
    raw = re.sub(r"<[^>]{0,2000}>", " ", raw)
    text = html.unescape(raw)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


# ---- opening hours ---------------------------------------------------------------------------

DAY_WORDS = {
    "mon": "mon",
    "monday": "mon",
    "tue": "tue",
    "tues": "tue",
    "tuesday": "tue",
    "wed": "wed",
    "wednesday": "wed",
    "thu": "thu",
    "thur": "thu",
    "thurs": "thu",
    "thursday": "thu",
    "fri": "fri",
    "friday": "fri",
    "sat": "sat",
    "saturday": "sat",
    "sun": "sun",
    "sunday": "sun",
}
_DAY = r"(mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
_TIME = r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?"
_SEG = re.compile(
    rf"(?i)\b{_DAY}\s*(?:(?:-|–|to|through)\s*{_DAY})?\s*:?\s*(?:(closed)|{_TIME}\s*(?:-|–|to|until)\s*{_TIME})"
)


def _hm(h: str, m: str | None, ap: str | None, other_ap: str | None) -> str | None:
    hour, minute = int(h), int(m or 0)
    ap = (ap or other_ap or "").lower().replace(".", "")
    if ap == "pm" and hour < 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    if hour > 24 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_hours(text: str) -> dict[str, list[str] | None] | None:
    """'Mon-Fri 8am-5pm, Sat 9-1, Sun closed' -> {"mon": ["08:00", "17:00"], ..., "sun": None}.
    None when nothing could be read (a person sets the hours instead)."""
    out: dict[str, list[str] | None] = {}
    for m in _SEG.finditer(text or ""):
        d1, d2, closed = DAY_WORDS[m.group(1).lower()], m.group(2), m.group(3)
        days = [d1]
        if d2:
            a, b = DAYS.index(d1), DAYS.index(DAY_WORDS[d2.lower()])
            days = [DAYS[(a + k) % 7] for k in range((b - a) % 7 + 1)]
        if closed:
            span = None
        else:
            start = _hm(m.group(4), m.group(5), m.group(6), None)
            end = _hm(m.group(7), m.group(8), m.group(9), None)
            if start and end and not m.group(9) and end <= start:
                end = _hm(m.group(7), m.group(8), "pm", None)  # "9-1" means 9am to 1pm
            if not start or not end or end <= start:
                continue
            span = [start, end]
        for day in days:
            out[day] = span
    if not out:
        return None
    return {d: out.get(d) for d in DAYS}


def check_hours(hours: dict) -> dict[str, list[str] | None]:
    """Validate structured hours given by a person: {"mon": ["09:00", "17:00"] or None, ...}."""
    if not isinstance(hours, dict) or set(hours) - set(DAYS):
        raise ValueError("Opening hours are given per day: mon, tue ... sun.")
    out: dict[str, list[str] | None] = {}
    for d in DAYS:
        span = hours.get(d)
        if span in (None, [], ""):
            out[d] = None
            continue
        if (
            not isinstance(span, list | tuple)
            or len(span) != 2
            or not all(isinstance(x, str) and re.fullmatch(r"([01]\d|2[0-4]):[0-5]\d", x) for x in span)
            or span[0] >= span[1]
        ):
            raise ValueError(f"Hours for {d} must look like 09:00 to 17:00.")
        out[d] = [span[0], span[1]]
    return out


def hours_text(hours: dict) -> str:
    parts = []
    for d in DAYS:
        span = hours.get(d)
        parts.append(f"{d.title()} {span[0]} to {span[1]}" if span else f"{d.title()} closed")
    return ", ".join(parts)
