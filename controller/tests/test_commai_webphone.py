"""The browser phone: own-extension sign-in details and the Verto profile (ADR 0039)."""

import pathlib
import xml.etree.ElementTree as ET

from exaconnect_controller import db

from .commai_helpers import base
from .test_commai_voice import setup_voice

DEPLOY = pathlib.Path(__file__).resolve().parents[2] / "deploy"
VERTO = DEPLOY / "freeswitch/autoload_configs/verto.conf.xml"


def test_webphone_gives_only_your_own_extension(client, monkeypatch):
    b = setup_voice(client)
    u = base(b)
    monkeypatch.delenv("EXA_VERTO_URL", raising=False)
    off = client.get(f"{u}/voice/webphone", headers=b["ana"]["h"])
    assert off.status_code == 200 and off.json() == {
        "enabled": False,
        "extension": "201",
        "reason": "The browser phone is not switched on yet: ExaCarib sets its secure address first.",
    }
    monkeypatch.setenv("EXA_VERTO_URL", "ws://insecure.example:8082")  # plain ws is refused
    assert client.get(f"{u}/voice/webphone", headers=b["ana"]["h"]).json()["enabled"] is False
    monkeypatch.setenv("EXA_VERTO_URL", "wss://voice.example.org:8082")
    assert client.get(f"{u}/voice/webphone", headers=b["ana"]["h"]).json() == {"enabled": True, "extension": "201"}
    r = client.get(f"{u}/voice/webphone?sign_in=true", headers=b["ana"]["h"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    ana = r.json()
    assert ana["enabled"] and ana["login"].startswith("201@") and ana["url"] == "wss://voice.example.org:8082"
    with db.tx() as conn:
        pw = conn.execute(
            "SELECT sip_password FROM voice_users WHERE customer_id = %s AND extension = '201'", (b["id"],)
        ).fetchone()["sip_password"]
        assert conn.execute(
            "SELECT 1 FROM audit_log WHERE action = 'commai.voice.webphone_sign_in' AND customer_id = %s", (b["id"],)
        ).fetchone()
    assert ana["password"] == pw
    ben = client.get(f"{u}/voice/webphone?sign_in=true", headers=b["ben"]["h"]).json()
    assert ben["login"].startswith("202@") and ben["password"] != pw
    # The boss has no extension of their own: the screen says so (no error), and
    # there is nothing to sign in with.
    boss = client.get(f"{u}/voice/webphone", headers=b["boss"]["h"])
    assert boss.status_code == 200 and boss.json()["enabled"] is False and boss.json()["extension"] is None
    assert client.get(f"{u}/voice/webphone?sign_in=true", headers=b["boss"]["h"]).status_code == 404
    # The same for their own phone settings and the emergency notice.
    mine = client.get(f"{u}/voice/me", headers=b["boss"]["h"])
    assert mine.status_code == 200 and mine.json()["extension"] is None and mine.json()["message"]
    assert client.patch(f"{u}/voice/me", json={"dnd": True}, headers=b["boss"]["h"]).status_code == 404
    notice = client.get(f"{u}/voice/me/emergency-notice", headers=b["boss"]["h"])
    assert notice.status_code == 200 and notice.json() is None
    # Without the voice module there is no browser phone.
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO commai_entitlements (customer_id, module, enabled, updated_by)"
            " VALUES (%s, 'voice', false, 't')",
            (b["id"],),
        )
    assert client.get(f"{u}/voice/webphone", headers=b["ana"]["h"]).status_code == 403


def test_phone_app_contacts_are_names_and_extensions_only(client):
    b = setup_voice(client)
    u = base(b)
    r = client.get(f"{u}/voice/webphone/contacts", headers=b["ana"]["h"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    out = r.json()
    assert out["me"]["extension"] == "201"
    exts = [p["extension"] for p in out["people"]]
    assert "202" in exts and "201" not in exts
    assert all(set(p) == {"name", "extension"} for p in out["people"])
    assert all(set(x) == {"name", "extension", "kind"} for x in out["lines"])
    assert "sip_password" not in r.text and "@" not in r.text
    # No extension of your own: nothing to call from.
    assert client.get(f"{u}/voice/webphone/contacts", headers=b["boss"]["h"]).status_code == 404


def test_verto_signs_in_through_the_public_proxy_only():
    root = ET.parse(VERTO).getroot()
    params = {p.get("name"): p.get("value") for p in root.iter("param")}
    # Plain WebSocket on the compose network; TLS ends at the public proxy, which serves it as /verto.
    assert params["bind-local"].endswith(":8081") and "secure-bind-local" not in params
    assert params["userauth"] == "true" and params["blind-reg"] == "false"
    assert "BEGIN" not in VERTO.read_text()  # no key or certificate in the repo
    caddy = (DEPLOY / "public/Caddyfile").read_text()
    assert "handle /verto {\n\t\treverse_proxy freeswitch:8081" in caddy
    assert "microphone=(self)" in caddy
    compose = (DEPLOY / "docker-compose.yml").read_text()
    fs = compose[compose.index("\n  freeswitch:") : compose.index("\n  kamailio:")]
    assert "verto.conf.xml:/etc/freeswitch/autoload_configs/verto.conf.xml:ro" in fs
    assert "ports:" not in fs and "8081" not in fs.replace("port 8081", "")  # never published on the host
