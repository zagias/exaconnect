"""Web Push for Jibsy Phone (ADR 0045): an incoming call rings a closed app.

A device that turns on "Ring when the app is closed" gives the controller a
Web Push subscription (the browser's push service address and two keys). When
a call comes in for an extension whose app is not connected, the dial plan
asks the controller (GET /api/v1/commai/internal/voice/ring, signed like the
outside-call check in pbx.py) and the controller sends each of that person's
devices a small encrypted message. The device's service worker shows a call
notification; a tap opens the app, which connects and answers while the
caller still hears ringing (deploy/freeswitch/scripts/exa_ring.lua).

The message is encrypted for the device (RFC 8291, aes128gcm) and signed with
ExaCarib's VAPID key (RFC 8292), so the push service can read neither. The
key is EXA_PUSH_VAPID_KEY (base64 PKCS#8 P-256), made on the server by
lab/scripts/init-env.sh and kept only in its .env. No paid service: Apple,
Google, Mozilla and Microsoft run the push services free of charge.

Only the browsers' own push services are ever contacted (ALLOWED_HOSTS), so a
subscription can't make the controller call an address of someone's choosing.
The one exception is PUSH_CHECK, a name that exists only on the compose
network while the live test runs (deploy/voice/uat/push.mjs).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .common import VoiceError

log = logging.getLogger(__name__)

KEY_ENV = "EXA_PUSH_VAPID_KEY"
SUBJECT = "https://www.exacarib.com"
ALLOWED_HOSTS = (
    "fcm.googleapis.com",  # Chrome, Edge on Android, Samsung Internet
    "updates.push.services.mozilla.com",  # Firefox
    "push.services.mozilla.com",
    "web.push.apple.com",  # Safari and Home Screen apps on iPhone, iPad and Mac
    "notify.windows.com",  # Edge on Windows (wns2-*.notify.windows.com)
)
PUSH_CHECK = "http://push-check:8099/"
RECORD_SIZE = 4096
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="push")


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _key() -> ec.EllipticCurvePrivateKey | None:
    raw = os.environ.get(KEY_ENV, "").strip()
    if not raw:
        return None
    try:
        key = serialization.load_der_private_key(base64.b64decode(raw), password=None)
    except ValueError:
        log.warning("%s is not a base64 PKCS#8 key; push is off", KEY_ENV)
        return None
    return key if isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1) else None


def public_key() -> str:
    """The VAPID public key the app subscribes with ("" while push is off)."""
    key = _key()
    if key is None:
        return ""
    return _b64u(
        key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )


def allowed(endpoint: str) -> bool:
    if endpoint.startswith(PUSH_CHECK):
        return True
    u = urllib.parse.urlparse(endpoint)
    host = (u.hostname or "").lower()
    return (
        u.scheme == "https"
        and u.port in (None, 443)
        and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)
    )


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """RFC 8291 message encryption, aes128gcm content coding (RFC 8188), one record."""
    ua_public = _unb64u(p256dh)
    auth_secret = _unb64u(auth)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_private = ec.generate_private_key(ec.SECP256R1())
    as_public = as_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    shared = as_private.exchange(ec.ECDH(), ua_key)
    ikm = HKDF(hashes.SHA256(), 32, auth_secret, b"WebPush: info\x00" + ua_public + as_public).derive(shared)
    salt = os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    if len(payload) > RECORD_SIZE - 17 - 86:
        raise VoiceError("The push message is too long.", 422)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public + body


def _vapid(endpoint: str, key: ec.EllipticCurvePrivateKey) -> str:
    u = urllib.parse.urlparse(endpoint)
    header = _b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = _b64u(
        json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": SUBJECT}).encode()
    )
    r, s = decode_dss_signature(key.sign(f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return f"vapid t={header}.{claims}.{_b64u(sig)}, k={_b64u(pub)}"


def send(sub: dict, message: dict, ttl: int = 30, urgency: str = "high") -> int:
    """Send one message to one device. The push service's HTTP status (0: not reached)."""
    key = _key()
    if key is None or not allowed(sub["endpoint"]):
        return 0
    body = encrypt(json.dumps(message, separators=(",", ":")).encode(), sub["p256dh"], sub["auth"])
    req = urllib.request.Request(
        sub["endpoint"],
        data=body,
        method="POST",
        headers={
            "Authorization": _vapid(sub["endpoint"], key),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(ttl),
            "Urgency": urgency,
            "Topic": "call" if message.get("type") == "call" else "jibsy",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - endpoint checked by allowed()
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError) as e:
        log.info("push not delivered: %s", e)
        return 0


def subscriptions(conn: psycopg.Connection, voice_user_id: Any) -> list[dict]:
    return conn.execute(
        "SELECT id, endpoint, p256dh, auth FROM voice_push_subscriptions WHERE voice_user_id = %s",
        (voice_user_id,),
    ).fetchall()


def send_all(conn: psycopg.Connection, subs: list[dict], message: dict, ttl: int = 30) -> int:
    """Send to every device at once; drop the ones the push service says are gone. Devices reached."""
    if not subs:
        return 0
    codes = list(_pool.map(lambda s: send(s, message, ttl), subs))
    reached = 0
    for s, code in zip(subs, codes, strict=True):
        if code in (404, 410):  # unsubscribed or expired: the browser makes a new one next time
            conn.execute("DELETE FROM voice_push_subscriptions WHERE id = %s", (s["id"],))
        elif 200 <= code < 300:
            reached += 1
            conn.execute(
                "UPDATE voice_push_subscriptions SET last_sent_at = now(), last_error = '' WHERE id = %s", (s["id"],)
            )
        else:
            conn.execute(
                "UPDATE voice_push_subscriptions SET last_error = %s WHERE id = %s",
                (f"push service answered {code}" if code else "push service not reached", s["id"]),
            )
    return reached


def subscribe(conn: psycopg.Connection, cid: Any, voice_user: dict, user_id: Any, body: dict, device: str = "") -> dict:
    endpoint = str(body.get("endpoint") or "").strip()
    keys = body.get("keys") or {}
    p256dh, auth = str(keys.get("p256dh") or ""), str(keys.get("auth") or "")
    if not endpoint or len(endpoint) > 1000 or not allowed(endpoint):
        raise VoiceError("This browser's push service isn't one Jibsy Phone can use.", 422)
    try:
        if len(_unb64u(p256dh)) != 65 or len(_unb64u(auth)) != 16:
            raise ValueError
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _unb64u(p256dh))
    except ValueError as e:
        raise VoiceError("The device's push keys are not valid.", 422) from e
    conn.execute(
        """INSERT INTO voice_push_subscriptions (customer_id, voice_user_id, user_id, endpoint, p256dh, auth, device)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (endpoint) DO UPDATE SET customer_id = EXCLUDED.customer_id,
             voice_user_id = EXCLUDED.voice_user_id, user_id = EXCLUDED.user_id, p256dh = EXCLUDED.p256dh,
             auth = EXCLUDED.auth, device = EXCLUDED.device, last_error = ''""",
        (cid, voice_user["id"], user_id, endpoint, p256dh, auth, device[:120]),
    )
    return {"subscribed": True}


def unsubscribe(conn: psycopg.Connection, user_id: Any, endpoint: str) -> None:
    conn.execute("DELETE FROM voice_push_subscriptions WHERE user_id = %s AND endpoint = %s", (user_id, endpoint))


def ring(conn: psycopg.Connection, cid: Any, ext: str, caller: str, caller_name: str, call: str) -> str:
    """A call is ringing extension `ext` whose app isn't connected: "PUSHED <n>" or "NONE"."""
    vu = conn.execute(
        "SELECT id FROM voice_users WHERE customer_id = %s AND extension = %s AND status = 'active'", (cid, ext)
    ).fetchone()
    if vu is None:
        return "NONE"
    subs = subscriptions(conn, vu["id"])
    if not subs or _key() is None:
        return "NONE"
    who = caller_name if caller_name and caller_name != caller else ""
    message = {"type": "call", "call": call, "from": caller, "name": who, "ext": ext, "at": int(time.time())}
    n = send_all(conn, subs, message, ttl=30)
    return f"PUSHED {n}" if n else "NONE"
