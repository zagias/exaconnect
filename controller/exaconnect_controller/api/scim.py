"""SCIM 2.0 at /api/v1/scim/v2 (ADR 0017). Authenticated by a business's SCIM
bearer token (made and revoked in sign-in settings, kept hashed). Every write
is audited as actor "scim:<token prefix>"."""

from __future__ import annotations

import functools
import re
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse, Response

from .. import audit, db
from ..identity import scim
from ..identity.directory import quirks
from ..identity.scim import ScimError
from ..security import token_hash

router = APIRouter(prefix="/scim/v2", tags=["scim"])
MEDIA = "application/scim+json"


def _out(content: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(content, status_code=status, media_type=MEDIA)


def _base(request: Request) -> str:
    s = request.app.state.settings
    root = s.public_url or str(request.base_url).rstrip("/")
    return f"{root}/api/v1/scim/v2"


def _token(conn, request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise ScimError(401, "A SCIM bearer token is required.")
    row = conn.execute(
        """UPDATE scim_tokens SET last_used_at = now() WHERE token_hash = %s AND revoked_at IS NULL
           RETURNING id, customer_id, prefix""",
        (token_hash(auth.removeprefix("Bearer ").strip()),),
    ).fetchone()
    if row is None:
        raise ScimError(401, "This SCIM token is not valid or has been revoked.")
    return row


def scim_endpoint(fn):
    """Runs the handler in one transaction with the token's business; SCIM error bodies."""

    @functools.wraps(fn)
    def wrapper(request: Request, *args, **kwargs):
        try:
            with db.tx() as conn:
                tok = _token(conn, request)
                return fn(request, *args, conn=conn, tok=tok, **kwargs)
        except ScimError as e:
            return _out(e.body(), e.status)

    # FastAPI reads the signature: hide conn/tok from it.
    import inspect

    sig = inspect.signature(fn)
    wrapper.__signature__ = sig.replace(parameters=[p for n, p in sig.parameters.items() if n not in ("conn", "tok")])
    return wrapper


_FILTER = re.compile(r'^\s*([A-Za-z.]+)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$')


def _filter(expr: str | None, allowed: dict[str, str]) -> tuple[str, Any] | None:
    if not expr:
        return None
    m = _FILTER.match(quirks.normalise_filter(expr) or "")
    if not m or m.group(1) not in allowed:
        raise ScimError(400, 'Only filters like userName eq "name" are supported.', "invalidFilter")
    return allowed[m.group(1)], m.group(2).replace('\\"', '"')


def _page(rows: list, start: int, count: int, total: int) -> dict:
    return {
        "schemas": [scim.LIST],
        "totalResults": total,
        "startIndex": start,
        "itemsPerPage": len(rows),
        "Resources": rows,
    }


def _ops(body: dict) -> list[dict]:
    ops = body.get("Operations")
    if not isinstance(ops, list) or not ops:
        raise ScimError(400, "Operations is required.", "invalidSyntax")
    return quirks.normalise_ops(ops)  # provider quirks (ADR 0036)


def _audit(conn, tok: dict, action: str, target: str, detail: dict | None = None) -> None:
    audit.record(conn, f"scim:{tok['prefix']}", action, target, tok["customer_id"], detail)


Start = Annotated[int, Query(alias="startIndex", ge=1)]
Count = Annotated[int, Query(ge=0)]


# ---- discovery ----------------------------------------------------------------


@router.get("/ServiceProviderConfig")
@scim_endpoint
def service_provider_config(request: Request, conn=None, tok=None):
    return _out(scim.service_provider_config(_base(request)))


@router.get("/ResourceTypes")
@scim_endpoint
def resource_types(request: Request, conn=None, tok=None):
    types = scim.resource_types(_base(request))
    return _out(_page(types, 1, len(types), len(types)))


@router.get("/Schemas")
@scim_endpoint
def schemas(request: Request, conn=None, tok=None):
    items = scim.schemas(_base(request))
    return _out(_page(items, 1, len(items), len(items)))


# ---- users --------------------------------------------------------------------

USER_FILTERS = {
    "userName": "lower(email) = lower(%s)",
    "externalId": "scim_external_id = %s",
    "emails.value": "lower(email) = lower(%s)",
    "id": "id::text = %s",
}


@router.get("/Users")
@scim_endpoint
def list_users(request: Request, filter: str | None = None, start: Start = 1, count: Count = 100, conn=None, tok=None):
    f = _filter(filter, USER_FILTERS)
    where, args = "customer_id = %s AND scim_deleted_at IS NULL", [tok["customer_id"]]
    if f:
        where += f" AND {f[0]}"
        args.append(f[1])
    total = conn.execute(f"SELECT count(*) AS n FROM users WHERE {where}", args).fetchone()["n"]
    rows = conn.execute(
        f"SELECT * FROM users WHERE {where} ORDER BY created_at, id OFFSET %s LIMIT %s",
        [*args, start - 1, min(count, scim.MAX_PAGE)],
    ).fetchall()
    base = _base(request)
    return _out(_page([scim.user_resource(conn, r, base) for r in rows], start, count, total))


@router.get("/Users/{user_id}")
@scim_endpoint
def get_user(request: Request, user_id: str, conn=None, tok=None):
    return _out(scim.user_resource(conn, scim.get_user(conn, tok["customer_id"], user_id), _base(request)))


@router.post("/Users")
@scim_endpoint
def create_user(request: Request, body: Annotated[dict, Body()], conn=None, tok=None):
    row, created = scim.create_user(conn, tok["customer_id"], body)
    _audit(conn, tok, "scim.user.create" if created else "scim.user.link", row["email"])
    return _out(scim.user_resource(conn, row, _base(request)), 201)


@router.put("/Users/{user_id}")
@scim_endpoint
def replace_user(request: Request, user_id: str, body: Annotated[dict, Body()], conn=None, tok=None):
    row = scim.replace_user(conn, tok["customer_id"], user_id, body)
    _audit(conn, tok, "scim.user.replace", row["email"], {"active": row["disabled_at"] is None})
    return _out(scim.user_resource(conn, row, _base(request)))


@router.patch("/Users/{user_id}")
@scim_endpoint
def patch_user(request: Request, user_id: str, body: Annotated[dict, Body()], conn=None, tok=None):
    row = scim.patch_user(conn, tok["customer_id"], user_id, _ops(body))
    _audit(conn, tok, "scim.user.patch", row["email"], {"active": row["disabled_at"] is None})
    return _out(scim.user_resource(conn, row, _base(request)))


@router.delete("/Users/{user_id}")
@scim_endpoint
def delete_user(request: Request, user_id: str, conn=None, tok=None):
    row = scim.get_user(conn, tok["customer_id"], user_id)
    scim.delete_user(conn, tok["customer_id"], user_id)
    _audit(conn, tok, "scim.user.delete", row["email"])
    return Response(status_code=204)


# ---- groups -------------------------------------------------------------------

GROUP_FILTERS = {"displayName": "display_name = %s", "externalId": "external_id = %s", "id": "id::text = %s"}


@router.get("/Groups")
@scim_endpoint
def list_groups(
    request: Request,
    filter: str | None = None,
    start: Start = 1,
    count: Count = 100,
    excludedAttributes: str | None = None,
    conn=None,
    tok=None,
):
    f = _filter(filter, GROUP_FILTERS)
    where, args = "customer_id = %s", [tok["customer_id"]]
    if f:
        where += f" AND {f[0]}"
        args.append(f[1])
    total = conn.execute(f"SELECT count(*) AS n FROM scim_groups WHERE {where}", args).fetchone()["n"]
    rows = conn.execute(
        f"SELECT * FROM scim_groups WHERE {where} ORDER BY created_at, id OFFSET %s LIMIT %s",
        [*args, start - 1, min(count, scim.MAX_PAGE)],
    ).fetchall()
    base = _base(request)
    items = [scim.group_resource(conn, g, base) for g in rows]
    if excludedAttributes and "members" in excludedAttributes:
        for i in items:
            i.pop("members", None)
    return _out(_page(items, start, count, total))


@router.get("/Groups/{group_id}")
@scim_endpoint
def get_group(request: Request, group_id: str, conn=None, tok=None):
    return _out(scim.group_resource(conn, scim.get_group(conn, tok["customer_id"], group_id), _base(request)))


@router.post("/Groups")
@scim_endpoint
def create_group(request: Request, body: Annotated[dict, Body()], conn=None, tok=None):
    g = scim.create_group(conn, tok["customer_id"], body)
    _audit(conn, tok, "scim.group.create", g["display_name"])
    return _out(scim.group_resource(conn, g, _base(request)), 201)


@router.put("/Groups/{group_id}")
@scim_endpoint
def replace_group(request: Request, group_id: str, body: Annotated[dict, Body()], conn=None, tok=None):
    g = scim.get_group(conn, tok["customer_id"], group_id)
    ops = [{"op": "replace", "path": "members", "value": body.get("members") or []}]
    if body.get("displayName"):
        ops.append({"op": "replace", "path": "displayName", "value": body["displayName"]})
    g = scim.patch_group(conn, tok["customer_id"], g, ops)
    _audit(conn, tok, "scim.group.replace", g["display_name"])
    return _out(scim.group_resource(conn, g, _base(request)))


@router.patch("/Groups/{group_id}")
@scim_endpoint
def patch_group(request: Request, group_id: str, body: Annotated[dict, Body()], conn=None, tok=None):
    g = scim.get_group(conn, tok["customer_id"], group_id)
    g = scim.patch_group(conn, tok["customer_id"], g, _ops(body))
    _audit(conn, tok, "scim.group.patch", g["display_name"])
    return _out(scim.group_resource(conn, g, _base(request)))


@router.delete("/Groups/{group_id}")
@scim_endpoint
def delete_group(request: Request, group_id: str, conn=None, tok=None):
    g = scim.get_group(conn, tok["customer_id"], group_id)
    scim.delete_group(conn, tok["customer_id"], g)
    _audit(conn, tok, "scim.group.delete", g["display_name"])
    return Response(status_code=204)
