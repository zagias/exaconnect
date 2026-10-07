"""The browser phone: own-extension sign-in details and the Verto profile (ADR 0039)."""

import pathlib
import xml.etree.ElementTree as ET

from exaconnect_controller import db

from .commai_helpers import base
from .test_commai_voice import setup_voice

VERTO = pathlib.Path(__file__).resolve().parents[2] / "deploy/freeswitch/autoload_configs/verto.conf.xml"


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
    # The boss has no extension of their own.
    assert client.get(f"{u}/voice/webphone", headers=b["boss"]["h"]).status_code == 404
    # Without the voice module there is no browser phone.
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO commai_entitlements (customer_id, module, enabled, updated_by)"
            " VALUES (%s, 'voice', false, 't')",
            (b["id"],),
        )
    assert client.get(f"{u}/voice/webphone", headers=b["ana"]["h"]).status_code == 403


def test_verto_profile_is_secure_websocket_with_directory_sign_in():
    root = ET.parse(VERTO).getroot()
    params = {p.get("name"): p.get("value") for p in root.iter("param")}
    assert params["secure-bind-local"].endswith(":8082") and "bind-local" not in params  # no plain ws listener
    assert params["userauth"] == "true" and params["blind-reg"] == "false"
    assert "BEGIN" not in VERTO.read_text()  # no key or certificate in the repo
