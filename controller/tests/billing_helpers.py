"""Shared set-up for the billing tests (ADR 0022)."""

from __future__ import annotations

import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from exaconnect_controller import db
from exaconnect_controller.billing import plans
from exaconnect_controller.security import hash_password

from .test_flow import _enrol, _seed

PASSWORD = "a long test password"
TABLE = [
    {"below_pct": 99.9, "credit_pct": 5},
    {"below_pct": 99.5, "credit_pct": 10},
    {"below_pct": 99, "credit_pct": 25},
]


def months() -> tuple[dt.date, dt.date, dt.datetime]:
    """(the month just ended, this month, the start of the month just ended)."""
    this_month = dt.datetime.now(dt.UTC).date().replace(day=1)
    billed = (this_month - dt.timedelta(days=1)).replace(day=1)
    return billed, this_month, dt.datetime(billed.year, billed.month, 1, tzinfo=dt.UTC)


def login(client, email, password=PASSWORD):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def add_user(conn, email, role, customer_id=None, carrier_id=None):
    conn.execute(
        "INSERT INTO users (email, password_hash, role, customer_id, carrier_id) VALUES (%s, %s, %s, %s, %s)",
        (email, hash_password(PASSWORD), role, customer_id, carrier_id),
    )


def plan_id(conn, product: str, name: str | None = None) -> str:
    name = name or ("Connect Standard" if product == "connect" else "Jibsy Standard")
    return str(conn.execute("SELECT id FROM plans WHERE product = %s AND name = %s", (product, name)).fetchone()["id"])


def new_customer(conn, name: str, site_created: dt.datetime | None = None) -> str:
    cid = conn.execute("INSERT INTO customers (name) VALUES (%s) RETURNING id", (name,)).fetchone()["id"]
    if site_created is not None:
        conn.execute(
            """INSERT INTO sites (customer_id, name, kind, asn, overlay_host, created_at)
               VALUES (%s, 'hq', 'site', 65100, %s, %s)""",
            (cid, 20 + abs(hash(name)) % 200, site_created),
        )
    return str(cid)


def lab(client, connect_only: bool = True) -> dict:
    """The lab inventory (Demo Organisation) with enrolled nodes. The seed gives it
    both plans; most billing tests want it on Connect Standard alone."""
    seeded = _seed()
    for name in ("pop-miami", "site-a", "site-b"):
        _enrol(client, seeded["tokens"], name)
    if connect_only:
        with db.tx() as conn:
            conn.execute(
                "DELETE FROM subscriptions WHERE customer_id = %s AND product = 'commai'", (seeded["customer_id"],)
            )
            plans.sync_products(conn, seeded["customer_id"])
    return seeded


def _metric(rows, at, cid, node, path, rtt, jitter, loss):
    rows.append((at, cid, node, path, 200, round(200 * (1 - loss / 100)), loss, rtt, rtt, rtt, jitter))


