"""Languages (ADR 0026): catalogues, the go-live registry gate, reviewer
sign-off, each person's language, the AI's reply languages and the widget."""

from __future__ import annotations

import re

from exaconnect_controller import db
from exaconnect_controller.commai import golive, i18n

from .commai_helpers import base, business

GL = "/api/v1/commai/golive"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def switch_on(client, admin_headers, locale: str, status="on", pilots=None):
    for c in ("tested", "security-review", "operations", *i18n.LANGUAGE_CRITERIA):
        r = client.put(
            f"{GL}/language/{locale}/criteria/{c}", json={"met": True, "evidence": "review 1"}, headers=admin_headers
        )
        assert r.status_code == 200, r.text
    body = {"status": status} if status == "on" else {"status": status, "pilots": pilots}
    r = client.put(f"{GL}/language/{locale}/status", json=body, headers=admin_headers)
    assert r.status_code == 200, r.text


def test_catalogues_are_complete_and_keep_their_placeholders():
    src = i18n.catalogue(i18n.SOURCE)
    assert len(src) > 100
    for loc in i18n.LOCALES:
        cat = i18n.catalogue(loc)
        assert i18n.missing_keys(loc) == [], loc
        assert set(cat) <= set(src), f"{loc} has keys English lacks"
        for k, v in cat.items():
            assert set(PLACEHOLDER.findall(v)) == set(PLACEHOLDER.findall(src[k])), (loc, k)
            assert v.strip(), (loc, k)
        assert any(cat[k] != src[k] for k in src) or loc == i18n.SOURCE  # really translated


def test_languages_are_declared_and_start_off(client):
    b = business(client, people=("agent",))
    with db.tx() as conn:
        for loc in ("es", "fr", "nl", "ht"):
            assert golive.get(conn, "language", loc)["status"] == "off"
            assert not i18n.available(conn, loc, b["id"])
        assert i18n.offered(conn, b["id"]) == ["en-GB"]


def test_my_language_is_offered_only_once_switched_on(client, admin_headers):
    b = business(client, people=("agent", "agent2"))
    u, h = base(b), b["agent"]["h"]
    out = client.get(f"{u}/languages", headers=h).json()
    assert out["my_locale"] == "en-GB"
    es = next(x for x in out["locales"] if x["locale"] == "es")
    assert es["available"] is False and es["status"] == "machine-drafted"

    r = client.put(f"{u}/languages/me", json={"locale": "es"}, headers=h)
    assert r.status_code == 409 and "not switched on" in r.json()["detail"]

    # A pilot for this business only.
    switch_on(client, admin_headers, "es", status="pilot", pilots=[b["id"]])
    r = client.put(f"{u}/languages/me", json={"locale": "es"}, headers=h)
    assert r.status_code == 200, r.text
    assert client.get(f"{u}/languages", headers=h).json()["my_locale"] == "es"
    # Another business doesn't get it.
    other = business(client, "Other Bank", people=("agent",))
    r = client.put(f"{base(other)}/languages/me", json={"locale": "es"}, headers=other["agent"]["h"])
    assert r.status_code == 409
    # Each person has their own language.
    assert client.get(f"{u}/languages", headers=b["agent2"]["h"]).json()["my_locale"] == "en-GB"

    # Switched off again: the person falls back to English.
    client.put(f"{GL}/language/es/criteria/support", json={"met": False}, headers=admin_headers)
    assert client.get(f"{u}/languages", headers=h).json()["my_locale"] == "en-GB"
    assert client.put(f"{u}/languages/me", json={"locale": "xx"}, headers=h).status_code == 422


def test_catalogue_is_machine_drafted_until_a_reviewer_signs_off(client, admin_headers, monkeypatch):
    b = business(client, people=("agent",))
    h = b["agent"]["h"]
    r = client.get("/api/v1/commai/i18n/catalogues/fr", headers=h)
    assert r.status_code == 200 and r.json()["machine_drafted"] is True
    assert r.json()["messages"]["widget.send"] == i18n.catalogue("fr")["widget.send"]
    assert client.get("/api/v1/commai/i18n/catalogues/en-GB", headers=h).json()["status"] == "source"
    assert client.get("/api/v1/commai/i18n/catalogues/zz", headers=h).status_code == 404

    # Only ExaCarib admins sign off.
    assert client.post("/api/v1/commai/i18n/catalogues/fr/review", json={}, headers=h).status_code == 403
    r = client.post("/api/v1/commai/i18n/catalogues/fr/review", json={"note": "Read by M."}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["status"] == "reviewed"
    assert client.get("/api/v1/commai/i18n/catalogues/fr", headers=h).json()["machine_drafted"] is False

    # Editing the catalogue after sign-off makes it a draft again.
    changed = {**i18n.catalogue("fr"), "widget.send": "Envoyer maintenant"}
    real = i18n._load
    monkeypatch.setattr(i18n, "_load", lambda loc: changed if loc == "fr" else real(loc))
    assert client.get("/api/v1/commai/i18n/catalogues/fr", headers=h).json()["status"] == "machine-drafted"
    monkeypatch.setattr(i18n, "_load", real)
    r = client.delete("/api/v1/commai/i18n/catalogues/fr/review", headers=admin_headers)
    assert r.json()["status"] == "machine-drafted"
    with db.tx() as conn:
        acts = {r["action"] for r in conn.execute("SELECT action FROM audit_log").fetchall()}
    assert {"commai.i18n.review", "commai.i18n.review_withdrawn"} <= acts


def test_language_picker_sets_the_ai_reply_languages(client):
    b = business(client)
    u = base(b)
    r = client.put(f"{u}/languages/ai", json={"languages": ["es", "fr"]}, headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    assert r.json()["languages"] == ["en", "es", "fr"]  # the business language always stays
    assert client.get(f"{u}/ai/profile", headers=b["agent"]["h"]).json()["languages"] == ["en", "es", "fr"]
    assert client.put(f"{u}/languages/ai", json={"languages": ["klingon"]}, headers=b["agent"]["h"]).status_code == 422
    r = client.put(f"{u}/languages/ai", json={"languages": ["es"]}, headers=b["internal"]["h"])
    assert r.status_code == 403


def test_widget_gets_its_words_only_in_a_switched_on_language(client, admin_headers):
    b = business(client, people=("agent",))
    r = client.post(
        f"{base(b)}/widget-keys",
        json={"name": "Site", "allowed_origins": ["https://www.example.tt"]},
        headers=b["agent"]["h"],
    )
    key = r.json()["public_key"]
    url = f"/api/v1/commai/i18n/widget/{key}"
    out = client.get(url, params={"lang": "es-TT,en"}).json()
    assert out["locale"] == "en-GB" and out["strings"]["send"] == "Send"
    switch_on(client, admin_headers, "es")
    out = client.get(url, params={"lang": "es-TT,en"}).json()
    assert out["locale"] == "es" and out["strings"]["send"] == i18n.catalogue("es")["widget.send"]
    assert out["machine_drafted"] is True
    assert client.get("/api/v1/commai/i18n/widget/wk_nope").status_code == 404
