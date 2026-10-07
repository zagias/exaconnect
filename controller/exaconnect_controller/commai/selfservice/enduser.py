"""End users on the help centre: sign-in and what they may see and do (ADR 0037).

Sign-in, two ways:

- An emailed one-time link. Asking for one always gets the same answer, known
  address or not, so the page never tells anyone whether an address is a
  customer. A link is sent only to an address the business already has as an
  email contact; it lasts 15 minutes, works once (marked used in the same
  statement that checks it) and only on its own business's help centre.
  Requests are rate limited per address (silently) and per client (429 for
  everyone alike).
- The business's own login, through the signed token its website chat already
  uses (an HS256 JWT signed with the chat key's secret, ADR 0018).

A session proves a set of addresses (identities). A signed-in end user sees
conversations on those addresses only, and only their customer-facing
messages, through inbox.messages and widget.public_message: no notes, no
staff chat, no AI runs or handover packets, and nobody else's records.
Bookings are changed through commai.actions, so the customer hears "moved" or
"cancelled" only after the calendar reports success.
"""

from __future__ import annotations

import datetime as dt
from email.message import EmailMessage
from typing import Any

import psycopg

from ... import audit
from ...security import new_token, token_hash
from .. import actions, channels, events, inbox, jobs
from ..channels import messaging, widget
from . import helpcentre
from .staff import SelfServiceError

events.register("help.signed_in", "help.data_request", "help.booking_change")

LINK_MINUTES = 15
SESSION_HOURS = 8
OPT_CHANNELS = ("whatsapp", "sms", "email")
BOOK_ACTION, CANCEL_ACTION = "book", "cancel"
FOLLOW_UP_S = 15
SIGNIN_ANSWER = "If that address is one we know, a sign-in link is on its way. It works once, for 15 minutes."


# ---- sign-in -------------------------------------------------------------------------------


def _email_account(conn: psycopg.Connection, customer_id: Any) -> dict | None:
    return conn.execute(
        """SELECT * FROM channel_accounts WHERE customer_id = %s AND channel = 'email'
           ORDER BY (status = 'live') DESC, created_at LIMIT 1""",
        (customer_id,),
    ).fetchone()


def _send_link(conn: psycopg.Connection, c: dict, to: str, url: str, business: str) -> None:
    from ..channels import email as email_ch

    acct = _email_account(conn, c["customer_id"])
    msg = EmailMessage()
    msg["From"] = acct["address"] if acct else "no-reply@exacarib.invalid"
    msg["To"] = to
    msg["Subject"] = f"Sign in to the {business} help centre"
    msg["Message-ID"] = f"<signin.{new_token()[:20]}@exacarib.invalid>"
    msg.set_content(
        f"Use this link to sign in to the {business} help centre:\n\n{url}\n\n"
        f"It works once and stops working after {LINK_MINUTES} minutes. "
        "If you didn't ask for it, you can ignore this email."
    )
    sender = email_ch.SENDERS.get(acct["provider"]) if acct else None
    (sender or email_ch.SENDERS["simulated"]).send(conn, acct or {"customer_id": c["customer_id"], "id": None}, msg)


def request_link(conn: psycopg.Connection, c: dict, email: str, client: str, public_base: str) -> None:
    """Send a one-time sign-in link if the address is known. Always returns
    nothing; raises 429 only for a client over its limit, whatever the address."""
    cid = c["customer_id"]
    if not c["settings"]["show"]["signin"]:
        raise SelfServiceError("Signing in isn't available on this help centre.", 409)
    if not helpcentre.hit(conn, cid, "signin_ip", client or "-"):
        raise SelfServiceError("Too many requests. Try again in 15 minutes.", 429)
    email = (email or "").strip().lower()
    if not helpcentre.hit(conn, cid, "signin_email", email):
        return  # over the per-address limit: same answer, no email
    ident = conn.execute(
        """SELECT * FROM contact_identities WHERE customer_id = %s AND channel = 'email' AND lower(address) = %s
           ORDER BY verified DESC, created_at LIMIT 1""",
        (cid, email),
    ).fetchone()
    if ident is None or "@" not in email:
        return
    token = new_token()
    conn.execute(
        """INSERT INTO ss_magic_links (customer_id, token_hash, identity_id, expires_at)
           VALUES (%s, %s, %s, now() + make_interval(mins => %s))""",
        (cid, token_hash(token), ident["id"], LINK_MINUTES),
    )
    business = conn.execute("SELECT name FROM customers WHERE id = %s", (cid,)).fetchone()["name"]
    _send_link(conn, c, ident["address"], f"{public_base}/help/{c['slug']}?signin={token}", business)


