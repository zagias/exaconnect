"""iCalendar (RFC 5545) for bookings: writing events, invites (iTIP, RFC 5546
METHOD:REQUEST and CANCEL), feeds, and reading events and free/busy back.

Writing escapes text, folds lines at 75 octets and ends lines with CRLF.
Reading unfolds lines, unescapes text and understands UTC times, local times
with a TZID (through the IANA time zone database) and all-day dates.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PRODID = "-//ExaCarib//Jibsy//EN"


def escape(text: str) -> str:
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def unescape(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def fold(line: str) -> str:
    """Fold a content line at 75 octets, never inside a UTF-8 character."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        limit = 75 if not parts else 74
        if size + n > limit:
            parts.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    parts.append(cur)
    return "\r\n ".join(parts)


def utc(t: dt.datetime) -> str:
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    return t.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class Event:
    uid: str
    start: dt.datetime
    end: dt.datetime
    summary: str = ""
    description: str = ""
    location: str = ""
    status: str = "CONFIRMED"  # TENTATIVE | CONFIRMED | CANCELLED
    organizer: str = ""  # an email address
    attendees: list[tuple[str, str]] = field(default_factory=list)  # (email, name)
    sequence: int = 0
    transparent: bool = False
    url: str = ""

    def lines(self, stamp: dt.datetime | None = None) -> list[str]:
        out = [
            "BEGIN:VEVENT",
            f"UID:{self.uid}",
            f"DTSTAMP:{utc(stamp or dt.datetime.now(dt.UTC))}",
            f"DTSTART:{utc(self.start)}",
            f"DTEND:{utc(self.end)}",
            f"SEQUENCE:{self.sequence}",
            f"STATUS:{self.status}",
            f"SUMMARY:{escape(self.summary)}",
        ]
        if self.description:
            out.append(f"DESCRIPTION:{escape(self.description)}")
        if self.location:
            out.append(f"LOCATION:{escape(self.location)}")
        if self.url:
            out.append(f"URL:{self.url}")
        if self.organizer:
            out.append(f"ORGANIZER:mailto:{self.organizer}")
        for email, name in self.attendees:
            cn = f';CN="{name.replace(chr(34), "")}"' if name else ""
            out.append(f"ATTENDEE{cn};ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{email}")
        out.append(f"TRANSP:{'TRANSPARENT' if self.transparent else 'OPAQUE'}")
        out.append("END:VEVENT")
        return out


