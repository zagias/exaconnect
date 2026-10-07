"""The public API surface (ADR 0038): one error shape with a code and a
request id, and rate limits shared through Postgres, per key."""

from exaconnect_controller import db
from exaconnect_controller.commai import ratelimit

from .commai_helpers import base, business


def test_one_error_shape(client):
    b = business(client)
    r = client.get(f"{base(b)}/conversations/00000000-0000-0000-0000-000000000000", headers=b["agent"]["h"])
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "Conversation not found." and body["code"] == "not_found"
    assert body["request_id"] and r.headers["x-request-id"] == body["request_id"]

    # Validation errors: a sentence in detail, the fields in errors.
    r = client.post(f"{base(b)}/conversations", json={"channel": "api"}, headers=b["agent"]["h"])
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_error" and isinstance(body["detail"], str)
    assert body["detail"].startswith("address:") and body["errors"][0]["loc"][-1] == "address"

    # The caller's own request id is echoed; a strange one is replaced.
    r = client.get(f"{base(b)}/teams", headers={**b["agent"]["h"], "X-Request-ID": "trace-123.abc"})
    assert r.status_code == 200 and r.headers["x-request-id"] == "trace-123.abc"
    r = client.get(f"{base(b)}/teams", headers={**b["agent"]["h"], "X-Request-ID": "bad id <script>"})
    assert r.headers["x-request-id"] != "bad id <script>" and len(r.headers["x-request-id"]) == 32

    # Signed out and forbidden carry codes too.
    r = client.get(f"{base(b)}/teams")
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"
    other = business(client, "Other Bank")
    r = client.get(f"{base(b)}/teams", headers=other["agent"]["h"])
    assert r.status_code == 403 and r.json()["code"] == "forbidden"

    # Middleware refusals use the same shape.
    r = client.post(
        f"{base(b)}/conversations",
        json={"address": "+18685550100"},
        headers={**b["agent"]["h"], "Idempotency-Key": "k" * 201},
    )
    assert r.status_code == 400 and r.json()["code"] == "bad_request" and r.json()["request_id"]


def test_rate_limit_is_shared_and_per_key(client, admin_headers):
    b = business(client)
    r = client.post(
        "/api/v1/auth/api-keys", json={"name": "script", "scopes": ["commai:read"]}, headers=b["agent"]["h"]
    )
    key = r.json()
    kh = {"Authorization": f"Bearer {key['token']}"}
    # Only ExaCarib admins set a key's budget.
    r = client.put(f"/api/v1/commai/rate-limits/keys/{key['id']}", json={"rate_per_min": 3}, headers=b["agent"]["h"])
    assert r.status_code == 403
    r = client.put(f"/api/v1/commai/rate-limits/keys/{key['id']}", json={"rate_per_min": 3}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["rate_per_min"] == 3

    got = [client.get(f"{base(b)}/teams", headers=kh) for _ in range(4)]
    assert [g.status_code for g in got[:3]] == [200, 200, 200]
    assert got[0].headers["x-ratelimit-limit"] == "3" and got[2].headers["x-ratelimit-remaining"] == "0"
    assert got[3].status_code == 429
    assert got[3].json()["code"] == "rate_limited" and int(got[3].headers["retry-after"]) >= 1
    # Another caller has its own budget.
    assert client.get(f"{base(b)}/teams", headers=b["agent"]["h"]).status_code == 200

    # The count lives in Postgres, so every API process sees the same number.
    with db.tx() as conn:
        assert ratelimit.hit(conn, "test:shared", 2)[0] is True
    with db.tx() as conn:  # "another process"
        assert ratelimit.hit(conn, "test:shared", 2)[:2] == (True, 0)
    with db.tx() as conn:
        assert ratelimit.hit(conn, "test:shared", 2)[0] is False