def _start(conn: psycopg.Connection, c: dict, ident: dict, via: str) -> dict:
    token = new_token()
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(hours=SESSION_HOURS)
    conn.execute("DELETE FROM ss_enduser_sessions WHERE expires_at < now()")
    conn.execute(
        """INSERT INTO ss_enduser_sessions (token_hash, customer_id, contact_id, identity_ids, via, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (token_hash(token), c["customer_id"], ident["contact_id"], [ident["id"]], via, expires),
    )
    events.emit(
        conn,
        c["customer_id"],
        "help.signed_in",
        {"contact_id": str(ident["contact_id"]), "via": via},
        ident["contact_id"],
    )
    return {"token": token, "expires_at": expires}


def redeem_link(conn: psycopg.Connection, c: dict, token: str) -> dict:
    """Use a sign-in link: once, before it expires, on its own help centre."""
    row = conn.execute(
        """UPDATE ss_magic_links SET used_at = now()
           WHERE token_hash = %s AND customer_id = %s AND used_at IS NULL AND expires_at > now()
           RETURNING identity_id""",
        (token_hash(token or ""), c["customer_id"]),
    ).fetchone()
    if row is None:
        raise SelfServiceError("This sign-in link has expired or has been used. Ask for a new one.", 401)
    ident = conn.execute(
        "UPDATE contact_identities SET verified = true WHERE id = %s RETURNING *", (row["identity_id"],)
    ).fetchone()
    return _start(conn, c, ident, "magic_link")


def signin_with_token(conn: psycopg.Connection, c: dict, user_token: str) -> dict:
    """The business's own login: a token its site signed (as for website chat)."""
    if not c["settings"]["show"]["signin"]:
        raise SelfServiceError("Signing in isn't available on this help centre.", 409)
    key = helpcentre._token_key(conn, c)
    if key is None:
        raise SelfServiceError("This business hasn't set up sign-in with its own website.", 409)
    try:
        claims = widget.verify_user_token(key, user_token)
    except widget.WidgetError as e:
        raise SelfServiceError(str(e), 401) from e
    ident = inbox.find_or_create_identity(
        conn, c["customer_id"], "web", f"user:{claims['sub']}", name=claims["name"], verified=True
    )
    return _start(conn, c, ident, "business_token")


def session(conn: psycopg.Connection, c: dict, token: str | None) -> dict | None:
    """The end user's session on THIS business's help centre, or None."""
    if not token:
        return None
    row = conn.execute(
        """SELECT * FROM ss_enduser_sessions WHERE token_hash = %s AND customer_id = %s AND expires_at > now()""",
        (token_hash(token), c["customer_id"]),
    ).fetchone()
    if row is None:
        return None
    help_ident = conn.execute(
        """SELECT id FROM contact_identities WHERE customer_id = %s AND channel = 'web' AND address = %s
           AND contact_id = %s""",
        (c["customer_id"], f"help:{row['contact_id']}", row["contact_id"]),
    ).fetchone()
    ids = list(row["identity_ids"]) + ([help_ident["id"]] if help_ident else [])
    return {**row, "identities": ids}


def require(conn: psycopg.Connection, c: dict, token: str | None) -> dict:
    s = session(conn, c, token)
    if s is None:
        raise SelfServiceError("Sign in to see this.", 401)
    return s


def sign_out(conn: psycopg.Connection, c: dict, token: str | None) -> None:
    conn.execute(
        "DELETE FROM ss_enduser_sessions WHERE token_hash = %s AND customer_id = %s",
        (token_hash(token or ""), c["customer_id"]),
    )


def me(conn: psycopg.Connection, s: dict) -> dict:
    ct = conn.execute("SELECT name, email, language FROM contacts WHERE id = %s", (s["contact_id"],)).fetchone()
    return {
        "name": ct["name"],
        "email": ct["email"],
        "language": ct["language"],
        "via": s["via"],
        "expires_at": s["expires_at"],
    }


# ---- conversations ---------------------------------------------------------------------------


def conversations(conn: psycopg.Connection, s: dict) -> list[dict]:
    return conn.execute(
        """SELECT c.id, c.channel, c.subject, c.state, c.created_at, c.last_message_at,
                  (SELECT body FROM messages m WHERE m.conversation_id = c.id ORDER BY m.created_at DESC LIMIT 1)
                    AS preview
           FROM conversations c WHERE c.customer_id = %s AND c.identity_id = ANY(%s)
           ORDER BY COALESCE(c.last_message_at, c.created_at) DESC LIMIT 100""",
        (s["customer_id"], s["identities"]),
    ).fetchall()


def own_conversation(conn: psycopg.Connection, s: dict, conversation_id: str) -> dict:
    try:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = %s::uuid AND customer_id = %s AND identity_id = ANY(%s)",
            (conversation_id, s["customer_id"], s["identities"]),
        ).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        row = None
    if row is None:
        raise SelfServiceError("Conversation not found.", 404)
    return row


