"""Jibsy inbox load test (not collected by pytest).

Drives the real controller (uvicorn on a local port, its own job worker
running in-process as in production) against a database you name, with:

- N simulated customers, half on website chat (the widget API, each from its
  own address via X-Forwarded-For) and half on SMS (signed webhooks from the
  simulated provider, all from one provider address, as Twilio's are), each
  sending a numbered message every `--think` seconds (jittered) and the
  website ones polling for replies;
- M agents, each listing the inbox, reading the conversations it owns (a
  fixed share, so two agents never answer the same conversation) and
  answering every unanswered customer message with a numbered reply.

The simulated SMS provider retries a webhook answered 429 or 5xx (after
Retry-After, up to --provider-retries times), as Meta and 360dialog do;
a webhook still refused after that is a lost message and is counted.

At the end it waits for the send queue to drain and checks, from the
database: every message a customer sent is stored exactly once, in that
customer's one conversation; every reply an agent wrote is stored once, sent
once (SMS: handed to the provider exactly once) and in the order written.

  python3 tests/load/inbox_load.py --db postgresql://exa:ci-only@127.0.0.1:5432/exatest_t \
      --customers 50 --agents 10 --duration 60

The database is emptied first (DROP SCHEMA public). Never point it at a
database you care about.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import secrets
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import httpx
import psycopg
from psycopg.rows import dict_row

HERE = os.path.dirname(os.path.abspath(__file__))
CONTROLLER = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, CONTROLLER)

from exaconnect_controller.commai.channels.providers import Simulated  # noqa: E402
from exaconnect_controller.security import hash_password  # noqa: E402

SITE = "https://www.loadtest.example"
PASSWORD = "load test password"
PROVIDER_IP = "198.51.100.7"


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.lat: dict[str, list[float]] = collections.defaultdict(list)
        self.codes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        self.errors: list[str] = []

    def add(self, name: str, seconds: float, code: int):
        with self.lock:
            self.lat[name].append(seconds)
            self.codes[name][code] += 1

    def error(self, msg: str):
        with self.lock:
            if len(self.errors) < 50:
                self.errors.append(msg)


def timed(stats: Stats, name: str, fn, *a, **kw) -> httpx.Response | None:
    t = time.perf_counter()
    try:
        r = fn(*a, **kw)
    except httpx.HTTPError as e:
        stats.add(name, time.perf_counter() - t, 0)
        stats.error(f"{name}: {type(e).__name__}: {e}")
        return None
    stats.add(name, time.perf_counter() - t, r.status_code)
    return r


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


# ---- server ---------------------------------------------------------------------------------------


def reset(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")


def start_server(url: str, port: int, data_dir: str, log_path: str = "") -> subprocess.Popen:
    env = {
        **os.environ,
        "EXA_DATABASE_URL": url,
        "EXA_DATA_DIR": data_dir,
        "EXA_PROXY_SECRET": secrets.token_hex(16),
        "EXA_ADMIN_EMAIL": "admin@loadtest.example",
        "EXA_ADMIN_PASSWORD": secrets.token_urlsafe(16),
        "EXA_ROUTING_INTERVAL_S": "10",  # > 0 runs the Jibsy job worker in-process, as in production
        "EXA_NHC_URL": "",
        "EXA_USGS_URL": "",
        "EXA_GDACS_URL": "",
        "EXA_TSUNAMI_URLS": "",
        "EXA_PUBLIC_URL": f"http://127.0.0.1:{port}",
        "PYTHONPATH": CONTROLLER,
    }
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "exaconnect_controller.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--proxy-headers",
        "--forwarded-allow-ips",
        "127.0.0.1",
        "--log-level",
        "warning",
    ]
    log = open(log_path, "w") if log_path else None  # noqa: SIM115 - lives as long as the server
    proc = subprocess.Popen(cmd, cwd=CONTROLLER, env=env, stdout=log, stderr=log)
    for _ in range(120):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    proc.kill()
    raise SystemExit("the controller did not start")


# ---- the business ----------------------------------------------------------------------------------


def seed(url: str, base: str, agents: int, sms_rate: int = 0) -> dict:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        cid = conn.execute("INSERT INTO customers (name) VALUES ('Load Test Bank') RETURNING id").fetchone()["id"]
        people = []
        for i in range(agents):
            email = f"agent{i}@loadtest.example"
            uid = conn.execute(
                "INSERT INTO users (email, password_hash, role, customer_id) VALUES (%s, %s, 'customer', %s)"
                " RETURNING id",
                (email, hash_password(PASSWORD), cid),
            ).fetchone()["id"]
            conn.execute("INSERT INTO commai_members (customer_id, user_id, seat) VALUES (%s, %s, 'agent')", (cid, uid))
            people.append({"id": str(uid), "email": email})
        # SMS to Trinidad and Tobago switched on for this business, as an
        # ExaCarib admin does once its criteria are met (ADR 0028).
        from exaconnect_controller.commai import golive

        for c in conn.execute(
            "SELECT criterion FROM commai_capability_criteria WHERE kind = 'country' AND key = 'TT'"
        ).fetchall():
            golive.check(conn, "country", "TT", c["criterion"], True, "load test", "load test")
        golive.set_status(conn, "country", "TT", "pilot", "load test", [str(cid)])
        if sms_rate:  # the country's SMS pace (default 60 a minute), as PUT /countries/TT/sms-rules sets it
            conn.execute(
                """INSERT INTO country_sms_overrides (country, rules, updated_by) VALUES ('TT', %s, 'load test')
                   ON CONFLICT (country) DO UPDATE SET rules = EXCLUDED.rules""",
                (json.dumps({"rate_per_minute": sms_rate}),),
            )
        conn.commit()
    c = httpx.Client(base_url=base, timeout=30)
    for p in people:
        r = c.post("/api/v1/auth/login", json={"email": p["email"], "password": PASSWORD})
        r.raise_for_status()
        p["h"] = {"Authorization": f"Bearer {r.json()['token']}"}
    u = f"/api/v1/commai/customers/{cid}"
    h = people[0]["h"]
    team = c.post(f"{u}/teams", json={"name": "Everyone", "members": [p["id"] for p in people]}, headers=h)
    team.raise_for_status()
    c.post(f"{u}/routing-rules", json={"name": "All", "match": {}, "team_id": team.json()["id"]}, headers=h)
    key = c.post(f"{u}/widget-keys", json={"name": "Site", "allowed_origins": [SITE]}, headers=h)
    key.raise_for_status()
    sms = c.post(
        f"{u}/channel-accounts",
        json={
            "channel": "sms",
            "provider": "simulated",
            "address": "+18685550000",
            "name": "SMS",
            "settings": {"daily_limits": {"*": 1_000_000}},
        },
        headers=h,
    )
    sms.raise_for_status()
    return {"id": str(cid), "u": u, "people": people, "key": key.json(), "sms": sms.json()}


# ---- customers and agents --------------------------------------------------------------------------


class Customer(threading.Thread):
    def __init__(self, i, biz, base, stats, stop, think, retries):
        super().__init__(daemon=True)
        self.i, self.biz, self.stats, self.stop, self.think, self.retries = i, biz, stats, stop, think, retries
        self.web = i % 2 == 0
        self.c = httpx.Client(base_url=base, timeout=30)
        self.sent: list[str] = []
        self.lost: list[str] = []
        self.conv: str | None = None
        self.address = f"+1868556{i:04d}"
        self.ip = f"10.{(i >> 16) & 255}.{(i >> 8) & 255}.{i & 255}"

    def run(self):
        time.sleep(random.random() * self.think)
        if self.web:
            w = f"/api/v1/commai/widget/{self.biz['key']['public_key']}"
            self.wh = {"Origin": SITE, "X-Forwarded-For": self.ip}
            r = timed(self.stats, "widget: start session", self.c.post, f"{w}/session", json={}, headers=self.wh)
            if r is None or r.status_code != 200:
                self.stats.error(f"customer {self.i}: no session {r and r.status_code}")
                return
            self.wh["X-Widget-Session"] = r.json()["token"]
        k = 0
        while not self.stop.is_set():
            body = f"cust{self.i}-msg{k}"
            ok = self.send_web(w, body, k) if self.web else self.send_sms(body, k)
            (self.sent if ok else self.lost).append(body)
            k += 1
            if self.web and self.conv and k % 2 == 0:
                timed(
                    self.stats,
                    "widget: poll messages",
                    self.c.get,
                    f"{w}/messages",
                    params={"conversation_id": self.conv},
                    headers=self.wh,
                )
            self.stop.wait(self.think * (0.5 + random.random()))

    def send_web(self, w, body, k) -> bool:
        payload = {"body": body, "client_id": f"client-{self.i:05d}-{k:06d}"}
        if self.conv:
            payload["conversation_id"] = self.conv
        for _ in range(self.retries + 1):  # the browser retries with the same client_id
            r = timed(self.stats, "widget: send message", self.c.post, f"{w}/messages", json=payload, headers=self.wh)
            if r is not None and r.status_code == 201:
                self.conv = r.json()["conversation_id"]
                return True
            time.sleep(float((r and r.headers.get("retry-after")) or 1))
        return False

    def send_sms(self, body, k) -> bool:
        raw = json.dumps({"messages": [{"id": f"sms-{self.i}-{k}", "from": self.address, "body": body}]}).encode()
        sms = self.biz["sms"]
        path = "/api/v1" + sms["webhook_url"].split("/api/v1", 1)[1]
        h = {
            "Content-Type": "application/json",
            "X-Exa-Signature": Simulated.sign(sms["secret"], raw),
            "X-Forwarded-For": PROVIDER_IP,
        }
        for _ in range(self.retries + 1):  # the provider retries a refused webhook
            r = timed(self.stats, "sms: provider webhook", self.c.post, path, content=raw, headers=h)
            if r is not None and r.status_code == 200:
                return True
            time.sleep(float((r and r.headers.get("retry-after")) or 1))
        return False


class Agent(threading.Thread):
    def __init__(self, j, n, person, biz, base, stats, stop, think):
        super().__init__(daemon=True)
        self.j, self.n, self.p, self.biz, self.stats, self.stop, self.think = j, n, person, biz, stats, stop, think
        self.c = httpx.Client(base_url=base, timeout=30)
        self.replies: list[tuple[str, str]] = []  # (conversation id, body) in the order written
        self.answered: dict[str, int] = collections.defaultdict(int)  # conversation -> inbound messages answered
        self.k = 0

    def mine(self, conv_id: str) -> bool:
        return uuid.UUID(conv_id).int % self.n == self.j

    def run(self):
        u, h = self.biz["u"], self.p["h"]
        while not self.stop.is_set():
            r = timed(
                self.stats,
                "agent: list conversations",
                self.c.get,
                f"{u}/conversations",
                params={"limit": 100},
                headers=h,
            )
            if r is not None and r.status_code == 200:
                for conv in r.json()["items"]:
                    if self.stop.is_set() or not self.mine(conv["id"]):
                        continue
                    m = timed(
                        self.stats,
                        "agent: read messages",
                        self.c.get,
                        f"{u}/conversations/{conv['id']}/messages",
                        headers=h,
                    )
                    if m is None or m.status_code != 200:
                        continue
                    inbound = [x for x in m.json() if x["direction"] == "in"]
                    if len(inbound) <= self.answered[conv["id"]]:
                        continue
                    body = f"agent{self.j}-reply{self.k}-to-{inbound[-1]['body']}"
                    self.k += 1
                    hh = {**h, "Idempotency-Key": f"a{self.j}-{self.k}"}
                    for _ in range(3):
                        s = timed(
                            self.stats,
                            "agent: send reply",
                            self.c.post,
                            f"{u}/conversations/{conv['id']}/messages",
                            json={"body": body, "take_over": True},
                            headers=hh,
                        )
                        if s is not None and s.status_code == 201:
                            self.replies.append((conv["id"], body))
                            self.answered[conv["id"]] = len(inbound)
                            break
                        if s is not None and s.status_code not in (429, 500, 502, 503):
                            self.stats.error(f"reply {s.status_code}: {s.text[:120]}")
                            break
                        time.sleep(float((s and s.headers.get("retry-after")) or 1))
            self.stop.wait(self.think * (0.5 + random.random()))


# ---- checks -----------------------------------------------------------------------------------------


def drain(url: str, timeout_s: float = 120) -> float:
    t0 = time.monotonic()
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        while time.monotonic() - t0 < timeout_s:
            n = conn.execute(
                "SELECT count(*) AS n FROM jobs WHERE status IN ('queued', 'running') AND kind = 'message.send'"
            ).fetchone()["n"]
            if n == 0:
                return time.monotonic() - t0
            time.sleep(0.5)
    return -1


def verify(url: str, biz: dict, customers: list[Customer], agents: list[Agent]) -> dict:
    out: dict = {"problems": []}
    with psycopg.connect(url, row_factory=dict_row) as conn:
        rows = conn.execute(
            """SELECT m.body, m.conversation_id, ci.address, ci.channel FROM messages m
               JOIN conversations c ON c.id = m.conversation_id JOIN contact_identities ci ON ci.id = c.identity_id
               WHERE m.customer_id = %s AND m.direction = 'in'""",
            (biz["id"],),
        ).fetchall()
        by_body = collections.defaultdict(list)
        for r in rows:
            by_body[r["body"]].append(r)
        sent = sum(len(c.sent) for c in customers)
        ok_in = 0
        for c in customers:
            convs = set()
            for body in c.sent:
                got = by_body.get(body, [])
                if len(got) != 1:
                    out["problems"].append(f"inbound {body}: stored {len(got)} times")
                    continue
                convs.add(str(got[0]["conversation_id"]))
                if not c.web and got[0]["address"] != c.address:
                    out["problems"].append(f"inbound {body}: in another customer's conversation")
                    continue
                ok_in += 1
            if len(convs) > 1:
                out["problems"].append(f"customer {c.i}: messages split over {len(convs)} conversations")
            if c.web and c.conv and convs and convs != {c.conv}:
                out["problems"].append(f"customer {c.i}: messages not in the widget's conversation")
        out["inbound_sent"], out["inbound_ok"] = sent, ok_in
        out["inbound_lost"] = sum(len(c.lost) for c in customers)
        out["unexpected_inbound"] = len(rows) - sent

        outs = conn.execute(
            """SELECT m.id, m.body, m.status, m.conversation_id, m.created_at, c.channel FROM messages m
               JOIN conversations c ON c.id = m.conversation_id
               WHERE m.customer_id = %s AND m.direction = 'out' AND m.author_kind = 'user'""",
            (biz["id"],),
        ).fetchall()
        sim = conn.execute(
            "SELECT id, body, to_address FROM sim_channel_outbox WHERE customer_id = %s ORDER BY id", (biz["id"],)
        ).fetchall()
    out_by_body = collections.defaultdict(list)
    for r in outs:
        out_by_body[r["body"]].append(r)
    sim_by_body = collections.Counter(r["body"] for r in sim)
    sim_order = {r["body"]: r["id"] for r in sim}
    written = [(conv, body) for a in agents for conv, body in a.replies]
    ok_out = 0
    statuses = collections.Counter(r["status"] for r in outs)
    for _conv, body in written:
        got = out_by_body.get(body, [])
        if len(got) != 1:
            out["problems"].append(f"reply {body}: stored {len(got)} times")
            continue
        r = got[0]
        if r["status"] != "sent":
            out["problems"].append(f"reply {body}: status {r['status']}")
            continue
        if r["channel"] == "sms" and sim_by_body[body] != 1:
            out["problems"].append(f"reply {body}: handed to the SMS provider {sim_by_body[body]} times")
            continue
        ok_out += 1
    # Order: within each conversation, replies go out in the order they were written.
    order_bad = 0
    for a in agents:
        per_conv = collections.defaultdict(list)
        for conv, body in a.replies:
            per_conv[conv].append(body)
        for conv, bodies in per_conv.items():
            created = [out_by_body[b][0]["created_at"] for b in bodies if len(out_by_body.get(b, [])) == 1]
            if created != sorted(created):
                order_bad += 1
            sms_ids = [sim_order[b] for b in bodies if b in sim_order]
            if sms_ids != sorted(sms_ids):
                order_bad += 1
                out["problems"].append(f"conversation {conv}: SMS replies sent out of order")
    out.update(
        replies_written=len(written),
        replies_ok=ok_out,
        replies_out_of_order=order_bad,
        reply_status=dict(statuses),
        sms_handed_to_provider=len(sim),
    )
    return out


# ---- main ---------------------------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("EXA_LOAD_DATABASE_URL", ""))
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--customers", type=int, default=50)
    ap.add_argument("--agents", type=int, default=10)
    ap.add_argument("--duration", type=float, default=60)
    ap.add_argument("--think", type=float, default=2.0, help="seconds between a customer's messages (mean)")
    ap.add_argument("--agent-think", type=float, default=1.0, help="seconds between an agent's inbox passes (mean)")
    ap.add_argument("--provider-retries", type=int, default=3)
    ap.add_argument("--sms-rate", type=int, default=0, help="SMS a minute to TT (0: the country default, 60 a minute)")
    ap.add_argument("--json", help="also write the results here")
    ap.add_argument("--server-log", default="", help="write the controller's output here")
    args = ap.parse_args()
    if not args.db:
        raise SystemExit("--db (or EXA_LOAD_DATABASE_URL) is required; the database is emptied")

    reset(args.db)
    data_dir = tempfile.mkdtemp(prefix="exa-load-")
    proc = start_server(args.db, args.port, data_dir, args.server_log)
    base = f"http://127.0.0.1:{args.port}"
    try:
        biz = seed(args.db, base, args.agents, args.sms_rate)
        stats, stop = Stats(), threading.Event()
        customers = [
            Customer(i, biz, base, stats, stop, args.think, args.provider_retries) for i in range(args.customers)
        ]
        agents = [
            Agent(j, args.agents, biz["people"][j], biz, base, stats, stop, args.agent_think)
            for j in range(args.agents)
        ]
        t0 = time.monotonic()
        for t in customers + agents:
            t.start()
        time.sleep(args.duration)
        stop.set()
        for t in customers:
            t.join(timeout=60)
        # Agents answer what is left, then stop.
        for t in agents:
            t.join(timeout=60)
        elapsed = time.monotonic() - t0
        drained = drain(args.db)
        result = {
            "customers": args.customers,
            "agents": args.agents,
            "duration_s": args.duration,
            "elapsed_s": round(elapsed, 1),
            "queue_drained_after_s": round(drained, 1),
            "requests": sum(len(v) for v in stats.lat.values()),
            "throughput_rps": round(sum(len(v) for v in stats.lat.values()) / elapsed, 1),
            "endpoints": {
                name: {
                    "n": len(xs),
                    "rps": round(len(xs) / elapsed, 2),
                    "p50_ms": round(pct(xs, 50) * 1000, 1),
                    "p95_ms": round(pct(xs, 95) * 1000, 1),
                    "p99_ms": round(pct(xs, 99) * 1000, 1),
                    "mean_ms": round(statistics.fmean(xs) * 1000, 1),
                    "codes": dict(stats.codes[name]),
                }
                for name, xs in sorted(stats.lat.items())
            },
            "errors": sum(n for c in stats.codes.values() for code, n in c.items() if code == 0 or code >= 400),
            "error_samples": stats.errors[:10],
            "checks": verify(args.db, biz, customers, agents),
        }
        result["checks"]["problems"] = result["checks"]["problems"][:20]
        print(json.dumps(result, indent=2))
        if args.json:
            with open(args.json, "w") as f:
                json.dump(result, f, indent=2)
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
