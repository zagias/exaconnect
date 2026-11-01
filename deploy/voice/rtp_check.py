"""Real PCMU RTP round-trip through the isolated production FreeSWITCH/Kamailio images.

Only rtp_check.sh invokes this. The synthetic originate/echo call stays on its
internal Docker network. This does not test PSTN, browser SRTP, kernel NAT,
customer authorisation/billing, capacity or preservation of calls across restart.
"""

from __future__ import annotations

import argparse
import ipaddress
import math
import os
import re
import socket
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path

from sbc_policy_check import headers


def command(*args: str, timeout: float = 10) -> str:
    result = subprocess.run(
        args, text=True, capture_output=True, check=False, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(
            f"{' '.join(args)}: {result.stderr.strip()} {result.stdout.strip()}"
        )
    return result.stdout.strip()


def eventually(check, description: str, timeout: float = 10):
    deadline = time.monotonic() + timeout
    error = ""
    while time.monotonic() < deadline:
        try:
            if result := check():
                return result
        except (RuntimeError, OSError) as exc:
            error = str(exc)
        time.sleep(0.1)
    raise AssertionError(f"{description} did not succeed within {timeout}s: {error}")


def isolated_vars(source: str) -> str:
    """A private-only fixture cannot infer its address through a default route."""
    if not source.startswith("<include>"):
        raise ValueError("Expected the production FreeSWITCH variables include")
    return source.replace(
        "<include>",
        "<include>\n  <!-- CI internal network has no default route: use its explicit interface. -->\n"
        '  <X-PRE-PROCESS cmd="exec-set" data="local_ip_v4=echo $EXA_VOICE_PUBLIC_IP"/>',
        1,
    )


def wait_for_call(endpoint, before: set[str], process, label: str, timeout: float = 10):
    """Do not hide the failed originate or a rejected SDP behind a generic timeout."""
    output = None

    def check():
        nonlocal output
        if endpoint.error:
            raise AssertionError(f"endpoint failed: {endpoint.error}")
        call = next(
            (call for key, call in endpoint.calls.copy().items() if key not in before),
            None,
        )
        if call:
            return call
        if process.poll() is not None:
            if output is None:
                output, _ = process.communicate(timeout=1)
            if process.returncode or "-ERR" in output:
                raise AssertionError(f"FreeSWITCH originate failed: {output.strip()}")
        return None

    try:
        return eventually(check, f"{label}: real SIP INVITE/SDP", timeout)
    except AssertionError as error:
        if output is None and process.poll() is not None:
            output, _ = process.communicate(timeout=1)
        raise AssertionError(
            f"{error}; endpoint received {endpoint.invites} INVITEs; "
            f"endpoint error={endpoint.error!r}; fs_cli={output!r}"
        ) from error


def encode_ulaw(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    value = min(abs(sample) + 132, 32635)
    exponent = max(0, value.bit_length() - 8)
    return ~(sign | exponent << 4 | ((value >> (exponent + 3)) & 15)) & 255


def decode_ulaw(value: int) -> int:
    value = ~value & 255
    magnitude = (((value & 15) << 3) + 132) << ((value >> 4) & 7)
    return 132 - magnitude if value & 128 else magnitude - 132


# 1 kHz is distinct from ordinary ringback. The received signal must contain
# this injected tone, not simply any non-zero RTP/audio packet.
TONE = bytes(
    encode_ulaw(int(10000 * math.sin(2 * math.pi * n / 8))) for n in range(160)
)


def carries_test_tone(payload: bytes) -> bool:
    if len(payload) < 80:
        return False
    samples = [decode_ulaw(value) for value in payload]
    energy = sum(value * value for value in samples)
    if energy < len(samples) * 10000:
        return False
    real = sum(value * math.cos(2 * math.pi * n / 8) for n, value in enumerate(samples))
    imag = sum(value * math.sin(2 * math.pi * n / 8) for n, value in enumerate(samples))
    return 2 * (real * real + imag * imag) / (len(samples) * energy) > 0.6


def media_address(sdp: str, expected: str) -> tuple[str, int]:
    address, port = "", 0
    for line in sdp.splitlines():
        if line.startswith("c=IN IP4 "):
            address = line.split()[2]
        elif line.startswith("m=audio "):
            port = int(line.split()[1])
    if address != expected or not 16384 <= port <= 16483:
        raise AssertionError(f"unexpected production media address: {address}:{port}")
    return address, port


class Call:
    def __init__(self, address: str, remote: tuple[str, int]):
        self.remote = remote
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((address, 0))
        self.socket.settimeout(0.02)
        self.port = self.socket.getsockname()[1]
        self.stop = threading.Event()
        self.ack = threading.Event()
        self.bye = threading.Event()
        self.sent = 0
        self.received: set[tuple[int, int]] = set()
        self.error = ""
        self.thread = threading.Thread(target=self.media, daemon=True)
        self.thread.start()

    def media(self):
        seq, timestamp, ssrc = 0, 0, 0xC1A0D10
        next_send = time.monotonic()
        try:
            while not self.stop.is_set():
                now = time.monotonic()
                if now >= next_send:
                    packet = struct.pack("!BBHII", 0x80, 0, seq, timestamp, ssrc) + TONE
                    self.socket.sendto(packet, self.remote)
                    self.sent += 1
                    seq, timestamp = (seq + 1) & 65535, (timestamp + 160) & 0xFFFFFFFF
                    next_send += 0.02
                try:
                    packet, peer = self.socket.recvfrom(65535)
                except TimeoutError:
                    continue
                if (
                    peer != self.remote
                    or len(packet) < 12
                    or packet[0] >> 6 != 2
                    or packet[1] & 127 != 0
                ):
                    continue
                # Reject header extensions/padding in this explicitly PCMU-only fixture.
                if packet[0] & 0x3F:
                    continue
                if carries_test_tone(packet[12:]):
                    self.received.add(
                        (
                            int.from_bytes(packet[2:4], "big"),
                            int.from_bytes(packet[8:12], "big"),
                        )
                    )
        except OSError as error:
            if not self.stop.is_set():
                self.error = str(error)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self.socket.close()


class Endpoint:
    def __init__(self, address: str, freeswitch: str):
        self.address, self.freeswitch = address, freeswitch
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((address, 0))
        self.socket.settimeout(0.2)
        self.port = self.socket.getsockname()[1]
        self.mode = "answer"
        self.calls: dict[str, Call] = {}
        self.stop = threading.Event()
        self.error = ""
        self.invites = 0
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def reply(self, request: str, peer: tuple[str, int], code: int, body: str = ""):
        h = headers(request)
        to = h["to"][0]
        if ";tag=" not in to:
            to += ";tag=ci-rtp"
        lines = [
            f"SIP/2.0 {code} CI",
            *[f"Via: {value}" for value in h["via"]],
            f"To: {to}",
        ]
        lines += [
            f"{name}: {h[name.lower()][0]}" for name in ("From", "Call-ID", "CSeq")
        ]
        lines += [f"Record-Route: {value}" for value in h.get("record-route", [])]
        if body:
            lines += [
                f"Contact: <sip:ci-rtp@{self.address}:{self.port}>",
                "Content-Type: application/sdp",
            ]
        lines += [f"Content-Length: {len(body.encode())}", "", body]
        self.socket.sendto("\r\n".join(lines).encode(), peer)

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    packet, peer = self.socket.recvfrom(65535)
                except TimeoutError:
                    continue
                request = packet.decode()
                h = headers(request)
                method = request.split()[0]
                call_id = h["call-id"][0]
                if method == "OPTIONS":
                    self.reply(request, peer, 200)
                elif method == "INVITE":
                    self.invites += 1
                    if self.mode == "unavailable":
                        self.reply(request, peer, 503)
                        continue
                    if call_id not in self.calls:
                        remote = media_address(
                            request.partition("\r\n\r\n")[2], self.freeswitch
                        )
                        self.calls[call_id] = Call(self.address, remote)
                    call = self.calls[call_id]
                    sdp = (
                        f"v=0\r\no=ci 1 1 IN IP4 {self.address}\r\ns=ci-rtp\r\n"
                        f"c=IN IP4 {self.address}\r\nt=0 0\r\nm=audio {call.port} RTP/AVP 0\r\n"
                        "a=rtpmap:0 PCMU/8000\r\na=ptime:20\r\na=sendrecv\r\n"
                    )
                    self.reply(request, peer, 200, sdp)
                elif method == "ACK" and call_id in self.calls:
                    self.calls[call_id].ack.set()
                elif method == "BYE":
                    self.reply(request, peer, 200)
                    if call_id in self.calls:
                        self.calls[call_id].bye.set()
                        self.calls[call_id].close()
                elif method == "CANCEL":
                    self.reply(request, peer, 200)
        except Exception as error:  # noqa: BLE001 - report thread errors to the main test
            if not self.stop.is_set():
                self.error = f"{type(error).__name__}: {error}"

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self.socket.close()
        for call in self.calls.values():
            call.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in (
        "kamailio",
        "freeswitch",
        "kamailio-ip",
        "freeswitch-ip",
        "endpoint-ip",
        "directory",
    ):
        parser.add_argument(f"--{field}", required=True)
    args = parser.parse_args()
    if os.environ.get("EXA_VOICE_RTP_TESTS") != "1":
        parser.error("only the explicit ephemeral RTP job may run this harness")
    for label, address in (
        ("Kamailio", args.kamailio_ip),
        ("FreeSWITCH", args.freeswitch_ip),
        ("local endpoint", args.endpoint_ip),
    ):
        try:
            private = ipaddress.IPv4Address(address).is_private
        except ValueError:
            parser.error(f"{label} has no valid isolated IPv4 address: {address!r}")
        if not private:
            parser.error(f"{label} must be on the private Docker network")

    def fs(text: str) -> str:
        return command("docker", "exec", args.freeswitch, "fs_cli", "-x", text)

    def rpc(*words: str) -> str:
        return command("docker", "exec", args.kamailio, "kamcmd", *words)

    def ready():
        eventually(lambda: rpc("core.uptime"), "Kamailio readiness", 30)
        eventually(lambda: fs("status").startswith("UP"), "FreeSWITCH readiness", 120)
        eventually(
            lambda: re.search(
                r"(?m)^\s*external\s+profile.*RUNNING", fs("sofia status")
            ),
            "external SIP profile",
            30,
        )
        gateway = fs("sofia status gateway exacarib_sip")
        assert re.search(r"(?m)^\s*Name\s+exacarib_sip\s*$", gateway), gateway
        assert fs("global_getvar local_ip_v4") == args.freeswitch_ip, (
            "FreeSWITCH must bind its isolated interface"
        )
        assert fs("global_getvar external_rtp_ip") == args.freeswitch_ip, (
            "RTP must advertise its isolated interface"
        )

    endpoint = Endpoint(args.endpoint_ip, args.freeswitch_ip)
    processes = []
    try:
        ready()
        directory = Path(args.directory)
        (directory / "address.list").write_text(
            f"2 {args.freeswitch_ip} 32 0 freeswitch\n1 {args.endpoint_ip} 32 0 ci-rtp\n"
        )
        (directory / "dispatcher.list").write_text(
            f"101 sip:{args.endpoint_ip}:{endpoint.port};transport=udp 0 100\n"
        )

        def reload_lists():
            rpc("permissions.addressReload")
            rpc("dispatcher.reload")

        reload_lists()

        def originate():
            call_uuid = str(uuid.uuid4())
            variables = f"origination_uuid={call_uuid},absolute_codec_string=PCMU,originate_timeout=5,sip_h_X-Exa-Route=101"
            process = subprocess.Popen(
                [
                    "docker",
                    "exec",
                    args.freeswitch,
                    "fs_cli",
                    "-x",
                    f"originate {{{variables}}}sofia/gateway/exacarib_sip/ci-rtp &echo()",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            processes.append(process)
            return call_uuid, process

        def media_check(label: str):
            before = set(endpoint.calls)
            call_uuid, process = originate()
            call = wait_for_call(endpoint, before, process, label)
            eventually(lambda: call.ack.is_set(), f"{label}: established SIP ACK")
            eventually(
                lambda: len(call.received) >= 100,
                f"{label}: 100 unique returned PCMU tone packets",
                8,
            )
            assert call.sent >= 100 and not call.error and not endpoint.error, (
                call.sent,
                call.error,
                endpoint.error,
            )
            assert fs(f"uuid_exists {call_uuid}") == "true", (
                f"{label}: no active FreeSWITCH channel"
            )
            fs(f"uuid_kill {call_uuid} NORMAL_CLEARING")
            eventually(lambda: call.bye.is_set(), f"{label}: SIP BYE reached endpoint")
            eventually(
                lambda: fs(f"uuid_exists {call_uuid}") == "false",
                f"{label}: channel cleanup",
            )
            result, _ = process.communicate(timeout=10)
            assert "+OK" in result, result
            print(
                f"PASS {label}: {call.sent} sent, {len(call.received)} unique echoed 1kHz PCMU packets; ACK/BYE and cleanup",
                flush=True,
            )

        media_check("baseline media")
        endpoint.mode = "unavailable"
        before = len(endpoint.calls)
        invites = endpoint.invites
        call_uuid, process = originate()
        result, _ = process.communicate(timeout=10)
        assert (
            endpoint.invites > invites
            and len(endpoint.calls) == before
            and "-ERR" in result
        ), result
        assert fs(f"uuid_exists {call_uuid}") == "false", (
            "unavailable endpoint created a live channel"
        )
        assert not endpoint.error, endpoint.error
        print(
            "PASS endpoint unavailable: real 503 refusal, no answered channel or media success",
            flush=True,
        )
        endpoint.mode = "answer"
        reload_lists()
        media_check("endpoint recovery")
        command("docker", "restart", args.kamailio, timeout=30)
        ready()
        reload_lists()
        media_check("fresh call after Kamailio restart")
        command("docker", "restart", args.freeswitch, timeout=30)
        ready()
        reload_lists()
        media_check("fresh call after FreeSWITCH restart")
    except Exception:
        for label, operation in (
            ("SIP profiles", lambda: fs("sofia status")),
            ("SIP gateway", lambda: fs("sofia status gateway exacarib_sip")),
            ("external profile", lambda: fs("sofia status profile external")),
            ("dispatcher", lambda: rpc("dispatcher.list")),
        ):
            try:
                print(f"-- failure diagnostic: {label}\n{operation()}", flush=True)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                print(
                    f"-- failure diagnostic unavailable: {label}: {error}", flush=True
                )
        raise
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
        endpoint.close()


if __name__ == "__main__":
    main()
