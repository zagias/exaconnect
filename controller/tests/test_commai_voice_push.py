"""Jibsy Phone rings when closed (ADR 0045, voice/push.py): Web Push encryption and
signing, subscribing a device, the PBX's signed ring request, and the dial plan step."""

from __future__ import annotations

import base64
import json
import struct
import uuid

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from exaconnect_controller import db
from exaconnect_controller.commai.voice import freeswitch, pbx, push

from .commai_helpers import base
from .test_commai_voice import setup_voice
from .test_commai_voice_pbx import secret, tenant  # noqa: F401 - fixture

RING = "/api/v1/commai/internal/voice/ring"
FCM = "https://fcm.googleapis.com/fcm/send/"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@pytest.fixture
def vapid(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())  # made at run time, never a real key
    der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    monkeypatch.setenv(push.KEY_ENV, base64.b64encode(der).decode())
    return key


class Device:
    """A browser's side of a subscription: its key pair and auth secret."""

    def __init__(self, endpoint: str):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = uuid.uuid4().bytes
        self.endpoint = endpoint
        pub = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.sub = {"endpoint": endpoint, "keys": {"p256dh": b64u(pub), "auth": b64u(self.auth)}}

    def decrypt(self, body: bytes) -> dict:
        salt, rs, idlen = body[:16], *struct.unpack("!IB", body[16:21])
        as_public = body[21 : 21 + idlen]
        assert rs == 4096 and idlen == 65
        ua_public = unb64u(self.sub["keys"]["p256dh"])
        shared = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
        ikm = HKDF(hashes.SHA256(), 32, self.auth, b"WebPush: info\x00" + ua_public + as_public).derive(shared)
        cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
        nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
        plain = AESGCM(cek).decrypt(nonce, body[21 + idlen :], None)
        assert plain.endswith(b"\x02")
        return json.loads(plain[:-1])


class Sent:
    """Stands in for the push services: records each request and answers with `code`."""

    def __init__(self, monkeypatch, code=201):
        self.requests: list = []
        self.code = code

        class Resp:
            def __init__(s, status):
                s.status = status

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

        def urlopen(req, timeout=None):
            self.requests.append(req)
            if self.code >= 400:
                import urllib.error

                raise urllib.error.HTTPError(req.full_url, self.code, "gone", {}, None)
            return Resp(self.code)

        monkeypatch.setattr(push.urllib.request, "urlopen", urlopen)


