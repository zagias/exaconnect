"""Forgotten passwords: a one-time link by email."""

from exaconnect_controller import db
from exaconnect_controller.api import password_reset

from .conftest import ADMIN

NEW = "a brand new passphrase"


def _mail(monkeypatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(password_reset.SmtpSender, "missing", staticmethod(lambda: []))
    monkeypatch.setattr(password_reset, "_send", lambda email, link: sent.append((email, link)))
    return sent


def test_reset_by_email_link(client, admin_headers, monkeypatch):
    sent = _mail(monkeypatch)
    r = client.post("/api/v1/auth/password-reset", json={"email": ADMIN[0].upper()})
    assert r.status_code == 202 and r.json() == {"emailed": True}
    ((to, link),) = sent
    assert to == ADMIN[0] and "/reset/" in link
    token = link.rsplit("/", 1)[1]
    with db.tx() as conn:
        stored = conn.execute("SELECT token_hash FROM password_resets").fetchone()["token_hash"]
        audit = conn.execute("SELECT string_agg(detail::text || target, ' ') AS a FROM audit_log").fetchone()["a"]
    assert token not in stored and token not in audit

    assert client.get(f"/api/v1/auth/password-reset/{token}").json() == {"email": ADMIN[0]}
    assert client.post(f"/api/v1/auth/password-reset/{token}", json={"password": "short"}).status_code == 422
    assert client.post(f"/api/v1/auth/password-reset/{token}", json={"password": NEW}).status_code == 204
    # Signed out everywhere; the old password is gone; the link is spent.
    assert client.get("/api/v1/auth/me", headers=admin_headers).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": ADMIN[0], "password": NEW}).status_code == 200
    assert client.post(f"/api/v1/auth/password-reset/{token}", json={"password": NEW}).status_code == 404
    with db.tx() as conn:
        conn.execute(
            "UPDATE users SET password_hash = %s WHERE email = %s", (password_reset.hash_password(ADMIN[1]), ADMIN[0])
        )


def test_asking_reveals_nothing_and_is_limited(client, monkeypatch):
    sent = _mail(monkeypatch)
    r = client.post("/api/v1/auth/password-reset", json={"email": "nobody@example.com"})
    assert r.status_code == 202 and r.json() == {"emailed": True} and sent == []
    for _ in range(5):
        client.post("/api/v1/auth/password-reset", json={"email": ADMIN[0]})
    assert len(sent) == password_reset.MAX_PER_HOUR
    # Only the newest link works.
    old, new = sent[0][1].rsplit("/", 1)[1], sent[-1][1].rsplit("/", 1)[1]
    assert client.get(f"/api/v1/auth/password-reset/{old}").status_code == 404
    assert client.get(f"/api/v1/auth/password-reset/{new}").status_code == 200
    with db.tx() as conn:
        conn.execute("UPDATE password_resets SET expires_at = now() - interval '1 second'")
    assert client.get(f"/api/v1/auth/password-reset/{new}").status_code == 404
    assert client.get("/api/v1/auth/password-reset/nonsense").status_code == 404


def test_no_email_until_smtp_is_set(client, monkeypatch):
    monkeypatch.setattr(password_reset.SmtpSender, "missing", staticmethod(lambda: ["EXA_SMTP_HOST"]))
    called = []
    monkeypatch.setattr(password_reset, "_send", lambda *a: called.append(a))
    r = client.post("/api/v1/auth/password-reset", json={"email": ADMIN[0]})
    assert r.status_code == 202 and r.json() == {"emailed": False} and called == []
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM password_resets").fetchone()["n"] == 0
