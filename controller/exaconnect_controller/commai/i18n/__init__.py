"""Interface languages for the CommAI screens and the chat widget (ADR 0026).

One message catalogue per locale, as JSON next to this file. English
(en-GB) is the source: every key exists there, and any key a draft lacks
falls back to English. The other catalogues are drafts written by machine;
each is shown as "machine-drafted" until a reviewer signs off that exact
version (its content hash). Editing a catalogue after sign-off makes it a
draft again.

Each non-source language is a capability in the go-live registry (kind
"language"): it is offered to a business's people, and to its website
visitors, only once ExaCarib has switched it on.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib import resources
from typing import Any

import psycopg

from .. import golive

SOURCE = "en-GB"

# locale -> (English name, name in the language itself, the AI language code)
LOCALES: dict[str, tuple[str, str, str]] = {
    "en-GB": ("English (UK)", "English (UK)", "en"),
    "es": ("Spanish", "Español", "es"),
    "fr": ("French", "Français", "fr"),
    "nl": ("Dutch", "Nederlands", "nl"),
    "ht": ("Haitian Creole", "Kreyòl ayisyen", "ht"),
}

LANGUAGE_CRITERIA = {
    "catalogue-reviewed": "A fluent reviewer has signed off the interface and chat widget catalogue in Connect.",
    "formats": "Dates, numbers and currency checked on the CommAI screens in this locale.",
    "support": "Someone on the support rota can answer customers in this language.",
}

for _code, (_en, _native, _ai) in LOCALES.items():
    if _code != SOURCE:
        golive.declare(
            "language",
            _code,
            f"{_en} ({_native})",
            LANGUAGE_CRITERIA,
            {"locale": _code, "covers": "CommAI screens and the website chat widget"},
        )


class LocaleError(Exception):
    def __init__(self, message: str, code: int = 422):
        super().__init__(message)
        self.code = code


@lru_cache(maxsize=16)
def _load(locale: str) -> dict[str, str]:
    if locale not in LOCALES:  # only known names ever reach the file system
        raise LocaleError(f"There is no {locale} catalogue.", 404)
    raw = resources.files(__package__).joinpath(f"{locale}.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    return {str(k): str(v) for k, v in data.items() if not k.startswith("_")}


def catalogue(locale: str) -> dict[str, str]:
    if locale not in LOCALES:
        raise LocaleError(f"There is no {locale} catalogue.", 404)
    return dict(_load(locale))


def catalogue_hash(locale: str) -> str:
    body = json.dumps(_load(locale), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def missing_keys(locale: str) -> list[str]:
    src = _load(SOURCE)
    have = _load(locale)
    return sorted(k for k in src if k not in have)


def merged(locale: str) -> dict[str, str]:
    """The catalogue with English for any key the draft lacks."""
    return {**_load(SOURCE), **catalogue(locale)}


def review_status(conn: psycopg.Connection, locale: str) -> dict:
    """source | reviewed | machine-drafted, for the catalogue as it is now."""
    if locale == SOURCE:
        return {"status": "source", "reviewed_by": "", "reviewed_at": None}
    row = conn.execute(
        "SELECT reviewed_by, reviewed_at FROM commai_catalogue_reviews WHERE locale = %s AND catalogue_hash = %s",
        (locale, catalogue_hash(locale)),
    ).fetchone()
    if row:
        return {"status": "reviewed", **row}
    return {"status": "machine-drafted", "reviewed_by": "", "reviewed_at": None}


def sign_off(conn: psycopg.Connection, locale: str, actor: str, note: str = "") -> dict:
    if locale == SOURCE:
        raise LocaleError("English is the source catalogue; it needs no sign-off.")
    catalogue(locale)
    conn.execute(
        """INSERT INTO commai_catalogue_reviews (locale, catalogue_hash, reviewed_by, note) VALUES (%s, %s, %s, %s)
           ON CONFLICT (locale, catalogue_hash) DO UPDATE SET reviewed_by = EXCLUDED.reviewed_by,
             note = EXCLUDED.note, reviewed_at = now()""",
        (locale, catalogue_hash(locale), actor, note.strip()[:500]),
    )
    return review_status(conn, locale)


def withdraw(conn: psycopg.Connection, locale: str) -> dict:
    conn.execute(
        "DELETE FROM commai_catalogue_reviews WHERE locale = %s AND catalogue_hash = %s",
        (locale, catalogue_hash(locale)),
    )
    return review_status(conn, locale)


def available(conn: psycopg.Connection, locale: str, customer_id: Any) -> bool:
    if locale == SOURCE:
        return True
    return locale in LOCALES and golive.enabled(conn, "language", locale, customer_id)


def offered(conn: psycopg.Connection, customer_id: Any) -> list[str]:
    return [code for code in LOCALES if available(conn, code, customer_id)]


def describe(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    out = []
    for code, (en, native, ai_code) in LOCALES.items():
        rs = review_status(conn, code)
        out.append(
            {
                "locale": code,
                "name": en,
                "native": native,
                "ai_language": ai_code,
                "available": available(conn, code, customer_id),
                "status": rs["status"],
                "reviewed_by": rs["reviewed_by"],
                "reviewed_at": rs["reviewed_at"],
                "missing_keys": len(missing_keys(code)),
            }
        )
    return out


def user_locale(conn: psycopg.Connection, user_id: Any, customer_id: Any) -> str:
    """The person's chosen locale, or English when it is not (or no longer) offered."""
    row = conn.execute("SELECT locale FROM commai_user_locale WHERE user_id = %s", (user_id,)).fetchone()
    if row and available(conn, row["locale"], customer_id):
        return row["locale"]
    return SOURCE


def set_user_locale(conn: psycopg.Connection, user_id: Any, customer_id: Any, locale: str) -> str:
    if locale not in LOCALES:
        raise LocaleError(f"There is no {locale} catalogue.")
    if not available(conn, locale, customer_id):
        raise LocaleError(f"{LOCALES[locale][0]} is not switched on yet.", 409)
    conn.execute(
        """INSERT INTO commai_user_locale (user_id, locale) VALUES (%s, %s)
           ON CONFLICT (user_id) DO UPDATE SET locale = EXCLUDED.locale, updated_at = now()""",
        (user_id, locale),
    )
    return locale


def for_language(code: str) -> str:
    """The catalogue locale for an AI language code ('es' -> 'es', 'en' -> 'en-GB')."""
    for loc, (_, _, ai) in LOCALES.items():
        if ai == code:
            return loc
    return SOURCE


def text(conn: psycopg.Connection, key: str, language: str, customer_id: Any, **values: Any) -> tuple[str, str]:
    """One message in a customer's language when that language is switched on
    for the business, else English. Returns (text, locale used)."""
    loc = for_language(language or "")
    if not available(conn, loc, customer_id):
        loc = SOURCE
    msg = merged(loc).get(key, key)
    try:
        return msg.format(**values), loc
    except (KeyError, IndexError, ValueError):
        return _load(SOURCE).get(key, key).format(**values), SOURCE


def widget_strings(locale: str) -> dict[str, str]:
    prefix = "widget."
    return {k[len(prefix) :]: v for k, v in merged(locale).items() if k.startswith(prefix)}