def conversation(conn: psycopg.Connection, s: dict, conversation_id: str) -> dict:
    """Customer-facing messages only, as the customer sees them."""
    conv = own_conversation(conn, s, conversation_id)
    msgs = inbox.messages(conn, s["customer_id"], conv["id"])
    return {
        "id": str(conv["id"]),
        "channel": conv["channel"],
        "subject": conv["subject"],
        "state": conv["state"],
        "can_reply": conv["state"] != "resolved",
        "messages": [widget.public_message(m) for m in msgs],
    }


def reply(conn: psycopg.Connection, s: dict, conversation_id: str, body: str, client_id: str) -> dict:
    conv = own_conversation(conn, s, conversation_id)
    if conv["state"] == "resolved":
        raise SelfServiceError("This conversation is closed. Use the contact form to start a new one.", 409)
    text = (body or "").strip()
    if not text:
        raise SelfServiceError("Write a message first.", 422)
    ident = conn.execute("SELECT * FROM contact_identities WHERE id = %s", (conv["identity_id"],)).fetchone()
    got = inbox.receive(
        conn,
        s["customer_id"],
        conv["channel"],
        ident["address"],
        text,
        external_id=f"help-reply:{client_id}",
        conversation_id=conv["id"],
    )
    return widget.public_message(got["message"])


# ---- bookings ---------------------------------------------------------------------------------


def _calendar_runs(conn: psycopg.Connection, s: dict) -> list[dict]:
    """Bookings made for this person: on their own conversations, or new times
    they moved a booking to here (once the calendar confirmed them)."""
    return conn.execute(
        """SELECT r.* FROM action_runs r
           WHERE r.customer_id = %(c)s AND r.action = 'book' AND NOT r.test
             AND (r.conversation_id IN (SELECT id FROM conversations WHERE customer_id = %(c)s
                                        AND identity_id = ANY(%(ids)s))
                  OR (r.status = 'succeeded' AND r.id IN (
                        SELECT book_run_id FROM ss_booking_changes WHERE customer_id = %(c)s
                        AND contact_id = %(ct)s AND book_run_id IS NOT NULL)))
             -- a reschedule not (yet) confirmed shows as a note on the original, not as a booking
             AND (r.status = 'succeeded' OR r.id NOT IN (
                  SELECT book_run_id FROM ss_booking_changes WHERE customer_id = %(c)s AND book_run_id IS NOT NULL))
           ORDER BY r.inputs->>'start'""",
        {"c": s["customer_id"], "ids": s["identities"], "ct": s["contact_id"]},
    ).fetchall()


def _cancel_run(conn, customer_id: Any, app: str, booking_id: str) -> dict | None:
    return conn.execute(
        """SELECT * FROM action_runs WHERE customer_id = %s AND app = %s AND action = 'cancel'
           AND inputs->>'booking_id' = %s ORDER BY created_at DESC LIMIT 1""",
        (customer_id, app, booking_id),
    ).fetchone()


