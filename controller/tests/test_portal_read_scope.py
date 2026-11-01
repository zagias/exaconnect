"""Selected-organisation reads never widen a customer or carrier's access."""

import uuid

import pytest
from fastapi import HTTPException

from exaconnect_controller import db
from exaconnect_controller.api import metering, views
from exaconnect_controller.api.deps import User
from exaconnect_controller.security import hash_password

from .test_flow import _seed


def _user(role, customer=None, carrier=None):
    return User(uuid.uuid4(), "reader@example.org", role, customer, carrier)


def test_read_scope_cannot_override_the_customers_organisation():
    own, other = uuid.uuid4(), uuid.uuid4()
    customer = _user("customer", own)
    assert views._read_scope(customer, other) == own
    assert metering._scope(customer, other) == {"c": own, "k": None}
    with pytest.raises(HTTPException):
        metering._scope(_user("customer"), other)


def test_staff_can_narrow_reads_and_carriers_keep_their_carrier_boundary():
    selected, carrier = uuid.uuid4(), uuid.uuid4()
    admin = _user("admin")
    assert views._read_scope(admin) is None
    assert views._read_scope(admin, selected) == selected
    assert metering._scope(admin, selected) == {"c": selected, "k": None}
    assert metering._scope(_user("carrier", carrier=carrier), selected) == {"c": selected, "k": carrier}


def _two_organisations():
    first = _seed()["customer_id"]
    with db.tx() as conn:
        source = conn.execute(
            "SELECT site_id, carrier_id FROM links WHERE customer_id = %s AND path = 'carrier-a' LIMIT 1",
            (first,),
        ).fetchone()
        second = conn.execute("INSERT INTO customers (name) VALUES ('Other organisation') RETURNING id").fetchone()[
            "id"
        ]
        site = conn.execute(
            "INSERT INTO sites (customer_id, name, kind, asn, overlay_host)"
            " VALUES (%s, 'Other office', 'site', 65199, 11) RETURNING id",
            (second,),
        ).fetchone()["id"]
        other_link = conn.execute(
            "INSERT INTO links (customer_id, site_id, carrier_id, path, underlay_type, underlay_interface)"
            " VALUES (%s, %s, %s, 'carrier-a', 'fibre', 'eth1') RETURNING id",
            (second, site, source["carrier_id"]),
        ).fetchone()["id"]
        for customer, sid, cls in ((first, source["site_id"], "payments"), (second, site, "business")):
            conn.execute(
                "INSERT INTO decisions (time, customer_id, site_id, class_name, kind, engine, reason)"
                " VALUES (now(), %s, %s, %s, 'move', 'test', %s)",
                (customer, sid, cls, f"Decision for {cls}"),
            )
            conn.execute(
                "INSERT INTO events (time, customer_id, kind) VALUES (now(), %s, %s)",
                (customer, f"scope-{cls}"),
            )
    return first, str(second), str(other_link)


def test_selected_organisation_scopes_decisions_events_usage_and_csv(client, admin_headers):
    first, second, other_link = _two_organisations()
    decisions = client.get(f"/api/v1/decisions?customer_id={first}&class_name=payments", headers=admin_headers)
    assert decisions.status_code == 200, decisions.text
    assert [d["class_name"] for d in decisions.json()] == ["payments"]
    assert client.get(f"/api/v1/decisions?customer_id={second}&class_name=payments", headers=admin_headers).json() == []
    events = client.get(f"/api/v1/events?customer_id={first}", headers=admin_headers).json()
    assert any(e["kind"] == "scope-payments" for e in events)
    assert not any(e["kind"] == "scope-business" for e in events)
    links = client.get(f"/api/v1/metering/links?customer_id={first}", headers=admin_headers).json()["links"]
    assert links and {link["customer_id"] for link in links} == {first}
    assert (
        client.get(
            f"/api/v1/metering/links/{other_link}/samples?customer_id={first}", headers=admin_headers
        ).status_code
        == 404
    )
    csv = client.get(f"/api/v1/metering/settlement.csv?customer_id={first}", headers=admin_headers)
    assert csv.status_code == 200 and "Other office" not in csv.text
    usage = client.get(f"/api/v1/metering/usage?customer_id={second}", headers=admin_headers).json()
    assert [p["site"] for p in usage["paths"]] == ["Other office"]


def test_customer_query_parameter_cannot_read_another_organisation(client, admin_headers):
    first, second, other_link = _two_organisations()
    with db.tx() as conn:
        user = conn.execute(
            "INSERT INTO users (email, password_hash, role, customer_id)"
            " VALUES ('scoped-reader@example.org', %s, 'customer', %s) RETURNING id",
            (hash_password("read scope password"), first),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO org_memberships (customer_id, user_id, role) VALUES (%s, %s, 'viewer')",
            (first, user),
        )
    login = client.post(
        "/api/v1/auth/login", json={"email": "scoped-reader@example.org", "password": "read scope password"}
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    decisions = client.get(f"/api/v1/decisions?customer_id={second}", headers=headers)
    assert decisions.status_code == 200, decisions.text
    assert {d["customer_id"] for d in decisions.json()} == {first}
    events = client.get(f"/api/v1/events?customer_id={second}", headers=headers).json()
    assert not any(e["kind"] == "scope-business" for e in events)
    links = client.get(f"/api/v1/metering/links?customer_id={second}", headers=headers).json()["links"]
    assert links and {link["customer_id"] for link in links} == {first}
    assert (
        client.get(f"/api/v1/metering/links/{other_link}/samples?customer_id={second}", headers=headers).status_code
        == 404
    )
    usage = client.get(f"/api/v1/metering/usage?customer_id={second}", headers=headers).json()
    assert "Other office" not in {p["site"] for p in usage["paths"]}
    assert (
        "Other office" not in client.get(f"/api/v1/metering/settlement.csv?customer_id={second}", headers=headers).text
    )


@pytest.mark.parametrize(
    "path", ["/decisions", "/events", "/metering/links", "/metering/usage", "/metering/settlement.csv"]
)
def test_read_scope_rejects_invalid_organisation_ids(client, admin_headers, path):
    assert client.get(f"/api/v1{path}?customer_id=not-a-uuid", headers=admin_headers).status_code == 422
