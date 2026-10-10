"""Test account for the organisation set-up live test (lab/ci/checks/org-setup.sh, ADR 0043).

Removes the organisations and people an earlier run made, then gives the ExaCarib staff test
account a fresh random password and clears its sessions. Run inside the controller container:
python - < setup_users.py. Prints one line of JSON for the test's environment, on stdout only;
never logged.
"""

import json
import secrets

import psycopg
from exaconnect_controller import db
from exaconnect_controller.security import hash_password
from exaconnect_controller.settings import get_settings

STAFF = "setup-test-staff@exacarib.local"
ORG_PREFIX = "Set-up check "
PEOPLE = "%@setup-check.example"


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
    """Delete matching rows from every (table, column, value), retrying in passes until
    nothing that refers to them is left; a row still referred to waits for the next pass."""
    for _ in range(8):
        left = False
        for table, column, value in where:
            try:
                with conn.transaction():
                    conn.execute(f'DELETE FROM "{table}" WHERE "{column}" = ANY(%s)', (value,))
            except psycopg.errors.ForeignKeyViolation:  # still referred to; next pass
                left = True
        if not left:
            return
    raise SystemExit("could not remove the earlier test organisations")


settings = get_settings()
db.init(settings.database_url)
with db.tx() as conn:
    orgs = [r["id"] for r in conn.execute("SELECT id FROM customers WHERE name LIKE %s", (ORG_PREFIX + "%",))]
    users = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE %s", (PEOPLE,))]
    if orgs or users:
        sites = [r["id"] for r in conn.execute("SELECT id FROM sites WHERE customer_id = ANY(%s)", (orgs,))]
        where = [(t, "site_id", sites) for t in _tables(conn, "site_id")]
        where += [(t, "user_id", users) for t in _tables(conn, "user_id")]
        where += [(t, "customer_id", orgs) for t in _tables(conn, "customer_id") if t != "users"]
        where += [("users", "id", users), ("users", "customer_id", orgs), ("customers", "id", orgs)]
        _remove(conn, where)
    password = secrets.token_urlsafe(18)
    user = conn.execute("SELECT id FROM users WHERE lower(email) = %s", (STAFF,)).fetchone()
    if user is None:
        conn.execute(
            "INSERT INTO users (email, password_hash, role, display_name) VALUES (%s, %s, 'admin', %s)",
            (STAFF, hash_password(password), "Set-up test (ExaCarib)"),
        )
    else:
        conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(password), user["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (user["id"],))
print(json.dumps({"STAFF_EMAIL": STAFF, "STAFF_PW": password, "ORG_PREFIX": ORG_PREFIX}))
