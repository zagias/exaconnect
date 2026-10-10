"""Test accounts for the phone app's live test (lab/ci/checks/voice-phone-app.sh).

Three portal accounts in the demo business: two with a phone extension (A and B, who call
each other) and one without (C). Each run gives them fresh random passwords and clears their
sessions, do not disturb and forwarding, so nothing from an earlier run carries over and no
password is kept anywhere. Run inside the controller container: python - < live_users.py
Prints one line of JSON for the test's environment, on stdout only; never logged.
"""

import json
import secrets

from exaconnect_controller import db, seed
from exaconnect_controller.commai.voice import config, freeswitch
from exaconnect_controller.identity import orgs
from exaconnect_controller.security import hash_password
from exaconnect_controller.settings import get_settings

ACTOR = "system:phone-app-test"
PEOPLE = [
    ("A", "phone-app-test-a@exacarib.local", "Phone app test A", True),
    ("B", "phone-app-test-b@exacarib.local", "Phone app test B", True),
    ("C", "phone-app-test-c@exacarib.local", "Phone app test C", False),
]

settings = get_settings()
db.init(settings.database_url)
env: dict[str, str] = {}
with db.tx() as conn:
    biz = conn.execute("SELECT id FROM customers WHERE name = %s", (seed.CUSTOMER,)).fetchone()
    if biz is None:
        raise SystemExit("no demo business yet")
    cid = biz["id"]
    site = conn.execute(
        """SELECT name FROM voice_sites WHERE customer_id = %s
           ORDER BY emergency_status = 'registered' DESC, created_at LIMIT 1""",
        (cid,),
    ).fetchone()
    for tag, email, name, phone in PEOPLE:
        password = secrets.token_urlsafe(18)
        user = conn.execute("SELECT id FROM users WHERE lower(email) = lower(%s)", (email,)).fetchone()
        if user is None:
            user = conn.execute(
                """INSERT INTO users (email, password_hash, role, customer_id, display_name)
                   VALUES (%s, %s, 'customer', %s, %s) RETURNING id""",
                (email, hash_password(password), cid, name),
            ).fetchone()
        else:
            conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(password), user["id"]))
            conn.execute("DELETE FROM sessions WHERE user_id = %s", (user["id"],))
        orgs.add(conn, cid, user["id"], "member", ACTOR)
        env[f"{tag}_EMAIL"], env[f"{tag}_PW"] = email, password
        if not phone:
            continue
        have = conn.execute(
            "SELECT extension FROM voice_users WHERE customer_id = %s AND user_id = %s AND status = 'active'",
            (cid, user["id"]),
        ).fetchone()
        if have is None:
            op = {"op": "add_user", "name": name, **({"site": site["name"]} if site else {})}
            out = config.apply(conn, cid, [op], actor=ACTOR, summary=name, check_price=False)
            if not out["ok"]:
                raise SystemExit(f"could not add {name}: {out['errors']}")
            ext = out["results"][-1]["extension"]
            conn.execute(
                "UPDATE voice_users SET user_id = %s WHERE customer_id = %s AND extension = %s",
                (user["id"], cid, ext),
            )
            have = {"extension": ext}
        conn.execute(
            """UPDATE voice_users SET dnd = false, dnd_until = NULL, forward_to = ''
               WHERE customer_id = %s AND user_id = %s""",
            (cid, user["id"]),
        )
        env[f"{tag}_EXT"], env[f"{tag}_NAME"] = have["extension"], name
    freeswitch.render_business(conn, cid)
db.close()
print(json.dumps(env))