def test_encrypted_for_the_device_and_signed_with_the_vapid_key(vapid, monkeypatch):
    dev = Device(FCM + "abc")
    sent = Sent(monkeypatch)
    assert push.send({"endpoint": dev.endpoint, **dev.sub["keys"]}, {"type": "test"}) == 201
    req = sent.requests[0]
    h = {k.lower(): v for k, v in req.header_items()}
    assert h["content-encoding"] == "aes128gcm" and h["ttl"] == "30" and h["urgency"] == "high"
    assert dev.decrypt(req.data) == {"type": "test"}
    # The VAPID token: for the push service's origin, signed with the server's key.
    token, k = h["authorization"].removeprefix("vapid t=").split(", k=")
    head, claims, sig = token.split(".")
    assert json.loads(unb64u(claims))["aud"] == "https://fcm.googleapis.com"
    pub = vapid.public_key()
    assert (
        k
        == push.public_key()
        == b64u(pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    )
    raw = unb64u(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if wrong


def test_only_the_browsers_push_services_are_contacted():
    for good in (
        FCM + "x",
        "https://updates.push.services.mozilla.com/wpush/v2/x",
        "https://web.push.apple.com/x",
        "https://wns2-par02p.notify.windows.com/w/?token=x",
        push.PUSH_CHECK + "dev/1",
    ):
        assert push.allowed(good), good
    for bad in (
        "http://fcm.googleapis.com/x",
        "https://fcm.googleapis.com.evil.example/x",
        "https://169.254.169.254/",
        "https://controller:8000/api",
        "https://evilfcm.googleapis.com.example/",
        "https://fcm.googleapis.com:8443/x",
        "http://push-check:8099.evil/",
    ):
        assert not push.allowed(bad), bad


def test_a_device_subscribes_for_its_own_extension_only(client, vapid, monkeypatch):
    b = setup_voice(client)
    u = base(b)
    st = client.get(f"{u}/voice/webphone/push", headers=b["ana"]["h"]).json()
    assert st == {"available": True, "public_key": push.public_key(), "this_device": False, "devices": 0}
    dev = Device(FCM + "ana-phone")
    bad = {**dev.sub, "endpoint": "https://attacker.example/collect"}
    assert client.post(f"{u}/voice/webphone/push", json=bad, headers=b["ana"]["h"]).status_code == 422
    junk = {**dev.sub, "keys": {"p256dh": "AAAA", "auth": "BBBB"}}
    assert client.post(f"{u}/voice/webphone/push", json=junk, headers=b["ana"]["h"]).status_code == 422
    assert client.post(f"{u}/voice/webphone/push", json=dev.sub, headers=b["ana"]["h"]).status_code == 200
    st = client.get(f"{u}/voice/webphone/push", params={"endpoint": dev.endpoint}, headers=b["ana"]["h"]).json()
    assert st["this_device"] and st["devices"] == 1
    # Nobody without an extension can subscribe; Ben doesn't see Ana's device.
    assert (
        client.post(f"{u}/voice/webphone/push", json=Device(FCM + "boss").sub, headers=b["boss"]["h"]).status_code
        == 404
    )
    assert client.get(f"{u}/voice/webphone/push", headers=b["ben"]["h"]).json()["devices"] == 0
    # The test notification reaches the device.
    sent = Sent(monkeypatch)
    r = client.post(f"{u}/voice/webphone/push/test", headers=b["ana"]["h"])
    assert r.status_code == 200 and r.json() == {"devices": 1, "reached": 1}
    assert dev.decrypt(sent.requests[0].data) == {"type": "test"}
    # Turning it off removes the device.
    client.request("DELETE", f"{u}/voice/webphone/push", json={"endpoint": dev.endpoint}, headers=b["ana"]["h"])
    assert client.get(f"{u}/voice/webphone/push", headers=b["ana"]["h"]).json()["devices"] == 0
    # With no key on the server, it says it's not available and refuses to subscribe.
    monkeypatch.delenv(push.KEY_ENV)
    assert client.get(f"{u}/voice/webphone/push", headers=b["ana"]["h"]).json()["available"] is False
    assert client.post(f"{u}/voice/webphone/push", json=dev.sub, headers=b["ana"]["h"]).status_code == 503


def ring(client, secret, b, ext="201", caller="202", name="Ben", call=None, sig=None):  # noqa: F811
    t = tenant(b)
    call = call or str(uuid.uuid4())
    headers = {} if sig is False else {pbx.HEADER: sig or pbx.sign(secret, t, ext, "", caller, call)}
    return client.get(
        RING, params={"tenant": t, "ext": ext, "caller": caller, "name": name, "call": call}, headers=headers
    )


def test_the_pbx_rings_a_closed_app(client, vapid, secret, monkeypatch):  # noqa: F811
    b = setup_voice(client, "Push Bank")
    u = base(b)
    # No device yet: nothing to wake, and the dial plan rings as before.
    assert ring(client, secret, b).text == "NONE"
    phone, laptop = Device(FCM + "ana-phone"), Device("https://web.push.apple.com/ana-mac")
    for d in (phone, laptop):
        assert client.post(f"{u}/voice/webphone/push", json=d.sub, headers=b["ana"]["h"]).status_code == 200
    # Only the PBX may ask.
    assert ring(client, secret, b, sig=False).status_code == 401
    assert ring(client, secret, b, sig="0" * 32).status_code == 401
    sent = Sent(monkeypatch)
    call = str(uuid.uuid4())
    r = ring(client, secret, b, call=call)
    assert r.status_code == 200 and r.text == "PUSHED 2"
    msgs = {
        req.full_url: d.decrypt(req.data)
        for req in sent.requests
        for d in (phone, laptop)
        if req.full_url == d.endpoint
    }
    assert msgs[phone.endpoint] == msgs[laptop.endpoint]
    m = msgs[phone.endpoint]
    assert m["type"] == "call" and m["call"] == call and m["from"] == "202" and m["name"] == "Ben" and m["ext"] == "201"
    # Another business's tenant can't ring Ana; an unknown extension rings nobody.
    other = setup_voice(client, "Other Push Bank")
    assert ring(client, secret, other).text == "NONE"
    assert ring(client, secret, b, ext="299").text == "NONE"
    # A device the push service says is gone is dropped.
    Sent(monkeypatch, code=410)
    assert ring(client, secret, b).text == "NONE"
    with db.tx() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM voice_push_subscriptions WHERE customer_id = %s", (b["id"],)
            ).fetchone()["n"]
            == 0
        )


def test_dial_plan_wakes_the_app_before_the_extension_rings(client, monkeypatch, secret):  # noqa: F811
    monkeypatch.setenv("EXA_PBX_CONTROLLER_URL", "http://controller:8000/")
    b = setup_voice(client, "Push Plan Bank")
    client.patch(f"{base(b)}/voice/me", json={"dnd": True}, headers=b["ben"]["h"])
    with db.tx() as conn:
        files = freeswitch.render(freeswitch.gather(conn, b["id"]))
    t = tenant(b)
    plan = files[f"{t}/dialplan.xml"]
    ana = plan.split('name="user-201"')[1].split("</extension>")[0]
    wake = f'<action application="lua" data="exa_ring.lua {t} 201 '
    assert wake in ana and ana.index(wake) < ana.index('application="bridge"')
    assert "http://controller:8000" in ana
    # Do not disturb goes straight to voicemail: nothing to wake.
    ben = plan.split('name="user-202"')[1].split("</extension>")[0]
    assert "exa_ring.lua" not in ben
