"""Accounts for the quote-to-invoice live test (lab/ci/checks/commercial.sh, ADR 0055).

Removes the organisation and people an earlier run made (and, with CLEAN_ONLY=1, stops
there: the check runs it again when it ends), then makes a fresh test
organisation (marked example) with an owner, and gives the ExaCarib staff test account a
fresh random password. Run inside the controller container: python - < commercial_users.py.
Prints one line of JSON for the test's environment, on stdout only; never logged.
"""

import json
import os
import secrets

import psycopg

from exaconnect_controller import db
from exaconnect_controller.security import hash_password
from exaconnect_controller.settings import get_settings

STAFF = "commercial-test-staff@exacarib.local"
# Sorts after "Demo Organisation", so lab checks and staff screens that take the first
# organisation by name never land on it. Earlier runs used "Commercial check ".
ORG_PREFIX = "Sales check "
OLD_PREFIXES = ("Commercial check ",)
PEOPLE = "%@commercial-check.example"


def _tables(conn, column: str) -> list[str]:
    return [
        r["table_name"]
        for r in conn.execute(
            """SELECT c.table_name FROM information_schema.columns c
               JOIN information_schema.tables t USING (table_schema, table_name)
               WHERE c.table_schema = 'public' AND c.column_name = %s AND t.table_type = 'BASE TABLE'""",
            (column,),
        ).fetchall()
    ]


def _remove(conn, where: list[tuple[str, str, object]]) -> None:
    """Delete matching rows, in passes, until nothing that refers to them is left."""
    for _ in range(10):
        left = False
        for table, column, value in where:
            try:
                with conn.transaction():
                    # As text: a few tables keep ids in text columns.
                    conn.execute(
                        f'DELETE FROM "{table}" WHERE "{column}"::text = ANY(%s)',
                        ([str(v) for v in value],),
                    )
            except psycopg.errors.ForeignKeyViolation:  # still referred to; next pass
                left = True
        if not left:
            return
    raise SystemExit("could not remove the earlier test organisation")


settings = get_settings()
db.init(settings.database_url)
with db.tx() as conn:
    orgs = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM customers WHERE name LIKE ANY(%s)", ([p + "%" for p in (ORG_PREFIX, *OLD_PREFIXES)],)
        )
    ]
    users = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE %s", (PEOPLE,))]
    if orgs or users:
        sites = [r["id"] for r in conn.execute("SELECT id FROM sites WHERE customer_id = ANY(%s)", (orgs,))]
        orders = [r["id"] for r in conn.execute("SELECT id FROM sales_orders WHERE customer_id = ANY(%s)", (orgs,))]
        where = [("contract_items", "customer_id", orgs), ("one_off_charges", "customer_id", orgs)]
        where += [("sales_order_items", "order_id", orders), ("sales_orders", "id", orders)]
        where += [(t, "site_id", sites) for t in _tables(conn, "site_id")]
        where += [(t, "user_id", users) for t in _tables(conn, "user_id")]
        where += [(t, "customer_id", orgs) for t in _tables(conn, "customer_id") if t != "users"]
        where += [("users", "id", users), ("users", "customer_id", orgs), ("customers", "id", orgs)]
        _remove(conn, where)
if os.environ.get("CLEAN_ONLY"):
    # After the test: leave nothing behind on live (the removal above is committed).
    print(json.dumps({"removed": len(orgs)}))
    raise SystemExit(0)
with db.tx() as conn:
    tag = secrets.token_hex(3)
    org = conn.execute(
        "INSERT INTO customers (name, example) VALUES (%s, true) RETURNING id, name", (ORG_PREFIX + tag,)
    ).fetchone()
    owner_pw = secrets.token_urlsafe(18)
    owner = f"owner-{tag}@commercial-check.example"
    conn.execute(
        "INSERT INTO users (email, password_hash, role, customer_id) VALUES (%s, %s, 'customer', %s)",
        (owner, hash_password(owner_pw), org["id"]),
    )
    password = secrets.token_urlsafe(18)
    user = conn.execute("SELECT id FROM users WHERE lower(email) = %s", (STAFF,)).fetchone()
    if user is None:
        conn.execute(
            "INSERT INTO users (email, password_hash, role, display_name) VALUES (%s, %s, 'admin', %s)",
            (STAFF, hash_password(password), "Commercial test (ExaCarib)"),
        )
    else:
        conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(password), user["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (user["id"],))
print(
    json.dumps(
        {
            "STAFF_EMAIL": STAFF,
            "STAFF_PW": password,
            "OWNER_EMAIL": owner,
            "OWNER_PW": owner_pw,
            "ORG_ID": str(org["id"]),
            "ORG_NAME": org["name"],
        }
    )
)
