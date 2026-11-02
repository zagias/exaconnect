"""The SBC fault fixture must model a cancellable SIP ringing branch."""

from __future__ import annotations

import importlib.util
from collections import deque
from pathlib import Path
from types import SimpleNamespace


def test_held_carrier_rings_before_accepting_cancel(monkeypatch):
    source = Path(__file__).resolve().parents[2] / "deploy/voice/sbc_policy_check.py"
    spec = importlib.util.spec_from_file_location("sbc_policy_check", source)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)

    def request(method):
        return (
            f"{method} sip:callee@example.invalid SIP/2.0\r\n"
            "Via: SIP/2.0/UDP 127.0.0.1:40000;branch=z9hG4bKtest\r\n"
            "From: <sip:caller@example.invalid>;tag=caller\r\n"
            "To: <sip:callee@example.invalid>\r\n"
            "Call-ID: held-carrier-test\r\n"
            f"CSeq: 1 {method}\r\nContent-Length: 0\r\n\r\n"
        ).encode()

    class DatagramSocket:
        """Drive the exact packet handler without requiring a local listener."""

        def __init__(self):
            self.incoming = deque([request("INVITE"), request("CANCEL")])
            self.outgoing = []

        def bind(self, _address):
            pass

        def getsockname(self):
            return "127.0.0.1", 40001

        def recvfrom(self, _limit):
            if not self.incoming:
                raise OSError("fixture finished")
            return self.incoming.popleft(), ("127.0.0.1", 40000)

        def sendto(self, packet, peer):
            assert peer == ("127.0.0.1", 40000)
            message = packet.decode()
            self.outgoing.append((int(message.split()[1]), fixture.headers(message)["cseq"][0]))

    datagrams = DatagramSocket()
    monkeypatch.setattr(fixture.socket, "socket", lambda *_: datagrams)
    monkeypatch.setattr(fixture.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
    carrier = fixture.Carrier(0, "127.0.0.1", {})
    carrier.code = None
    carrier.run()
    assert datagrams.outgoing == [(180, "1 INVITE"), (200, "1 CANCEL"), (487, "1 INVITE")]
