"""Test organisation for the calling platforms live test (lab/ci/checks/voice-platforms.sh,
ADR 0056).

Removes what the last run made, then sets up "Platform check organisation" (Jibsy plan only)
with an owner account, a phone site (Head office) and the owner's own extension, so the owner can
sign in to the browser phone, and two numbers on a simulated trunk of its own (free) to give
people on Teams and Zoom. With CLEAN_ONLY=1 it only removes, for the end of the test. The owner
gets a fresh random password and no sessions. Run inside the controller container:
python - < platform_users.py. Prints one line of JSON for the test's environment, on stdout only;
never logged.
"""

import json
import os
import secrets

import psycopg
from exaconnect_controller import db, organisation
from exaconnect_controller.commai.voice import carriers, config, freeswitch, trunks
from exaconnect_controller.identity import orgs
from exaconnect_controller.security import hash_password
from exaconnect_controller.settings import get_settings

ACTOR = "system:platform-test"
ORG = "Platform check organisation"
OWNER = "platform-test@exacarib.local"
NUMBERS = ("+18685559450", "+18685559451")


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
        carriers.render_kamailio(conn)  # its links leave Kamailio's lists
if os.environ.get("CLEAN_ONLY"):
    db.close()
    print(json.dumps({"removed": bool(old)}))
    raise SystemExit(0)
with db.tx() as conn:
    cid = conn.execute(
        "INSERT INTO customers (name, products, country) VALUES (%s, '{commai}', 'TT') RETURNING id", (ORG,)
    ).fetchone()["id"]
    password = secrets.token_urlsafe(18)
    user = conn.execute("SELECT id FROM users WHERE lower(email) = %s", (OWNER,)).fetchone()
    if user is None:
        user = conn.execute(
            """INSERT INTO users (email, password_hash, role, customer_id, display_name)
               VALUES (%s, %s, 'customer', %s, 'Platform test owner') RETURNING id""",
            (OWNER, hash_password(password), cid),
        ).fetchone()
    else:
        conn.execute(
            "UPDATE users SET password_hash = %s, customer_id = %s WHERE id = %s",
            (hash_password(password), cid, user["id"]),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (user["id"],))
    orgs.add(conn, cid, user["id"], "owner", ACTOR)
    ops = [
        {
            "op": "add_site",
            "name": "Head office",
            "address_line1": "1 Frederick Street",
            "city": "Port of Spain",
            "island": "Trinidad",
            "country": "TT",
        },
        {"op": "add_user", "name": "Platform test owner", "site": "Head office", "portal_email": OWNER},
    ]
    out = config.apply(conn, cid, ops, actor=ACTOR, summary="Platform test", check_price=False)
    if not out["ok"]:
        raise SystemExit(f"could not set up the test organisation: {out['errors']}")
    ext = next(r["extension"] for r in out["results"] if r.get("extension"))
    organisation.join_up(conn, cid)
    loc = conn.execute("SELECT id FROM org_locations WHERE customer_id = %s", (cid,)).fetchone()
    t = trunks.save_trunk(conn, cid, {"name": "Numbers for the test", "adapter": "simulated"}, ACTOR)
    for n in NUMBERS:
        trunks.add_number(conn, cid, t["id"], {"e164": n, "location_id": str(loc["id"])}, ACTOR)
    freeswitch.render_business(conn, cid)
db.close()
print(json.dumps({"OWNER_EMAIL": OWNER, "OWNER_PW": password, "OWNER_EXT": ext, "NUMBER": NUMBERS[0]}))
