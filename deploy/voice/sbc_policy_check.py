#!/usr/bin/env python3
"""Exercise the real, isolated Kamailio container with local fake SIP carriers.

Only deploy/voice/check.sh calls this, with its temporary allow-list directory.
No provider credentials, public listeners, external calls or controller are used.
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path


def headers(message: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for line in message.split("\r\n")[1:]:
        key, separator, value = line.partition(":")
        if separator:
            result.setdefault(key.lower(), []).append(value.strip())
    return result


class Carrier:
    def __init__(self, index: int, address: str, attempts: dict[str, list[int]]):
        self.index, self.attempts = index, attempts
        self.code: int | None = 486
        self.messages: list[tuple[str, str]] = []
        self.pending: dict[str, tuple[str, tuple[str, int]]] = {}
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((address, 0))
        self.port = self.socket.getsockname()[1]
        threading.Thread(target=self.run, daemon=True).start()

    def reply(self, message: str, peer: tuple[str, int], code: int):
        h = headers(message)
        to = h["to"][0]
        if ";tag=" not in to:
            to += f";tag=carrier-{self.index}"
        lines = [f"SIP/2.0 {code} Test", *[f"Via: {v}" for v in h["via"]], f"To: {to}"]
        lines += [f"{name}: {h[name.lower()][0]}" for name in ("From", "Call-ID", "CSeq")]
        lines += [f"Record-Route: {value}" for value in h.get("record-route", [])]
        if code == 200 and "INVITE" in h["cseq"][0]:
            address, port = self.socket.getsockname()
            lines.append(f"Contact: <sip:callee@{address}:{port}>")
        self.socket.sendto(("\r\n".join(lines) + "\r\nContent-Length: 0\r\n\r\n").encode(), peer)

    def run(self):
        while True:
            try:
                packet, peer = self.socket.recvfrom(65535)
                message = packet.decode()
                method = message.split(" ", 1)[0]
                call = headers(message)["call-id"][0]
                self.messages.append((call, method))
                if method == "OPTIONS":
                    self.reply(message, peer, 200)
                elif method == "INVITE":
                    seen = self.attempts.setdefault(call, [])
                    if self.index not in seen:
                        seen.append(self.index)
                    if self.code is not None:
                        self.reply(message, peer, self.code)
                    else:
                        self.pending[call] = message, peer
                        # A ringing branch has acknowledged the INVITE. With
                        # Kamailio's RFC-conforming cancel_b_method=1, CANCEL
                        # waits for this provisional reply before forwarding.
                        self.reply(message, peer, 180)
                elif method == "CANCEL":
                    self.reply(message, peer, 200)
                    if call in self.pending:
                        self.reply(*self.pending.pop(call), 487)
                elif method == "BYE":
                    self.reply(message, peer, 200)
            except OSError:
                return


def main():
    proxy, address, directory, container = sys.argv[1:]
    attempts: dict[str, list[int]] = {}
    peers = [Carrier(i, address, attempts) for i in range(3)]
    caller = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    caller.bind((address, 0))
    caller.settimeout(4)
    port = caller.getsockname()[1]

    def rpc(*args):
        return subprocess.check_output(["docker", "exec", container, "kamcmd", *args], text=True)

    root = Path(directory)
    (root / "address.list").write_text(f"2 {address} 32 0 freeswitch\n")
    (root / "dispatcher.list").write_text(
        f"101 sip:{address}:{peers[0].port};transport=udp 0 100\n"
        f"101 sip:{address}:{peers[1].port};transport=udp 0 50\n"
        f"102 sip:{address}:{peers[2].port};transport=udp 0 100\n"
    )
    rpc("permissions.addressReload")
    rpc("dispatcher.reload")

    def request(method="INVITE", route="101,102", *, call=None, branch=None, tagged=False, to_tag="forged"):
        call, branch = call or uuid.uuid4().hex, branch or "z9hG4bK" + uuid.uuid4().hex
        target = f"sip:+18685550100@{proxy}:5060"
        if tagged:
            target = f"sip:forged@203.0.113.10:5060;alias={address}~{peers[0].port}~1"
        lines = [
            f"{method} {target} SIP/2.0",
            f"Via: SIP/2.0/UDP {address}:{port};rport;branch={branch}",
            "Max-Forwards: 70",
            f"From: <sip:caller@{address}>;tag={call}",
            f"To: <sip:+18685550100@{proxy}>" + (f";tag={to_tag}" if tagged else ""),
            f"Call-ID: {call}",
            f"CSeq: {2 if method == 'BYE' else 1} {method}",
            f"Contact: <sip:caller@{address}:{port}>",
        ]
        if route is not None:
            lines.append(f"X-Exa-Route: {route}")
        if tagged:
            lines.append("Route: <sip:203.0.113.10:5060;lr>")
        return call, branch, ("\r\n".join(lines) + "\r\nContent-Length: 0\r\n\r\n").encode()

    def final(call):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            packet, _ = caller.recvfrom(65535)
            message = packet.decode()
            if headers(message).get("call-id") != [call]:
                continue
            code = int(message.split()[1])
            if code >= 200 and "CANCEL" not in headers(message)["cseq"][0]:
                return code
        raise AssertionError("Kamailio did not return a final response")

    def send(**kwargs):
        call, _, packet = request(**kwargs)
        caller.sendto(packet, (proxy, 5060))
        return call, final(call)

    for route in (None, "101,evil"):
        call, code = send(route=route)
        assert code == 503 and attempts.get(call, []) == [], (route, code, attempts.get(call))
    print("ok   missing and malformed authorised routes fail closed")

    call, code = send(method="BYE", tagged=True)
    assert code == 481 and attempts.get(call, []) == [], (code, attempts.get(call))
    print("ok   forged dialog and Contact alias cannot relay a request")

    peers[0].code, peers[1].code = 503, 486
    call, code = send(route="101")
    assert code == 486 and attempts.get(call) == [0, 1], (code, attempts.get(call))
    print("ok   primary carrier address failure reaches its secondary address")

    peers[0].code = peers[1].code = 503
    call, code = send()
    assert code == 486 and attempts.get(call) == [0, 1, 2], (code, attempts.get(call))
    print("ok   exhausted primary carrier fails over to the next authorised carrier")

    peers[0].code = 486
    call, code = send()
    assert code == 486 and attempts.get(call) == [0], (code, attempts.get(call))
    print("ok   a busy subscriber does not trigger another carrier attempt")

    peers[0].code = 200
    call, code = send()
    assert code == 200 and attempts.get(call) == [0]
    _, _, ack = request(method="ACK", call=call, tagged=True, to_tag="carrier-0", route=None)
    caller.sendto(ack, (proxy, 5060))
    deadline = time.monotonic() + 2
    while (call, "ACK") not in peers[0].messages and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (call, "ACK") in peers[0].messages
    _, _, bye = request(method="BYE", call=call, tagged=True, to_tag="carrier-0", route=None)
    caller.sendto(bye, (proxy, 5060))
    assert final(call) == 200 and (call, "BYE") in peers[0].messages
    print("ok   a real established dialog still carries ACK, Contact alias and BYE")

    # The same INVITE must not consume several new-call slots when UDP retries.
    rpc("htable.seti", "invites", address, "119")
    rpc("htable.setex", "invites", address, "2")
    peers[0].code = None
    call, branch, packet = request()
    caller.sendto(packet, (proxy, 5060))
    deadline = time.monotonic() + 2
    while call not in attempts and time.monotonic() < deadline:
        time.sleep(0.02)
    assert attempts.get(call) == [0]
    for _ in range(3):
        caller.sendto(packet, (proxy, 5060))
        time.sleep(0.1)
    counter = rpc("htable.get", "invites", address)
    assert re.search(r"value:\s*120\b", counter), counter
    _, code = send()
    assert code == 503
    _, _, cancel = request(method="CANCEL", call=call, branch=branch)
    caller.sendto(cancel, (proxy, 5060))
    assert final(call) == 487 and attempts[call] == [0]
    print("ok   retransmissions count once; cancellation never fails over")
    peers[0].code = 486
    time.sleep(3)
    _, code = send()
    assert code == 486, code
    print("ok   call-cap window expires without needing a quiet minute")
    caller.close()
    for peer in peers:
        peer.socket.close()


if __name__ == "__main__":
    main()
