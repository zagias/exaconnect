"""What the knowledge-source connectors share (ADR 0029): Google Drive and
OneDrive/SharePoint.

A business picks folders (``folders`` setting); a sync lists them (and
their sub-folders, three levels down), reads the text of each file CommAI
can read, and turns it into a knowledge source **that a person approves**
before the AI uses it (``knowledge.add_source(approved=False)``). A changed
file is updated and needs approving again; a file that left the folders, or
that the business can no longer see, is removed from the knowledge base.

File rules (``sharing`` setting):
  folder        anything in the chosen folders (the default)
  organisation  only files shared with the whole organisation
Files whose owner turned off downloading or copying are always skipped.

Syncs run as a job: on request, when the provider says something changed
(push notifications through the app hook), and every six hours.
"""

from __future__ import annotations

from typing import Any

from . import ActionSpec, ConnectorError, Field, kit
from .more_common import MoreConnector, queue_sync, sync_knowledge

MAX_DEPTH = 3
MAX_FILES = 500
MAX_BYTES = 2_000_000
RESYNC_S = 6 * 3600


class KnowledgeFiles(MoreConnector):
    category = "files"
    folder_pattern = r"[A-Za-z0-9_!./,:-]{1,2000}"
    actions = {
        "list_folders": ActionSpec(
            "list_folders", "List folders", "read", fields=(Field("parent", "Inside folder", required=False),)
        ),
        "list_files": ActionSpec("list_files", "List files in a folder", "read", fields=(Field("folder", "Folder"),)),
    }

    def folders(self, connection: dict) -> list[str]:
        return [f.strip() for f in str(self.settings(connection).get("folders") or "").split(",") if f.strip()]

    # ---- provider specifics ---------------------------------------------------------

    def children(self, conn, connection: dict, folder: str) -> list[dict]:
        """Items in a folder: {"id", "name", "folder": bool, "version", "url", "kind", "meta"}."""
        raise NotImplementedError

    def rule(self, connection: dict, item: dict) -> str:
        """Why the business's file rules exclude this file ("" when allowed)."""
        raise NotImplementedError

    def text(self, conn, connection: dict, item: dict) -> str | None:
        raise NotImplementedError

    # ---- the actions -----------------------------------------------------------------

    def execute(self, conn, connection: dict, action: str, inputs: dict, key: str) -> dict:
        if action == "list_folders":
            items = self.children(conn, connection, inputs.get("parent") or "")
            return {"folders": [{"id": i["id"], "name": i["name"]} for i in items if i["folder"]][:200]}
        if action == "list_files":
            items = self.children(conn, connection, inputs["folder"])
            return {
                "files": [
                    {
                        "id": i["id"],
                        "name": i["name"],
                        "readable": bool(i.get("kind")),
                        "excluded": self.rule(connection, i),
                    }
                    for i in items
                    if not i["folder"]
                ][:200]
            }
        raise ConnectorError(f"Unknown action {action}.", "input")

    # ---- sync --------------------------------------------------------------------------

    def collect(self, conn, connection: dict) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()

        def walk(folder: str, depth: int) -> None:
            for i in self.children(conn, connection, folder):
                if len(out) >= MAX_FILES or i["id"] in seen:
                    continue
                seen.add(i["id"])
                if i["folder"]:
                    if depth < MAX_DEPTH:
                        walk(i["id"], depth + 1)
                    continue
                why = self.rule(connection, i)
                if not why and not i.get("kind"):
                    why = "CommAI reads documents, text, Markdown and CSV files only."
                if not why and int(i.get("size") or 0) > MAX_BYTES:
                    why = "The file is larger than 2 MB."
                out.append({**i, "allowed": not why, "reason": why})

        for f in self.folders(connection):
            walk(f, 0)
        return out

    def sync(self, conn, connection: dict) -> dict:
        if not self.folders(connection):
            raise ConnectorError(f"Choose the {self.label} folders to sync first.", "mapping")
        files = self.collect(conn, connection)

        def fetch(item: dict) -> str | None:
            body = self.text(conn, connection, item)
            return body if isinstance(body, str) else None

        counts = sync_knowledge(conn, connection["customer_id"], self.app, self.label, files, fetch)
        queue_sync(conn, connection["customer_id"], self.app, delay_s=RESYNC_S)
        return counts

    def on_webhook_event(self, conn, connection: dict, event: dict) -> None:
        if event.get("type") == "knowledge.changed":
            queue_sync(conn, connection["customer_id"], self.app)


def as_text(body: Any) -> str | None:
    """A file body from the HTTP layer: text, or JSON that happened to parse."""
    import json

    if not body:
        return None
    if isinstance(body, str):
        return body
    if isinstance(body, dict | list):
        return json.dumps(body, indent=1)
    return None


Setting = kit.Setting
