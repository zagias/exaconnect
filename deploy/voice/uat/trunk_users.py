"""Test organisation for the SIP trunks live test (lab/ci/checks/voice-trunks.sh, ADR 0044).

Removes what the last run made, then sets up "Trunk check organisation" (Jibsy plan only) with
an owner account, two phone sites (each its own location: Head office and Branch office) and a
person with an extension at each. The owner gets a fresh random password and no sessions. The
test itself adds the trunks through the portal. Run inside the controller container:
python - < trunk_users.py. Prints one line of JSON for the test's environment, on stdout only;
never logged.
"""

import json
import secrets

import psycopg
from exaconnect_controller import db, organisation
from exaconnect_controller.commai.voice import carriers, config, freeswitch
from exaconnect_controller.identity import orgs
from exaconnect_controller.security import hash_password
from exaconnect_controller.settings import get_settings

ACTOR = "system:trunk-test"
ORG = "Trunk check organisation"
OWNER = "trunk-test@exacarib.local"
SITES = [("Head office", "1 Frederick Street", "Port of Spain"), ("Branch office", "5 High Street", "San Fernando")]


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


def _remove(conn, cid) -> None:
    """Everything the organisation owns, in passes until nothing refers to it any more."""
    where = [(t, "customer_id") for t in _tables(conn, "customer_id") if t != "users"]
    for _ in range(8):
        left = False
        for table, column in [*where, ("customers", "id")]:
            try:
                with conn.transaction():
                    conn.execute(f'DELETE FROM "{table}" WHERE "{column}" = %s', (cid,))
            except psycopg.errors.ForeignKeyViolation:  # still referred to; next pass
                left = True
        if not left:
            return
    raise SystemExit("could not remove the last run's test organisation")


settings = get_settings()
db.init(settings.database_url)
with db.tx() as conn:
    old = conn.execute("SELECT id FROM customers WHERE name = %s", (ORG,)).fetchone()
    if old:
        conn.execute("UPDATE users SET customer_id = NULL WHERE customer_id = %s", (old["id"],))
        _remove(conn, old["id"])
        carriers.render_kamailio(conn)  # its trunks leave Kamailio's lists
    cid = conn.execute(
        "INSERT INTO customers (name, products, country) VALUES (%s, '{commai}', 'TT') RETURNING id", (ORG,)
    ).fetchone()["id"]
    password = secrets.token_urlsafe(18)
    user = conn.execute("SELECT id FROM users WHERE lower(email) = %s", (OWNER,)).fetchone()
    if user is None:
        user = conn.execute(
            """INSERT INTO users (email, password_hash, role, customer_id, display_name)
               VALUES (%s, %s, 'customer', %s, 'Trunk test owner') RETURNING id""",
            (OWNER, hash_password(password), cid),
        ).fetchone()
    else:
        conn.execute(
            "UPDATE users SET password_hash = %s, customer_id = %s WHERE id = %s",
            (hash_password(password), cid, user["id"]),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (user["id"],))
    orgs.add(conn, cid, user["id"], "owner", ACTOR)
    ops: list[dict] = [
        {"op": "add_site", "name": n, "address_line1": a, "city": c, "island": "Trinidad", "country": "TT"}
        for n, a, c in SITES
    ]
    ops += [
        {"op": "add_user", "name": "Head office caller", "site": "Head office"},
        {"op": "add_user", "name": "Branch caller", "site": "Branch office"},
    ]
    out = config.apply(conn, cid, ops, actor=ACTOR, summary="Trunk test", check_price=False)
    if not out["ok"]:
        raise SystemExit(f"could not set up the test organisation: {out['errors']}")
    exts = [r["extension"] for r in out["results"] if r.get("extension")]
    organisation.join_up(conn, cid)  # each phone site gets its location
    freeswitch.render_business(conn, cid)
db.close()
print(json.dumps({"OWNER_EMAIL": OWNER, "OWNER_PW": password, "HEAD_EXT": exts[0], "BRANCH_EXT": exts[1]}))
