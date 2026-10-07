"""AI quality (ADR 0026): review against the business's own criteria with
quotes as evidence, the knowledge-gap report with drafted articles, follow-up
reminders from promises, and what the AI reads from attachments and voice notes."""

from __future__ import annotations

import base64
import datetime as dt
import zlib
from zoneinfo import ZoneInfo

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import inbox
from exaconnect_controller.commai.ai import attachments, followups, runtime
from exaconnect_controller.commai.ai.model import ModelInput, ModelOutput, SimulatedModel

from .commai_helpers import base, business, run_jobs

HOURS = "We are open Monday to Friday from 8am to 4pm, and on Saturday from 9am to 1pm."
SITE = "https://www.examplebank.tt"


class Capturing:
    name = "capturing"

    def __init__(self):
        self.inner = SimulatedModel()
        self.inputs: list[ModelInput] = []

    def complete(self, inp: ModelInput) -> ModelOutput:
        self.inputs.append(inp)
        return self.inner.complete(inp)


@pytest.fixture(autouse=True)
def simulated():
    runtime.set_model(SimulatedModel())
    attachments.set_vision(None)
    attachments.set_speech(attachments.SimulatedSpeech())
    yield
    runtime.set_model(None)
    attachments.set_speech(None)


def ai_business(client) -> dict:
    b = business(client)
    h = b["agent"]["h"]
    assert client.patch(f"{base(b)}/settings", json={"mode": "ai_first"}, headers=h).status_code == 200
    r = client.post(
        f"{base(b)}/ai/knowledge", json={"title": "Opening hours", "body": HOURS, "approved": True}, headers=h
    )
    assert r.status_code == 201
    return b


def inbound(b, body, address, *, name="", channel="web", attachments_=None):
    with db.tx() as conn:
        return inbox.receive(conn, b["id"], channel, address, body, name=name, attachments=attachments_)


