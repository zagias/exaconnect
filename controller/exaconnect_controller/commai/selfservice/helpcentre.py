"""The help centre: a public page per business for its own customers (ADR 0037).

What it shows is chosen by the business's admins and is off until they switch
it on. Articles are knowledge sources a person both approved (for the AI) and
published (for the public); editing a source hides its article until someone
publishes it again. Nothing else from the knowledge base, and nothing from
notes, AI runs or other customers, is ever read by the public functions here.

"Ask" is the customer AI agent (commai.ai.agent) with all its rules: it answers
only from approved knowledge, escalates when unsure, and never confirms an
action itself. The question becomes a website-chat conversation in the inbox.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import secrets
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import events, inbox
from ..channels import widget
from . import branding as brand
from .staff import SelfServiceError

events.register("help.contact_form", "help.asked")

SHOW = ("articles", "search", "ask", "contact", "hours", "channels", "signin", "bookings", "data_requests")
DEFAULT_SETTINGS: dict = {
    "title": "Help centre",
    "intro": "Answers to common questions, and ways to reach us.",
    "colour": "",
    "show": {k: True for k in SHOW},
    "hours": {},
    "whatsapp": "",
    "phone": "",
    "email": "",
    "widget_key_id": "",
    "reschedule": True,
    "cancel": True,
}
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")
PHONE = re.compile(r"^\+?[0-9 ()-]{7,20}$")
ASK_SUBJECT = "Asked in the help centre"
CONTACT_SUBJECT = "Help centre contact form"
NOT_ANSWERED = (
    "We couldn't answer that here. Send it to the team with the contact form, or sign in, and a person will reply."
)

# Public rate limits: (requests, minutes) per client address.
LIMITS = {"ask": (20, 10), "contact": (5, 10), "signin_ip": (20, 15), "signin_email": (3, 15)}


def key_hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def hit(conn: psycopg.Connection, customer_id: Any, bucket: str, key: str) -> bool:
    """Record a public request; False when this key is over its limit (the
    request is still recorded, so hammering never resets the window)."""
    n, minutes = LIMITS[bucket]
    kh = key_hash(key)
    used = conn.execute(
        """SELECT count(*) AS n FROM ss_public_hits WHERE customer_id = %s AND bucket = %s AND key_hash = %s
           AND at > now() - make_interval(mins => %s)""",
        (customer_id, bucket, kh, minutes),
    ).fetchone()["n"]
    conn.execute(
        "INSERT INTO ss_public_hits (customer_id, bucket, key_hash) VALUES (%s, %s, %s)", (customer_id, bucket, kh)
    )
    return used < n


# ---- admin -------------------------------------------------------------------------------


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "help"
    return s if len(s) >= 3 else f"{s}-help"


def centre(conn: psycopg.Connection, customer_id: Any) -> dict:
    """The business's help centre settings, created (switched off) on first read."""
    row = conn.execute("SELECT * FROM help_centres WHERE customer_id = %s", (customer_id,)).fetchone()
    if row is None:
        name = conn.execute("SELECT name FROM customers WHERE id = %s", (customer_id,)).fetchone()["name"]
        slug = _slugify(name)
        if conn.execute("SELECT 1 FROM help_centres WHERE slug = %s", (slug,)).fetchone():
            slug = f"{slug[:40]}-{secrets.token_hex(3)}"
        row = conn.execute(
            """INSERT INTO help_centres (customer_id, slug) VALUES (%s, %s)
               ON CONFLICT (customer_id) DO UPDATE SET slug = help_centres.slug RETURNING *""",
            (customer_id, slug),
        ).fetchone()
    return {**row, "settings": settings_of(row)}


def settings_of(row: dict) -> dict:
    s = {**DEFAULT_SETTINGS, **(row.get("settings") or {})}
    s["show"] = {**DEFAULT_SETTINGS["show"], **(s.get("show") or {})}
    return s


