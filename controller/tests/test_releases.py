"""Releases (ADR 0025): the host's release script records each release; admins read them."""

from __future__ import annotations

from exaconnect_controller import db


def test_releases_are_listed_for_admins_only(client, admin_headers, monkeypatch):
    monkeypatch.setenv("EXA_BUILD_COMMIT", "abc123def456")
    with db.tx() as conn:
        conn.execute("INSERT INTO releases (commit, status, finished_at) VALUES ('111111111111', 'live', now())")
        conn.execute(
            """INSERT INTO releases (commit, previous, status, backup, detail, finished_at)
               VALUES (%s, %s, 'rolled_back', 'exaconnect-x.dump.enc', 'portal not served', now())""",
            ("222222222222", "111111111111"),
        )
    out = client.get("/api/v1/releases", headers=admin_headers).json()
    assert out["running"]["commit"] == "abc123def456"
    assert [(r["commit"], r["status"], r["backed_up"]) for r in out["releases"]] == [
        ("222222222222", "rolled_back", True),
        ("111111111111", "live", False),
    ]
    assert "backup" not in out["releases"][0]
    assert client.get("/api/v1/version").json()["commit"] == "abc123def456"
    assert client.get("/api/v1/releases").status_code == 401