def reply(client, b, conv_id, body, who="agent"):
    r = client.post(
        f"{base(b)}/conversations/{conv_id}/messages", json={"body": body, "take_over": True}, headers=b[who]["h"]
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---- quality review -----------------------------------------------------------------------


def test_review_scores_ai_and_human_conversations_with_quotes_and_flags(client):
    b = ai_business(client)
    u, h = base(b), b["agent"]["h"]
    ai_conv = inbound(b, "When are you open on Saturday?", "v-ben", name="Ben Ali")["conversation"]
    run_jobs()
    with db.tx() as conn:
        assert inbox.messages(conn, b["id"], ai_conv["id"])[-1]["author_kind"] == "ai"
    client.patch(f"{u}/settings", json={"mode": "human_first"}, headers=h)
    bad = inbound(b, "My card was charged twice", "v-ana", name="Ana Lopez")["conversation"]
    reply(client, b, bad["id"], "Hello Ana. We will refund you in full today.")
    good = inbound(b, "Can I change my address?", "v-carl", name="Carl Joseph")["conversation"]
    reply(client, b, good["id"], "Hello Carl, yes. You can change it in online banking under Profile.")
    # A private note is never judged or quoted.
    client.post(f"{u}/conversations/{good['id']}/notes", json={"body": "SECRET-NOTE refund promised"}, headers=h)

    assert client.post(f"{u}/quality/reviews", json={}, headers=h).status_code == 422  # no criteria yet
    for text in ("Greets the customer by name", "Never promises refunds"):
        assert client.post(f"{u}/quality/criteria", json={"text": text}, headers=h).status_code == 201
    assert client.post(f"{u}/quality/criteria", json={"text": "x"}, headers=h).status_code == 422
    r = client.post(f"{u}/quality/criteria", json={"text": "Never shares a PIN"}, headers=b["internal"]["h"])
    assert r.status_code == 403

    cap = Capturing()
    runtime.set_model(cap)
    r = client.post(f"{u}/quality/reviews", json={"sample_size": 10, "days": 7}, headers=h)
    assert r.status_code == 202, r.text
    review_id = r.json()["id"]
    run_jobs()
    judged = [i for i in cap.inputs if i.task == "judge"]
    assert len(judged) == 3
    assert all("SECRET-NOTE" not in i.as_json() for i in judged)

    rev = client.get(f"{u}/quality/reviews/{review_id}", headers=h).json()
    assert (
        rev["status"] == "done" and rev["summary"]["judged"] == 3 and rev["summary"]["counted"] == {"ai": 1, "human": 2}
    )
    by_conv = {str(r["conversation_id"]): r for r in rev["results"]}
    bad_r = {x["criterion"]: x for x in by_conv[str(bad["id"])]["results"]}
    assert bad_r["Greets the customer by name"]["verdict"] == "pass"
    refund = bad_r["Never promises refunds"]
    assert refund["verdict"] == "fail" and refund["quote"] == "We will refund you in full today."
    assert by_conv[str(bad["id"])]["flagged"] is True
    assert by_conv[str(good["id"])]["flagged"] is False and float(by_conv[str(good["id"])]["score"]) == 1.0
    ai_r = {x["criterion"]: x for x in by_conv[str(ai_conv["id"])]["results"]}
    assert ai_r["Greets the customer by name"]["verdict"] == "fail"
    assert by_conv[str(ai_conv["id"])]["handled_by"] == "ai"

    flags = client.get(f"{u}/quality/flags", headers=h).json()
    assert {str(f["conversation_id"]) for f in flags} == {str(bad["id"]), str(ai_conv["id"])}
    fid = next(f["id"] for f in flags if str(f["conversation_id"]) == str(bad["id"]))
    r = client.post(f"{u}/quality/flags/{fid}/review", json={"note": "Coached on refunds."}, headers=h)
    assert r.status_code == 200 and r.json()["flag_status"] == "reviewed"
    assert len(client.get(f"{u}/quality/flags", headers=h).json()) == 1


def test_a_quote_not_in_the_conversation_is_dropped(client):
    class Inventing:
        name = "inventing"

        def complete(self, inp):
            res = [{"id": c["id"], "verdict": "fail", "quote": "We guarantee a refund.", "why": "x"}
                   for c in inp.context["criteria"]]  # fmt: skip
            return ModelOutput(data={"results": res}, model=self.name)

    b = business(client)
    u, h = base(b), b["agent"]["h"]
    conv = inbound(b, "Hello", "v1", name="Dee")["conversation"]
    reply(client, b, conv["id"], "Hello Dee, how can I help?")
    client.post(f"{u}/quality/criteria", json={"text": "Never promises refunds"}, headers=h)
    runtime.set_model(Inventing())
    rid = client.post(f"{u}/quality/reviews", json={"sample_size": 5}, headers=h).json()["id"]
    run_jobs()
    res = client.get(f"{u}/quality/reviews/{rid}", headers=h).json()["results"][0]["results"][0]
    assert res["quote"] == "" and res["quote_dropped"] is True


def test_quality_is_isolated_between_businesses(client):
    a, other = business(client, "Bank A"), business(client, "Bank B")
    r = client.post(f"{base(a)}/quality/criteria", json={"text": "Greets by name"}, headers=a["agent"]["h"])
    cid = r.json()["id"]
    assert client.get(f"{base(a)}/quality/criteria", headers=other["agent"]["h"]).status_code == 403
    r = client.patch(f"{base(other)}/quality/criteria/{cid}", json={"enabled": False}, headers=other["agent"]["h"])
    assert r.status_code == 404


# ---- knowledge gaps -----------------------------------------------------------------------


def test_gap_report_groups_questions_and_drafts_an_article_a_person_approves(client):
    b = ai_business(client)
    u, h = base(b), b["agent"]["h"]
    for i, q in enumerate(
        ["How do I reset my online banking password?", "I forgot my online banking password", "Do you sell gold coins?"]
    ):
        inbound(b, q, f"g{i}")
        run_jobs()
    rep = client.get(f"{u}/ai/gaps/report", headers=h).json()
    assert rep["open"] == 3
    biggest = rep["groups"][0]
    assert len(biggest["questions"]) == 2 and "password" in biggest["topic"]

    r = client.post(f"{u}/ai/gaps/draft", json={"gap_ids": biggest["gap_ids"]}, headers=h)
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["approved"] is False and draft["placeholders"] is True
    assert "forgot my online banking password" in draft["body"]
    # The AI never uses an unapproved draft.
    hits = client.get(f"{u}/ai/knowledge/search", params={"q": "online banking password"}, headers=h).json()
    assert not any(
        str(x.get("source_id")) == str(draft["id"]) for x in (hits.get("hits") if isinstance(hits, dict) else hits)
    )
    # Placeholders must be filled before approval.
    r = client.post(f"{u}/ai/gaps/drafts/{draft['id']}/approve", headers=h)
    assert r.status_code == 409
    body = "To reset your online banking password, choose Forgot password on the sign-in page."
    assert client.patch(f"{u}/ai/knowledge/{draft['id']}", json={"body": body}, headers=h).status_code == 200
    r = client.post(f"{u}/ai/gaps/drafts/{draft['id']}/approve", headers=h)
    assert r.status_code == 200 and sorted(r.json()["resolved_gaps"]) == sorted(biggest["gap_ids"])
    assert client.get(f"{u}/ai/gaps/report", headers=h).json()["open"] == 1
    # Now the AI can answer it.
    conv = inbound(b, "How do I reset my online banking password?", "g9")["conversation"]
    run_jobs()
    with db.tx() as conn:
        last = inbox.messages(conn, b["id"], conv["id"])[-1]
    assert last["author_kind"] == "ai" and "Forgot password" in last["body"]


# ---- follow-up reminders ------------------------------------------------------------------


def test_promise_detection_and_due_times():
    tz = ZoneInfo("America/Port_of_Spain")
    now = dt.datetime(2026, 10, 7, 14, 0, tzinfo=dt.UTC)  # a Wednesday, 10:00 local
    assert followups.detect("Thanks! I will call you tomorrow. Bye") == ["I will call you tomorrow"]
    assert followups.detect("We'll email you the form by Friday.") == ["We'll email you the form by Friday"]
    assert followups.detect("You will get an email.") == []
    assert followups.due_from("I will call you tomorrow", now, tz) == dt.datetime(2026, 10, 8, 13, 0, tzinfo=dt.UTC)
    assert followups.due_from("I'll ring you in 2 hours", now, tz) == now + dt.timedelta(hours=2)
    assert followups.due_from("we'll send it by Friday", now, tz) == dt.datetime(2026, 10, 9, 13, 0, tzinfo=dt.UTC)
    assert followups.due_from("I'll check", now, tz) == now + dt.timedelta(days=1)


def test_a_promise_becomes_a_reminder_on_the_owner(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    conv = inbound(b, "Is my loan approved?", "v1")["conversation"]
    r = client.post(f"{u}/conversations/{conv['id']}/assign", json={"assignee_id": b["agent2"]["id"]}, headers=h)
    assert r.status_code == 200, r.text
    reply(client, b, conv["id"], "Not yet. I will call you tomorrow with the answer.")
    run_jobs()
    run_jobs()  # the reminder's due job (made ready at once by run_jobs)
    rems = client.get(f"{u}/followups", headers=h).json()
    assert len(rems) == 1 and rems[0]["promise"] == "I will call you tomorrow with the answer"
    assert rems[0]["owner"] == b["agent2"]["email"]
    note = client.get(f"{u}/notifications", headers=b["agent2"]["h"]).json()
    kinds = sorted(n["title"] for n in note["items"] if n["kind"] == "reminder")
    assert kinds == ["A promise to follow up", "Follow-up due now"]
    mine = client.get(f"{u}/followups", params={"mine": True}, headers=b["agent2"]["h"]).json()
    assert len(mine) == 1
    r = client.post(f"{u}/followups/{rems[0]['id']}", json={"status": "done"}, headers=b["agent2"]["h"])
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert client.get(f"{u}/followups", headers=h).json() == []


# ---- attachments and voice notes ----------------------------------------------------------


def _pdf(text: str) -> bytes:
    content = zlib.compress(f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET".encode())
    return (
        b"%PDF-1.4\n1 0 obj << /Length " + str(len(content)).encode() + b" /Filter /FlateDecode >>\nstream\n"
        + content + b"\nendstream\nendobj\n%%EOF"
    )  # fmt: skip


def test_sniffing_and_refusals():
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x02\x80\x00\x00\x01\xe0" + b"\x00" * 20
    assert attachments.check("a.png", "image/png", png) == "image/png"
    assert attachments.image_size(png, "image/png") == (640, 480)
    with pytest.raises(attachments.AttachmentError, match="match"):
        attachments.check("a.pdf", "application/pdf", png)
    with pytest.raises(attachments.AttachmentError, match="program"):
        attachments.check("setup.txt", "text/plain", b"MZ\x90\x00rest")
    with pytest.raises(attachments.AttachmentError, match="archive"):
        attachments.check("doc.txt", "text/plain", b"PK\x03\x04zip")
    with pytest.raises(attachments.AttachmentError):
        attachments.check("page.txt", "text/plain", b"<html><script>alert(1)</script></html>")
    with pytest.raises(attachments.AttachmentError):
        attachments.check("../x.txt", "text/plain", b"hello")
    with pytest.raises(attachments.AttachmentError, match="MB"):
        attachments.check("big.txt", "text/plain", b"a" * (2 * 1024 * 1024), max_bytes=1024)
    assert attachments.pdf_text(_pdf("Invoice 42 total 300 dollars")) == "Invoice 42 total 300 dollars"


def _upload(client, key, visitor_h, name, mime, data):
    r = client.post(
        f"/api/v1/commai/widget/{key}/files",
        json={"name": name, "type": mime, "data": base64.b64encode(data).decode()},
        headers=visitor_h,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_the_ai_reads_documents_images_and_voice_notes(client):
    b = ai_business(client)
    u, h = base(b), b["agent"]["h"]
    key = client.post(
        f"{u}/widget-keys",
        json={"name": "Site", "allowed_origins": [SITE], "settings": {"attachments": True}},
        headers=h,
    ).json()["public_key"]
    s = client.post(f"/api/v1/commai/widget/{key}/session", json={}, headers={"Origin": SITE}).json()["token"]
    vh = {"Origin": SITE, "X-Widget-Session": s}
    cap = Capturing()
    runtime.set_model(cap)

    # A voice note: its words become the customer's words and the AI answers them.
    voice = b"OggS" + b"\x00" * 30 + b"SIMTEXT:When are you open on Saturday?"
    fid = _upload(client, key, vh, "note.ogg", "audio/ogg", voice)
    r = client.post(
        f"/api/v1/commai/widget/{key}/messages",
        json={"body": "", "client_id": "client-001", "attachments": [fid]},
        headers=vh,
    )
    assert r.status_code == 201, r.text
    conv_id = r.json()["conversation_id"]
    run_jobs()
    with db.tx() as conn:
        last = inbox.messages(conn, b["id"], conv_id)[-1]
    assert last["author_kind"] == "ai" and "Saturday from 9am to 1pm" in last["body"]

    # A PDF and an image go to the model as data.
    pdf = _upload(client, key, vh, "bill.pdf", "application/pdf", _pdf("Account 7781 balance due 120 dollars"))
    png = _upload(client, key, vh, "card.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    client.post(
        f"/api/v1/commai/widget/{key}/messages",
        json={
            "body": "Can you check these?",
            "client_id": "client-002",
            "conversation_id": conv_id,
            "attachments": [pdf, png],
        },
        headers=vh,
    )
    run_jobs()
    ctx = cap.inputs[-1].context
    names = {a["name"]: a for a in ctx["attachments"]}
    assert "balance due 120 dollars" in names["bill.pdf"]["text"]
    assert names["card.png"]["kind"] == "image" and "can't see" in names["card.png"]["text"]

    read = client.get(f"{u}/conversations/{conv_id}/attachments", headers=h).json()
    assert {r["name"]: r["status"] for r in read} == {"note.ogg": "read", "bill.pdf": "read", "card.png": "read"}
    assert next(r for r in read if r["name"] == "note.ogg")["reader"] == "simulated-speech"


def test_an_unreadable_voice_note_goes_to_a_person(client):
    b = ai_business(client)
    with db.tx() as conn:
        f = conn.execute(
            """INSERT INTO channel_files (customer_id, owner, name, content_type, size, data)
               VALUES (%s, 'v1', 'n.ogg', 'audio/ogg', 10, %s) RETURNING id""",
            (b["id"], b"OggS" + b"\x00" * 6),
        ).fetchone()
    conv = inbound(b, "", "v1", attachments_=[{"id": str(f["id"]), "name": "n.ogg", "type": "audio/ogg"}])
    run_jobs()
    with db.tx() as conn:
        c = inbox.get(conn, b["id"], conv["conversation"]["id"])
        h = conn.execute("SELECT reason FROM handovers WHERE conversation_id = %s", (c["id"],)).fetchone()
    assert c["handler"] == "none" and "No words could be made out" in h["reason"]
