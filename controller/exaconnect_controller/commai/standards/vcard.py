"""vCard (RFC 6350, version 4.0; version 3.0 is read too) for contacts:
export, import and the CardDAV connector.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .ical import ICalError, escape, fold, parse, unescape


@dataclass
class Card:
    uid: str = ""
    name: str = ""
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    org: str = ""
    note: str = ""
    language: str = ""

    @property
    def email(self) -> str:
        return self.emails[0] if self.emails else ""

    @property
    def phone(self) -> str:
        return self.phones[0] if self.phones else ""


def _n(name: str) -> str:
    """The structured name (family;given;additional;prefixes;suffixes) from a full name."""
    parts = name.split()
    if len(parts) >= 2:
        return f"{escape(parts[-1])};{escape(' '.join(parts[:-1]))};;;"
    return f"{escape(name)};;;;"


def write(card: Card, rev: dt.datetime | None = None) -> str:
    lines = ["BEGIN:VCARD", "VERSION:4.0"]
    if card.uid:
        lines.append(f"UID:{card.uid}")
    name = card.name or card.email or card.phone or "Unknown"
    lines += [f"FN:{escape(name)}", f"N:{_n(name)}"]
    for i, e in enumerate(card.emails):
        lines.append(f"EMAIL{';PREF=1' if i == 0 else ''}:{e}")
    for i, p in enumerate(card.phones):
        lines.append(f"TEL;VALUE=uri{';PREF=1' if i == 0 else ''}:tel:{p.replace(' ', '')}")
    if card.org:
        lines.append(f"ORG:{escape(card.org)}")
    if card.note:
        lines.append(f"NOTE:{escape(card.note)}")
    if card.language:
        lines.append(f"LANG:{card.language}")
    lines.append(f"REV:{(rev or dt.datetime.now(dt.UTC)).strftime('%Y%m%dT%H%M%SZ')}")
    lines.append("END:VCARD")
    return "\r\n".join(fold(x) for x in lines) + "\r\n"


def write_many(cards: list[Card]) -> str:
    return "".join(write(c) for c in cards)


def read(text: str, limit: int = 10_000) -> list[Card]:
    """Every card in a .vcf stream. Raises ValueError for a stream that isn't vCard."""
    try:
        root = parse(text)
    except ICalError as e:
        raise ValueError(f"That is not a valid vCard file: {e}") from None
    cards = []
    for c in root.walk("VCARD"):
        card = Card(uid=c.text("UID"), name=c.text("FN"))
        if not card.name and c.get("N"):
            fam, _, rest = c.get("N").value.partition(";")
            given = rest.split(";")[0]
            card.name = unescape(f"{given} {fam}".strip())
        card.emails = [p.value.strip().removeprefix("mailto:") for p in c.all("EMAIL") if p.value.strip()]
        card.phones = [p.value.strip().removeprefix("tel:") for p in c.all("TEL") if p.value.strip()]
        org = c.get("ORG")
        card.org = unescape(org.value.split(";")[0]) if org else ""
        card.note = c.text("NOTE")
        card.language = c.text("LANG")
        cards.append(card)
        if len(cards) >= limit:
            break
    if not cards and text.strip():
        raise ValueError("No contacts found: a vCard file starts with BEGIN:VCARD.")
    return cards