def build_connect_month(cid, start: dt.datetime) -> None:
    """Inventory dates, usage samples, SLA windows and a circuit for the billed month
    (see the hand-worked example in test_billing.py)."""
    with db.tx() as conn:
        conn.execute("UPDATE sites SET created_at = %s WHERE customer_id = %s", (start - dt.timedelta(days=365), cid))
        nodes = {
            r["name"]: (r["site"], r["node"])
            for r in conn.execute(
                "SELECT s.name, s.id AS site, n.id AS node FROM sites s JOIN nodes n ON n.site_id = s.id"
            ).fetchall()
        }
        links = {
            (r["site"], r["path"]): r
            for r in conn.execute(
                "SELECT l.id, l.carrier_id, s.name AS site, l.path FROM links l JOIN sites s ON s.id = l.site_id"
            ).fetchall()
        }
        usage = []
        a = links[("site-a", "carrier-a")]
        for k in range(20):  # 10, 20, ... 200 Mbps in; out is half
            usage.append(
                (a["id"], start + dt.timedelta(minutes=5 * k), cid, a["carrier_id"], 10.0 * (k + 1), 5.0 * (k + 1), 300)
            )
        sat = links[("site-a", "sat")]
        for k in range(4):
            usage.append(
                (sat["id"], start + dt.timedelta(hours=1, minutes=5 * k), cid, sat["carrier_id"], 8.0, 2.0, 300)
            )
        # Outside the month: never billed.
        usage.append((a["id"], start - dt.timedelta(minutes=5), cid, a["carrier_id"], 900.0, 900.0, 300))
        conn.cursor().executemany(
            """INSERT INTO usage_5m (link_id, bucket, customer_id, carrier_id, in_mbps, out_mbps, seconds)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            usage,
        )

        metrics = []
        site_a, node_a = nodes["site-a"]
        site_b, node_b = nodes["site-b"]
        for i in range(1000):
            at = start + dt.timedelta(seconds=10 * (i + 1))
            _metric(metrics, at, cid, node_a, "carrier-a", 25, 3, 3 if i < 13 else 0)
            _metric(metrics, at, cid, node_b, "carrier-b", 35, 40 if i < 6 else 4, 0)
        # A bad window ending exactly at midnight belongs to the month before.
        _metric(metrics, start, cid, node_b, "carrier-b", 900, 90, 50)
        conn.cursor().executemany(
            """INSERT INTO path_metrics (time, customer_id, node_id, path, sent, received, loss_pct,
                                         rtt_avg_ms, rtt_min_ms, rtt_max_ms, jitter_ms)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            metrics,
        )
        for site, path in ((site_a, "carrier-a"), (site_b, "carrier-b")):
            for cls in ("voice", "business", "bulk"):
                conn.execute(
                    """INSERT INTO steering (site_id, class_name, customer_id, path, since) VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (site_id, class_name) DO UPDATE SET path = EXCLUDED.path""",
                    (site, cls, cid, path, start - dt.timedelta(days=30)),
                )

        circuit = conn.execute(
            """INSERT INTO circuits (customer_id, name, kind, bandwidth_mbps, price_per_mbps_month, created_by,
                                     created_at)
               VALUES (%s, 'dr-link', 'site', 20, 2.0, 'test', %s) RETURNING id""",
            (cid, start - dt.timedelta(days=1)),
        ).fetchone()["id"]
        t0 = start + dt.timedelta(days=1)
        t1 = t0 + dt.timedelta(hours=73)
        conn.execute(
            """INSERT INTO circuit_bandwidth (circuit_id, mbps, valid_from, valid_to, changed_by)
               VALUES (%s, 10, %s, %s, 'test'), (%s, 20, %s, %s, 'test')""",
            (circuit, t0, t1, circuit, t1, t1 + dt.timedelta(hours=36.5)),
        )


def build_commai_month(cid, start: dt.datetime) -> None:
    """Jibsy usage and voice charges for the billed month (see test_billing.py)."""
    from exaconnect_controller.commai.voice import billing as voice_billing

    with db.tx() as conn:
        rows = []
        for meter, n in (("ai_reply", 1000), ("message_out:whatsapp", 300), ("message_out:sms", 200), ("copilot", 5)):
            rows += [(cid, meter, 1, f"{meter}-{i}", start + dt.timedelta(minutes=i)) for i in range(n)]
        # Voice minutes are rated by voice billing, never by the plan's meter prices.
        rows.append((cid, "voice_minute", 99, "vm-1", start + dt.timedelta(hours=2)))
        # Last month and next month: never on this invoice.
        rows.append((cid, "ai_reply", 500, "old", start - dt.timedelta(seconds=1)))
        conn.cursor().executemany(
            "INSERT INTO usage_records (customer_id, meter, quantity, ref, at) VALUES (%s, %s, %s, %s, %s)", rows
        )
        card = voice_billing.card_at(conn, cid)
        for ref, minutes, amount in (("call-1", 10, "0.1500"), ("call-2", 20, "0.3000")):
            conn.execute(
                """INSERT INTO voice_charges (customer_id, ref, kind, description, rate_card_id, rate_card_version,
                     quantity, unit_price, amount, at) VALUES (%s, %s, 'call', %s, %s, %s, %s, 0.015, %s, %s)""",
                (cid, ref, f"Call {ref}", card["id"], card["version"], minutes, amount, start + dt.timedelta(days=2)),
            )
        conn.execute(
            """INSERT INTO voice_users (customer_id, name, extension, sip_password, billing_from)
               VALUES (%s, 'Reception', '100', 'not-a-real-secret', %s)""",
            (cid, start - dt.timedelta(days=10)),
        )


class FakeGateway(BaseHTTPRequestHandler):
    """A local stand-in for Stripe and the hosted payment page. Records each request."""

    seen: list = []

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        FakeGateway.seen.append(
            {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": raw.decode()}
        )
        if self.path == "/v1/checkout/sessions":
            data = {"id": "cs_test_123", "url": "https://checkout.stripe.test/cs_test_123"}
        elif self.path == "/hosted-page":
            body = json.loads(raw)
            data = {"TransactionIdentifier": body["TransactionIdentifier"], "RedirectUrl": "https://pay.fac.test/hp/1"}
        elif self.path == "/fail":
            self.send_response(500)
            self.end_headers()
            return
        else:
            data = {}
        out = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def fake_gateway():
    srv = HTTPServer(("127.0.0.1", 0), FakeGateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeGateway.seen = []
    return srv
