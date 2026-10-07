"""Shared helpers for the integration tests: local fake HTTP, UDP, TCP and
TLS servers (nothing ever leaves the machine), organisations and carriers."""

from __future__ import annotations

import datetime as dt
import json
import socket
import ssl
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from exaconnect_controller import db
from exaconnect_controller.commai import jobs
from exaconnect_controller.security import hash_password

PASSWORD = "integration test password"


class Fake:
    """A local HTTP server that records requests and answers from a script.

    `reply(method, path_prefix, status, body)` sets an answer; `queue` makes
    the next answers for a prefix different (for retry tests)."""

    def __init__(self):
        self.requests: list[dict] = []
        self.routes: dict[tuple[str, str], tuple[int, object]] = {}
        self.queued: dict[tuple[str, str], list[tuple[int, object]]] = {}
        self.handler: Callable[[dict], tuple[int, object] | None] | None = None
        fake = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                req = {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "raw": raw,
                }
                try:
                    req["json"] = json.loads(raw) if raw else None
                except ValueError:
                    req["json"] = None
                fake.requests.append(req)
                status, body = fake.answer(req)
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _do  # noqa: N815

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def answer(self, req: dict) -> tuple[int, object]:
        for (m, prefix), q in self.queued.items():
            if req["method"] == m and req["path"].startswith(prefix) and q:
                return q.pop(0)
        if self.handler:
            out = self.handler(req)
            if out is not None:
                return out
        best = None
        for (m, prefix), ans in self.routes.items():
            if req["method"] == m and req["path"].startswith(prefix) and (best is None or len(prefix) > len(best[0])):
                best = (prefix, ans)
        return best[1] if best else (200, {})

    def reply(self, method: str, prefix: str, status: int, body: object = None) -> None:
        self.routes[(method, prefix)] = (status, {} if body is None else body)

    def queue(self, method: str, prefix: str, *answers: tuple[int, object]) -> None:
        self.queued.setdefault((method, prefix), []).extend(answers)

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.close()


@pytest.fixture
def live(monkeypatch):
    """Integrations send for real, to local addresses only, with secure storage set up."""
    monkeypatch.setenv("EXA_INTEGRATIONS_LIVE", "1")
    monkeypatch.setenv("EXA_WEBHOOK_ALLOW_PRIVATE", "1")
    monkeypatch.setenv("EXA_SECRETS_KEY", Fernet.generate_key().decode())


@pytest.fixture
def vault(monkeypatch):
    """Secure storage only: integrations stay simulated."""
    monkeypatch.delenv("EXA_INTEGRATIONS_LIVE", raising=False)
    monkeypatch.setenv("EXA_SECRETS_KEY", Fernet.generate_key().decode())


class Udp:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]

    def recv(self) -> bytes:
        return self.sock.recvfrom(65535)[0]

    def close(self):
        self.sock.close()


class Tcp:
    """Accepts connections and keeps what each one sent (optionally over TLS)."""

    def __init__(self, tls: ssl.SSLContext | None = None):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.received: list[bytes] = []
        self.errors: list[Exception] = []
        self.done = threading.Event()
        self.tls = tls
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            data = b""
            try:
                if self.tls:
                    c = self.tls.wrap_socket(c, server_side=True)
                c.settimeout(5)
                while True:
                    chunk = c.recv(65535)
                    if not chunk:
                        break
                    data += chunk
            except (OSError, ssl.SSLError) as e:
                self.errors.append(e)
            finally:
                if data:
                    self.received.append(data)
                c.close()
                self.done.set()

    def close(self):
        self.sock.close()


def self_signed(tmp_path) -> tuple[ssl.SSLContext, str]:
    """A server TLS context for 127.0.0.1 and the PEM a client trusts."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    import ipaddress

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1))
        .not_valid_after(dt.datetime.now(dt.UTC) + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    cf, kf = tmp_path / "c.pem", tmp_path / "k.pem"
    cf.write_text(pem)
    kf.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cf, kf)
    return ctx, pem


def login(client, email: str) -> dict:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def user(client, role: str, *, customer_id=None, carrier_id=None, email: str | None = None) -> dict:
    email = email or f"{role}-{customer_id or carrier_id}@example.org"
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users (email, password_hash, role, customer_id, carrier_id) VALUES (%s, %s, %s, %s, %s)",
            (email, hash_password(PASSWORD), role, customer_id, carrier_id),
        )
    return login(client, email)


def lab(client) -> dict:
    """The lab inventory (two sites, a PoP, Carrier A and B, satellite) with users for each role."""
    from exaconnect_controller.seed import seed_lab

    with db.tx() as conn:
        s = seed_lab(conn)
        carriers = {r["name"]: str(r["id"]) for r in conn.execute("SELECT id, name FROM carriers")}
        links = conn.execute(
            """SELECT l.id::text AS id, s.name AS site, l.path, c.name AS carrier, l.site_id::text AS site_id
               FROM links l JOIN sites s ON s.id = l.site_id JOIN carriers c ON c.id = l.carrier_id"""
        ).fetchall()
        sites = {r["name"]: str(r["id"]) for r in conn.execute("SELECT id, name FROM sites")}
    cid = str(s["customer_id"])
    return {
        **s,
        "customer_id": cid,
        "carriers": carriers,
        "links": links,
        "sites": sites,
        "customer": user(client, "customer", customer_id=cid),
        "carrier_a": user(client, "carrier", carrier_id=carriers["Carrier A"], email="noc@carrier-a.example"),
        "carrier_b": user(client, "carrier", carrier_id=carriers["Carrier B"], email="noc@carrier-b.example"),
    }


def link(lab_: dict, site: str, path: str) -> str:
    return next(lk["id"] for lk in lab_["links"] if lk["site"] == site and lk["path"] == path)


def other_org(client, name: str = "Other Bank") -> dict:
    with db.tx() as conn:
        cid = str(conn.execute("INSERT INTO customers (name) VALUES (%s) RETURNING id", (name,)).fetchone()["id"])
    return {
        "id": cid,
        "h": user(client, "customer", customer_id=cid, email=f"ops@{name.replace(' ', '').lower()}.example"),
    }


def run_jobs() -> int:
    return jobs.run_pending(kinds=["integrations.publish", "integrations.deliver"])


def deliveries(integration_id) -> list[dict]:
    with db.tx() as conn:
        return conn.execute(
            "SELECT * FROM connect_deliveries WHERE integration_id = %s ORDER BY id", (integration_id,)
        ).fetchall()


def add(client, headers, provider: str, **kw) -> dict:
    body = {"provider": provider, "name": kw.pop("name", provider), **kw}
    r = client.post("/api/v1/integrations", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def event(conn_or_none=None, *, customer_id, node_id=None, kind: str, detail: dict) -> None:
    from psycopg.types.json import Jsonb

    with db.tx() as conn:
        conn.execute(
            "INSERT INTO events (time, customer_id, node_id, kind, detail) VALUES (now(), %s, %s, %s, %s)",
            (customer_id, node_id, kind, Jsonb(detail)),
        )
