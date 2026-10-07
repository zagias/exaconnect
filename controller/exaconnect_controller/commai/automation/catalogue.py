"""The integration catalogue as the portal shows it (ADR 0028): every
registered connector grouped by category, with its actions, this business's
status and what ExaCarib still needs to register; plus the standard
interfaces and the apps reached through them.

Data-driven: a connector registered in ``connectors/installed.py`` appears
here with no change to this module or the portal.
"""

from __future__ import annotations

from typing import Any

import psycopg

from ..connectors import kit
from . import integrations

ORDER = [
    "crm",
    "email",
    "calendar",
    "contacts",
    "helpdesk",
    "chat",
    "commerce",
    "payments",
    "files",
    "automation",
    "custom",
    "other",
    "example",
]

# Standard interfaces CommAI speaks, so any system that speaks them can connect.
STANDARDS = [
    {
        "id": "caldav",
        "name": "CalDAV and iCalendar",
        "spec": "RFC 4791, RFC 5545",
        "what": "Any CalDAV calendar (iCloud, Fastmail, Nextcloud, Zimbra); a bookings feed; .ics invites.",
    },
    {
        "id": "carddav",
        "name": "CardDAV and vCard",
        "spec": "RFC 6352, RFC 6350",
        "what": "Any CardDAV address book; vCard import and export of contacts.",
    },
    {
        "id": "mailbox",
        "name": "IMAP and SMTP",
        "spec": "RFC 9051, RFC 5321",
        "what": "Any mailbox as an email channel: new mail by IMAP (IDLE or polling), replies by SMTP.",
    },
    {
        "id": "webhooks",
        "name": "Webhooks: CloudEvents and Standard Webhooks",
        "spec": "CloudEvents 1.0, Standard Webhooks",
        "what": "Events out to any address; signed inbound webhooks that start workflows (Zapier, Make, n8n).",
    },
    {
        "id": "rest",
        "name": "Your own REST API",
        "spec": "OpenAPI 3.0 and 3.1",
        "what": "Import an OpenAPI document, choose operations as actions, map fields; a person approves.",
    },
    {
        "id": "openapi",
        "name": "CommAI's API",
        "spec": "OpenAPI 3.1, AsyncAPI 3.0",
        "what": "Published descriptions of every CommAI endpoint and of the event stream.",
    },
    {
        "id": "csv",
        "name": "CSV data exchange",
        "spec": "RFC 4180",
        "what": "Contacts in and out; conversations out (and history in).",
    },
]

# Popular apps without a ready-made connector here, reached through a standard.
VIA_STANDARD = [
    {"name": "iCloud Calendar and Contacts", "category": "calendar", "via": "caldav"},
    {"name": "Fastmail", "category": "email", "via": "mailbox"},
    {"name": "Nextcloud", "category": "calendar", "via": "caldav"},
    {"name": "Zoho Mail, Yahoo Mail, any mailbox", "category": "email", "via": "mailbox"},
    {"name": "Acuity Scheduling", "category": "calendar", "via": "rest"},
    {"name": "Square Appointments", "category": "calendar", "via": "rest"},
    {"name": "SugarCRM, Odoo, Copper, Insightly, Keap", "category": "crm", "via": "rest"},
    {"name": "ActiveCampaign, Mailchimp", "category": "crm", "via": "rest"},
    {"name": "Airtable, Google Sheets, Notion", "category": "other", "via": "rest"},
    {"name": "Zapier, Make, n8n", "category": "automation", "via": "webhooks"},
]


def _own_apis(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    """This business's approved REST apps (only its own), shaped like any app."""
    from ..connectors import rest_generic

    out = []
    for c in rest_generic.owned(conn, customer_id):
        item = kit.describe_any(c)
        row = integrations._row(conn, customer_id, c.app)
        real, reasons = c.real_ready(conn, customer_id)
        out.append(
            {
                **item,
                "simulated": (row is None and not real) or bool(row and row.get("auth_method") == "simulated"),
                "sign_in_ready": real,
                "not_live_reason": " ".join(reasons),
                "token_entry": False,
                "read_actions": [a["name"] for a in item["actions"] if a["kind"] == "read"],
                "write_actions": [a["name"] for a in item["actions"] if a["kind"] in integrations.WRITE_KINDS],
                "mapping_objects": [],
                "connection": integrations.public(row),
            }
        )
    return out


def grouped(conn: psycopg.Connection, customer_id: Any) -> dict:
    """The catalogue by category. Each app: its actions (read kept apart from
    writes), whether this business has it live or on a stand-in, and what is
    still needed from ExaCarib."""
    items = integrations.catalogue(conn, customer_id) + _own_apis(conn, customer_id)
    by: dict[str, list[dict]] = {}
    for it in items:
        it["status"] = (it.get("connection") or {}).get("status") or "not connected"
        it["needs"] = {
            "env": it.get("env") or [],
            "app_registration": it.get("needs_from_exacarib", ""),
            "reason": it.get("not_live_reason", ""),
        }
        by.setdefault(it.get("category") or "other", []).append(it)
    cats = sorted(by, key=lambda c: ORDER.index(c) if c in ORDER else len(ORDER))
    return {
        "categories": [
            {"id": c, "label": kit.CATEGORIES.get(c, c), "apps": sorted(by[c], key=lambda x: x["label"])} for c in cats
        ],
        "standards": STANDARDS,
        "via_standard": VIA_STANDARD,
    }
