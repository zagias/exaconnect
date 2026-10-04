"""The overview's 24-hour SLA, steering and recent moves, against a hand-worked example."""

import datetime as dt

from exaconnect_controller import db

from .test_flow import _enrol, _seed

# Six 10 s windows ending 60, 50, ... 10 s ago (W1..W6), per path:
# (latency ms, jitter ms, loss %). Voice SLA: 150 ms, 30 ms, 1 %. Business: 250 ms, no
# jitter limit, 2 %. Bulk: loss 5 % only (best effort).
A = [(25, 3, 0), (25, 3, 0), (25, 3, 2), (25, 3, 3), (25, 3, 3), (25, 3, 3)]
B = [(35, 4, 0), (35, 4, 0), (35, 4, 0), (35, 4, 0), (35, 40, 0), (35, 4, 0)]


def _metric(conn, at, customer, node, path, rtt, jitter, loss):
    conn.execute(
        """INSERT INTO path_metrics (time, customer_id, node_id, path, sent, received, loss_pct,
                                     rtt_avg_ms, rtt_min_ms, rtt_max_ms, jitter_ms)
           VALUES (%s, %s, %s, %s, 200, %s, %s, %s, %s, %s, %s)""",
        (at, customer, node, path, round(200 * (1 - loss / 100)), loss, rtt, rtt, rtt, jitter),
    )


def _decision(conn, at, customer, site, cls, kind, frm, to, shadow=False):
    conn.execute(
        """INSERT INTO decisions (time, customer_id, site_id, class_name, kind, from_path, to_path, shadow,
                                  engine, reason)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'test', %s)""",
        (at, customer, site, cls, kind, frm, to, shadow, f"{kind} {cls} {frm} to {to}"),
    )


def test_overview_sla_24h_hand_worked(client, admin_headers):
    seeded = _seed()
    _enrol(client, seeded["tokens"], "pop-miami")
    _enrol(client, seeded["tokens"], "site-a")
    cid = seeded["customer_id"]
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    t = [now - dt.timedelta(seconds=60 - 10 * i) for i in range(6)]  # t[0] = W1 end
    with db.tx() as conn:
        row = conn.execute(
            "SELECT s.id AS site, n.id AS node FROM sites s JOIN nodes n ON n.site_id = s.id WHERE n.name = 'site-a'"
        ).fetchone()
        site, node = row["site"], row["node"]
        for i in range(6):
            _metric(conn, t[i], cid, node, "carrier-a", *A[i])
            _metric(conn, t[i], cid, node, "carrier-b", *B[i])
        # Older than 24 h: a breach on carrier A that must not count.
        _metric(conn, now - dt.timedelta(hours=25), cid, node, "carrier-a", 900, 90, 50)
        # Voice moves A -> B between W3 and W4. A shadow move and a hold are ignored.
        _decision(conn, t[2] + dt.timedelta(seconds=5), cid, site, "voice", "move", "carrier-a", "carrier-b")
        _decision(conn, t[1], cid, site, "business", "move", "carrier-a", "carrier-b", shadow=True)
        _decision(conn, t[3], cid, site, "bulk", "hold", "carrier-b", None)
        for cls, path in (("voice", "carrier-b"), ("business", "carrier-a"), ("bulk", "carrier-b")):
            conn.execute(
                """INSERT INTO steering (site_id, class_name, customer_id, path, since) VALUES (%s, %s, %s, %s, now())
                   ON CONFLICT (site_id, class_name) DO UPDATE SET path = EXCLUDED.path""",
                (site, cls, cid, path),
            )
        # The agent still reports voice on carrier A.
        conn.execute(
            """INSERT INTO steering_actual (node_id, class_name, dst, path, updated_at)
               VALUES (%s, 'voice', '', 'carrier-a', now())
               ON CONFLICT (node_id, class_name, dst) DO UPDATE SET path = EXCLUDED.path""",
            (node,),
        )

    r = client.get("/api/v1/overview", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    sla = {c["class_name"]: c for c in body["sla_24h"]}
    # Voice: W1-W3 on A (W3 loss 2 % > 1 %: 2 met), W4-W6 on B (W5 jitter 40 > 30: 2 met) = 4 of 6.
    assert (sla["voice"]["windows"], sla["voice"]["met"], sla["voice"]["pct"]) == (6, 4, 66.67)
    # Business stays on A (the shadow move is ignored): W1-W3 within 2 %, W4-W6 at 3 % = 3 of 6.
    assert (sla["business"]["windows"], sla["business"]["met"], sla["business"]["pct"]) == (6, 3, 50.0)
    # Bulk on B: jitter has no limit, loss 0 % = 6 of 6, and it is marked best effort.
    assert (sla["bulk"]["windows"], sla["bulk"]["met"], sla["bulk"]["pct"]) == (6, 6, 100.0)
    assert sla["bulk"]["best_effort"] is True and sla["voice"]["best_effort"] is False
    assert sla["voice"]["max_loss_pct"] == 1
    assert [s["site"] for s in sla["voice"]["sites"]] == ["site-a"]
    assert body["sla_target_pct"] == 99.5

    site_a = next(s for s in body["sites"] if s["name"] == "site-a")
    steer = {x["class_name"]: x for x in site_a["steering"]}
    assert steer["voice"]["intended"] == "carrier-b" and steer["voice"]["actual"] == "carrier-a"
    assert steer["voice"]["moves_24h"] == 1 and steer["business"]["moves_24h"] == 0
    assert steer["business"]["actual"] is None
    assert body["moves_24h"] == 1
    assert [(d["class_name"], d["from_label"], d["to_label"]) for d in body["recent_decisions"]] == [
        ("voice", "Carrier A", "Carrier B")
    ]

    # An admin can narrow the overview to one customer.
    one = client.get(f"/api/v1/overview?customer_id={cid}", headers=admin_headers).json()
    assert {s["name"] for s in one["sites"]} == {s["name"] for s in body["sites"]}
    assert client.get("/api/v1/overview?customer_id=nope", headers=admin_headers).status_code == 422
