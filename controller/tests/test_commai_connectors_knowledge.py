# ruff: noqa: F401, F811  (pytest fixtures imported from the helpers)
"""Knowledge-source connectors (ADR 0035): Google Drive and OneDrive/SharePoint
files become knowledge sources a person approves; changes need approving
again; files the business can no longer see are removed."""

import json
import urllib.parse

from exaconnect_controller import db
from exaconnect_controller.commai.connectors import kit

from .commai_helpers import base, business, run_jobs
from .connectors_more_helpers import (
    connect_real,
    connect_simulated,
    deliver,
    execute_direct,
    fake,
    go_live,
    hook,
    propose_and_run,
    q,
    real_env,
)

DRIVE = r"https://www\.googleapis\.com/drive/v3/"
GRAPH = r"https://graph\.microsoft\.com/v1\.0/"


def _sources(customer_id):
    return {r["title"]: r for r in q("SELECT * FROM knowledge_sources WHERE customer_id = %s", customer_id)}


def _sync(client, b, app):
    r = client.post(f"{base(b)}/connectors/{app}/sync", headers=b["agent"]["h"])
    assert r.status_code == 202, r.text
    run_jobs()
    return {f["name"]: f for f in client.get(f"{base(b)}/connectors/{app}/files", headers=b["agent"]["h"]).json()}


def test_drive_stand_in_sync_needs_approval_and_follows_changes(client, fake):
    b, other = business(client), business(client, "Other Bank")
    connect_simulated(b["id"], "google_drive", ["list_folders", "list_files"])
    h = b["agent"]["h"]
    assert client.post(f"{base(b)}/connectors/google_drive/sync", headers=h).status_code == 409  # no folders yet
    run = propose_and_run(b["id"], "google_drive", "list_folders", {}, "f1")
    assert run["result"]["folders"] == [{"id": "fld_help", "name": "Help centre (example data)"}]
    r = client.put(
        f"{base(b)}/integrations/google_drive/settings", json={"settings": {"folders": "fld_help"}}, headers=h
    )
    assert r.status_code == 200, r.text
    files = _sync(client, b, "google_drive")
    assert files["Opening hours"]["status"] == "synced" and files["Opening hours"]["approved"] is False
    assert files["Fees.md"]["status"] == "synced"
    assert files["Board minutes"]["status"] == "skipped" and "copying" in files["Board minutes"]["reason"]
    assert files["Form.pdf"]["status"] == "skipped"
    src = _sources(b["id"])
    assert src["Opening hours"]["body"] == "We are open 8am to 4pm, Monday to Friday."
    assert not src["Opening hours"]["approved"] and "Board minutes" not in src
    assert not _sources(other["id"])  # tenant isolation
    # A person approves it; then the file changes: it needs approving again.
    sid = src["Opening hours"]["id"]
    r = client.post(f"{base(b)}/ai/knowledge/{sid}/approve", json={"approved": True}, headers=h)
    assert r.status_code == 200, r.text
    assert _sources(b["id"])["Opening hours"]["approved"]
    with db.tx() as conn:
        conn.execute(
            """UPDATE sim_records SET data = data || '{"version": "2", "text": "We are open 8am to 6pm."}'::jsonb
               WHERE app = 'sim:google_drive' AND idempotency_key = 'file:doc_hours' AND customer_id = %s""",
            (b["id"],),
        )
    _sync(client, b, "google_drive")
    s = _sources(b["id"])["Opening hours"]
    assert s["body"] == "We are open 8am to 6pm." and not s["approved"] and s["id"] == sid
    # The file leaves the folder: it is removed from the knowledge base.
    with db.tx() as conn:
        conn.execute(
            """UPDATE sim_records SET data = data || '{"trashed": true}'::jsonb
               WHERE app = 'sim:google_drive' AND idempotency_key = 'file:txt_fees' AND customer_id = %s""",
            (b["id"],),
        )
    files = _sync(client, b, "google_drive")
    assert files["Fees.md"]["status"] == "removed" and "Fees.md" not in _sources(b["id"])


