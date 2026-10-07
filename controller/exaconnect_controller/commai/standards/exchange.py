"""Data exchange in standard formats (ADR 0028): CSV (RFC 4180) and vCard.

- Contacts out as CSV or vCard 4.0; contacts in from CSV or vCard (3.0 or
  4.0), matched by email then phone so an import run twice changes nothing.
- Conversations out as CSV, one row per message.

Exports guard against spreadsheet formula injection: a cell that starts with
=, +, -, @, tab or carriage return is prefixed with an apostrophe.
Every import is logged in commai_data_imports with what it did.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import vcard

MAX_ROWS = 10_000
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
COLUMNS = {
    "name": ("name", "full name", "fullname", "display name", "contact name"),
    "email": ("email", "e-mail", "email address", "mail"),
    "phone": ("phone", "phone number", "mobile", "telephone", "tel", "whatsapp"),
    "language": ("language", "lang", "locale"),
    "external_ref": ("external_ref", "external id", "id", "reference", "ref"),
}


def safe(v: Any) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _csv(header: list[str], rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow([safe(x) for x in r])
    return buf.getvalue()


# ---- contacts ---------------------------------------------------------------------------


def _contacts(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    return conn.execute(
        "SELECT * FROM contacts WHERE customer_id = %s ORDER BY created_at, id LIMIT %s", (customer_id, MAX_ROWS * 5)
    ).fetchall()


def contacts_csv(conn: psycopg.Connection, customer_id: Any) -> str:
    rows = _contacts(conn, customer_id)
    return _csv(
        ["id", "name", "email", "phone", "language", "external_ref", "created_at"],
        [[r["id"], r["name"], r["email"], r["phone"], r["language"], r["external_ref"], r["created_at"].isoformat()]
         for r in rows],
    )  # fmt: skip


def contacts_vcf(conn: psycopg.Connection, customer_id: Any) -> str:
    cards = [
        vcard.Card(
            uid=f"urn:uuid:{r['id']}",
            name=r["name"] or r["email"] or r["phone"],
            emails=[r["email"]] if r["email"] else [],
            phones=[r["phone"]] if r["phone"] else [],
            language=r["language"],
        )
        for r in _contacts(conn, customer_id)
    ]
    return vcard.write_many(cards)


def _log(conn, customer_id: Any, kind: str, counts: dict, problems: list[str], actor: str) -> dict:
    row = conn.execute(
        """INSERT INTO commai_data_imports (customer_id, kind, rows, created, updated, skipped, problems, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (
            customer_id,
            kind,
            counts["rows"],
            counts["created"],
            counts["updated"],
            counts["skipped"],
            Jsonb(problems[:100]),
            actor,
        ),
    ).fetchone()
    return {k: row[k] for k in ("id", "kind", "rows", "created", "updated", "skipped", "problems")}


def _apply(conn, customer_id: Any, cards: list[vcard.Card], source: str) -> tuple[dict, list[str]]:
    from ..connectors.carddav import upsert_contact

    counts = {"rows": len(cards), "created": 0, "updated": 0, "skipped": 0}
    problems: list[str] = []
    for i, card in enumerate(cards, start=1):
        if card.email and not EMAIL.match(card.email):
            problems.append(f"Row {i}: {card.email!r} is not an email address.")
            counts["skipped"] += 1
            continue
        outcome = upsert_contact(conn, customer_id, card, source)
        if outcome == "skipped":
            problems.append(f"Row {i}: no email or phone.")
        counts[outcome] += 1
    return counts, problems


def import_contacts_csv(conn: psycopg.Connection, customer_id: Any, text: str, actor: str) -> dict:
    """Contacts from CSV with a header row. Recognised columns (any case):
    name, email, phone, language, external_ref (and common synonyms)."""
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = [h.strip().lower() for h in next(reader)]
    except StopIteration:
        raise ValueError("The file is empty.") from None
    where = {}
    for field, names in COLUMNS.items():
        for i, h in enumerate(header):
            if h in names:
                where[field] = i
                break
    if "email" not in where and "phone" not in where:
        raise ValueError("The file needs an email or phone column.")
    cards = []
    for n, row in enumerate(reader):
        if n >= MAX_ROWS:
            raise ValueError(f"Import at most {MAX_ROWS} rows at a time.")
        if not any(c.strip() for c in row):
            continue

        def get(f: str, row: list[str] = row) -> str:
            i = where.get(f)
            return row[i].strip() if i is not None and i < len(row) else ""

        cards.append(
            vcard.Card(
                uid=get("external_ref"),
                name=get("name"),
                emails=[get("email").lower()] if get("email") else [],
                phones=[get("phone")] if get("phone") else [],
                language=get("language"),
            )
        )
    counts, problems = _apply(conn, customer_id, cards, "csv")
    return _log(conn, customer_id, "contacts.csv", counts, problems, actor)


def import_contacts_vcf(conn: psycopg.Connection, customer_id: Any, text: str, actor: str) -> dict:
    cards = vcard.read(text, limit=MAX_ROWS)
    counts, problems = _apply(conn, customer_id, cards, "vcard")
    return _log(conn, customer_id, "contacts.vcf", counts, problems, actor)


# ---- conversations ------------------------------------------------------------------------


def conversations_csv(conn: psycopg.Connection, customer_id: Any, since: Any = None, limit: int = 100_000) -> str:
    """One row per message, oldest first: the conversation, the contact, the message."""
    if since:
        try:
            since = dt.datetime.fromisoformat(str(since).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("since must be a date or time like 2026-10-01.") from None
    rows = conn.execute(
        """SELECT c.id AS conversation_id, c.channel, c.subject, c.state, c.created_at AS opened_at,
                  ct.name AS contact_name, ct.email AS contact_email, ct.phone AS contact_phone,
                  m.created_at, m.direction, m.author_kind, m.author, m.status, m.body
           FROM messages m JOIN conversations c ON c.id = m.conversation_id
           LEFT JOIN contacts ct ON ct.id = c.contact_id
           WHERE m.customer_id = %s AND (%s::timestamptz IS NULL OR m.created_at >= %s::timestamptz)
           ORDER BY c.created_at, c.id, m.created_at, m.id LIMIT %s""",
        (customer_id, since, since, limit),
    ).fetchall()
    cols = [
        "conversation_id",
        "channel",
        "subject",
        "state",
        "opened_at",
        "contact_name",
        "contact_email",
        "contact_phone",
        "created_at",
        "direction",
        "author_kind",
        "author",
        "status",
        "body",
    ]
    return _csv(cols, [[r[c].isoformat() if hasattr(r[c], "isoformat") else r[c] for c in cols] for r in rows])
