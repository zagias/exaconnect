# ruff: noqa: F811  (pytest fixtures imported from commai_connector_kit)
"""CalDAV and CardDAV (ADR 0034), with iCalendar and vCard: the stand-in, a
mock DAV server for the real path, idempotent writes, ETags, sync tokens,
rate limits, expired sign-in and tenant isolation."""

from __future__ import annotations

import datetime as dt

from exaconnect_controller import db
from exaconnect_controller.commai import actions, connectors
from exaconnect_controller.commai.automation import integrations
from exaconnect_controller.commai.connectors import kit
from exaconnect_controller.commai.connectors.dav_common import DavStandIn
from exaconnect_controller.commai.standards import ical, vcard

from .commai_connector_kit import (  # noqa: F401
    connect_real,
    connect_simulated,
    execute_direct,
    fake,
    go_live,
    propose_and_run,
    real_env,
)
from .commai_helpers import base, business

TOMORROW = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).date()


def slot(hour: int) -> str:
    return dt.datetime.combine(TOMORROW, dt.time(hour), tzinfo=dt.UTC).isoformat()


# ---- the standards themselves ----------------------------------------------------------


def test_icalendar_write_fold_and_read_back():
    start = dt.datetime(2026, 10, 8, 14, 0, tzinfo=dt.UTC)
    ev = ical.Event(
        uid="u1",
        start=start,
        end=start + dt.timedelta(minutes=30),
        summary="Check-up; Ms Brown, room 2",
        description="Line one\nLine two " + "x" * 120,
        attendees=[("brown@example.com", "Ann Brown")],
    )
    text = ical.invite(ev)
    assert "METHOD:REQUEST" in text and "\r\n " in text  # folded
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    assert "SUMMARY:Check-up\\; Ms Brown\\, room 2" in text
    back = ical.events(text)
    assert back[0]["uid"] == "u1" and back[0]["start"] == start
    assert back[0]["summary"] == "Check-up; Ms Brown, room 2"
    root = ical.parse(text)
    assert next(root.walk("VEVENT")).text("DESCRIPTION").startswith("Line one\nLine two")
    # Local time with a TZID, an all-day event, a duration and free/busy periods.
    other = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a\r\nDTSTART;TZID=America/Port_of_Spain:20261008T090000\r\n"
        "DURATION:PT45M\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:b\r\nDTSTART;VALUE=DATE:20261009\r\n"
        "TRANSP:TRANSPARENT\r\nEND:VEVENT\r\nBEGIN:VFREEBUSY\r\n"
        "FREEBUSY;FBTYPE=BUSY:20261010T100000Z/PT1H,20261010T140000Z/20261010T150000Z\r\nEND:VFREEBUSY\r\n"
        "END:VCALENDAR\r\n"
    )
    evs = ical.events(other)
    assert evs[0]["start"] == dt.datetime(2026, 10, 8, 13, 0, tzinfo=dt.UTC)
    assert evs[0]["end"] - evs[0]["start"] == dt.timedelta(minutes=45)
    busy = ical.busy_periods(other)
    assert len(busy) == 3  # the transparent all-day event is free time
    cancel = ical.cancellation(ev)
    assert "METHOD:CANCEL" in cancel and "STATUS:CANCELLED" in cancel and "SEQUENCE:1" in cancel


def test_vcard_round_trip_and_version_3():
    text = vcard.write(vcard.Card(uid="c1", name="Ann Brown", emails=["ann@example.com"], phones=["+1 868 555 0100"]))
    assert "VERSION:4.0" in text and "TEL;VALUE=uri;PREF=1:tel:+18685550100" in text
    card = vcard.read(text)[0]
    assert (card.name, card.email, card.phone) == ("Ann Brown", "ann@example.com", "+18685550100")
    v3 = (
        "BEGIN:VCARD\nVERSION:3.0\nN:Lee;Sam;;;\nEMAIL;TYPE=INTERNET:sam@example.com\n"
        "TEL;TYPE=CELL:+12465550101\nEND:VCARD\n"
    )
    card = vcard.read(v3)[0]
    assert card.name == "Sam Lee" and card.phone == "+12465550101"
    try:
        vcard.read("hello")
        raise AssertionError("not a vCard")
    except ValueError:
        pass


