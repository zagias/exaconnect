"""CommAI voice, phone system (ADR 0021): adds, moves and changes, price
impact and spend permission, scheduled changes, rollback, bulk CSV,
self-service limits, the "say what you want" parser and FreeSWITCH rendering."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai.voice import freeswitch, selfservice

from .commai_helpers import base, business, run_jobs

PEOPLE = ("boss", "lead", "ana", "ben")
TZ = ZoneInfo("America/Port_of_Spain")


def setup_voice(client, name="Voice Bank"):
    """boss: voice admin with spend; lead: voice admin without spend; ana, ben: staff."""
    b = business(client, name, PEOPLE)
    u = base(b)
    r = client.put(
        f"{u}/voice/permissions",
        json={
            "people": [
                {"user_id": b["boss"]["id"], "voice_admin": True, "spend": True},
                {"user_id": b["lead"]["id"], "voice_admin": True, "spend": False},
            ]
        },
        headers=b["boss"]["h"],
    )
    assert r.status_code == 200, r.text
    team = client.post(f"{u}/teams", json={"name": "Cards"}, headers=b["boss"]["h"])
    assert team.status_code == 201, team.text
    ops = [
        {
            "op": "add_site",
            "name": "Port of Spain",
            "address_line1": "1 Frederick Street",
            "city": "Port of Spain",
            "island": "Trinidad",
            "country": "TT",
        },
        {
            "op": "add_site",
            "name": "San Fernando",
            "address_line1": "5 High Street",
            "city": "San Fernando",
            "island": "Trinidad",
            "country": "TT",
        },
        {
            "op": "add_user",
            "name": "Ana Lee",
            "extension": "201",
            "site": "Port of Spain",
            "team": "Cards",
            "email": b["ana"]["email"],
            "mobile": "+1 868 555 0123",
            "portal_email": b["ana"]["email"],
        },
        {
            "op": "add_user",
            "name": "Ben Ali",
            "extension": "202",
            "site": "Port of Spain",
            "portal_email": b["ben"]["email"],
        },
    ]
    save(client, b, ops)
    return b


def save(client, b, ops, who="boss", **extra):
    u = base(b)
    pv = client.post(f"{u}/voice/preview", json={"ops": ops}, headers=b[who]["h"])
    assert pv.status_code == 200, pv.text
    assert pv.json()["ok"], pv.json()["errors"]
    price = pv.json()["price_impact"]
    r = client.post(
        f"{u}/voice/changes",
        json={
            "ops": ops,
            "accepted_price": {"monthly_delta": price["monthly_delta"], "one_time": price["one_time"]},
            **extra,
        },
        headers=b[who]["h"],
    )
    assert r.status_code == 201, r.text
    return r.json()


def overview(client, b, who="boss"):
    r = client.get(f"{base(b)}/voice", headers=b[who]["h"])
    assert r.status_code == 200, r.text
    return r.json()


def test_add_user_shows_price_and_needs_spend_permission(client):
    b = setup_voice(client)
    u = base(b)
    ops = [{"op": "add_user", "name": "Cara Jones", "extension": "203", "site": "San Fernando"}]
    pv = client.post(f"{u}/voice/preview", json={"ops": ops}, headers=b["lead"]["h"]).json()
    impact = pv["price_impact"]
    assert impact["changes_bill"] and impact["monthly_delta"] == "12.00" and impact["example_prices"]
    assert pv["diff"] == [{"area": "users", "change": "added", "item": "Cara Jones (ext 203)"}]
    # Not saved by the preview.
    assert "203" not in [x["extension"] for x in overview(client, b)["users"]]

    accepted = {"monthly_delta": "12.00", "one_time": "0.00"}
    # A voice admin without spend permission can't save a change to the bill...
    r = client.post(f"{u}/voice/changes", json={"ops": ops, "accepted_price": accepted}, headers=b["lead"]["h"])
    assert r.status_code == 403 and "spend permission" in r.json()["detail"]
    # ...someone with it must have seen the price...
    r = client.post(f"{u}/voice/changes", json={"ops": ops}, headers=b["boss"]["h"])
    assert r.status_code == 409 and "12.00" in r.json()["detail"]
    r = client.post(
        f"{u}/voice/changes",
        json={"ops": ops, "accepted_price": {"monthly_delta": "1.00", "one_time": "0.00"}},
        headers=b["boss"]["h"],
    )
    assert r.status_code == 409
    # ...and then it saves.
    r = client.post(f"{u}/voice/changes", json={"ops": ops, "accepted_price": accepted}, headers=b["boss"]["h"])
    assert r.status_code == 201, r.text
    # Staff can't change the phone system at all.
    r = client.post(f"{u}/voice/changes", json={"ops": ops, "accepted_price": accepted}, headers=b["ana"]["h"])
    assert r.status_code == 403
    # A change that doesn't touch the bill needs no spend permission.
    r = client.post(
        f"{u}/voice/changes",
        json={"ops": [{"op": "save_ring_group", "name": "Sales", "extension": "300", "members": ["201", "202"]}]},
        headers=b["lead"]["h"],
    )
    assert r.status_code == 201, r.text
    with db.tx() as conn:
        acts = [
            r["action"]
            for r in conn.execute("SELECT action FROM audit_log WHERE customer_id = %s", (b["id"],)).fetchall()
        ]
    assert acts.count("commai.voice.change") >= 3


def test_move_updates_emergency_address_of_user_and_numbers(client):
    b = setup_voice(client)
    out = save(client, b, [{"op": "add_number", "target_type": "user", "target": "201"}])
    e164 = out["results"][0]["e164"]
    assert e164.startswith("+186855501")
    out = save(client, b, [{"op": "move_user", "user": "201", "site": "San Fernando"}])
    moved = out["results"][0]
    assert moved["emergency_address"]["address_line1"] == "5 High Street"
    assert moved["numbers_updated"] == [e164]
    ov = overview(client, b)
    ana = next(x for x in ov["users"] if x["extension"] == "201")
    assert ana["site"] == "San Fernando" and ana["emergency_address"]["city"] == "San Fernando"
    num = next(n for n in ov["numbers"] if n["e164"] == e164)
    assert num["emergency_address"]["address_line1"] == "5 High Street"
    assert next(s for s in ov["sites"] if s["name"] == "San Fernando")["emergency_status"] == "pending"


def test_scheduled_change_runs_at_its_time(client):
    b = setup_voice(client)
    u = base(b)
    ops = [{"op": "save_hours", "name": "Office", "schedule": {"mon": [["08:00", "17:00"]]}}]
    run_at = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)).isoformat()
    r = client.post(f"{u}/voice/changes", json={"ops": ops, "run_at": run_at}, headers=b["lead"]["h"])
    assert r.status_code == 201, r.text
    cid = r.json()["change"]["id"]
    run_jobs()  # not due yet
    ov = overview(client, b)
    assert ov["hours"] == [] and ov["scheduled"][0]["status"] == "scheduled"
    with db.tx() as conn:
        conn.execute("UPDATE voice_changes SET run_at = now() - interval '1 second' WHERE id = %s", (cid,))
    run_jobs()
    ov = overview(client, b)
    assert [h["name"] for h in ov["hours"]] == ["Office"]
    sched = ov["scheduled"][0]
    assert sched["status"] == "applied" and sched["version"] == ov["version"]


def test_rollback_restores_routing(client):
    b = setup_voice(client)
    u = base(b)
    v1 = save(
        client,
        b,
        [
            {
                "op": "save_ring_group",
                "name": "Sales",
                "extension": "300",
                "members": ["201", "202"],
                "strategy": "simultaneous",
            }
        ],
    )["version"]
    group = overview(client, b)["ring_groups"][0]
    save(
        client,
        b,
        [
            {
                "op": "save_ring_group",
                "id": group["id"],
                "name": "Sales",
                "extension": "300",
                "members": ["202"],
                "strategy": "sequential",
            },
            {"op": "save_queue", "name": "Help", "extension": "400", "members": ["201"]},
        ],
    )
    ov = overview(client, b)
    assert ov["ring_groups"][0]["strategy"] == "sequential" and len(ov["queues"]) == 1

    pv = client.post(f"{u}/voice/versions/{v1}/rollback", json={"preview": True}, headers=b["lead"]["h"]).json()
    assert pv["version"] is None and {d["area"] for d in pv["diff"]} == {"ring_groups", "queues"}
    assert overview(client, b)["ring_groups"][0]["strategy"] == "sequential"  # preview changed nothing

    r = client.post(f"{u}/voice/versions/{v1}/rollback", json={}, headers=b["lead"]["h"])
    assert r.status_code == 200, r.text
    ov = overview(client, b)
    assert ov["ring_groups"][0]["id"] == group["id"] and ov["ring_groups"][0]["strategy"] == "simultaneous"
    assert len(ov["ring_groups"][0]["members"]) == 2 and ov["queues"] == []
    hist = client.get(f"{u}/voice/versions", headers=b["boss"]["h"]).json()
    assert hist[0]["kind"] == "rollback" and hist[0]["version"] == r.json()["version"]
    detail = client.get(f"{u}/voice/versions/{hist[0]['version']}", headers=b["boss"]["h"]).json()
    assert any(d["area"] == "queues" and d["change"] == "removed" for d in detail["diff"])


def test_bulk_csv_validated_row_by_row_before_anything_applies(client):
    b = setup_voice(client)
    u = base(b)
    bad = (
        "action,name,extension,site,team,mac,kind,email\n"
        "add_user,Dee,210,Port of Spain,Cards,,,\n"
        "add_user,Eve,201,Port of Spain,,,,\n"  # extension taken
        "add_user,Fay,211,Nowhere,,,,\n"  # no such site
        "move_user,,202,San Fernando,,,,\n"
        "launch_rocket,,,,,,,\n"  # unknown action
        "add_device,,202,,,zz,desk,\n"  # bad MAC
    )
    r = client.post(f"{u}/voice/bulk", json={"csv": bad, "apply": True}, headers=b["boss"]["h"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert not out["applied"]
    assert [e["row"] for e in out["errors"]] == [3, 4, 6, 7]
    assert "already in use" in out["errors"][0]["error"]
    exts = [x["extension"] for x in overview(client, b)["users"]]
    assert "210" not in exts  # the good rows did not apply either

    good = (
        "action,name,extension,site,team,mac,kind\n"
        "add_user,Dee,210,Port of Spain,Cards,,\n"
        "move_user,,202,San Fernando,,,\n"
        "add_device,,210,,,00:15:65:aa:bb:cc,desk\n"
    )
    check = client.post(f"{u}/voice/bulk", json={"csv": good}, headers=b["boss"]["h"]).json()
    assert check["errors"] == [] and not check["applied"]
    price = check["price_impact"]
    assert price["monthly_delta"] == "12.00" and price["one_time"] == "25.00"
    r = client.post(
        f"{u}/voice/bulk",
        json={
            "csv": good,
            "apply": True,
            "accepted_price": {"monthly_delta": price["monthly_delta"], "one_time": price["one_time"]},
        },
        headers=b["boss"]["h"],
    )
    assert r.status_code == 200 and r.json()["applied"], r.text
    link = next(x for x in r.json()["results"] if x.get("setup_url"))
    assert link["setup_url"].endswith("/001565aabbcc.cfg")
    ov = overview(client, b)
    assert "210" in [x["extension"] for x in ov["users"]]
    assert next(x for x in ov["users"] if x["extension"] == "202")["site"] == "San Fernando"

    # The desk phone loads its file with its token and MAC; anything else is 404.
    path = link["setup_url"].split("testserver")[1]
    cfg = client.get(path)
    assert cfg.status_code == 200 and "account.1.user_name = 210" in cfg.text
    assert client.get(path.replace("001565aabbcc", "001565aabbcd")).status_code == 404
    assert client.get(path.replace(path.split("/")[-2], "x" * 32)).status_code == 404


def test_staff_change_only_their_own_settings(client):
    b = setup_voice(client)
    u = base(b)
    later = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=3)).isoformat()
    r = client.patch(
        f"{u}/voice/me", json={"forward_to": "+1 868 555 0199", "forward_until": later}, headers=b["ana"]["h"]
    )
    assert r.status_code == 200, r.text
    assert r.json()["forward_to"] == "+18685550199" and r.json()["extension"] == "201"
    # Ben's own request only ever touches Ben; there is no way to name Ana.
    r = client.patch(f"{u}/voice/me", json={"dnd": True}, headers=b["ben"]["h"])
    assert r.json()["extension"] == "202" and r.json()["dnd"]
    me = client.get(f"{u}/voice/me", headers=b["ana"]["h"]).json()
    assert not me["dnd"] and me["forward_to"] == "+18685550199"
    # Fields beyond self-service are refused...
    r = client.patch(f"{u}/voice/me", json={"extension": "999"}, headers=b["ben"]["h"])
    assert client.get(f"{u}/voice/me", headers=b["ben"]["h"]).json()["extension"] == "202"
    # ...and staff can't use the admin endpoints to reach someone else.
    move = [{"op": "update_user", "user": "201", "mobile": "+1 868 555 0100"}]
    assert client.post(f"{u}/voice/changes", json={"ops": move}, headers=b["ben"]["h"]).status_code == 403
    assert client.post(f"{u}/voice/preview", json={"ops": move}, headers=b["ben"]["h"]).status_code == 403
    assert "users" not in client.get(f"{u}/voice", headers=b["ben"]["h"]).json()
    # Forwarding to a blocked premium number is refused.
    r = client.patch(f"{u}/voice/me", json={"forward_to": "+1 900 555 0100"}, headers=b["ben"]["h"])
    assert r.status_code == 422 and "blocked" in r.json()["detail"]
    # Voicemail to email needs an email on the extension (Ben has none).
    r = client.patch(f"{u}/voice/me", json={"voicemail_to_email": True}, headers=b["ben"]["h"])
    assert r.status_code == 422
    r = client.patch(
        f"{u}/voice/me", json={"voicemail_to_email": True, "voicemail_greeting": "Ana here."}, headers=b["ana"]["h"]
    )
    assert r.json()["voicemail_to_email"] and r.json()["voicemail_greeting"] == "Ana here."
    with db.tx() as conn:
        row = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'commai.voice.self' ORDER BY id LIMIT 1"
        ).fetchone()
    assert "+18685550199" not in str(row["detail"])  # personal numbers stay out of the audit detail

    # Forwarding "until" ends at that time.
    with db.tx() as conn:
        conn.execute(
            "UPDATE voice_users SET forward_until = now() - interval '1 second' WHERE extension = '201'"
            " AND customer_id = %s",
            (b["id"],),
        )
    run_jobs()
    assert client.get(f"{u}/voice/me", headers=b["ana"]["h"]).json()["forward_to"] == ""


def test_recordings_follow_company_policy(client):
    b = setup_voice(client)
    u = base(b)
    assert client.get(f"{u}/voice/me/recordings", headers=b["ana"]["h"]).status_code == 200  # default: own
    r = client.put(f"{u}/voice/policy", json={"recording_access": "none"}, headers=b["boss"]["h"])
    assert r.status_code == 200
    r = client.get(f"{u}/voice/me/recordings", headers=b["ana"]["h"])
    assert r.status_code == 403 and "policy" in r.json()["detail"]
    assert client.put(f"{u}/voice/policy", json={"recording_access": "own"}, headers=b["ana"]["h"]).status_code == 403


AT = dt.datetime(2026, 10, 6, 10, 15, tzinfo=TZ)


@pytest.mark.parametrize(
    "text,changes,summary",
    [
        (
            "forward my calls to my mobile until 5",
            {"forward_to": "+18685550123", "forward_until": "2026-10-06T17:00:00-04:00"},
            "Forward your calls to your mobile (+18685550123) until 5:00 pm today.",
        ),
        (
            "Forward calls to +1 868 555 0199 till 9am tomorrow",
            {"forward_to": "+18685550199", "forward_until": "2026-10-07T09:00:00-04:00"},
            None,
        ),
        (
            "forward my calls to extension 202 for 2 hours",
            {"forward_to": "202", "forward_until": "2026-10-06T12:15:00-04:00"},
            None,
        ),
        ("stop forwarding my calls", {"forward_to": ""}, "Stop forwarding your calls."),
        ("turn on do not disturb until 3pm", {"dnd": True, "dnd_until": "2026-10-06T15:00:00-04:00"}, None),
        ("turn off dnd", {"dnd": False}, None),
        ("send my voicemail to email", {"voicemail_to_email": True}, None),
        ("stop emailing my voicemails", {"voicemail_to_email": False}, None),
        ('Set my greeting to "Ana is away until Monday"', {"voicemail_greeting": "Ana is away until Monday"}, None),
    ],
)
def test_say_parser(client, text, changes, summary):
    b = setup_voice(client)
    with db.tx() as conn:
        me = selfservice.mine(conn, b["id"], b["ana"]["id"])
        out = selfservice.parse(conn, b["id"], text, me, False, at=AT)
    assert out["understood"], out
    assert out["scope"] == "self" and out["changes"] == changes
    if summary:
        assert out["summary"] == summary


def test_say_needs_confirmation_and_stays_in_bounds(client):
    b = setup_voice(client)
    u = base(b)
    r = client.post(f"{u}/voice/say", json={"text": "forward my calls to my mobile until 5"}, headers=b["ana"]["h"])
    assert r.status_code == 200 and r.json()["understood"], r.text
    prop = r.json()
    assert prop["change"]["forward_to"] == "+18685550123" and "until" in prop["summary"]
    # Nothing has changed yet.
    assert client.get(f"{u}/voice/me", headers=b["ana"]["h"]).json()["forward_to"] == ""
    # Only the person who asked can confirm it.
    assert client.post(f"{u}/voice/say/{prop['id']}/confirm", json={}, headers=b["ben"]["h"]).status_code == 404
    r = client.post(f"{u}/voice/say/{prop['id']}/confirm", json={}, headers=b["ana"]["h"])
    assert r.status_code == 200, r.text
    assert client.get(f"{u}/voice/me", headers=b["ana"]["h"]).json()["forward_to"] == "+18685550123"
    assert client.post(f"{u}/voice/say/{prop['id']}/confirm", json={}, headers=b["ana"]["h"]).status_code == 409

    # Staff can't move people by chat; admins can, and it's still only a proposal.
    r = client.post(f"{u}/voice/say", json={"text": "move Ben to San Fernando"}, headers=b["ana"]["h"]).json()
    assert not r["understood"] and "voice admins" in r["message"]
    r = client.post(f"{u}/voice/say", json={"text": "move Ben to San Fernando"}, headers=b["lead"]["h"]).json()
    assert r["understood"] and r["scope"] == "admin" and "emergency address" in r["summary"]
    assert next(x for x in overview(client, b)["users"] if x["extension"] == "202")["site"] == "Port of Spain"
    c = client.post(f"{u}/voice/say/{r['id']}/confirm", json={}, headers=b["lead"]["h"])
    assert c.status_code == 200, c.text
    assert next(x for x in overview(client, b)["users"] if x["extension"] == "202")["site"] == "San Fernando"

    r = client.post(f"{u}/voice/say", json={"text": "order me a pizza"}, headers=b["ana"]["h"]).json()
    assert not r["understood"] and "didn't understand" in r["message"]


def _render(client, b):
    with db.tx() as conn:
        return freeswitch.render(freeswitch.gather(conn, b["id"]))


def test_freeswitch_render_is_deterministic_and_complete(client, tmp_path):
    b = setup_voice(client)
    save(
        client,
        b,
        [
            {
                "op": "save_hours",
                "name": "Office",
                "schedule": {"mon": [["08:00", "12:00"], ["13:00", "17:00"]], "fri": [["08:00", "16:00"]]},
                "holidays": ["2026-12-25"],
            },
            {
                "op": "save_ring_group",
                "name": "Sales",
                "extension": "300",
                "members": ["201", "202"],
                "strategy": "sequential",
                "ring_seconds": 15,
            },
            {"op": "save_queue", "name": "Help", "extension": "400", "members": ["202", "201"]},
            {
                "op": "save_menu",
                "name": "Main",
                "extension": "500",
                "greeting": "Welcome to Voice Bank & Co.",
                "options": {"1": {"type": "ring_group", "id": "300"}, "2": {"type": "queue", "id": "400"}},
                "hours": "Office",
                "closed_target": {"type": "ai"},
            },
            {"op": "add_number", "target_type": "menu", "target": "500"},
            {
                "op": "save_ai_rule",
                "name": "Nights",
                "condition": "after_hours",
                "hours": "Office",
                "fallback": "queue",
            },
        ],
    )
    client.patch(f"{base(b)}/voice/me", json={"dnd": True}, headers=b["ben"]["h"])
    files = _render(client, b)
    assert files == _render(client, b)  # same data, same bytes
    tenant = next(iter(files)).split("/")[0]
    assert sorted(f.split("/")[1] for f in files) == [
        "agents.xml",
        "dialplan.xml",
        "directory.xml",
        "ivr.xml",
        "public.xml",
        "queues.xml",
        "tiers.xml",
    ]
    d = files[f"{tenant}/directory.xml"]
    assert '<user id="201">' in d and 'name="a1-hash"' in d and "sip_password" not in d
    with db.tx() as conn:
        pw = conn.execute(
            "SELECT sip_password FROM voice_users WHERE extension = '201' AND customer_id = %s", (b["id"],)
        ).fetchone()["sip_password"]
    assert pw not in "".join(files.values())  # no SIP password in any file
    plan = files[f"{tenant}/dialplan.xml"]
    assert "[leg_timeout=15]user/201@" in plan and "|[leg_timeout=15]user/202@" in plan  # sequential
    assert 'wday="2" time-of-day="08:00-12:00"' in plan and 'wday="6" time-of-day="08:00-16:00"' in plan
    assert "2026-12-25" in plan
    assert 'expression="^(112|211|811|911|990|999)$"' in plan  # with Trinidad's 811 (ADR 0027)  # emergency first
    assert plan.index("emergency") < plan.index("blocked-destinations") < plan.index("user-201")
    assert "1900" in plan  # blocked premium prefixes are refused in the dial plan
    # Ben is on do not disturb: no bridge to his phone, straight to voicemail.
    ben = plan.split('name="user-202"')[1].split("</extension>")[0]
    assert 'application="bridge"' not in ben and "voicemail" in ben
    # The AI agent always has a fallback, so calls never depend on the AI service.
    ai = plan.split('name="exa-ai"')[1].split("</extension>")[0]
    assert "exacarib_ai" in ai and 'application="transfer" data="400 XML' in ai
    assert "&amp;" in files[f"{tenant}/ivr.xml"] and 'digits="1"' in files[f"{tenant}/ivr.xml"]
    assert "callcenter" in plan and "<tier agent=" in files[f"{tenant}/tiers.xml"]
    pub = files[f"{tenant}/public.xml"]
    assert "^\\+?18685550" in pub and f"XML {tenant}" in pub

    with db.tx() as conn:
        out = freeswitch.render_business(conn, b["id"], root=str(tmp_path))
    assert (tmp_path / tenant / "dialplan.xml").read_text() == plan and out["written_to"] == str(tmp_path)
    # The render job runs after every change.
    run_jobs()
    with db.tx() as conn:
        row = conn.execute("SELECT digest FROM voice_pbx_renders WHERE customer_id = %s", (b["id"],)).fetchone()
    assert row["digest"] == freeswitch.digest(files)


def test_read_endpoints_and_who_may_use_them(client, admin_headers):
    b = setup_voice(client)
    u = base(b)
    for path in (
        "/voice",
        "/voice/versions",
        "/voice/orders",
        "/voice/ports",
        "/voice/pbx",
        "/voice/spend",
        "/voice/fraud-limits",
        "/voice/invoices",
        "/voice/calls",
        "/voice/rate-cards",
        "/voice/permissions",
        "/voice/bulk/template",
    ):
        r = client.get(u + path, headers=b["boss"]["h"])
        assert r.status_code == 200, (path, r.text)
        assert client.get(u + path, headers=admin_headers).status_code == 200, path
    pbx = client.get(f"{u}/voice/pbx", headers=b["boss"]["h"]).json()
    assert pbx["live"] is False and any(f.endswith("dialplan.xml") for f in pbx["files"])
    for path in (
        "/voice/versions",
        "/voice/orders",
        "/voice/pbx",
        "/voice/spend",
        "/voice/invoices",
        "/voice/permissions",
    ):
        assert client.get(u + path, headers=b["ana"]["h"]).status_code == 403, path
    # Staff can't name voice admins; a voice admin without spend can't hand out spend permission.
    people = {"people": [{"user_id": b["lead"]["id"], "voice_admin": True, "spend": True}]}
    assert client.put(f"{u}/voice/permissions", json=people, headers=b["ana"]["h"]).status_code == 403
    assert client.put(f"{u}/voice/permissions", json=people, headers=b["lead"]["h"]).status_code == 403
    # Another business never sees this one's phone system.
    other = business(client, "Other Shop", ("owner",))
    assert client.get(f"{u}/voice", headers=other["owner"]["h"]).status_code == 403
    assert client.get(f"{base(other)}/voice", headers=other["owner"]["h"]).json()["is_admin"] is True  # bootstrap
