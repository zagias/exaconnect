"""A test phone extension for ExaCarib's own admin (make voice-up).

Gives the first admin account (EXA_ADMIN_EMAIL) a phone extension in the demo
business, so they can sign in to the browser phone and try calls between
extensions, menus, queues, voicemail and the AI agent. The example "Test
office" site carries a placeholder address and is marked as example data:
emergency calls are not registered for it. Safe to run again; changes nothing
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
        have = conn.execute(
            "SELECT extension FROM voice_users WHERE customer_id = %s AND user_id = %s AND status = 'active'",
            (cid, admin["id"]),
        ).fetchone()
        if have:
            print(f"test phone: extension {have['extension']} already set up")
        else:
            ops = []
            site = conn.execute("SELECT 1 FROM voice_sites WHERE customer_id = %s AND name = %s", (cid, SITE))
            if not site.fetchone():
                ops.append({"op": "add_site", "name": SITE, "address_line1": "Example data, not a real address"})
            ops.append({"op": "add_user", "name": "ExaCarib test phone", "site": SITE})
            out = config.apply(conn, cid, ops, actor=ACTOR, summary="Admin test phone", check_price=False)
            if not out["ok"]:
                raise SystemExit(f"test phone: {out['errors']}")
            ext = out["results"][-1]["extension"]
            conn.execute(
                "UPDATE voice_users SET user_id = %s WHERE customer_id = %s AND extension = %s",
                (admin["id"], cid, ext),
            )
            freeswitch.render_business(conn, cid)
            print(f"test phone: extension {ext} set up for the admin account")
db.close()
