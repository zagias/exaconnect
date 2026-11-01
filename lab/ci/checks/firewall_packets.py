"""Opt-in Linux packet regressions. Every interface, route and sysctl is namespaced.

Run only on an ephemeral CI runner, as root, with EXA_FIREWALL_PACKET_TESTS=1.
Rules are exported from the Go production renderer, never reimplemented here.
No physical interface or host routing/firewall/sysctl is changed.
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import uuid
from pathlib import Path


def run(*args: str, timeout: float = 10, **kwargs) -> str:
    result = subprocess.run(
        args, text=True, capture_output=True, timeout=timeout, check=False, **kwargs
    )
    if result.returncode:
        raise RuntimeError(
            f"{' '.join(args)}: {result.stderr.strip()} {result.stdout.strip()}"
        )
    return result.stdout.strip()


def read_json(process: subprocess.Popen, timeout: float = 5) -> dict:
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout):
            raise RuntimeError("packet endpoint did not answer before its deadline")
    line = process.stdout.readline()
    if not line:
        raise RuntimeError(f"packet endpoint exited unexpectedly ({process.poll()})")
    return json.loads(line)


class Session:
    """One actual TCP connection, kept open while the firewall changes."""

    def __init__(
        self, lab: PacketLab, namespace: str, host: str, port: int, source: str = ""
    ):
        self.process = lab.spawn(
            namespace, "client", "--host", host, "--port", str(port), "--source", source
        )
        ready = read_json(self.process)
        if not ready.get("ok"):
            self.close()
            raise RuntimeError(f"TCP connection to {host}:{port} failed: {ready}")

    def exchange(self, payload: str) -> dict:
        self.process.stdin.write(json.dumps({"payload": payload}) + "\n")
        self.process.stdin.flush()
        return read_json(self.process)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=3)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class PacketLab:
    """Disposable router, two sites and an outside peer; no host veth endpoint."""

    def __init__(self):
        prefix = f"exa-fw-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.names = {
            name: f"{prefix}-{name}"
            for name in ("router", "site_a", "site_b", "outside")
        }
        self.created: list[str] = []
        self.processes: list[subprocess.Popen] = []

    def ns(self, namespace: str, *args: str, **kwargs) -> str:
        return run("ip", "netns", "exec", self.names[namespace], *args, **kwargs)

    def ip(self, namespace: str, *args: str) -> str:
        return run("ip", "-n", self.names[namespace], *args)

    def spawn(self, namespace: str, *args: str) -> subprocess.Popen:
        process = subprocess.Popen(
            [
                "ip",
                "netns",
                "exec",
                self.names[namespace],
                sys.executable,
                str(Path(__file__).resolve()),
                *args,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.processes.append(process)
        return process

    def server(self, namespace: str, host: str, port: int):
        ready = read_json(
            self.spawn(namespace, "server", "--host", host, "--port", str(port))
        )
        assert ready.get("ok"), ready

    def probe(self, namespace: str, host: str, port: int, source: str = "") -> dict:
        try:
            with Session(self, namespace, host, port, source) as session:
                return session.exchange(uuid.uuid4().hex)
        except RuntimeError as error:
            return {"ok": False, "error": str(error)}

    def allowed(self, namespace: str, host: str, port: int, source: str = "") -> dict:
        response = self.probe(namespace, host, port, source)
        assert response.get("ok"), (namespace, host, port, source, response)
        return response

    def blocked(self, namespace: str, host: str, port: int, source: str = ""):
        response = self.probe(namespace, host, port, source)
        assert not response.get("ok"), (
            "forbidden traffic was forwarded",
            namespace,
            host,
            port,
            source,
            response,
        )

    def apply(self, path: Path):
        self.ns("router", "nft", "--file", str(path.resolve()))

    def counter(self, comment: str, family: str = "ip", table: str = "exa_inet") -> int:
        document = json.loads(
            self.ns("router", "nft", "--json", "list", "table", family, table)
        )
        return sum(
            expression["counter"]["packets"]
            for item in document["nftables"]
            if (rule := item.get("rule", {})).get("comment") == comment
            for expression in rule.get("expr", [])
            if isinstance(expression.get("counter"), dict)
        )

    def setup(self):
        for name in self.names.values():
            run("ip", "netns", "add", name)
            self.created.append(name)
        for name in self.names:
            self.ip(name, "link", "set", "lo", "up")
        for peer, interface, v4, v6 in (
            ("site_a", "lan-a", "10.70.1", "fd70:1"),
            ("site_b", "lan-b", "10.70.2", "fd70:2"),
            ("outside", "eth9", "192.0.2", "fd70:9"),
        ):
            # Both ends are born inside the disposable namespaces.
            self.ip(
                "router",
                "link",
                "add",
                interface,
                "type",
                "veth",
                "peer",
                "name",
                "peer0",
                "netns",
                self.names[peer],
            )
            self.ip("router", "address", "add", f"{v4}.1/24", "dev", interface)
            self.ip(
                "router",
                "-6",
                "address",
                "add",
                f"{v6}::1/64",
                "dev",
                interface,
                "nodad",
            )
            self.ip(peer, "address", "add", f"{v4}.2/24", "dev", "peer0")
            self.ip(
                peer, "-6", "address", "add", f"{v6}::2/64", "dev", "peer0", "nodad"
            )
            self.ip("router", "link", "set", interface, "up")
            self.ip(peer, "link", "set", "peer0", "up")
            self.ip(peer, "route", "add", "default", "via", f"{v4}.1")
            self.ip(peer, "-6", "route", "add", "default", "via", f"{v6}::1")
        self.ip("outside", "address", "add", "192.0.2.3/24", "dev", "peer0")
        self.ip("outside", "address", "add", "198.51.100.10/32", "dev", "lo")
        self.ip(
            "outside", "-6", "address", "add", "fd70:100::10/128", "dev", "lo", "nodad"
        )
        self.ip("router", "route", "add", "default", "via", "192.0.2.2")
        self.ip("router", "-6", "route", "add", "default", "via", "fd70:9::2")
        self.ns(
            "router",
            "sysctl",
            "-qw",
            "net.ipv4.ip_forward=1",
            "net.ipv6.conf.all.forwarding=1",
        )

    def cached_pre_off_steering(self):
        """A real old mark rule wins over the unreachable Internet table."""
        self.ip(
            "router",
            "route",
            "add",
            "table",
            "101",
            "default",
            "via",
            "192.0.2.2",
            "dev",
            "eth9",
        )
        self.ip(
            "router", "route", "add", "table", "101", "10.70.2.0/24", "dev", "lan-b"
        )
        self.ip(
            "router", "rule", "add", "pref", "1000", "fwmark", "0x101", "lookup", "101"
        )
        self.ip("router", "route", "add", "table", "251", "unreachable", "default")
        self.ip(
            "router",
            "rule",
            "add",
            "pref",
            "31000",
            "from",
            "10.70.1.0/24",
            "lookup",
            "main",
            "suppress_prefixlength",
            "0",
        )
        self.ip(
            "router",
            "rule",
            "add",
            "pref",
            "31001",
            "from",
            "10.70.1.0/24",
            "lookup",
            "251",
        )
        self.ns(
            "router",
            "nft",
            "--file",
            "-",
            input="""table inet exa_ci_cached {
 chain mark {
  type filter hook prerouting priority mangle; policy accept;
  ip saddr 10.70.1.0/24 meta mark set 0x101
 }
}
""",
        )

    def close(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        failures = []
        for name in reversed(self.created):
            result = subprocess.run(
                ["ip", "netns", "delete", name],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            if result.returncode:
                failures.append(f"{name}: {result.stderr.strip()}")
        self.created.clear()
        if failures:
            raise RuntimeError("namespace cleanup failed: " + "; ".join(failures))

    def __enter__(self):
        try:
            self.setup()
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()


def packets(fixtures: Path):
    for name in ("gateway.nft", "first-deny.nft", "guard.nft", "off.nft"):
        if not (fixtures / name).is_file():
            raise RuntimeError(f"production renderer fixture missing: {name}")
    with PacketLab() as lab:
        for port in (8000, 8001, 8002):
            lab.server("outside", "198.51.100.10", port)
        lab.server("outside", "fd70:100::10", 8000)
        lab.server("site_a", "10.70.1.2", 9000)
        lab.server("site_b", "10.70.2.2", 9000)
        lab.server("router", "192.0.2.1", 9443)
        lab.server("router", "fd70:9::1", 9443)

        # Positive controls prevent a broken route/server from passing a deny test.
        lab.allowed("site_a", "198.51.100.10", 8001)
        lab.allowed("site_a", "fd70:100::10", 8000)
        lab.allowed("outside", "10.70.1.2", 9000)
        print("PASS baseline IPv4/IPv6 forwarding and inbound reachability", flush=True)

        lab.apply(fixtures / "gateway.nft")
        lab.apply(fixtures / "guard.nft")
        lab.blocked("site_a", "198.51.100.10", 8001)
        assert lab.counter("fw11") > 0, "site deny was not exercised"
        lab.allowed("site_b", "198.51.100.10", 8001)
        response = lab.allowed("site_a", "198.51.100.10", 8000)
        assert response["peer"] == "192.0.2.1", ("outbound NAT missing", response)
        lab.blocked("outside", "10.70.1.2", 9000)
        lab.allowed("site_a", "198.51.100.10", 8002)
        lab.apply(fixtures / "first-deny.nft")
        lab.blocked("site_a", "198.51.100.10", 8002)
        assert lab.counter("fw13") > 0, "first-match deny was not exercised"
        rejected = False
        try:
            lab.ns(
                "router",
                "nft",
                "--file",
                "-",
                input=(fixtures / "gateway.nft").read_text()
                + "\nadd rule ip exa_inet filter_fwd invalid-ci-expression\n",
            )
        except RuntimeError:
            rejected = True
        assert rejected, "intentionally invalid nft transaction was accepted"
        lab.blocked("site_a", "198.51.100.10", 8002)
        lab.allowed("site_a", "198.51.100.10", 8000)
        lab.apply(fixtures / "gateway.nft")
        lab.allowed("site_a", "198.51.100.10", 8002)
        print(
            "PASS site isolation, NAT/return traffic, inbound deny, ordered rules and atomic rejection",
            flush=True,
        )

        lab.allowed("outside", "192.0.2.1", 8080, "192.0.2.2")
        lab.allowed("outside", "192.0.2.1", 8081, "192.0.2.3")
        lab.blocked("outside", "192.0.2.1", 8081, "192.0.2.2")
        lab.blocked("outside", "192.0.2.1", 8080, "192.0.2.3")
        assert lab.counter("inbound") > 0, (
            "unauthorised port forwards did not reach the inbound deny"
        )
        print("PASS separate public-forward allowlists to the same backend", flush=True)

        lab.blocked("site_a", "fd70:100::10", 8000)
        assert lab.counter("IPv6 forwarding unsupported", "inet", "exa_inet_guard") > 0
        lab.allowed("outside", "fd70:9::1", 9443)
        lab.allowed("router", "fd70:100::10", 8000)
        print(
            "PASS IPv6 forwarding denied while host input/output remains reachable",
            flush=True,
        )

        lab.cached_pre_off_steering()
        # This succeeds even with table 251 unreachable: the old mark rule wins.
        lab.allowed("site_a", "198.51.100.10", 8000)
        with (
            Session(lab, "site_a", "198.51.100.10", 8000) as outbound,
            Session(lab, "outside", "192.0.2.1", 8080, "192.0.2.2") as inbound,
        ):
            assert outbound.exchange("outbound-before-off")["ok"]
            assert inbound.exchange("inbound-before-off")["ok"]
            connections = lab.ns("router", "conntrack", "--list", "--proto", "tcp")
            assert (
                "ESTABLISHED" in connections
                and "dport=8000" in connections
                and "dport=8080" in connections
            )
            lab.apply(fixtures / "off.nft")
            assert not outbound.exchange("outbound-after-off")["ok"], (
                "established outbound bypassed Internet Off"
            )
            assert not inbound.exchange("inbound-after-off")["ok"], (
                "established inbound bypassed Internet Off"
            )
            lab.blocked("site_a", "198.51.100.10", 8000)
            lab.blocked("outside", "192.0.2.1", 8080, "192.0.2.2")
            assert lab.counter("Internet off", "inet", "exa_inet_guard") > 0
            lab.allowed("site_a", "10.70.2.2", 9000)
            lab.allowed("site_b", "198.51.100.10", 8000)
            lab.allowed("outside", "192.0.2.1", 9443)
            lab.allowed("router", "198.51.100.10", 8000)
        print(
            "PASS Internet Off defeats cached steering and established flows; corporate, other-site and host traffic survives",
            flush=True,
        )


def endpoint_server(host: str, port: int):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            while line := self.rfile.readline():
                response = {
                    "payload": line.decode().strip(),
                    "peer": self.client_address[0],
                }
                self.wfile.write((json.dumps(response) + "\n").encode())
                self.wfile.flush()

    class Server(socketserver.ThreadingTCPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        allow_reuse_address = True
        daemon_threads = True

    with Server((host, port), Handler) as server:
        print(json.dumps({"ok": True}), flush=True)
        server.serve_forever(poll_interval=0.1)


def endpoint_client(host: str, port: int, source: str):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.8)
        try:
            if source:
                connection.bind((source, 0))
            connection.connect((host, port))
        except OSError as error:
            print(json.dumps({"ok": False, "error": str(error)}), flush=True)
            return
        print(json.dumps({"ok": True}), flush=True)
        for line in sys.stdin:
            payload = json.loads(line)["payload"]
            try:
                connection.sendall((payload + "\n").encode())
                buffer = b""
                while not buffer.endswith(b"\n"):
                    part = connection.recv(65536)
                    if not part:
                        raise OSError("server closed the connection")
                    buffer += part
                response = json.loads(buffer)
                if response["payload"] != payload:
                    raise OSError("reply did not match this packet probe")
                response["ok"] = True
            except OSError as error:
                response = {"ok": False, "error": str(error)}
            print(json.dumps(response), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "server", "client"))
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--source", default="")
    args = parser.parse_args()
    if args.mode == "server":
        endpoint_server(args.host, args.port)
    elif args.mode == "client":
        endpoint_client(args.host, args.port, args.source)
    else:
        if (
            os.environ.get("EXA_FIREWALL_PACKET_TESTS") != "1"
            or sys.platform != "linux"
            or os.geteuid() != 0
        ):
            parser.error(
                "requires explicit EXA_FIREWALL_PACKET_TESTS=1, Linux and root on an ephemeral CI runner"
            )
        for tool in ("ip", "nft", "sysctl", "conntrack"):
            if not shutil.which(tool):
                parser.error(f"required tool missing: {tool}")
        if args.fixtures is None:
            parser.error("--fixtures is required")

        def stop(signum, _frame):
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, stop)
        packets(args.fixtures.resolve())


if __name__ == "__main__":
    main()