def bookings(conn: psycopg.Connection, s: dict) -> list[dict]:
    """The person's bookings with their state in words. 'confirmed' only once
    the calendar has reported success."""
    out = []
    for r in _calendar_runs(conn, s):
        booking_id = (r["result"] or {}).get("booking_id", "")
        state = {"succeeded": "confirmed", "failed": "failed", "rejected": "failed"}.get(r["status"], "pending")
        cancel = _cancel_run(conn, s["customer_id"], r["app"], booking_id) if booking_id else None
        if cancel and cancel["status"] == "succeeded":
            state = "cancelled"
        elif cancel and cancel["status"] in ("awaiting_approval", "approved", "executing", "proposed"):
            state = "cancelling"
        change = conn.execute(
            """SELECT ch.kind, ch.new_start, b.status AS book_status, b.error AS book_error FROM ss_booking_changes ch
               LEFT JOIN action_runs b ON b.id = ch.book_run_id
               WHERE ch.booking_run_id = %s ORDER BY ch.created_at DESC LIMIT 1""",
            (r["id"],),
        ).fetchone()
        note = ""
        if state == "failed":
            note = "This booking didn't go through."
        elif state == "cancelling":
            note = "Cancellation requested. The business confirms it."
        elif change and change["kind"] == "reschedule":
            if change["book_status"] in ("failed", "rejected"):
                why = change["book_error"] or "the calendar refused"
                note = f"We couldn't move it to {change['new_start']}: {why}."
            elif change["book_status"] != "succeeded":
                note = f"Moving to {change['new_start']}: waiting for the calendar."
        moving = bool(
            change
            and change["kind"] == "reschedule"
            and change["book_status"] in ("proposed", "awaiting_approval", "approved", "executing")
        )
        can_change = state == "confirmed" and not moving
        out.append(
            {
                "id": str(r["id"]),
                "start": r["inputs"].get("start"),
                "reason": r["inputs"].get("reason", ""),
                "reference": booking_id,
                "state": state,
                "note": note,
                "can_change": bool(can_change),
            }
        )
    return out


def _own_booking(conn, s: dict, run_id: str) -> dict:
    run = next((r for r in _calendar_runs(conn, s) if str(r["id"]) == str(run_id)), None)
    if run is None:
        raise SelfServiceError("Booking not found.", 404)
    if run["status"] != "succeeded":
        raise SelfServiceError("Only a confirmed booking can be changed.", 409)
    cancel = _cancel_run(conn, s["customer_id"], run["app"], run["result"].get("booking_id", ""))
    if cancel and cancel["status"] not in ("failed", "rejected"):
        raise SelfServiceError("This booking is cancelled or being cancelled.", 409)
    return run


def _actor(s: dict) -> str:
    return f"contact:{s['contact_id']}"


def reschedule(conn: psycopg.Connection, c: dict, s: dict, run_id: str, new_start: str) -> dict:
    """Book the new time first; the old one is cancelled only after the
    calendar confirms the new one. The customer hears "moved" only then."""
    if not (c["settings"]["show"]["bookings"] and c["settings"]["reschedule"]):
        raise SelfServiceError("Rescheduling isn't available here. Contact the team instead.", 409)
    old = _own_booking(conn, s, run_id)
    inputs = {
        "start": new_start,
        "name": old["inputs"].get("name", ""),
        "contact": old["inputs"].get("contact", ""),
        "reason": old["inputs"].get("reason", ""),
    }
    change = conn.execute(
        """INSERT INTO ss_booking_changes (customer_id, contact_id, booking_run_id, kind, new_start)
           VALUES (%s, %s, %s, 'reschedule', %s) RETURNING id""",
        (s["customer_id"], s["contact_id"], old["id"], new_start),
    ).fetchone()
    try:
        run = actions.propose(
            conn,
            s["customer_id"],
            role="person",
            app=old["app"],
            action=BOOK_ACTION,
            inputs=inputs,
            actor=_actor(s),
            conversation_id=old["conversation_id"],
            idempotency_key=f"help-reschedule:{change['id']}",
            on_success={"reply": "Your booking has moved to {start}. Reference: {booking_id}."},
        )
    except actions.ActionRefused as e:
        raise SelfServiceError(str(e), e.code) from e
    conn.execute("UPDATE ss_booking_changes SET book_run_id = %s WHERE id = %s", (run["id"], change["id"]))
    jobs.enqueue(
        conn,
        "selfservice.booking_follow_up",
        {"change_id": str(change["id"])},
        customer_id=s["customer_id"],
        dedupe_key=f"help-booking:{change['id']}",
        max_attempts=2000,
    )
    events.emit(
        conn,
        s["customer_id"],
        "help.booking_change",
        {"run_id": str(old["id"]), "kind": "reschedule"},
        old["conversation_id"] or old["id"],
    )
    return {
        "status": run["status"],
        "message": "We've asked the calendar for the new time. Your booking shows as moved once it confirms.",
    }