# ---- CalDAV on the stand-in ------------------------------------------------------------


def _utc(cid: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO commai_settings (customer_id, timezone) VALUES (%s, 'UTC')"
            " ON CONFLICT (customer_id) DO UPDATE SET timezone = 'UTC'",
            (cid,),
        )


def test_caldav_runs_on_stand_in_until_switched_on(client):
    b = business(client, people=("agent", "agent2"))
    _utc(b["id"])
    u, h = base(b), b["agent"]["h"]
    cat = client.get(f"{u}/integrations", headers=h).json()
    cal = next(x for x in cat if x["app"] == "caldav")
    assert cal["category"] == "calendar" and cal["simulated"] and not cal["sign_in_ready"]
    assert "go-live" in cal["not_live_reason"] and cal["golive_key"] == "integration-caldav"
    assert [a["name"] for a in cal["actions"] if a["kind"] == "read"] == ["find_slots"]

    r = client.post(f"{u}/integrations/caldav/connect", headers=h)
    assert r.status_code == 200 and r.json()["connection"]["auth_method"] == "simulated"
    client.put(f"{u}/integrations/caldav/actions", json={"actions": ["find_slots", "book", "cancel"]}, headers=h)
    t = client.post(f"{u}/integrations/caldav/test", headers=h).json()
    assert t["test"]["ok"], t
    assert _slot_in(t["test"]["results"][0]["result"]["slots"], 10)
    with db.tx() as conn:
        conn.execute("UPDATE integration_connections SET status = 'live' WHERE app = 'caldav'")

    run = propose_and_run(b["id"], "caldav", "book", {"start": slot(10), "name": "Ann", "contact": "a@x.org"}, "k-book")
    assert run["status"] == "succeeded", run["error"]
    uid = run["result"]["booking_id"]
    # A retry with the same key finds the first booking: never booked twice.
    again = execute_direct(
        b["id"], "caldav", "book", {"start": slot(10), "name": "Ann", "contact": "a@x.org"}, "k-book"
    )
    assert again["booking_id"] == uid and again.get("replayed")
    # The booked time is no longer offered; another booking there is refused.
    free = execute_direct(b["id"], "caldav", "find_slots", {"date": TOMORROW.isoformat()}, "k-free")
    assert not _slot_in(free["slots"], 10) and _slot_in(free["slots"], 11)
    clash = propose_and_run(b["id"], "caldav", "book", {"start": slot(10), "name": "Bo", "contact": "b@x.org"}, "k-2")
    assert clash["status"] == "failed" and "(input)" in clash["error"]
    # Cancelling is sensitive: a different person approves.
    run = propose_and_run(b["id"], "caldav", "cancel", {"booking_id": uid}, "k-cancel", approve_as="user:approver")
    assert run["status"] == "succeeded" and run["result"]["cancelled"]

    # Rehearse a broken stand-in: expired sign-in shows the repair cause.
    client.put(
        f"{u}/integrations/caldav/settings", json={"settings": {"simulate_failure": "expired_signin"}}, headers=h
    )
    hl = client.get(f"{u}/integrations/caldav/health?check=true", headers=h).json()
    assert hl["cause"] == "expired_signin" and hl["repair"]["steps"][0]["id"] == "sign_in"


def _slot_in(slots: list[str], hour: int) -> bool:
    return any(dt.datetime.fromisoformat(s) == dt.datetime.fromisoformat(slot(hour)) for s in slots)


# ---- CalDAV and CardDAV against a mock DAV server (the real path) ----------------------


def mock_dav(fake, app: str, owner_id: str, host: str = "dav.example.com") -> DavStandIn:
    """A DAV server at https://<host>/ behind the fake transport, answering like a real one."""
    server = DavStandIn(f"mock-{app}")

    def handler(call):
        with db.tx() as conn:
            req = kit.Request(
                call["method"],
                call["url"],
                call["headers"],
                call["body"],
                (call["body"] or "").encode() if isinstance(call["body"], str) else None,
            )
            r = server.handle(conn, {"customer_id": owner_id, "settings": {}}, req)
        return r.status, r.body, r.headers

    for m in ("PROPFIND", "REPORT", "GET", "PUT", "DELETE"):
        fake.on(m, rf"https://{host}/", handler)
    return server


