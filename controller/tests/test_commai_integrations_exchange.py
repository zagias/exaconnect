"""Data exchange (ADR 0028): contacts in and out as CSV and vCard, conversations
out as CSV, and the iCalendar feed of bookings with signed .ics invites."""

from __future__ import annotations

import csv
import datetime as dt
import io
import urllib.parse

from exaconnect_controller import db
from exaconnect_controller.commai import inbox
from exaconnect_controller.commai.standards import ical

from .commai_connector_kit import connect_simulated, propose_and_run
from .commai_helpers import base, business

TOMORROW = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).date()


def test_contacts_csv_and_vcard_round_trip(client):
    b = business(client, "Exchange Ltd", people=("agent",))
    other = business(client, "Exchange Other", people=("agent",))
    u, h = base(b), b["agent"]["h"]
    text = (
        "﻿Full Name;E-mail;Mobile;Language\r\n"
        "Ann Lee;ANN@example.org;+18685550100;en\r\n"
        '=HYPERLINK("x");bo@example.org;;\r\n'
        "No Address;;;\r\n"
        "Bad;not-an-email;;\r\n"
    )
    r = client.post(f"{u}/imports/contacts.csv", json={"data": text}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["rows"], out["created"], out["skipped"]) == (4, 2, 2)
    assert any("not an email" in p for p in out["problems"])
    again = client.post(f"{u}/imports/contacts.csv", json={"data": text}, headers=h).json()
    assert again["created"] == 0 and again["updated"] == 2  # matched by email: no duplicates
    assert client.post(f"{u}/imports/contacts.csv", json={"data": "name,city\nAnn,POS\n"}, headers=h).status_code == 422

    exp = client.get(f"{u}/exports/contacts.csv", headers=h)
    assert exp.status_code == 200 and exp.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(exp.text)))
    assert {r["email"] for r in rows} == {"ann@example.org", "bo@example.org"}
    assert any(r["name"].startswith("'=") for r in rows)  # formula injection neutralised
    assert client.get(f"{base(other)}/exports/contacts.csv", headers=other["agent"]["h"]).text.count("\n") == 1

    vcf = client.get(f"{u}/exports/contacts.vcf", headers=h)
    assert vcf.text.count("BEGIN:VCARD") == 2 and "EMAIL;PREF=1:ann@example.org" in vcf.text
    r = client.post(f"{base(other)}/imports/contacts.vcf", json={"data": vcf.text}, headers=other["agent"]["h"])
    assert r.json()["created"] == 2
    bad = client.post(f"{u}/imports/contacts.vcf", json={"data": "hello"}, headers=h)
    assert bad.status_code == 422
    log = client.get(f"{u}/imports", headers=h).json()
    assert [x["kind"] for x in log][:2] == ["contacts.csv", "contacts.csv"]


def test_conversations_csv(client):
    b = business(client, "Talk Ltd", people=("agent",))
    with db.tx() as conn:
        inbox.receive(conn, b["id"], "web", "visitor-1", "Hello, is the branch open?", name="Visitor")
    r = client.get(f"{base(b)}/exports/conversations.csv", headers=b["agent"]["h"])
    assert r.status_code == 200
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows[-1]["body"] == "Hello, is the branch open?" and rows[-1]["direction"] == "in"
    assert (
        client.get(f"{base(b)}/exports/conversations.csv?since=yesterday", headers=b["agent"]["h"]).status_code == 422
    )


def test_bookings_feed_and_signed_invites(client, monkeypatch):
    monkeypatch.setenv("EXA_PUBLIC_URL", "https://connect.example.org")
    b = business(client, "Clinic Ltd", people=("agent",))
    other = business(client, "Clinic Other", people=("agent",))
    u, h = base(b), b["agent"]["h"]
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO commai_settings (customer_id, timezone) VALUES (%s, 'UTC')"
            " ON CONFLICT (customer_id) DO UPDATE SET timezone = 'UTC'",
            (b["id"],),
        )
    connect_simulated(b["id"], "caldav", ["book", "cancel"])
    start = dt.datetime.combine(TOMORROW, dt.time(10), tzinfo=dt.UTC).isoformat()
    run = propose_and_run(b["id"], "caldav", "book", {"start": start, "name": "Ann", "contact": "ann@x.org"}, "bk-1")
    assert run["status"] == "succeeded", run["error"]
    invite_url = run["result"]["invite_url"]
    assert invite_url.startswith("https://connect.example.org/api/v1/commai/ical/invites/")

    # The feed: shown once, any calendar app can subscribe.
    assert not client.get(f"{u}/ical-feed", headers=h).json()["published"]
    feed = client.post(f"{u}/ical-feed", headers=h).json()
    assert feed["webcal"].startswith("webcal://connect.example.org/")
    path = urllib.parse.urlsplit(feed["url"]).path
    r = client.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    events = ical.events(r.text)
    assert len(events) == 1 and events[0]["summary"] == "Appointment: Ann"

    # The invite: a REQUEST with the customer as attendee; forged links fail.
    inv_path = urllib.parse.urlsplit(invite_url)
    inv = client.get(inv_path.path + "?" + inv_path.query)
    assert inv.status_code == 200 and "METHOD:REQUEST" in inv.text
    assert "mailto:ann@x.org" in inv.text.replace("\r\n ", "")  # unfolded (RFC 5545 3.1)
    assert client.get(inv_path.path + "?sig=" + "0" * 32).status_code == 404

    # Cancelled: the feed and the invite say so.
    propose_and_run(b["id"], "caldav", "cancel", {"booking_id": run["result"]["booking_id"]}, "bk-2", "user:ok")
    assert "STATUS:CANCELLED" in client.get(path).text
    assert "METHOD:CANCEL" in client.get(inv_path.path + "?" + inv_path.query).text

    # A new address stops the old one; the invite link still works; isolation holds.
    client.post(f"{u}/ical-feed", headers=h)
    assert client.get(path).status_code == 404
    assert client.get(inv_path.path + "?" + inv_path.query).status_code == 200
    other_feed = client.post(f"{base(other)}/ical-feed", headers=other["agent"]["h"]).json()
    assert "VEVENT" not in client.get(urllib.parse.urlsplit(other_feed["url"]).path).text
