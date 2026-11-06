"""Real loopback HTTP at the LiveKit adapter; no SDK, provider or call setup."""

import base64
import hashlib
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from exaconnect_controller.commai.voice import livekit
from exaconnect_controller.commai.voice.common import VoiceError


@pytest.fixture
def control(monkeypatch):
    requests = []
    state = {"status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers["Authorization"], body))
            self.send_response(state["status"])
            if state["status"] == 302:
                self.send_header("Location", "/steal-token")
            self.end_headers()
            if state.get("not_found"):
                self.wfile.write(b'{"code":"not_found","msg":"participant missing"}')
                return
            self.wfile.write(
                json.dumps(
                    {"kind": "SIP", "attributes": {"sip.callID": "actual-call"}, "private": "PROVIDER-SECRET"}
                ).encode()
            )

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("The desktop sandbox forbids local TCP listeners; run this transport test in Linux CI")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for key in livekit.SETTINGS:
        monkeypatch.setenv(key, "controller-test-secret")
    monkeypatch.setenv("EXA_LIVEKIT_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        yield requests, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def claims(header):
    head, body, signature = header.removeprefix("Bearer ").split(".")
    decoded = base64.urlsafe_b64decode(signature + "==")
    assert hmac.compare_digest(
        decoded, hmac.new(b"controller-test-secret", f"{head}.{body}".encode(), hashlib.sha256).digest()
    )
    return json.loads(base64.urlsafe_b64decode(body + "=="))


@pytest.mark.parametrize(
    "url",
    [
        "https://user:private@livekit",
        "http://[invalid",
        "http://livekit:70000",
        "file:///private",
        "http://livekit?private=query",
        "http://livekit/unsupported-prefix",
        "http://livekit\n",
    ],
)
def test_invalid_media_server_root_refuses_before_transport(monkeypatch, url):
    for name in livekit.SETTINGS:
        monkeypatch.setenv(name, "fixture")
    monkeypatch.setenv("EXA_LIVEKIT_URL", url)
    assert not livekit.valid_url() and not livekit.phone_ready()
    with pytest.raises(VoiceError, match="URL is invalid") as error:
        livekit.sip_participant("room", "person", "call")
    assert error.value.code == 503 and "private" not in str(error.value)


def test_transfer_checks_actual_sip_call_and_uses_scoped_room_and_sip_grants(control):
    requests, _ = control
    assert livekit.transfer("real-room", "sip-person", "actual-call", "sip:internal-only@pbx") == {
        "state": "accepted",
        "human_answered": False,
    }
    assert len(requests) == 2
    path, header, body = requests[0]
    assert path == "/twirp/livekit.RoomService/GetParticipant"
    assert body == {"room": "real-room", "identity": "sip-person"}
    assert claims(header)["video"] == {"room": "real-room", "roomAdmin": True}
    path, header, body = requests[1]
    assert path == "/twirp/livekit.SIP/TransferSIPParticipant"
    assert claims(header)["sip"] == {"call": True}
    assert body["room_name"] == "real-room" and body["participant_identity"] == "sip-person"
    with pytest.raises(VoiceError, match="active SIP participant"):
        livekit.transfer("real-room", "sip-person", "forged-call", "sip:internal-only@pbx")
    assert len(requests) == 3  # No transfer RPC on a mismatched actual call.


@pytest.mark.parametrize("status", [302, 500])
def test_control_transport_refuses_redirects_and_redacts_remote_errors(control, status):
    requests, state = control
    state["status"] = status
    with pytest.raises(VoiceError) as error:
        livekit.sip_participant("real-room", "sip-person", "actual-call")
    assert "PROVIDER-SECRET" not in str(error.value)
    assert len(requests) == 1


def test_media_cleanup_requires_real_twirp_not_found_not_a_generic_proxy_404(control):
    _, state = control
    state["status"] = 404
    with pytest.raises(VoiceError) as error:
        livekit.hangup("real-room", "sip-person", "actual-call")
    assert error.value.code == 502
    state["not_found"] = True
    livekit.hangup("real-room", "sip-person", "actual-call")  # Confirmed gone; cleanup can complete.
