"""Real API lifecycle on the explicitly disposable integration database only.

The shared client fixture resets that database. Never run this file against a
restored customer database; the isolated Linux launcher must enforce its name.
"""

import json
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from exaconnect_controller import db

from .test_commercial import API, _org


def _brief():
    return {
        "format": "xakore-planning-brief",
        "version": 1,
        "offer": "Connect",
        "facts": [
            {"label": "Estate", "value": "20 existing shops; pilot may add two new locations"},
            *[{"label": f"Requirement {i}", "value": "Confirmed requirement " + "x" * 180} for i in range(23)],
        ],
        "gaps": ["Confirm new site names and backup capacity."],
    }


def _item(client, admin_headers, *, code="design.service", max_discount=10):
    response = client.post(
        f"{API}/catalogue/items",
        headers=admin_headers,
        json={
            "code": code,
            "name": "Fictional isolated design service",
            "product": "connect",
            "charge_type": "one_off",
            "fulfilment": "manual",
            "max_discount_pct": max_discount,
            "price": {"unit_price": "75", "currency": "USD"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _body(cid, item=None, **changes):
    return {
        "customer_id": cid,
        "title": "Fictional pilot design",
        "planning_source": _brief(),
        "creation_key": str(uuid.uuid4()),
        "lines": [] if item is None else [{"item": item["code"], "quantity": "2"}],
        **changes,
    }


def _counts():
    with db.tx() as conn:
        return {
            name: conn.execute(f"SELECT count(*) AS n FROM {name}").fetchone()["n"]
            for name in ("quotes", "sales_orders", "sites")
        }


def _approve_design(client, q, admin_headers):
    response = client.post(
        f"{API}/quotes/{q['id']}/design-review",
        headers=admin_headers,
        json={"digest": q["design_review"]["current_digest"], "note": "Fictional scope and priced items checked."},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_planning_intake_is_tenant_scoped_and_preview_does_not_create_orders(client, admin_headers):
    cid, people = _org(client, admin_headers, "Design Intake")
    foreign, other = _org(client, admin_headers, "Other Design")
    item = _item(client, admin_headers)
    body = _body(cid, item)
    before = _counts()
    options = {"customer_id": cid, "brief": body["planning_source"], "currency": "USD"}
    for headers, customer, expected in (
        (people["member"], cid, 403),
        (other["owner"], cid, 403),
        (admin_headers, str(uuid.uuid4()), 404),
    ):
        response = client.post(
            f"{API}/quotes/design-options", headers=headers, json={**options, "customer_id": customer}
        )
        assert response.status_code == expected, response.text
    response = client.post(f"{API}/quotes/design-options", headers=people["owner"], json=options)
    assert response.status_code == 200, response.text
    assert [x["code"] for x in response.json()["items"]] == [item["code"]]
    assert response.json()["source"] == body["planning_source"]
    for scopes in (["connect:read"], ["commai:read"]):
        key = client.post(
            f"{API}/auth/api-keys",
            headers=admin_headers,
            json={"name": "Fictional scoped design test", "scopes": scopes},
        )
        assert key.status_code == 201, key.text
        scoped = {"Authorization": f"Bearer {key.json()['token']}"}
        assert client.post(f"{API}/quotes/design-options", headers=scoped, json=options).status_code == 403
        assert client.post(f"{API}/quotes", headers=scoped, json=body).status_code == 403
    preview = client.post(f"{API}/quotes/preview", headers=people["owner"], json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["one_off_total"] == "150.00"
    assert _counts() == before
    assert foreign != cid


def test_full_brief_scope_draft_retry_and_immutable_source(client, admin_headers):
    cid, people = _org(client, admin_headers, "Scope Draft")
    body = _body(cid)
    before = _counts()
    created = client.post(f"{API}/quotes", headers=people["owner"], json=body)
    assert created.status_code == 201, created.text
    q = created.json()
    assert q["planning_source"] == body["planning_source"] and len(str(q["planning_source"])) > 4000
    assert q["pricing_status"] == "pending" and not q["pricing_complete"]
    assert q["design_review"]["status"] == "pending"
    retry = client.post(f"{API}/quotes", headers=people["owner"], json=body)
    assert retry.status_code == 201 and retry.json()["id"] == q["id"]
    changed = client.post(f"{API}/quotes", headers=people["owner"], json={**body, "notes": "Different intent"})
    assert changed.status_code == 409, changed.text
    assert _counts() == {**before, "quotes": before["quotes"] + 1}
    assert (
        client.put(f"{API}/quotes/{q['id']}", headers=people["owner"], json={"planning_source": None}).status_code
        == 409
    )
    assert client.post(f"{API}/quotes/{q['id']}/send", headers=people["owner"]).status_code == 409
    assert (
        client.post(
            f"{API}/quotes/{q['id']}/design-review",
            headers=admin_headers,
            json={"digest": q["design_review"]["current_digest"], "note": "This draft has no agreed price."},
        ).status_code
        == 409
    )
    document = client.get(f"{API}/quotes/{q['id']}/document", headers=people["owner"])
    assert document.status_code == 200 and "Pricing pending — no price agreed" in document.text


def test_server_pricing_retry_survives_retired_price_and_copy_needs_review(client, admin_headers):
    cid, people = _org(client, admin_headers, "Priced Draft")
    item = _item(client, admin_headers)
    body = _body(cid, item)
    body["lines"][0]["price"] = {"unit_price": "0.01"}
    created = client.post(f"{API}/quotes", headers=people["owner"], json=body)
    assert created.status_code == 201, created.text
    q = created.json()
    assert q["one_off_total"] == "150.00" and q["lines"][0]["price"]["unit_price"] == "75"
    review = {"digest": q["design_review"]["current_digest"], "note": "Customer cannot approve its own scope."}
    assert client.post(f"{API}/quotes/{q['id']}/design-review", headers=people["owner"], json=review).status_code == 403
    q = _approve_design(client, q, admin_headers)
    assert q["design_review"]["status"] == "approved"
    copied = client.post(f"{API}/quotes/{q['id']}/copy", headers=people["owner"])
    assert copied.status_code == 201, copied.text
    assert copied.json()["planning_source"] == q["planning_source"]
    assert copied.json()["creation_key"] != q["creation_key"]
    assert copied.json()["design_review"]["status"] == "pending"
    assert (
        client.patch(f"{API}/catalogue/items/{item['code']}", headers=admin_headers, json={"active": False}).status_code
        == 200
    )
    retry = client.post(f"{API}/quotes", headers=people["owner"], json=body)
    assert retry.status_code == 201 and retry.json()["id"] == q["id"]
    assert retry.json()["one_off_total"] == "150.00"


def test_concurrent_planning_save_has_one_quote_and_one_audit(client, admin_headers):
    cid, people = _org(client, admin_headers, "Concurrent Design")
    item = _item(client, admin_headers)
    body = _body(cid, item)
    before = _counts()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: client.post(f"{API}/quotes", headers=people["owner"], json=body),
                range(2),
            )
        )
    assert all(response.status_code == 201 for response in responses), [r.text for r in responses]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    assert _counts() == {**before, "quotes": before["quotes"] + 1}
    with db.tx() as conn:
        audits = conn.execute(
            "SELECT count(*) AS n FROM audit_log WHERE customer_id = %s AND action = 'quote.create'", (cid,)
        ).fetchone()["n"]
    assert audits == 1


def test_design_and_discount_reviews_are_independent_before_explicit_acceptance(client, admin_headers):
    cid, people = _org(client, admin_headers, "Design Approval")
    item = _item(client, admin_headers, max_discount=5)
    body = _body(cid, item, lines=[{"item": item["code"], "quantity": "2", "discount_pct": 20}])
    response = client.post(f"{API}/quotes", headers=admin_headers, json=body)
    assert response.status_code == 201, response.text
    q = _approve_design(client, response.json(), admin_headers)
    assert client.post(f"{API}/quotes/{q['id']}/send", headers=admin_headers).status_code == 409
    assert client.post(f"{API}/quotes/{q['id']}/submit", headers=admin_headers).status_code == 200
    approved = client.post(
        f"{API}/quotes/{q['id']}/approve",
        headers=admin_headers,
        json={"approve": True, "note": "Fictional pilot discount checked."},
    )
    assert approved.status_code == 200 and approved.json()["design_review"]["status"] == "approved"
    changed = client.put(f"{API}/quotes/{q['id']}", headers=admin_headers, json={"notes": "Revised delivery scope"})
    assert changed.status_code == 200 and changed.json()["design_review"]["status"] == "pending"
    assert client.post(f"{API}/quotes/{q['id']}/send", headers=admin_headers).status_code == 409
    q = _approve_design(client, changed.json(), admin_headers)
    assert client.post(f"{API}/quotes/{q['id']}/submit", headers=admin_headers).status_code == 200
    assert (
        client.post(
            f"{API}/quotes/{q['id']}/approve",
            headers=admin_headers,
            json={"approve": True, "note": "Revised discount approved."},
        ).status_code
        == 200
    )
    before = _counts()
    sent = client.post(f"{API}/quotes/{q['id']}/send", headers=admin_headers)
    assert sent.status_code == 200, sent.text
    assert _counts() == before
    accepted = client.post(
        f"{API}/quotes/{q['id']}/accept", headers=people["owner"], json={"name": "Fictional Authorised Buyer"}
    )
    assert accepted.status_code == 200, accepted.text
    assert _counts() == {**before, "sales_orders": before["sales_orders"] + 1}


@pytest.mark.skipif(not os.environ.get("EXA_DQ_LEGACY_CONTROLLER"), reason="exact pristine 5209 source required")
def test_pristine_5209_rollback_fails_closed_without_stripping_source(client, admin_headers):
    cid, people = _org(client, admin_headers, "Legacy Design")
    item = _item(client, admin_headers)
    body = _body(cid, item)
    created = client.post(f"{API}/quotes", headers=people["owner"], json=body)
    assert created.status_code == 201, created.text
    q = created.json()
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            filter(
                None,
                (
                    os.environ["EXA_DQ_LEGACY_CONTROLLER"],
                    os.environ.get("EXA_DQ_PUBLIC_TEST_TOOLS"),
                ),
            )
        ),
    }
    # This is a separate interpreter so current modules cannot satisfy the
    # legacy import accidentally. The launcher binds the verified 5209 archive.
    script = """
import json, os, pathlib, sys
import psycopg
from psycopg.rows import dict_row
from exaconnect_controller.commercial import quotes
assert pathlib.Path(quotes.__file__).resolve().is_relative_to(pathlib.Path(os.environ['EXA_DQ_LEGACY_CONTROLLER']))
p = json.loads(sys.stdin.read())
out = []
with psycopg.connect(os.environ['EXA_TEST_DATABASE_URL'], row_factory=dict_row) as conn:
    if p['phase'] == 'blocked':
        for name, operation in (
            ('create', lambda: quotes.create(
                conn, p['customer'], {'title': 'Legacy draft', 'lines': []}, p['actor'], True)),
            ('copy', lambda: quotes.copy(conn, p['quote'], p['actor'], True)),
            ('send', lambda: quotes.send(conn, p['quote'], p['actor'], True)),
        ):
            try:
                with conn.transaction():
                    operation()
            except psycopg.errors.CheckViolation:
                out.append(name)
            else:
                raise AssertionError(name + ' bypassed the durable design guard')
    else:
        with conn.transaction():
            result = quotes.accept(conn, p['quote'], 'Fictional Legacy Buyer', p['actor'], p['customer'])
            assert result['order']['id']
        out.append('reviewed_accept_preserved')
print(json.dumps(out))
"""
    payload = {"phase": "blocked", "customer": cid, "quote": q["id"], "actor": "user:owner@legacydesign.example"}
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        input=json.dumps(payload),
        env=environment,
        cwd=os.environ["EXA_DQ_LEGACY_CONTROLLER"],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert json.loads(result.stdout) == ["create", "copy", "send"]
    q = _approve_design(client, q, admin_headers)
    sent = client.post(f"{API}/quotes/{q['id']}/send", headers=people["owner"])
    assert sent.status_code == 200, sent.text
    before = _counts()
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        input=json.dumps({**payload, "phase": "accept"}),
        env=environment,
        cwd=os.environ["EXA_DQ_LEGACY_CONTROLLER"],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert json.loads(result.stdout) == ["reviewed_accept_preserved"]
    after = client.get(f"{API}/quotes/{q['id']}", headers=people["owner"])
    assert after.status_code == 200, after.text
    assert after.json()["planning_source"] == body["planning_source"]
    assert after.json()["state"] == "accepted"
    assert _counts() == {**before, "sales_orders": before["sales_orders"] + 1}