def check_settings(conn: psycopg.Connection, customer_id: Any, s: dict) -> dict:
    out = {k: v for k, v in s.items() if k in DEFAULT_SETTINGS}
    for k in ("title", "intro"):
        if k in out:
            out[k] = str(out[k] or "").strip()[: 80 if k == "title" else 400]
    if "show" in out:
        if not isinstance(out["show"], dict) or set(out["show"]) - set(SHOW):
            raise SelfServiceError(f"You can show or hide: {', '.join(SHOW)}.", 422)
        out["show"] = {k: bool(v) for k, v in out["show"].items()}
    if out.get("colour") and not brand.HEX.match(str(out["colour"])):
        raise SelfServiceError("The colour must look like #155EEF.", 422)
    if "hours" in out:
        try:
            out["hours"] = widget.check_settings({"hours": out["hours"]})["hours"]
        except widget.WidgetError as e:
            raise SelfServiceError(str(e), 422) from e
    for k in ("whatsapp", "phone"):
        if out.get(k) and not PHONE.match(str(out[k])):
            raise SelfServiceError(f"The {k} number must look like +1 868 555 0100.", 422)
    if out.get("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(out["email"])):
        raise SelfServiceError("That email address doesn't look right.", 422)
    if out.get("widget_key_id"):
        ok = conn.execute(
            "SELECT 1 FROM widget_keys WHERE id::text = %s AND customer_id = %s", (out["widget_key_id"], customer_id)
        ).fetchone()
        if not ok:
            raise SelfServiceError("That website chat key isn't one of yours.", 422)
    for k in ("reschedule", "cancel"):
        if k in out:
            out[k] = bool(out[k])
    return out


def update(
    conn: psycopg.Connection,
    customer_id: Any,
    *,
    actor: str,
    slug: str | None = None,
    enabled: bool | None = None,
    settings: dict | None = None,
) -> dict:
    cur = centre(conn, customer_id)
    if slug is not None:
        slug = slug.strip().lower()
        if not SLUG.match(slug):
            raise SelfServiceError("The address can use a to z, 0 to 9 and hyphens, 3 to 50 characters.", 422)
        taken = conn.execute(
            "SELECT 1 FROM help_centres WHERE slug = %s AND customer_id <> %s", (slug, customer_id)
        ).fetchone()
        if taken:
            raise SelfServiceError("Another business already uses that address.", 409)
    merged = None
    if settings is not None:
        merged = {**(cur["settings"]), **check_settings(conn, customer_id, settings)}
        if "show" in settings:
            merged["show"] = {**cur["settings"]["show"], **merged["show"]}
    conn.execute(
        """UPDATE help_centres SET slug = COALESCE(%s, slug), enabled = COALESCE(%s, enabled),
                  settings = COALESCE(%s, settings), updated_by = %s, updated_at = now()
           WHERE customer_id = %s""",
        (slug, enabled, Jsonb(merged) if merged is not None else None, actor, customer_id),
    )
    return centre(conn, customer_id)


def article_admin_list(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    """Every knowledge source with whether it can be, and is, published."""
    rows = conn.execute(
        """SELECT s.id AS source_id, s.title, s.approved, s.updated_at, a.id AS article_id,
                  COALESCE(a.category, 'General') AS category, COALESCE(a.position, 100) AS position,
                  COALESCE(a.published, false) AS published, a.published_by, a.published_at,
                  (a.published AND s.approved AND a.published_at >= s.updated_at) AS live
           FROM knowledge_sources s
           LEFT JOIN help_articles a ON a.source_id = s.id AND a.customer_id = s.customer_id
           WHERE s.customer_id = %s ORDER BY COALESCE(a.position, 100), s.title""",
        (customer_id,),
    ).fetchall()
    for r in rows:
        r["live"] = bool(r["live"])
        r["needs_republish"] = bool(r["published"] and not r["live"] and r["approved"])
    return rows


def publish(
    conn: psycopg.Connection,
    customer_id: Any,
    source_id: Any,
    *,
    published: bool,
    actor: str,
    category: str | None = None,
    position: int | None = None,
) -> dict:
    """A person publishes (or withdraws) an approved knowledge source as an article."""
    src = conn.execute(
        "SELECT id, approved FROM knowledge_sources WHERE id = %s AND customer_id = %s", (source_id, customer_id)
    ).fetchone()
    if src is None:
        raise SelfServiceError("Knowledge source not found.", 404)
    if published and not src["approved"]:
        raise SelfServiceError("Approve the source on the AI agents screen before publishing it.", 409)
    if not actor.startswith("user:"):
        raise SelfServiceError("Only a person can publish an article.", 403)
    cat = (category or "").strip()[:60] or None
    conn.execute(
        """INSERT INTO help_articles (customer_id, source_id, category, position, published, published_by, published_at)
           VALUES (%(c)s, %(s)s, COALESCE(%(cat)s, 'General'), COALESCE(%(pos)s, 100), %(p)s,
                   CASE WHEN %(p)s THEN %(a)s ELSE '' END, CASE WHEN %(p)s THEN clock_timestamp() END)
           ON CONFLICT (customer_id, source_id) DO UPDATE SET
             category = COALESCE(%(cat)s, help_articles.category),
             position = COALESCE(%(pos)s, help_articles.position),
             published = %(p)s,
             published_by = CASE WHEN %(p)s THEN %(a)s ELSE help_articles.published_by END,
             published_at = CASE WHEN %(p)s THEN clock_timestamp() ELSE help_articles.published_at END""",
        {"c": customer_id, "s": source_id, "cat": cat, "pos": position, "p": published, "a": actor},
    )
    return next(r for r in article_admin_list(conn, customer_id) if str(r["source_id"]) == str(source_id))


# ---- public -----------------------------------------------------------------------------

# Published, approved and not edited since publishing: the only articles the public sees.
PUBLISHED = """FROM help_articles a JOIN knowledge_sources s ON s.id = a.source_id AND s.customer_id = a.customer_id
               WHERE a.customer_id = %(c)s AND a.published AND s.approved AND a.published_at >= s.updated_at"""


def find(conn: psycopg.Connection, slug: str, *, preview_for: Any = None) -> dict:
    """The switched-on help centre at this address. A switched-off one (or an
    unknown address) is 'not found' to the public; its own admins can preview it."""
    row = conn.execute("SELECT * FROM help_centres WHERE slug = %s", ((slug or "").lower(),)).fetchone()
    if row is None or not (row["enabled"] or (preview_for and str(preview_for) == str(row["customer_id"]))):
        raise SelfServiceError("There is no help centre at this address.", 404)
    return {**row, "settings": settings_of(row)}


def _channel_links(conn: psycopg.Connection, c: dict) -> list[dict]:
    s = c["settings"]
    accts = {
        r["channel"]: r["address"]
        for r in conn.execute(
            """SELECT DISTINCT ON (channel) channel, address FROM channel_accounts
               WHERE customer_id = %s AND status = 'live' ORDER BY channel, created_at""",
            (c["customer_id"],),
        ).fetchall()
    }
    out = []
    wa = s["whatsapp"] or accts.get("whatsapp", "")
    if wa:
        digits = re.sub(r"\D", "", wa)
        out.append({"kind": "whatsapp", "label": "WhatsApp", "value": wa, "href": f"https://wa.me/{digits}"})
    phone = s["phone"] or accts.get("sms", "")
    if phone:
        out.append({"kind": "phone", "label": "Phone", "value": phone, "href": "tel:" + re.sub(r"[^\d+]", "", phone)})
    email = s["email"] or accts.get("email", "")
    if email:
        out.append({"kind": "email", "label": "Email", "value": email, "href": f"mailto:{email}"})
    return out


def ask_available(conn: psycopg.Connection, customer_id: Any) -> bool:
    from ..ai import runtime

    if not inbox.ai_available() or inbox.settings(conn, customer_id)["mode"] == "human_only":
        return False
    return bool(runtime.profile(conn, customer_id).get("enabled", True))


def home(conn: psycopg.Connection, c: dict) -> dict:
    """Everything the public page shows, as chosen by the business."""
    s, show = c["settings"], c["settings"]["show"]
    cid = c["customer_id"]
    tz = inbox.settings(conn, cid)["timezone"]
    b = brand.branding(conn, cid)
    out: dict = {
        "slug": c["slug"],
        "enabled": c["enabled"],
        "business": b["name"],
        "colour": b["colour"],
        "logo_url": b["logo_url"],
        "platform": b["platform"],
        "title": s["title"],
        "intro": s["intro"],
        "show": {**show, "ask": bool(show["ask"] and ask_available(conn, cid))},
        "signin_with_business": bool(show["signin"] and _token_key(conn, c)),
    }
    if show["articles"]:
        rows = conn.execute(
            "SELECT a.id, s.title, a.category " + PUBLISHED + " ORDER BY a.position, s.title", {"c": cid}
        ).fetchall()
        cats: dict[str, list] = {}
        for r in rows:
            cats.setdefault(r["category"], []).append({"id": str(r["id"]), "title": r["title"]})
        out["categories"] = [{"name": k, "articles": v} for k, v in cats.items()]
    if show["hours"]:
        key = {"settings": {"hours": s["hours"]}}
        out["hours"] = {"text": widget.hours_text(key), "open_now": widget.open_now(key, tz), "timezone": tz}
    if show["channels"]:
        out["channels"] = _channel_links(conn, c)
    return out


def article(conn: psycopg.Connection, c: dict, article_id: str) -> dict:
    if not c["settings"]["show"]["articles"]:
        raise SelfServiceError("Article not found.", 404)
    try:
        row = conn.execute(
            "SELECT a.id, s.title, s.body, s.source_url, a.category, s.updated_at "
            + PUBLISHED
            + " AND a.id = %(a)s::uuid",
            {"c": c["customer_id"], "a": article_id},
        ).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        row = None
    if row is None:
        raise SelfServiceError("Article not found.", 404)
    return {**row, "id": str(row["id"])}


def search(conn: psycopg.Connection, c: dict, q: str, limit: int = 10) -> list[dict]:
    """Published articles matching the words, best first."""
    sh = c["settings"]["show"]
    if not (sh["articles"] and sh["search"]) or not q.strip():
        return []
    rows = conn.execute(
        """SELECT a.id, s.title, a.category,
                  ts_headline('english', s.body, websearch_to_tsquery('english', %(q)s),
                              'MaxFragments=1, MaxWords=25, MinWords=8, StartSel="", StopSel=""') AS snippet,
                  ts_rank(setweight(to_tsvector('english', s.title), 'A') || to_tsvector('english', s.body),
                          websearch_to_tsquery('english', %(q)s)) AS rank """
        + PUBLISHED
        + """ AND (setweight(to_tsvector('english', s.title), 'A') || to_tsvector('english', s.body))
                  @@ websearch_to_tsquery('english', %(q)s)
              ORDER BY rank DESC, s.title LIMIT %(n)s""",
        {"c": c["customer_id"], "q": q[:200], "n": limit},
    ).fetchall()
    return [{"id": str(r["id"]), "title": r["title"], "category": r["category"], "snippet": r["snippet"]} for r in rows]


def _token_key(conn: psycopg.Connection, c: dict) -> dict | None:
    """The website chat key whose secret signs the business's own sign-in tokens."""
    kid = c["settings"].get("widget_key_id") or ""
    return conn.execute(
        """SELECT * FROM widget_keys WHERE customer_id = %s AND active AND (%s = '' OR id::text = %s)
           ORDER BY created_at LIMIT 1""",
        (c["customer_id"], kid, kid),
    ).fetchone()


# ---- Ask and the contact form ---------------------------------------------------------


def help_identity(conn: psycopg.Connection, customer_id: Any, contact_id: Any) -> dict:
    """The website-chat address a signed-in end user writes from on the help centre."""
    return inbox.find_or_create_identity(
        conn, customer_id, "web", f"help:{contact_id}", contact_id=contact_id, verified=True
    )


def ask(conn: psycopg.Connection, c: dict, question: str, client_id: str, session: dict | None) -> dict:
    """Put a question to the customer AI agent. Returns what the customer may see."""
    from ..ai import agent

    cid = c["customer_id"]
    if not (c["settings"]["show"]["ask"] and ask_available(conn, cid)):
        raise SelfServiceError("Ask isn't available on this help centre.", 409)
    question = " ".join(question.split())
    if len(question) < 3:
        raise SelfServiceError("Write a question first.", 422)
    if session:
        address = help_identity(conn, cid, session["contact_id"])["address"]
    else:
        address = f"help:visitor:{secrets.token_urlsafe(16)}"
    got = inbox.receive(
        conn,
        cid,
        "web",
        address,
        question,
        external_id=f"help-ask:{c['slug']}:{client_id}",
        subject=ASK_SUBJECT,
        verified=bool(session),
    )
    conv, msg = got["conversation"], got["message"]
    if not got["duplicate"]:
        events.emit(conn, cid, "help.asked", {"conversation_id": str(conv["id"])}, conv["id"])
        if conv["handler"] == "none" and conv["handler_user_id"] is None:
            conv = inbox.set_handler(conn, conv, "ai", None, "help centre", "asked in the help centre")
        if conv["handler"] == "ai":
            agent.respond(conn, cid, conv["id"], msg["id"])
    replies = conn.execute(
        """SELECT * FROM messages WHERE conversation_id = %s AND direction = 'out' AND created_at >= %s
           ORDER BY created_at, id""",
        (conv["id"], msg["created_at"]),
    ).fetchall()
    run = conn.execute(
        "SELECT outcome FROM ai_runs WHERE message_id = %s AND role = 'customer_agent'", (msg["id"],)
    ).fetchone()
    outcome = run["outcome"] if run else ("waiting" if conv["handler"] == "human" else "none")
    answered = outcome in ("replied", "proposed")
    if not answered and not session and not got["duplicate"]:
        # Nobody can reach an anonymous visitor: close it, so it doesn't wait in
        # the inbox. The question is kept (and recorded as a knowledge gap).
        inbox.set_state(
            conn,
            cid,
            conv["id"],
            "resolved",
            actor="help centre",
            reason="asked anonymously in the help centre; pointed to the contact form",
        )
    out = {
        "answered": answered,
        "outcome": outcome,
        "replies": [widget.public_message(m) for m in replies] if answered else [],
        "conversation_id": str(conv["id"]) if session else None,
    }
    if not answered:
        out["message"] = "A member of the team will reply in your conversations." if session else NOT_ANSWERED
    return out


def contact(
    conn: psycopg.Connection, c: dict, *, name: str, email: str, message: str, client_id: str, session: dict | None
) -> dict:
    """The contact form: a website-chat conversation for the team."""
    cid = c["customer_id"]
    if not c["settings"]["show"]["contact"]:
        raise SelfServiceError("The contact form isn't available on this help centre.", 409)
    text = message.strip()
    if not text:
        raise SelfServiceError("Write your message first.", 422)
    if session:
        address = help_identity(conn, cid, session["contact_id"])["address"]
    else:
        email = email.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise SelfServiceError("Give an email address so the team can reply.", 422)
        address = f"help:visitor:{secrets.token_urlsafe(16)}"
    got = inbox.receive(
        conn,
        cid,
        "web",
        address,
        text,
        external_id=f"help-contact:{c['slug']}:{client_id}",
        name=name.strip()[:200],
        subject=CONTACT_SUBJECT,
        verified=bool(session),
    )
    conv = got["conversation"]
    if not got["duplicate"]:
        if not session:
            conn.execute("UPDATE contacts SET email = %s WHERE id = %s AND email = ''", (email, conv["contact_id"]))
            inbox.add_note(
                conn,
                cid,
                conv["id"],
                author="Jibsy",
                body=(
                    f"Sent from the help centre without signing in. The email address ({email}) was typed, "
                    "not checked: reply by starting an email conversation to it."
                ),
            )
        events.emit(conn, cid, "help.contact_form", {"conversation_id": str(conv["id"])}, conv["id"])
    return {
        "ok": True,
        "conversation_id": str(conv["id"]) if session else None,
        "message": "Thanks. The team will reply in your conversations here."
        if session
        else "Thanks. The team will reply by email.",
    }


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