def calendar(events: list[Event], *, method: str = "", name: str = "", stamp: dt.datetime | None = None) -> str:
    """A VCALENDAR object. ``method`` is REQUEST or CANCEL for an invite (iTIP)."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN"]
    if method:
        lines.append(f"METHOD:{method}")
    if name:
        lines.append(f"X-WR-CALNAME:{escape(name)}")
    for e in events:
        lines += e.lines(stamp)
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in lines) + "\r\n"


def invite(e: Event) -> str:
    return calendar([e], method="REQUEST")


def cancellation(e: Event) -> str:
    e.status = "CANCELLED"
    e.sequence += 1
    return calendar([e], method="CANCEL")


# ---- reading -------------------------------------------------------------------------


@dataclass
class Prop:
    name: str
    params: dict[str, str]
    value: str


def unfold(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for line in text.split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line:
            out.append(line)
    return out


_PARAM = re.compile(r';([A-Za-z0-9-]+)=("[^"]*"|[^;:]*)')


def parse_line(line: str) -> Prop:
    # The name ends at the first ';' or ':'; a ':' inside a quoted parameter value is not the end.
    i, quoted = 0, False
    while i < len(line):
        ch = line[i]
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            break
        i += 1
    head, value = line[:i], line[i + 1 :]
    name, _, rest = head.partition(";")
    params = {m.group(1).upper(): m.group(2).strip('"') for m in _PARAM.finditer(";" + rest)} if rest else {}
    return Prop(name.upper(), params, value)


@dataclass
class Component:
    name: str
    props: list[Prop] = field(default_factory=list)
    children: list[Component] = field(default_factory=list)

    def get(self, name: str) -> Prop | None:
        return next((p for p in self.props if p.name == name), None)

    def all(self, name: str) -> list[Prop]:
        return [p for p in self.props if p.name == name]

    def text(self, name: str) -> str:
        p = self.get(name)
        return unescape(p.value) if p else ""

    def walk(self, name: str):
        for c in self.children:
            if c.name == name:
                yield c
            yield from c.walk(name)


class ICalError(ValueError):
    pass


def parse(text: str) -> Component:
    """Parse an iCalendar or vCard stream into components (a root holding them)."""
    root = Component("ROOT")
    stack = [root]
    for line in unfold(text):
        p = parse_line(line)
        if p.name == "BEGIN":
            c = Component(p.value.upper())
            stack[-1].children.append(c)
            stack.append(c)
        elif p.name == "END":
            if len(stack) == 1 or stack[-1].name != p.value.upper():
                raise ICalError(f"Unexpected END:{p.value}.")
            stack.pop()
        else:
            stack[-1].props.append(p)
    if len(stack) != 1:
        raise ICalError(f"{stack[-1].name} is not closed.")
    return root


def to_time(p: Prop, default_tz: dt.tzinfo = dt.UTC) -> dt.datetime:
    v = p.value.strip()
    if p.params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        d = dt.datetime.strptime(v[:8], "%Y%m%d")
        return d.replace(tzinfo=_tz(p.params.get("TZID"), default_tz))
    if v.endswith("Z"):
        return dt.datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
    t = dt.datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
    return t.replace(tzinfo=_tz(p.params.get("TZID"), default_tz))


def _tz(name: str | None, default: dt.tzinfo) -> dt.tzinfo:
    if not name:
        return default
    try:
        return ZoneInfo(name.strip("/"))
    except (ZoneInfoNotFoundError, ValueError):
        return default


_DUR = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def duration(v: str) -> dt.timedelta:
    m = _DUR.match(v.strip())
    if not m:
        raise ICalError(f"Not a duration: {v}")
    sign, w, d, h, mi, s = m.groups()
    td = dt.timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0), seconds=int(s or 0))
    return -td if sign == "-" else td


def events(text: str, default_tz: dt.tzinfo = dt.UTC) -> list[dict]:
    """VEVENTs as {"uid", "start", "end", "summary", "status", "transparent"}."""
    out = []
    for ev in parse(text).walk("VEVENT"):
        start_p = ev.get("DTSTART")
        if start_p is None:
            continue
        start = to_time(start_p, default_tz)
        end_p = ev.get("DTEND")
        if end_p is not None:
            end = to_time(end_p, default_tz)
        elif ev.get("DURATION"):
            end = start + duration(ev.get("DURATION").value)
        else:
            end = start + (dt.timedelta(days=1) if start_p.params.get("VALUE") == "DATE" else dt.timedelta(0))
        out.append(
            {
                "uid": ev.text("UID"),
                "start": start,
                "end": end,
                "summary": ev.text("SUMMARY"),
                "status": ev.text("STATUS").upper() or "CONFIRMED",
                "transparent": ev.text("TRANSP").upper() == "TRANSPARENT",
            }
        )
    return out


def busy_periods(text: str, default_tz: dt.tzinfo = dt.UTC) -> list[tuple[dt.datetime, dt.datetime]]:
    """Busy time from events (opaque, not cancelled) and VFREEBUSY components."""
    out = [
        (e["start"], e["end"]) for e in events(text, default_tz) if not e["transparent"] and e["status"] != "CANCELLED"
    ]
    for fb in parse(text).walk("VFREEBUSY"):
        for p in fb.all("FREEBUSY"):
            if p.params.get("FBTYPE", "BUSY").upper() == "FREE":
                continue
            for period in p.value.split(","):
                a, _, b = period.partition("/")
                start = to_time(Prop("X", {}, a))
                end = start + duration(b) if b.startswith(("P", "+P", "-P")) else to_time(Prop("X", {}, b))
                out.append((start, end))
    return out
