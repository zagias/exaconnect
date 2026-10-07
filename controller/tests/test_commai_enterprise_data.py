"""Data governance (ADR 0030): retention with legal hold, subject export and
erasure, full business export, processing locations."""

from __future__ import annotations

import io
import json
import secrets
import zipfile

from exaconnect_controller import db
from exaconnect_controller.commai import inbox

from .commai_helpers import base, business, run_jobs


def _conv(b, address, body, channel="web", name="Ana"):
    with db.tx() as conn:
        out = inbox.receive(conn, b["id"], channel, address, body, name=name)
        return out["conversation"]


def _age(table: str, ids: list, days: int, column: str = "created_at") -> None:
    with db.tx() as conn:
        conn.execute(
            f"UPDATE {table} SET {column} = now() - make_interval(days => %s) WHERE id = ANY(%s)",  # noqa: S608
            (days, ids),
        )


def _seed(b, address, text, phone):
    """A contact with an old and a new message, note, AI run and a recorded call."""
    conv = _conv(b, address, text)
    with db.tx() as conn:
        conn.execute("UPDATE contacts SET phone = %s WHERE id = %s", (phone, conv["contact_id"]))
        old_note = inbox.add_note(conn, b["id"], conv["id"], author="user:x", body=f"old note {text}")["id"]
        new_note = inbox.add_note(conn, b["id"], conv["id"], author="user:x", body=f"new note {text}")["id"]
        old_msg = conn.execute("SELECT id FROM messages WHERE conversation_id = %s", (conv["id"],)).fetchone()["id"]
        new_msg = conn.execute(
            """INSERT INTO messages (customer_id, conversation_id, direction, author_kind, body)
               VALUES (%s, %s, 'in', 'contact', %s) RETURNING id""",
            (b["id"], conv["id"], f"recent {text}"),
        ).fetchone()["id"]
        run = conn.execute(
            """INSERT INTO ai_runs (customer_id, conversation_id, role, outcome, reason)
               VALUES (%s, %s, 'customer_agent', 'replied', 'old run') RETURNING id""",
            (b["id"], conv["id"]),
        ).fetchone()["id"]
        conn.execute(
            """INSERT INTO voice_cdrs
                 (customer_id, call_id, direction, from_number, started_at, ended_at, recording_ref)
               VALUES (%s, %s, 'inbound', %s, now() - interval '100 days', now() - interval '100 days', 'rec-1')""",
            (b["id"], f"call-{phone}", phone),
        )
    _age("messages", [old_msg], 100)
    _age("commai_notes", [old_note], 100)
    _age("ai_runs", [run], 100)
    return {
        "conv": conv,
        "old_msg": old_msg,
        "new_msg": new_msg,
        "old_note": old_note,
        "new_note": new_note,
        "run": run,
    }