def cancel(conn: psycopg.Connection, c: dict, s: dict, run_id: str) -> dict:
    """Ask to cancel. Cancelling is a sensitive action: the business confirms it."""
    if not (c["settings"]["show"]["bookings"] and c["settings"]["cancel"]):
        raise SelfServiceError("Cancelling isn't available here. Contact the team instead.", 409)
    old = _own_booking(conn, s, run_id)
    try:
        run = _propose_cancel(conn, s["customer_id"], old, _actor(s), f"help-cancel:{old['id']}")
    except actions.ActionRefused as e:
        raise SelfServiceError(str(e), e.code) from e
    conn.execute(
        """INSERT INTO ss_booking_changes (customer_id, contact_id, booking_run_id, kind, cancel_run_id)
           VALUES (%s, %s, %s, 'cancel', %s)""",
        (s["customer_id"], s["contact_id"], old["id"], run["id"]),
    )
    events.emit(
        conn,
        s["customer_id"],
        "help.booking_change",
        {"run_id": str(old["id"]), "kind": "cancel"},
        old["conversation_id"] or old["id"],
    )
    waiting = run["status"] == "awaiting_approval"
    return {
        "status": run["status"],
        "message": "Cancellation requested. The business confirms it here."
        if waiting
        else "Cancellation requested. It shows as cancelled once the calendar confirms.",
    }


def _propose_cancel(conn, customer_id: Any, old: dict, actor: str, key: str) -> dict:
    return actions.propose(
        conn,
        customer_id,
        role="person",
        app=old["app"],
        action=CANCEL_ACTION,
        inputs={"booking_id": old["result"].get("booking_id", "")},
        actor=actor,
        conversation_id=old["conversation_id"],
        idempotency_key=key,
        on_success={"reply": "Your booking {booking_id} is cancelled."},
    )


@jobs.handler("selfservice.booking_follow_up")
def _follow_up(conn: psycopg.Connection, job: dict):
    """After a reschedule: once the new booking is confirmed, release the old
    time; if the calendar refused, tell the customer the old time stands."""
    ch = conn.execute("SELECT * FROM ss_booking_changes WHERE id = %s", (job["payload"]["change_id"],)).fetchone()
    if ch is None or ch["book_run_id"] is None:
        return None
    new = conn.execute("SELECT * FROM action_runs WHERE id = %s", (ch["book_run_id"],)).fetchone()
    if new["status"] in ("proposed", "awaiting_approval", "approved", "executing"):
        return jobs.Later("waiting for the calendar", delay_s=FOLLOW_UP_S)
    old = conn.execute("SELECT * FROM action_runs WHERE id = %s", (ch["booking_run_id"],)).fetchone()
    if new["status"] == "succeeded":
        if ch["cancel_run_id"] is None:
            try:
                run = _propose_cancel(
                    conn, ch["customer_id"], old, f"contact:{ch['contact_id']}", f"help-reschedule-release:{ch['id']}"
                )
                conn.execute("UPDATE ss_booking_changes SET cancel_run_id = %s WHERE id = %s", (run["id"], ch["id"]))
            except actions.ActionRefused as e:
                if old["conversation_id"]:
                    inbox.add_note(
                        conn,
                        ch["customer_id"],
                        old["conversation_id"],
                        author="CommAI",
                        body=f"Rescheduled by the customer; release the old time by hand ({e}).",
                    )
        return None
    if old["conversation_id"]:
        conv = conn.execute("SELECT * FROM conversations WHERE id = %s", (old["conversation_id"],)).fetchone()
        text = "We couldn't move your booking, so your original time is unchanged."
        try:
            channels.get(conv["channel"]).check_send(conn, conv, text, "")
            inbox._insert_out(conn, conv, text, "system", "CommAI")
        except channels.SendBlocked as e:
            inbox.add_note(conn, conv["customer_id"], conv["id"], author="CommAI", body=f"Not sent ({e}): {text}")
    return None


# ---- message preferences ---------------------------------------------------------------


def _mask(address: str) -> str:
    if "@" in address:
        user, _, dom = address.partition("@")
        return (user[:1] + "•••@" + dom) if user else address
    return address[:-4].rstrip()[:4] + " ••• " + address[-4:] if len(address) > 8 else "•••"


def preferences(conn: psycopg.Connection, s: dict) -> dict:
    rows = conn.execute(
        """SELECT id, channel, address, opted_out FROM contact_identities
           WHERE customer_id = %s AND contact_id = %s AND channel = ANY(%s) ORDER BY channel, address""",
        (s["customer_id"], s["contact_id"], list(OPT_CHANNELS)),
    ).fetchall()
    ct = conn.execute("SELECT language FROM contacts WHERE id = %s", (s["contact_id"],)).fetchone()
    return {
        "language": ct["language"],
        "channels": [
            {
                "id": str(r["id"]),
                "channel": r["channel"],
                "address": r["address"] if r["id"] in s["identities"] else _mask(r["address"]),
                "receive": not r["opted_out"],
            }
            for r in rows
        ],
    }


