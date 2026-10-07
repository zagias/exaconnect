"""CardDAV address books (RFC 6352) with vCard 4.0 (RFC 6350), ADR 0034.

Works with iCloud (contacts.icloud.com), Fastmail (carddav.fastmail.com),
Nextcloud, Google's CardDAV endpoint and other servers. User name and app
password, stored encrypted; the address book is found by discovery or given.

Actions: look up a contact by email (addressbook-query), create (PUT of a new
card, UID from the idempotency key, If-None-Match: *), update (If-Match on
the card's ETag, so another person's change is never overwritten), delete
(sensitive) and sync into Jibsy contacts (sync-collection, RFC 6578, keeping
the sync token so each run fetches only what changed).

Off until ExaCarib switches ``feature/integration-carddav`` on; until then a
connection runs on a stand-in DAV server.
"""

from __future__ import annotations

import hashlib
from typing import Any

import psycopg

from .. import events
from ..standards import dav, vcard
from . import ActionSpec, ConnectorError, Field
from .dav_common import DavConnector, DavStandIn, remember_setting
from .kit import register


def uid_for(key: str) -> str:
    return "commai-" + hashlib.sha256(key.encode()).hexdigest()[:32]


def upsert_contact(conn: psycopg.Connection, customer_id: Any, card: vcard.Card, source: str) -> str:
    """Create or update a Jibsy contact from a card, matched by email (then phone).
    Returns 'created', 'updated' or 'skipped'."""
    email, phone = card.email.strip().lower(), card.phone.strip()
    if not email and not phone:
        return "skipped"
    row = None
    if email:
        row = conn.execute(
            "SELECT * FROM contacts WHERE customer_id = %s AND lower(email) = %s LIMIT 1", (customer_id, email)
        ).fetchone()
    if row is None and phone:
        row = conn.execute(
            "SELECT * FROM contacts WHERE customer_id = %s AND phone = %s LIMIT 1", (customer_id, phone)
        ).fetchone()
    ref = f"{source}:{card.uid}" if card.uid else ""
    if row is None:
        new = conn.execute(
            """INSERT INTO contacts (customer_id, name, email, phone, language, external_ref)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (customer_id, card.name[:200], email, phone, card.language[:10], ref),
        ).fetchone()
        events.emit(conn, customer_id, "contact.created", {"contact_id": str(new["id"]), "source": source}, new["id"])
        return "created"
    conn.execute(
        """UPDATE contacts SET name = CASE WHEN %s <> '' THEN %s ELSE name END,
                  email = CASE WHEN email = '' THEN %s ELSE email END,
                  phone = CASE WHEN phone = '' THEN %s ELSE phone END,
                  external_ref = CASE WHEN external_ref = '' THEN %s ELSE external_ref END
           WHERE id = %s""",
        (card.name[:200], card.name[:200], email, phone, ref, row["id"]),
    )
    return "updated"


class CardDAV(DavConnector):
    app = "carddav"
    label = "CardDAV contacts"
    category = "contacts"
    description = "Look up, add, update and sync contacts with any CardDAV address book: iCloud, Fastmail, Nextcloud."
    home_set = (dav.CARDDAV, "addressbook-home-set")
    collection_type = (dav.CARDDAV, "addressbook")
    needs_from_exacarib = (
        "Nothing to register: each business enters its CardDAV server and an app password. "
        "ExaCarib switches the capability on after a test against iCloud, Fastmail and Nextcloud."
    )
    webhooks = "CardDAV has no push; 'Sync contacts' fetches only what changed since the last sync."
    docs_url = "https://www.rfc-editor.org/rfc/rfc6352"
    actions = {
        "find_contact": ActionSpec(
            "find_contact", "Look up a contact", "read", fields=(Field("email", "Email", "email"),)
        ),
        "sync_contacts": ActionSpec("sync_contacts", "Sync contacts into Jibsy", "read"),
        "create_contact": ActionSpec(
            "create_contact",
            "Create a contact",
            "create",
            fields=(
                Field("name", "Name"),
                Field("email", "Email", "email"),
                Field("phone", "Phone", "phone", False),
                Field("company", "Company", required=False),
                Field("notes", "Notes", "text", False),
            ),
        ),
        "update_contact": ActionSpec(
            "update_contact",
            "Update a contact",
            "update",
            fields=(
                Field("contact_id", "Contact"),
                Field("name", "Name", required=False),
                Field("phone", "Phone", "phone", False),
                Field("notes", "Notes", "text", False),
            ),
        ),
        "delete_contact": ActionSpec(
            "delete_contact", "Delete a contact", "delete", sensitive=True, fields=(Field("contact_id", "Contact"),)
        ),
    }
    mapping_targets = {"contact": ["FN", "EMAIL", "TEL", "ORG", "NOTE"]}
    golive_criteria = {
        "servers": "Look-up, create, update with ETags, delete and sync tested against iCloud, Fastmail and "
        "Nextcloud test accounts.",
    }

    def __init__(self):
        self.simulator = DavStandIn(self.app)

    def _href(self, url: str, cid: str) -> str:
        if not cid.replace("-", "").isalnum():
            raise ConnectorError("That is not a contact reference.", "input")
        return dav.join(url, f"{cid}.vcf")

    def _find(self, conn, connection: dict, url: str, email: str) -> tuple[str, vcard.Card] | None:
        r = self.dav(conn, connection, "REPORT", url, dav.addressbook_query("EMAIL", email), depth="1")
        res, _ = dav.multistatus(r.body if isinstance(r.body, str) else "")
        for x in res:
            data = x.text(dav.CARDDAV, "address-data")
            if data:
                cards = vcard.read(data)
                if cards:
                    return x.href, cards[0]
        return None

    def execute(self, conn: psycopg.Connection, connection: dict, action: str, inputs: dict, key: str) -> dict:
        url = self.collection(conn, connection)
        if action == "find_contact":
            hit = self._find(conn, connection, url, inputs["email"])
            if not hit:
                return {"found": False, "contact": None}
            href, card = hit
            cid = href.rstrip("/").rsplit("/", 1)[-1].removesuffix(".vcf")
            return {
                "found": True,
                "contact": {
                    "id": cid,
                    "name": card.name,
                    "email": card.email,
                    "phone": card.phone,
                    "company": card.org,
                },
            }
        if action == "sync_contacts":
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"REPORT": "sync-collection"}}
            return self.sync(conn, connection, url)
        if action == "create_contact":
            known = self.known(conn, connection, key)
            if known:
                return {"contact_id": known["object_id"], "replayed": True}
            uid = uid_for(key)
            card = vcard.Card(
                uid=uid,
                name=inputs["name"],
                emails=[inputs["email"]],
                phones=[inputs["phone"]] if inputs.get("phone") else [],
                org=inputs.get("company", ""),
                note=inputs.get("notes", ""),
            )
            body = vcard.write(card)
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"PUT": self._href(url, uid), "vcard": body}}
            hit = self._find(conn, connection, url, inputs["email"])
            if hit:  # the address book already has this person: never a duplicate
                cid = hit[0].rstrip("/").rsplit("/", 1)[-1].removesuffix(".vcf")
                self.remember(conn, connection, key, "contact", cid)
                return {"contact_id": cid, "existing": True}
            r = self.dav(
                conn,
                connection,
                "PUT",
                self._href(url, uid),
                body.encode(),
                ctype="text/vcard; charset=utf-8",
                headers={"If-None-Match": "*"},
                allow=(412,),
            )
            self.remember(conn, connection, key, "contact", uid)
            return {"contact_id": uid, "existing": r.status == 412}
        if action == "update_contact":
            href = self._href(url, str(inputs["contact_id"]))
            r = self.dav(conn, connection, "GET", href, ok=(200,), allow=(404,))
            if r.status == 404:
                raise ConnectorError("No such contact.", "input")
            tag = next((v for k, v in (r.headers or {}).items() if k.lower() == "etag"), "")
            cards = vcard.read(r.body if isinstance(r.body, str) else "")
            card = cards[0]
            if inputs.get("name"):
                card.name = inputs["name"]
            if inputs.get("phone"):
                card.phones = [inputs["phone"]] + [p for p in card.phones if p != inputs["phone"]]
            if inputs.get("notes"):
                card.note = inputs["notes"]
            body = vcard.write(card)
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"PUT": href, "vcard": body}}
            headers = {"If-Match": tag} if tag else {}
            r = self.dav(
                conn,
                connection,
                "PUT",
                href,
                body.encode(),
                ctype="text/vcard; charset=utf-8",
                headers=headers,
                allow=(412,),
            )
            if r.status == 412:
                raise ConnectorError("Someone changed this contact at the same time. Try again.", "input")
            return {"contact_id": inputs["contact_id"], "updated": True}
        if action == "delete_contact":
            href = self._href(url, str(inputs["contact_id"]))
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"DELETE": href}}
            r = self.dav(conn, connection, "DELETE", href, ok=(200, 204), allow=(404, 410))
            return {
                "contact_id": inputs["contact_id"],
                "deleted": True,
                "already_gone": r.status != 204 and r.status != 200,
            }
        raise ConnectorError(f"Unknown action {action}.", "input")

    def sync(self, conn, connection: dict, url: str) -> dict:
        """Fetch what changed since the last sync token and update Jibsy contacts."""
        token = (connection.get("settings") or {}).get("sync_token", "")
        r = self.dav(
            conn,
            connection,
            "REPORT",
            url,
            dav.sync_collection(token, (dav.CARDDAV, "address-data")),
            depth="1" if not token else None,
            allow=(403,),
        )
        if r.status == 403 and token:  # the server forgot the token: start again from everything
            remember_setting(conn, connection, {"sync_token": ""})
            return self.sync(
                conn, {**connection, "settings": {**(connection.get("settings") or {}), "sync_token": ""}}, url
            )
        if r.status == 403:
            raise self.fail(r)
        res, new_token = dav.multistatus(r.body if isinstance(r.body, str) else "")
        counts = {"created": 0, "updated": 0, "skipped": 0, "removed_upstream": 0}
        for x in res:
            if x.status == 404:
                counts["removed_upstream"] += 1  # kept in Jibsy: a contact is never deleted by a sync
                continue
            data = x.text(dav.CARDDAV, "address-data")
            for card in vcard.read(data) if data else []:
                counts[upsert_contact(conn, connection["customer_id"], card, "carddav")] += 1
        if new_token:
            remember_setting(conn, connection, {"sync_token": new_token})
        return {**counts, "sync_token_saved": bool(new_token)}

    def sample_inputs(self, action: str) -> dict:
        return {"find_contact": {"email": "test@example.com"}}.get(action, {})


register(CardDAV())