def test_retention_job_deletes_only_expired_data_and_never_held_items(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    a = _seed(b, "+18685550201", "alpha", "+18685550201")
    held = _seed(b, "+18685550202", "bravo", "+18685550202")
    r = client.put(
        f"{u}/data/contacts/{held['conv']['contact_id']}/hold", json={"on": True, "reason": "Claim 7"}, headers=h
    )
    assert r.status_code == 200 and r.json()["legal_hold"]
    rules = {"messages": 30, "notes": 30, "call_recordings": 30, "transcripts": 30, "ai_logs": 30}
    r = client.put(f"{u}/data/retention", json={"rules": rules}, headers=h)
    assert r.status_code == 200, r.text
    assert client.post(f"{u}/data/retention/run", headers=h).status_code == 202
    run_jobs()
    with db.tx() as conn:
        msg = lambda i: conn.execute("SELECT body, redacted_at FROM messages WHERE id = %s", (i,)).fetchone()  # noqa: E731
        note = lambda i: conn.execute("SELECT 1 FROM commai_notes WHERE id = %s", (i,)).fetchone()  # noqa: E731
        # Expired, not held: redacted or deleted.
        assert msg(a["old_msg"])["body"] == "" and msg(a["old_msg"])["redacted_at"] is not None
        assert note(a["old_note"]) is None
        assert conn.execute("SELECT 1 FROM ai_runs WHERE id = %s", (a["run"],)).fetchone() is None
        rec = conn.execute("SELECT recording_ref FROM voice_cdrs WHERE from_number = '+18685550201'").fetchone()
        assert rec["recording_ref"] == ""
        # Not expired: untouched.
        assert msg(a["new_msg"])["body"] == "recent alpha" and note(a["new_note"])
        # Held: untouched, however old.
        assert msg(held["old_msg"])["body"] == "bravo" and note(held["old_note"])
        assert conn.execute("SELECT 1 FROM ai_runs WHERE id = %s", (held["run"],)).fetchone()
        rec = conn.execute("SELECT recording_ref FROM voice_cdrs WHERE from_number = '+18685550202'").fetchone()
        assert rec["recording_ref"] == "rec-1"
    # Saving the rules queued today's run; the manual run found nothing more to do.
    runs = client.get(f"{u}/data", headers=h).json()["runs"]
    assert sorted(r["trigger"] for r in runs) == ["manual", "schedule"]
    assert sum(r["counts"]["notes"] for r in runs) == 1
    assert all(r["held"]["notes"] == 1 and r["held"]["messages"] == 1 for r in runs)

    # A business-wide hold stops the job altogether.
    client.put(f"{u}/data/retention", json={"rules": rules, "hold_all": True, "hold_reason": "Audit"}, headers=h)
    _age("commai_notes", [a["new_note"]], 100)
    client.post(f"{u}/data/retention/run", headers=h)
    run_jobs()
    with db.tx() as conn:
        assert conn.execute("SELECT 1 FROM commai_notes WHERE id = %s", (a["new_note"],)).fetchone()


def test_subject_export_contains_only_that_contact_and_that_business(client):
    b = business(client)
    other = business(client, "Other Bank", people=("agent",))
    u, h = base(b), b["agent"]["h"]
    mine = _seed(b, "+18685550301", "mine-secret-words", "+18685550301")
    _seed(b, "+18685550302", "neighbour-words", "+18685550302")
    _seed(other, "+18685550301", "other-tenant-words", "+18685550301")  # same address, other business
    cid = mine["conv"]["contact_id"]
    r = client.get(f"{u}/data/contacts/{cid}/export", headers=h)
    assert r.status_code == 200, r.text
    text = json.dumps(r.json())
    assert "mine-secret-words" in text
    assert "neighbour-words" not in text and "other-tenant-words" not in text
    assert "old note" not in text and r.json()["notes_included"] is False  # notes only when asked
    assert len(r.json()["calls"]) == 1
    r = client.get(f"{u}/data/contacts/{cid}/export", params={"include_notes": True, "format": "zip"}, headers=h)
    assert r.headers["content-type"] == "application/zip"
    data = json.loads(zipfile.ZipFile(io.BytesIO(r.content)).read("subject.json"))
    assert {n["body"] for n in data["notes"]} == {"old note mine-secret-words", "new note mine-secret-words"}
    # Another business can't export this contact, and can't find it through its own id.
    assert client.get(f"{u}/data/contacts/{cid}/export", headers=other["agent"]["h"]).status_code == 403
    assert client.get(f"{base(other)}/data/contacts/{cid}/export", headers=other["agent"]["h"]).status_code == 404
    reqs = client.get(f"{u}/data", headers=h).json()["requests"]
    assert [x["kind"] for x in reqs] == ["export", "export"]


def test_erasure_is_blocked_by_legal_hold_then_anonymises_or_deletes(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    s = _seed(b, "+18685550401", "erase-me", "+18685550401")
    keep = _seed(b, "+18685550402", "keep-me", "+18685550402")
    cid = str(s["conv"]["contact_id"])
    client.put(f"{u}/data/contacts/{cid}/hold", json={"on": True}, headers=h)
    r = client.post(f"{u}/data/contacts/{cid}/erase", json={"mode": "anonymise", "confirm": cid}, headers=h)
    assert r.status_code == 409 and "legal hold" in r.json()["detail"]
    client.put(f"{u}/data/contacts/{cid}/hold", json={"on": False}, headers=h)
    assert (
        client.post(f"{u}/data/contacts/{cid}/erase", json={"mode": "anonymise", "confirm": "x"}, headers=h).status_code
        == 422
    )
    r = client.post(f"{u}/data/contacts/{cid}/erase", json={"mode": "anonymise", "confirm": cid}, headers=h)
    assert r.status_code == 200, r.text
    with db.tx() as conn:
        c = conn.execute("SELECT * FROM contacts WHERE id = %s", (cid,)).fetchone()
        assert c["name"] == "" and c["phone"] == "" and c["anonymised_at"] is not None
        assert not conn.execute(
            "SELECT 1 FROM messages WHERE conversation_id = %s AND body <> ''", (s["conv"]["id"],)
        ).fetchone()
        assert not conn.execute("SELECT 1 FROM contact_identities WHERE contact_id = %s", (cid,)).fetchone()
        assert (
            conn.execute("SELECT from_number FROM voice_cdrs WHERE call_id = 'call-+18685550401'").fetchone()[
                "from_number"
            ]
            == "removed"
        )
        # The other contact is untouched.
        assert conn.execute("SELECT body FROM messages WHERE id = %s", (keep["new_msg"],)).fetchone()["body"]
    kid = str(keep["conv"]["contact_id"])
    r = client.post(f"{u}/data/contacts/{kid}/erase", json={"mode": "delete", "confirm": kid}, headers=h)
    assert r.status_code == 200
    with db.tx() as conn:
        assert not conn.execute("SELECT 1 FROM contacts WHERE id = %s", (kid,)).fetchone()
        assert not conn.execute("SELECT 1 FROM conversations WHERE id = %s", (keep["conv"]["id"],)).fetchone()
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM commai_subject_requests WHERE customer_id = %s", (b["id"],)
            ).fetchone()["n"]
            == 3
        )  # refused, anonymise, delete


