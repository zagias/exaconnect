"""Changing and removing inventory, and managing open enrolment tokens (admin only).

Creating and updating sites and links lives in admin.py; these are the other half:
rename a customer, delete a link or a site, and list or cancel enrolment tokens.
Every change is audited and pushes new desired state to the customer's agents."""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import audit, db, desired
from .deps import AdminDep

router = APIRouter()

# Metering samples from the current or previous billing month are what settlement and
# invoices are worked out from; deleting a link would delete them with it.
_BILLED = """SELECT count(*) AS n FROM usage_5m u JOIN links l ON l.id = u.link_id
             WHERE {where} AND u.bucket >= date_trunc('month', now() - interval '1 month')"""


def _guard_billing(conn, where: str, arg: str, force: bool) -> None:
    if force:
        return
    n = conn.execute(_BILLED.format(where=where), (arg,)).fetchone()["n"]
    if n:
        raise HTTPException(
            409,
            "This has metering samples from the current or last billing month, which settlement and invoices "
            "use. Delete it after that month is settled, or delete it anyway with force=true.",
        )


class CustomerPatch(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.patch("/customers/{customer_id}")
def rename_customer(customer_id: str, body: CustomerPatch, user: AdminDep) -> dict:
    with db.tx() as conn:
        old = conn.execute("SELECT name FROM customers WHERE id = %s", (customer_id,)).fetchone()
        if old is None:
            raise HTTPException(404, "Customer not found.")
        try:
            with conn.transaction():
                row = conn.execute(
                    "UPDATE customers SET name = %s WHERE id = %s RETURNING id, name", (body.name, customer_id)
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, f"There is already a customer called {body.name}.") from None
        audit.record(conn, user.actor, "customer.rename", body.name, customer_id, {"from": old["name"]})
    return {"id": str(row["id"]), "name": row["name"]}


@router.delete("/sites/{site_id}/links/{link_id}", status_code=204)
def delete_link(site_id: str, link_id: str, user: AdminDep, force: bool = False) -> None:
    with db.tx() as conn:
        _guard_billing(conn, "l.id = %s", link_id, force)
        try:
            with conn.transaction():
                link = conn.execute(
                    """DELETE FROM links l USING sites s WHERE l.id = %s AND l.site_id = %s AND s.id = l.site_id
                       RETURNING l.customer_id, l.path, s.name AS site""",
                    (link_id, site_id),
                ).fetchone()
        except psycopg.errors.ForeignKeyViolation as e:
            raise HTTPException(
                409, f"This link is still used by other records ({e.diag.table_name}). Remove those first."
            ) from None
        if link is None:
            raise HTTPException(404, "Link not found.")
        audit.record(
            conn, user.actor, "link.delete", f"{link['site']}/{link['path']}", link["customer_id"], {"force": force}
        )
        desired.refresh(conn, link["customer_id"])


@router.delete("/sites/{site_id}", status_code=204)
def delete_site(site_id: str, user: AdminDep, force: bool = False) -> None:
    """Removes the site, its links, its agent and its tokens. Its agent's certificate stops
    working at once; the box keeps forwarding on its last state until it is cleaned up."""
    with db.tx() as conn:
        _guard_billing(conn, "l.site_id = %s", site_id, force)
        try:
            with conn.transaction():
                site = conn.execute(
                    "DELETE FROM sites WHERE id = %s RETURNING customer_id, name", (site_id,)
                ).fetchone()
        except psycopg.errors.ForeignKeyViolation as e:
            raise HTTPException(
                409, f"This site is still used by other records ({e.diag.table_name}). Remove those first."
            ) from None
        if site is None:
            raise HTTPException(404, "Site not found.")
        audit.record(conn, user.actor, "site.delete", site["name"], site["customer_id"], {"force": force})
        desired.refresh(conn, site["customer_id"])


@router.get("/enrolment-tokens")
def open_tokens(user: AdminDep) -> list[dict]:
    """Tokens not yet used and not expired. The tokens themselves are never shown again."""
    with db.tx() as conn:
        return conn.execute(
            """SELECT t.id, s.name AS site, t.site_id, t.customer_id, t.created_by, t.created_at, t.expires_at
               FROM enrolment_tokens t JOIN sites s ON s.id = t.site_id
               WHERE t.used_at IS NULL AND t.expires_at > now()
               ORDER BY t.created_at DESC"""
        ).fetchall()


@router.delete("/enrolment-tokens/{token_id}", status_code=204)
def cancel_token(token_id: str, user: AdminDep) -> None:
    with db.tx() as conn:
        tok = conn.execute(
            """UPDATE enrolment_tokens t SET expires_at = now() FROM sites s
               WHERE t.id = %s AND s.id = t.site_id AND t.used_at IS NULL AND t.expires_at > now()
               RETURNING s.name, t.customer_id""",
            (token_id,),
        ).fetchone()
        if tok is None:
            raise HTTPException(404, "No open token with that id.")
        audit.record(conn, user.actor, "enrolment_token.cancel", tok["name"], tok["customer_id"])
