"""Bulk changes from a spreadsheet (CSV), checked row by row (ADR 0021).

One row per change. The `action` column says what the row does; the other
columns depend on it:

  add_user       name, [extension, email, mobile, site, team, portal_email]
  update_user    extension, [name, email, mobile]
  move_user      extension, [site, team, new_extension]
  remove_user    extension
  add_number     [target_type, target, site]
  remove_number  number
  assign_number  number, target_type, [target]
  add_device     extension, kind (desk | softphone), [mac, model]

Every row is checked (first on its own, then by trying the whole change in a
savepoint that is always rolled back), and the errors come back with their
row numbers. Nothing applies unless every row passes.
"""

from __future__ import annotations

import csv
import io

from .common import VoiceError

COLUMNS = {
    "add_user": (("name",), ("extension", "email", "mobile", "site", "team", "portal_email")),
    "update_user": (("extension",), ("name", "email", "mobile")),
    "move_user": (("extension",), ("site", "team", "new_extension")),
    "remove_user": (("extension",), ()),
    "add_number": ((), ("target_type", "target", "site")),
    "remove_number": (("number",), ()),
    "assign_number": (("number", "target_type"), ("target",)),
    "add_device": (("extension", "kind"), ("mac", "model")),
}
TEMPLATE = "action,name,extension,email,mobile,site,team,new_extension,number,target_type,target,kind,mac,model\n"
MAX_ROWS = 2000


def parse(text: str) -> tuple[list[dict], list[dict], list[int]]:
    """-> (ops, errors [{"row", "error"}], row number of each op)."""
    if len(text) > 2_000_000:
        raise VoiceError("The file is too big (2 MB at most).", 413)
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or "action" not in [f.strip().lower() for f in reader.fieldnames]:
        raise VoiceError("The first row must be the column names, including 'action'. Download the template.", 422)
    ops, errors, rows = [], [], []
    for n, raw in enumerate(reader, start=2):
        if n - 1 > MAX_ROWS:
            raise VoiceError(f"Up to {MAX_ROWS} rows at once.", 422)
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        if not any(row.values()):
            continue
        action = row.get("action", "")
        if action not in COLUMNS:
            errors.append({"row": n, "error": f"Unknown action {action!r}. Use one of: {', '.join(COLUMNS)}."})
            continue
        required, optional = COLUMNS[action]
        missing = [c for c in required if not row.get(c)]
        if missing:
            errors.append({"row": n, "error": f"{action} needs {', '.join(missing)}."})
            continue
        op: dict = {"op": action}
        for c in (*required, *optional):
            if row.get(c):
                op[c] = row[c]
        if "extension" in op and action != "add_user":
            op["user"] = op.pop("extension")
        if action == "move_user" and "new_extension" in op:
            op["extension"] = op.pop("new_extension")
        if action == "add_device" and op["kind"] not in ("desk", "softphone"):
            errors.append({"row": n, "error": "kind is desk or softphone."})
            continue
        ops.append(op)
        rows.append(n)
    if not ops and not errors:
        raise VoiceError("The file has no rows.", 422)
    return ops, errors, rows
