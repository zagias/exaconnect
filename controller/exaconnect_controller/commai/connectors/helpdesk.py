"""What the helpdesk connectors share (ADR 0035): Zendesk, Freshdesk and
ServiceNow offer the same actions, so AI roles and workflows use them the
same way.

  find_ticket     read     a ticket by its id or number
  list_tickets    read     a requester's tickets, newest first (paged)
  create_ticket   create   a ticket, linked to the conversation it came from
  update_ticket   update   status and priority
  add_comment     update   a private note (default) or a public reply

Statuses are shown in Jibsy's words: new, open, pending, on_hold, solved,
closed. A created ticket carries a reference derived from the action's
idempotency key, and the connector looks for it before creating, so a retry
never makes a second ticket. The helpdesk's change notifications (webhooks)
sync the status back to the linked conversation.
"""

from __future__ import annotations

from typing import Any

import psycopg

from . import ActionSpec, ConnectorError, Field, kit
from .more_common import MoreConnector, link_ticket, sync_ticket_status

STATUSES = ("new", "open", "pending", "on_hold", "solved", "closed")
PRIORITIES = ("low", "normal", "high", "urgent")

ACTIONS = {
    "find_ticket": ActionSpec("find_ticket", "Look up a ticket", "read", fields=(Field("ticket_id", "Ticket"),)),
    "list_tickets": ActionSpec(
        "list_tickets", "List a customer's tickets", "read", fields=(Field("email", "Customer email", "email"),)
    ),
    "create_ticket": ActionSpec(
        "create_ticket",
        "Create a ticket",
        "create",
        fields=(
            Field("subject", "Subject"),
            Field("description", "Description", "text"),
            Field("email", "Customer email", "email"),
            Field("name", "Customer name", required=False),
            Field("priority", "Priority", required=False),
            Field("conversation_id", "Conversation", required=False),
        ),
    ),
    "update_ticket": ActionSpec(
        "update_ticket",
        "Change a ticket's status or priority",
        "update",
        fields=(
            Field("ticket_id", "Ticket"),
            Field("status", "Status", required=False),
            Field("priority", "Priority", required=False),
        ),
    ),
    "add_comment": ActionSpec(
        "add_comment",
        "Add a note or reply to a ticket",
        "update",
        fields=(
            Field("ticket_id", "Ticket"),
            Field("body", "Text", "text"),
            Field("public", "Send to the customer (yes or no)", required=False),
        ),
    ),
}


class Helpdesk(MoreConnector):
    category = "helpdesk"
    actions = ACTIONS
    mapping_targets: dict[str, list[str]] = {}

    def validate(self, action: str, inputs: dict) -> dict:
        out = super().validate(action, inputs)
        if out.get("status") and out["status"] not in STATUSES:
            raise ValueError(f"Status is one of: {', '.join(STATUSES)}.")
        if out.get("priority") and out["priority"] not in PRIORITIES:
            raise ValueError(f"Priority is one of: {', '.join(PRIORITIES)}.")
        if action == "update_ticket" and not (out.get("status") or out.get("priority")):
            raise ValueError("Say what to change: a status or a priority.")
        if "public" in out:
            v = str(out["public"]).lower()
            if v not in ("yes", "no", "true", "false"):
                raise ValueError("Send to the customer is yes or no.")
            out["public"] = v in ("yes", "true")
        if "ticket_id" in out:
            out["ticket_id"] = str(out["ticket_id"]).strip().lstrip("#")
        return out

    # ---- provider specifics (each helpdesk fills these in) -------------------------------

    def _get(self, conn, connection: dict, ticket_id: str) -> dict | None:
        raise NotImplementedError

    def _list(self, conn, connection: dict, email: str) -> list[dict]:
        raise NotImplementedError

    def _find_ref(self, conn, connection: dict, ref: str) -> dict | None:
        raise NotImplementedError

    def _create(self, conn, connection: dict, inputs: dict, ref: str, key: str) -> dict:
        raise NotImplementedError

    def _create_body(self, connection: dict, inputs: dict, ref: str) -> dict:
        raise NotImplementedError

    def _update(self, conn, connection: dict, ticket_id: str, status: str, priority: str) -> dict:
        raise NotImplementedError

    def _comment(self, conn, connection: dict, ticket_id: str, body: str, public: bool, key: str) -> dict:
        raise NotImplementedError

    # ---- the actions -------------------------------------------------------------------

    def execute(self, conn: psycopg.Connection, connection: dict, action: str, inputs: dict, key: str) -> dict:
        cid = connection["customer_id"]
        if action == "find_ticket":
            t = self._get(conn, connection, inputs["ticket_id"])
            return {"found": bool(t), "ticket": t}
        if action == "list_tickets":
            items = self._list(conn, connection, inputs["email"])
            return {"tickets": items[:50], "count": len(items)}
        if action == "create_ticket":
            known = self.known(conn, connection, key)
            if known:
                return {"ticket_id": known["object_id"], "replayed": True}
            ref = kit.ref(key)
            if self.dry(connection):
                return {"dry_run": True, "would_send": self._create_body(connection, inputs, ref)}
            t = self._find_ref(conn, connection, ref)
            existing = t is not None
            if t is None:
                t = self._create(conn, connection, inputs, ref, key)
            self.remember(conn, connection, key, "ticket", t["id"])
            link_ticket(
                conn,
                cid,
                self.app,
                t["id"],
                conversation_id=inputs.get("conversation_id"),
                number=t.get("number", ""),
                status=t.get("status", ""),
                url=t.get("url", ""),
            )
            return {
                "ticket_id": t["id"],
                "number": t.get("number", t["id"]),
                "status": t.get("status", ""),
                "url": t.get("url", ""),
                "existing": existing,
            }
        if action == "update_ticket":
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"ticket": inputs}}
            t = self._update(
                conn, connection, inputs["ticket_id"], inputs.get("status", ""), inputs.get("priority", "")
            )
            if t.get("status"):
                sync_ticket_status(conn, cid, self.app, self.label, t["id"], t["status"])
            return {"ticket_id": t["id"], "status": t.get("status", ""), "priority": t.get("priority", "")}
        if action == "add_comment":
            known = self.known(conn, connection, key)
            if known:
                return {"comment_id": known["object_id"], "replayed": True}
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"comment": inputs}}
            out = self._comment(conn, connection, inputs["ticket_id"], inputs["body"], bool(inputs.get("public")), key)
            self.remember(conn, connection, key, "comment", str(out.get("id", "")))
            return {"comment_id": str(out.get("id", "")), "ticket_id": inputs["ticket_id"], "public": out["public"]}
        raise ConnectorError(f"Unknown action {action}.", "input")

    def sample_inputs(self, action: str) -> dict:
        return {"find_ticket": {"ticket_id": "1"}, "list_tickets": {"email": "test@example.com"}}.get(action, {})

    # ---- change notifications -----------------------------------------------------------

    def on_webhook_event(self, conn, connection: dict, event: dict) -> None:
        """Apply one verified event: sync a ticket's status back to Jibsy."""
        if event.get("type") == "ticket.updated":
            d = event.get("data") or {}
            sync_ticket_status(
                conn, connection["customer_id"], self.app, self.label, str(d.get("ticket_id", "")), d.get("status", "")
            )


def not_found(r: Any) -> bool:
    return getattr(r, "status", 0) == 404