def set_preference(conn: psycopg.Connection, s: dict, identity_id: str, receive: bool) -> dict:
    """Opt out of (or back into) messages on one of the person's addresses.
    The channel gateway refuses to send to an opted-out address. Opting back in
    needs an address this session proved."""
    try:
        ident = conn.execute(
            """SELECT * FROM contact_identities WHERE id = %s::uuid AND customer_id = %s AND contact_id = %s
               AND channel = ANY(%s)""",
            (identity_id, s["customer_id"], s["contact_id"], list(OPT_CHANNELS)),
        ).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        ident = None
    if ident is None:
        raise SelfServiceError("Address not found.", 404)
    if receive and ident["id"] not in s["identities"]:
        raise SelfServiceError("To receive messages there again, sign in from that address or ask the team.", 403)
    messaging.set_opt_out(conn, ident, not receive, "help centre preference")
    return preferences(conn, s)


def set_language(conn: psycopg.Connection, s: dict, lang: str) -> dict:
    from ..ai import language

    if lang and lang not in language.NAMES:
        raise SelfServiceError("Choose one of the listed languages.", 422)
    conn.execute("UPDATE contacts SET language = %s WHERE id = %s", (lang, s["contact_id"]))
    return preferences(conn, s)


# ---- data requests ------------------------------------------------------------------------


def data_request(conn: psycopg.Connection, c: dict, s: dict, kind: str, detail: str = "") -> dict:
    """Record a request to download or delete the person's data. A business admin
    carries it out from Help centre > Data requests, through data governance."""
    if not c["settings"]["show"]["data_requests"]:
        raise SelfServiceError("Data requests aren't available here. Contact the team instead.", 409)
    if kind not in ("download", "delete"):
        raise SelfServiceError("Ask to download or to delete your data.", 422)
    open_one = conn.execute(
        """SELECT * FROM ss_data_requests WHERE customer_id = %s AND contact_id = %s AND kind = %s
           AND status = 'open'""",
        (s["customer_id"], s["contact_id"], kind),
    ).fetchone()
    if open_one:
        return open_one
    row = conn.execute(
        """INSERT INTO ss_data_requests (customer_id, contact_id, kind, detail) VALUES (%s, %s, %s, %s) RETURNING *""",
        (s["customer_id"], s["contact_id"], kind, (detail or "").strip()[:1000]),
    ).fetchone()
    events.emit(conn, s["customer_id"], "help.data_request", {"request_id": str(row["id"]), "kind": kind}, row["id"])
    audit.record(conn, _actor(s), f"commai.help.data_request.{kind}", str(row["id"]), s["customer_id"])
    return row


def my_requests(conn: psycopg.Connection, s: dict) -> list[dict]:
    return conn.execute(
        """SELECT id, kind, status, created_at, handled_at FROM ss_data_requests
           WHERE customer_id = %s AND contact_id = %s ORDER BY created_at DESC""",
        (s["customer_id"], s["contact_id"]),
    ).fetchall()


def export_contact(conn: psycopg.Connection, customer_id: Any, contact_id: Any) -> dict:
    """What a business admin sends for a "download my data" request: the
    contact record, their addresses and customer-facing messages. No notes."""
    ct = conn.execute(
        "SELECT id, name, email, phone, language, created_at FROM contacts WHERE id = %s AND customer_id = %s",
        (contact_id, customer_id),
    ).fetchone()
    if ct is None:
        raise SelfServiceError("Contact not found.", 404)
    convs = conn.execute(
        "SELECT id, channel, subject, state, created_at FROM conversations WHERE customer_id = %s AND contact_id = %s"
        " ORDER BY created_at",
        (customer_id, contact_id),
    ).fetchall()
    return {
        "contact": ct,
        "addresses": conn.execute(
            "SELECT channel, address, verified, opted_out FROM contact_identities WHERE contact_id = %s",
            (contact_id,),
        ).fetchall(),
        "conversations": [
            {**cv, "messages": [widget.public_message(m) for m in inbox.messages(conn, customer_id, cv["id"])]}
            for cv in convs
        ],
    }
