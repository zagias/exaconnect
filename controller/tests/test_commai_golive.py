"""Go-live registry (ADR 0028): capabilities start off and switch on only after their criteria are met."""

from __future__ import annotations

from exaconnect_controller import db
from exaconnect_controller.commai import golive

from .commai_helpers import business

URL = "/api/v1/commai/golive"


def _declare():
    golive.declare("channel", "test-chan", "Test channel", {"provider": "Provider contract signed."})
    with db.tx() as conn:
        golive.sync(conn)


def test_capability_starts_off_and_needs_every_criterion(client, admin_headers):
    _declare()
    b = business(client, people=("agent",))
    with db.tx() as conn:
        assert not golive.enabled(conn, "channel", "test-chan", b["id"])

    r = client.put(f"{URL}/channel/test-chan/status", json={"status": "on"}, headers=admin_headers)
    assert r.status_code == 409 and "Provider contract signed." in r.json()["detail"]

    # Evidence is required to mark a criterion met.
    r = client.put(f"{URL}/channel/test-chan/criteria/tested", json={"met": True}, headers=admin_headers)
    assert r.status_code == 422
    for c in ("tested", "security-review", "operations", "provider"):
        r = client.put(
            f"{URL}/channel/test-chan/criteria/{c}", json={"met": True, "evidence": "run 42"}, headers=admin_headers
        )
        assert r.status_code == 200, r.text

    r = client.put(f"{URL}/channel/test-chan/status", json={"status": "pilot"}, headers=admin_headers)
    assert r.status_code == 422  # a pilot names its customers
    r = client.put(
        f"{URL}/channel/test-chan/status", json={"status": "pilot", "pilots": [b["id"]]}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    with db.tx() as conn:
        assert golive.enabled(conn, "channel", "test-chan", b["id"])
        assert not golive.enabled(conn, "channel", "test-chan", None)

    # The customer sees availability, not ExaCarib's internal checks.
    rows = client.get(URL, params={"kind": "channel"}, headers=b["agent"]["h"]).json()
    mine = next(r for r in rows if r["key"] == "test-chan")
    assert mine["available"] is True and "met" not in mine

    # Customers cannot change the registry.
    r = client.put(f"{URL}/channel/test-chan/status", json={"status": "on"}, headers=b["agent"]["h"])
    assert r.status_code == 403

    # A criterion that stops holding switches the capability off.
    client.put(f"{URL}/channel/test-chan/criteria/operations", json={"met": False}, headers=admin_headers)
    with db.tx() as conn:
        assert not golive.enabled(conn, "channel", "test-chan", b["id"])


def test_sync_keeps_status_and_checks(client, admin_headers):
    _declare()
    client.put(
        f"{URL}/channel/test-chan/criteria/tested", json={"met": True, "evidence": "run 7"}, headers=admin_headers
    )
    with db.tx() as conn:
        golive.sync(conn)
        row = golive.get(conn, "channel", "test-chan")
    assert next(c for c in row["criteria"] if c["criterion"] == "tested")["met"] is True