def test_business_export_has_own_data_and_no_secrets(client):
    b = business(client)
    other = business(client, "Other Bank", people=("agent",))
    u, h = base(b), b["agent"]["h"]
    _seed(b, "+18685550501", "ours", "+18685550501")
    _seed(other, "+18685550502", "theirs", "+18685550502")
    widget_secret = secrets.token_hex(16)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO widget_keys (customer_id, public_key, secret) VALUES (%s, %s, %s)",
            (b["id"], "pk_" + secrets.token_hex(6), widget_secret),
        )
    r = client.post(f"{u}/data/exports", json={"include_notes": False}, headers=h)
    assert r.status_code == 202, r.text
    eid = r.json()["id"]
    assert client.get(f"{u}/data/exports/{eid}/download", headers=h).status_code == 409
    run_jobs()
    r = client.get(f"{u}/data/exports/{eid}/download", headers=h)
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    blob = b"".join(z.read(n) for n in z.namelist()).decode()
    assert "ours" in blob and "theirs" not in blob
    assert widget_secret not in blob and "password_hash" not in blob
    assert "commai_notes.json" not in z.namelist() and "widget_keys.json" in z.namelist()
    assert client.get(f"{u}/data/exports/{eid}/download", headers=other["agent"]["h"]).status_code == 403


def test_roles_govern_data_export_and_processing_page_is_honest(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    s = _seed(b, "+18685550601", "x", "+18685550601")
    role = client.post(f"{u}/roles", json={"name": "Reader", "permissions": ["read_inbox"]}, headers=h).json()
    client.post(f"{u}/roles/assignments", json={"user_id": b["agent2"]["id"], "role_id": role["id"]}, headers=h)
    h2 = b["agent2"]["h"]
    r = client.get(f"{u}/data/contacts/{s['conv']['contact_id']}/export", headers=h2)
    assert r.status_code == 403 and "export" in r.json()["detail"]
    assert client.put(f"{u}/data/retention", json={"rules": {"notes": 1}}, headers=h2).status_code == 403
    page = client.get(f"{u}/data/processing", headers=h2).json()
    whats = {row["what"]: row for row in page["rows"]}
    assert whats["AI answers and summaries"]["status"] == "simulated"
    assert whats["Phone numbers and calls (SIP)"]["status"] == "simulated"
    assert "not audited" in page["note"] or "has not audited" in page["note"]
