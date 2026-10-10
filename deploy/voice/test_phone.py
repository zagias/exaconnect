"""A test phone extension for ExaCarib's own admin (make voice-up).

Gives the first admin account (EXA_ADMIN_EMAIL) a phone extension in the demo
business, so they can sign in to the browser phone and try calls between
extensions, menus, queues, voicemail and the AI agent. It sits on a site whose
emergency address is already accepted; only when there is none does it add a
placeholder "Test office" (marked as example data), removed again once a
checked site exists. Safe to run again; changes nothing
once the extension exists. Its SIP password is generated and stays in the
database, shown only to the admin when they sign in to the browser phone.
"""

from exaconnect_controller import db, seed
from exaconnect_controller.commai.voice import config, freeswitch
from exaconnect_controller.settings import get_settings

ACTOR = "system:voice-test-phone"
SITE = "Test office"

settings = get_settings()
db.init(settings.database_url)
with db.tx() as conn:
    admin = conn.execute("SELECT id FROM users WHERE lower(email) = lower(%s)", (settings.admin_email,)).fetchone()
    biz = conn.execute("SELECT id FROM customers WHERE name = %s", (seed.CUSTOMER,)).fetchone()
    if admin is None or biz is None:
        print("test phone: no admin account or demo business yet, skipped")
    else:
        cid = biz["id"]
        # The test phone sits on a site whose emergency address is already accepted, so it
        # raises no address notices; a placeholder "Test office" only when there is none.
        home = conn.execute(
            """SELECT name FROM voice_sites WHERE customer_id = %s AND name <> %s AND emergency_status = 'registered'
               ORDER BY created_at LIMIT 1""",
            (cid, SITE),
        ).fetchone()
        site = home["name"] if home else SITE
        have = conn.execute(
            """SELECT v.id, v.extension, s.name AS site FROM voice_users v LEFT JOIN voice_sites s ON s.id = v.site_id
               WHERE v.customer_id = %s AND v.user_id = %s AND v.status = 'active'""",
            (cid, admin["id"]),
        ).fetchone()
        if have:
            if home and have["site"] == SITE:
                out = config.apply(
                    conn,
                    cid,
                    [{"op": "move_user", "user": str(have["id"]), "site": site}],
                    actor=ACTOR,
                    summary="Admin test phone to a checked site",
                    check_price=False,
                )
                if not out["ok"]:
                    raise SystemExit(f"test phone: {out['errors']}")
                print(f"test phone: extension {have['extension']} moved to {site}")
            else:
                print(f"test phone: extension {have['extension']} already set up")
        else:
            ops = []
            if (
                not home
                and not conn.execute(
                    "SELECT 1 FROM voice_sites WHERE customer_id = %s AND name = %s", (cid, SITE)
                ).fetchone()
            ):
                ops.append(
                    {
                        "op": "add_site",
                        "name": SITE,
                        "address_line1": "Example data, not a real address",
                        "city": "Port of Spain",
                    }
                )
            ops.append({"op": "add_user", "name": "ExaCarib test phone", "site": site})
            out = config.apply(conn, cid, ops, actor=ACTOR, summary="Admin test phone", check_price=False)
            if not out["ok"]:
                raise SystemExit(f"test phone: {out['errors']}")
            ext = out["results"][-1]["extension"]
            conn.execute(
                "UPDATE voice_users SET user_id = %s WHERE customer_id = %s AND extension = %s",
                (admin["id"], cid, ext),
            )
            print(f"test phone: extension {ext} set up for the admin account")
        # The placeholder site goes once nothing uses it.
        if home:
            conn.execute(
                """DELETE FROM voice_sites s WHERE s.customer_id = %s AND s.name = %s
                     AND NOT EXISTS (SELECT 1 FROM voice_users v WHERE v.site_id = s.id AND v.status <> 'removed')
                     AND NOT EXISTS (SELECT 1 FROM voice_numbers n WHERE n.site_id = s.id AND n.status <> 'removed')""",
                (cid, SITE),
            )
        freeswitch.render_business(conn, cid)
db.close()