def test_caldav_real_path_basic_auth_discovery_and_retries(client, real_env, fake, monkeypatch):
    monkeypatch.setenv("EXA_WEBHOOK_ALLOW_PRIVATE", "1")  # no DNS in tests
    b = business(client, people=("agent",))
    _utc(b["id"])
    go_live("caldav", b["id"])
    mock_dav(fake, "caldav", b["id"])
    u, h = base(b), b["agent"]["h"]
    # Credentials through secure entry: checked with one live call, stored encrypted.
    client.post(f"{u}/integrations/caldav/connect", headers=h)
    client.put(
        f"{u}/integrations/caldav/settings", json={"settings": {"server_url": "https://dav.example.com/"}}, headers=h
    )
    bad = client.post(f"{u}/integrations/caldav/credentials", json={"credentials": {"username": "ann"}}, headers=h)
    assert bad.status_code == 422
    password = "app-" + "pw" * 6
    r = client.post(
        f"{u}/integrations/caldav/credentials",
        json={"credentials": {"username": "ann", "password": password}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert password not in r.text and r.json()["auth_method"] == "credentials"
    with db.tx() as conn:
        assert all(password not in s["ciphertext"] for s in conn.execute("SELECT ciphertext FROM commai_secrets"))
    assert fake.calls[0]["headers"]["Authorization"].startswith("Basic ")
    with db.tx() as conn:
        row = connectors.connection(conn, b["id"], "caldav")
        conn.execute(
            "UPDATE integration_connections SET status = 'live', allowed_actions = %s WHERE id = %s",
            (["find_slots", "book", "cancel"], row["id"]),
        )
    assert row["settings"]["collection_url"] == "https://dav.example.com/calendars/me/bookings/"  # discovered

    run = propose_and_run(b["id"], "caldav", "book", {"start": slot(11), "name": "Ann", "contact": "a@x.org"}, "real-1")
    assert run["status"] == "succeeded", run["error"]
    put = fake.last("PUT", r"\.ics$")
    assert put["headers"]["If-None-Match"] == "*" and "BEGIN:VEVENT" in put["body"]
    assert put["headers"]["Content-Type"].startswith("text/calendar")
    # The record of the first booking is lost (crash before recording): the retry
    # still finds it on the server (GET, then 412) and never books twice.
    with db.tx() as conn:
        conn.execute("DELETE FROM commai_connector_objects")
    again = execute_direct(
        b["id"], "caldav", "book", {"start": slot(11), "name": "Ann", "contact": "a@x.org"}, "real-1"
    )
    assert again.get("replayed") and fake.count("PUT", r"\.ics$") == 1

    # Rate limited: a short Retry-After is waited out in place.
    state = {"n": 0}
    original = fake.routes[:]

    def limited(call):
        state["n"] += 1
        if state["n"] == 1:
            return 429, "", {"Retry-After": "1"}
        return next(h for m, p, h in original if m == "REPORT")(call)

    fake.on("REPORT", r"https://dav\.example\.com/", limited)
    free = execute_direct(b["id"], "caldav", "find_slots", {"date": TOMORROW.isoformat()}, "rl")
    assert state["n"] == 2 and not _slot_in(free["slots"], 11)
    # A long Retry-After becomes a provider error (the job retries later).
    fake.on("REPORT", r"https://dav\.example\.com/", (429, "", {"Retry-After": "600"}))
    try:
        execute_direct(b["id"], "caldav", "find_slots", {"date": TOMORROW.isoformat()}, "rl2")
        raise AssertionError("expected a provider error")
    except connectors.ConnectorError as e:
        assert e.cause == "provider" and "600" in str(e)

    # The server stops accepting the app password: expired sign-in, with repair steps.
    for m in ("PROPFIND", "REPORT", "GET", "PUT"):
        fake.on(m, r"https://dav\.example\.com/", (401, "", {}))
    run = propose_and_run(b["id"], "caldav", "book", {"start": slot(13), "name": "C", "contact": "c@x.org"}, "real-2")
    assert run["status"] == "failed" and "(expired_signin)" in run["error"]
    hl = client.get(f"{u}/integrations/caldav/health", headers=h).json()
    assert hl["status"] == "broken" and hl["cause"] == "expired_signin"


def test_carddav_create_update_etag_delete_and_sync(client, real_env, fake, monkeypatch):
    monkeypatch.setenv("EXA_WEBHOOK_ALLOW_PRIVATE", "1")
    b = business(client, people=("agent",))
    go_live("carddav", b["id"])
    mock_dav(fake, "carddav", b["id"], "cards.example.com")
    connect_real(
        b["id"],
        "carddav",
        ["find_contact", "sync_contacts", "create_contact", "update_contact", "delete_contact"],
        creds={"username": "ann", "password": "app-password"},
        settings={"server_url": "https://cards.example.com/"},
    )
    run = propose_and_run(
        b["id"],
        "carddav",
        "create_contact",
        {"name": "Ann Brown", "email": "ann@example.com", "phone": "+18685550100"},
        "c-1",
    )
    assert run["status"] == "succeeded", run["error"]
    cid = run["result"]["contact_id"]
    assert fake.last("PUT", r"\.vcf$")["headers"]["If-None-Match"] == "*"
    # Same key: replayed. Another key, same email: the existing card, no duplicate.
    assert execute_direct(
        b["id"], "carddav", "create_contact", {"name": "Ann Brown", "email": "ann@example.com"}, "c-1"
    )["replayed"]
    dup = execute_direct(b["id"], "carddav", "create_contact", {"name": "Ann B", "email": "ann@example.com"}, "c-2")
    assert dup["existing"] and dup["contact_id"] == cid and fake.count("PUT", r"\.vcf$") == 1

    found = execute_direct(b["id"], "carddav", "find_contact", {"email": "ann@example.com"}, "f")
    assert found["found"] and found["contact"]["id"] == cid and found["contact"]["phone"] == "+18685550100"
    run = propose_and_run(b["id"], "carddav", "update_contact", {"contact_id": cid, "phone": "+18685550199"}, "u-1")
    assert run["status"] == "succeeded", run["error"]
    assert fake.last("PUT", r"\.vcf$")["headers"]["If-Match"].startswith('"')

    # Sync into Jibsy contacts, then only what changed.
    first = execute_direct(b["id"], "carddav", "sync_contacts", {}, "s-1")
    assert first["created"] == 1 and first["sync_token_saved"]
    with db.tx() as conn:
        c = conn.execute("SELECT * FROM contacts WHERE customer_id = %s", (b["id"],)).fetchone()
    assert c["email"] == "ann@example.com" and c["external_ref"].startswith("carddav:")
    second = execute_direct(b["id"], "carddav", "sync_contacts", {}, "s-2")
    assert second["created"] == 0 and second["updated"] == 0

    run = propose_and_run(b["id"], "carddav", "delete_contact", {"contact_id": cid}, "d-1", approve_as="user:approver")
    assert run["status"] == "succeeded" and run["sensitive"]
    third = execute_direct(b["id"], "carddav", "sync_contacts", {}, "s-3")
    assert third["removed_upstream"] == 1  # never deleted from Jibsy by a sync


def test_dav_tenant_isolation(client):
    a = business(client, "Alpha Ltd", people=("agent",))
    z = business(client, "Zeta Ltd", people=("agent",))
    _utc(a["id"])
    connect_simulated(a["id"], "caldav", ["book", "find_slots"])
    run = propose_and_run(a["id"], "caldav", "book", {"start": slot(9), "name": "Ann", "contact": "a@x.org"}, "iso")
    assert run["status"] == "succeeded"
    connect_simulated(z["id"], "caldav", ["find_slots"])
    _utc(z["id"])
    free = execute_direct(z["id"], "caldav", "find_slots", {"date": TOMORROW.isoformat()}, "z")
    assert _slot_in(free["slots"], 9)  # Zeta's stand-in calendar never sees Alpha's booking
    with db.tx() as conn:
        try:
            actions.propose(
                conn,
                z["id"],
                role="person",
                app="caldav",
                action="book",
                inputs={"start": slot(9), "name": "x", "contact": "y"},
                actor="user:x",
            )
            raise AssertionError("book is not allowed for Zeta")
        except actions.ActionRefused as e:
            assert e.code == 403
    # Zeta cannot read Alpha's integrations.
    assert client.get(f"{base(a)}/integrations", headers=z["agent"]["h"]).status_code == 403
    assert integrations  # imported for the registry