def test_drive_live_rules_paging_and_expired_sign_in(client, real_env, fake):
    b = business(client)
    go_live("google_drive", b["id"])
    connect_real(
        b["id"],
        "google_drive",
        ["list_files"],
        settings={"folders": "F1", "sharing": "organisation", "domain": "examplebank.example"},
    )

    def listing(call):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(call["url"]).query)
        assert qs["q"] == ["'F1' in parents and trashed=false"]
        if "pageToken" not in qs:
            return 200, {
                "nextPageToken": "p2",
                "files": [
                    {
                        "id": "d1",
                        "name": "Hours",
                        "mimeType": "application/vnd.google-apps.document",
                        "version": "4",
                        "permissions": [{"type": "domain", "domain": "examplebank.example"}],
                    }
                ],
            }
        return 200, {
            "files": [
                {
                    "id": "d2",
                    "name": "Private",
                    "mimeType": "text/plain",
                    "version": "1",
                    "permissions": [{"type": "user"}],
                }
            ]
        }

    fake.on("GET", DRIVE + r"files\?", listing)
    fake.on("GET", DRIVE + r"files/d1/export\?mimeType=text%2Fplain", (200, "Open 8 to 4."))
    with db.tx() as conn:
        from exaconnect_controller.commai import connectors

        c = connectors.get("google_drive")
        row = connectors.connection(conn, b["id"], "google_drive")
        counts = c.sync(conn, {**row, "test": False})
    assert counts["added"] == 1 and counts["skipped"] == 1
    assert _sources(b["id"])["Hours"]["body"] == "Open 8 to 4."
    assert "organisation" in q("SELECT reason FROM commai_knowledge_files WHERE file_id = 'd2'")[0]["reason"]
    assert fake.last("GET", "export")["headers"]["Authorization"] == "Bearer tok-google_drive"
    fake.on("GET", DRIVE + r"files\?", (401, {"error": {"code": 401, "message": "Invalid Credentials"}}))
    run = propose_and_run(b["id"], "google_drive", "list_files", {"folder": "F1"}, "k1")
    assert run["status"] == "failed" and "(expired_signin)" in run["error"]


def test_drive_push_channel_token(client, real_env, fake):
    b = business(client)
    go_live("google_drive", b["id"])
    connect_real(b["id"], "google_drive", ["list_files"], settings={"folders": "F1"})
    fake.on("GET", DRIVE + r"changes/startPageToken", (200, {"startPageToken": "42"}))
    fake.on("POST", DRIVE + r"changes/watch", lambda c: (200, {"id": c["body"]["id"], "resourceId": "r1"}))
    r = client.post(f"{base(b)}/connectors/google_drive/webhooks", headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    sent = fake.last("POST", "changes/watch")
    assert "pageToken=42" in sent["url"] and sent["body"]["token"] == hook(b["id"], "google_drive")["secret"]
    headers = {
        "X-Goog-Channel-ID": sent["body"]["id"],
        "X-Goog-Resource-State": "change",
        "X-Goog-Message-Number": "7",
        "X-Goog-Channel-Token": "wrong",
    }
    assert deliver(b["id"], "google_drive", headers, b"") is None
    headers["X-Goog-Channel-Token"] = sent["body"]["token"]
    assert deliver(b["id"], "google_drive", {**headers, "X-Goog-Resource-State": "sync"}, b"") == []
    evs = deliver(b["id"], "google_drive", headers, b"")
    assert evs[0]["type"] == "knowledge.changed"
    assert q("SELECT 1 FROM jobs WHERE kind = 'connector.knowledge_sync' AND customer_id = %s", b["id"])


def test_onedrive_stand_in_and_graph_notifications(client, real_env, fake):
    b = business(client)
    connect_simulated(
        b["id"],
        "onedrive",
        ["list_folders", "list_files", "find_libraries"],
        settings={"folders": "b!sim/F1", "sharing": "organisation"},
    )
    run = propose_and_run(b["id"], "onedrive", "find_libraries", {"search": "service"}, "l1")
    assert run["result"]["libraries"][0]["folder"] == "b!sim/root"
    run = propose_and_run(b["id"], "onedrive", "list_folders", {"parent": "b!sim/root"}, "l2")
    assert run["result"]["folders"][0]["id"] == "b!sim/F1"
    files = _sync(client, b, "onedrive")
    assert files["Returns.md"]["status"] == "synced"
    assert files["Private notes.txt"]["status"] == "skipped"  # not shared with the organisation
    assert files["Handbook.docx"]["status"] == "skipped"
    assert _sources(b["id"])["Returns.md"]["body"].startswith("Returns are accepted")

    # Graph subscriptions, live: validation handshake, then clientState on each notification.
    go_live("onedrive", b["id"])
    connect_real(b["id"], "onedrive", ["list_files"], settings={"folders": "b!abc/F1,b!abc/F2,b!xyz/F9"})
    fake.on("POST", GRAPH + "subscriptions$", lambda c: (201, {"id": "s-" + c["body"]["resource"]}))
    r = client.post(f"{base(b)}/connectors/onedrive/webhooks", headers=b["agent"]["h"])
    assert r.status_code == 200 and len(r.json()["subscriptions"]) == 2, r.text
    sub = fake.last("POST", "subscriptions")["body"]
    assert sub["changeType"] == "updated" and sub["clientState"] == hook(b["id"], "onedrive")["secret"]
    from exaconnect_controller.commai import connectors

    c = connectors.get("onedrive")
    assert c.webhook_handshake({}, b"", {"validationToken": "abc 123"}) == (200, "text/plain", "abc 123")
    note = {"value": [{"subscriptionId": "s1", "clientState": sub["clientState"], "resource": "drives/b!abc/root"}]}
    bad = {"value": [{"subscriptionId": "s1", "clientState": "nope", "resource": "drives/b!abc/root"}]}
    assert deliver(b["id"], "onedrive", {}, json.dumps(bad).encode()) is None
    assert deliver(b["id"], "onedrive", {}, json.dumps(note).encode())[0]["type"] == "knowledge.changed"
