"""Self-service API (ADR 0037).

- `router` (signed in, under /customers/{customer_id}):
  - /me/...: the signed-in person's own settings. The person is always the
    caller; no endpoint takes a user id, so nobody can change anyone else's.
  - /help-centre/...: business admins switch the help centre on, choose what
    it shows, publish articles and handle data requests.
- `public` (no staff sign-in, under /help/{slug}): the help centre for the
  business's own customers, and their sign-in and self-service. Writes need
  the header X-Requested-With: exa-help, which a cross-site form can't send.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.auth import _client_ip
from ...api.deps import UserDep
from ...identity import sessions as staff_sessions
from ...security import token_hash
from .. import access
from ..enterprise import governance
from ..selfservice import enduser, helpcentre, staff
from ..voice import provisioning
from ..voice import selfservice as voice_self
from .common import errors

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: self-service"])
public = APIRouter(prefix="/help/{slug}", tags=["commai: help centre (public)"])

HELP_COOKIE = "exa_help"
HELP_HEADER = "x-help-session"
CSRF_VALUE = "exa-help"


# ---- my settings -------------------------------------------------------------------------


class Notify(BaseModel):
    in_app: bool | None = None
    email: bool | None = None


class QuietHours(BaseModel):
    start: str = Field(default="", max_length=5)
    end: str = Field(default="", max_length=5)


class MeSettingsIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    given_name: str | None = Field(default=None, max_length=200)
    family_name: str | None = Field(default=None, max_length=200)
    language: str | None = Field(default=None, max_length=12)
    languages_spoken: list[str] | None = Field(default=None, max_length=20)
    notify: dict[Literal["assignment", "mention", "sla_warning"], Notify] | None = None
    quiet_hours: QuietHours | None = None
    timezone: str | None = Field(default=None, max_length=60)
    availability: Literal["online", "away", "offline"] | None = None


def _me_check(conn, user, customer_id: str) -> None:
    if user.role == "customer" and access.seat(conn, user, customer_id) not in ("agent", "internal"):
        raise HTTPException(403, "You don't have a Jibsy seat in this business.")


@router.get("/me/settings")
def my_settings(customer_id: str, user: UserDep) -> dict:
    """Your own profile, language, notifications, quiet hours and availability."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _me_check(conn, user, customer_id)
        return staff.profile(conn, customer_id, user.id)


@router.patch("/me/settings")
def update_my_settings(customer_id: str, body: MeSettingsIn, user: UserDep) -> dict:
    """Change your own settings. There is no way to name another person."""
    access.check(user, customer_id, "commai:write")
    changes = body.model_dump(exclude_unset=True)
    if "notify" in changes:
        changes["notify"] = {k: {w: x for w, x in v.items() if x is not None} for k, v in changes["notify"].items()}
    with db.tx() as conn, errors():
        _me_check(conn, user, customer_id)
        out = staff.update(conn, customer_id, user.id, changes)
        audit.record(conn, user.actor, "commai.me.settings", str(user.id), customer_id, {"fields": sorted(changes)})
    return out


class AvailabilityIn(BaseModel):
    availability: Literal["online", "away", "offline"]


@router.put("/me/availability")
def set_my_availability(customer_id: str, body: AvailabilityIn, user: UserDep) -> dict:
    """Online, away or offline. Only people who are online get new conversations."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        _me_check(conn, user, customer_id)
        staff.update(conn, customer_id, user.id, {"availability": body.availability})
        audit.record(conn, user.actor, "commai.me.availability", body.availability, customer_id)
    return {"availability": body.availability}


@router.get("/me/notifications")
def my_notifications(customer_id: str, user: UserDep, limit: int = Query(30, ge=1, le=100)) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _me_check(conn, user, customer_id)
        return staff.notifications(conn, customer_id, user.id, user.email, limit)


def _current_hash(request: Request) -> str | None:
    tok, _ = staff_sessions.request_token(request)
    return token_hash(tok) if tok else None


def _session_only(user) -> None:
    if user.via == "key":
        raise HTTPException(403, "Manage your sessions from the portal, not with an API key.")


@router.get("/me/sessions")
def my_sessions(customer_id: str, user: UserDep, request: Request) -> list[dict]:
    """Where you are signed in. `current` marks this one."""
    access.check(user, customer_id, "commai:read")
    _session_only(user)
    with db.tx() as conn:
        return staff.sessions(conn, user.id, _current_hash(request))


@router.post("/me/sessions/sign-out-others")
def sign_out_others(customer_id: str, user: UserDep, request: Request) -> dict:
    access.check(user, customer_id, "commai:read")
    _session_only(user)
    with db.tx() as conn:
        n = staff.end_other_sessions(conn, user.id, _current_hash(request))
        audit.record(conn, user.actor, "commai.me.sign_out_others", str(user.id), customer_id, {"ended": n})
    return {"ended": n}


@router.delete("/me/sessions/{session_id}", status_code=204)
def end_my_session(customer_id: str, session_id: str, user: UserDep, request: Request) -> None:
    access.check(user, customer_id, "commai:read")
    _session_only(user)
    with db.tx() as conn:
        if not staff.end_session(conn, user.id, session_id, _current_hash(request)):
            raise HTTPException(404, "Session not found (use Sign out for this one).")
        audit.record(conn, user.actor, "commai.me.sign_out_session", session_id, customer_id)


@router.post("/me/softphone-link")
def my_softphone_link(customer_id: str, user: UserDep, request: Request) -> dict:
    """A fresh sign-in link for your own softphone (also the text for its QR
    code). The previous link stops working."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        me = voice_self.mine(conn, customer_id, user.id)
        d = conn.execute(
            """SELECT id FROM voice_devices WHERE customer_id = %s AND voice_user_id = %s AND kind = 'softphone'
               AND status <> 'removed' ORDER BY created_at LIMIT 1""",
            (customer_id, me["id"]),
        ).fetchone()
        if d is None:
            raise HTTPException(404, "You don't have a softphone yet. Ask your company's voice admin to add one.")
        link = provisioning.issue_device_link(conn, customer_id, d["id"])
        audit.record(conn, user.actor, "commai.me.softphone_link", str(d["id"]), customer_id)
    base = str(request.base_url).rstrip("/")
    return {"device_id": str(d["id"]), "join_link": f"{base}/api/v1/commai/voice/softphone/join?token={link['token']}"}


# ---- help centre admin ---------------------------------------------------------------------


def _help_admin(conn, user, customer_id: str) -> None:
    if access.seat(conn, user, customer_id) == "internal":
        raise HTTPException(403, "Your seat can't change settings.")


def _admin_out(conn, customer_id: str, request: Request) -> dict:
    c = helpcentre.centre(conn, customer_id)
    base = request.app.state.settings.public_url or str(request.base_url).rstrip("/")
    return {
        "slug": c["slug"],
        "enabled": c["enabled"],
        "settings": c["settings"],
        "url": f"{base}/help/{c['slug']}",
        "updated_by": c["updated_by"],
        "updated_at": c["updated_at"],
        "articles": helpcentre.article_admin_list(conn, customer_id),
        "preview": helpcentre.home(conn, c),
        "widget_keys": conn.execute(
            "SELECT id, name FROM widget_keys WHERE customer_id = %s AND active ORDER BY created_at", (customer_id,)
        ).fetchall(),
        "open_data_requests": conn.execute(
            "SELECT count(*) AS n FROM ss_data_requests WHERE customer_id = %s AND status = 'open'", (customer_id,)
        ).fetchone()["n"],
    }


@router.get("/help-centre")
def get_help_centre(customer_id: str, user: UserDep, request: Request) -> dict:
    """The help centre's settings, its articles and a preview of the public page."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _help_admin(conn, user, customer_id)
        return _admin_out(conn, customer_id, request)


class HelpCentreIn(BaseModel):
    slug: str | None = Field(default=None, max_length=50)
    enabled: bool | None = None
    settings: dict | None = None


@router.patch("/help-centre")
def update_help_centre(customer_id: str, body: HelpCentreIn, user: UserDep, request: Request) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _help_admin(conn, user, customer_id)
        helpcentre.update(
            conn, customer_id, actor=user.actor, slug=body.slug, enabled=body.enabled, settings=body.settings
        )
        audit.record(
            conn,
            user.actor,
            "commai.help_centre.update",
            "",
            customer_id,
            {"fields": sorted(body.model_dump(exclude_none=True)), "enabled": body.enabled},
        )
        return _admin_out(conn, customer_id, request)


class ArticleIn(BaseModel):
    published: bool
    category: str | None = Field(default=None, max_length=60)
    position: int | None = Field(default=None, ge=0, le=10000)


@router.put("/help-centre/articles/{source_id}")
def publish_article(customer_id: str, source_id: str, body: ArticleIn, user: UserDep) -> dict:
    """Publish an approved knowledge source as a help article, or withdraw it."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _help_admin(conn, user, customer_id)
        out = helpcentre.publish(
            conn,
            customer_id,
            source_id,
            published=body.published,
            actor=user.actor,
            category=body.category,
            position=body.position,
        )
        audit.record(
            conn,
            user.actor,
            "commai.help_centre.publish" if body.published else "commai.help_centre.withdraw",
            source_id,
            customer_id,
        )
    return out


@router.get("/help-centre/data-requests")
def list_data_requests(customer_id: str, user: UserDep, status: str = "open") -> list[dict]:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _help_admin(conn, user, customer_id)
        return conn.execute(
            """SELECT r.*, ct.name AS contact_name, ct.email AS contact_email FROM ss_data_requests r
               LEFT JOIN contacts ct ON ct.id = r.contact_id
               WHERE r.customer_id = %s AND (%s = 'all' OR r.status = %s) ORDER BY r.created_at DESC LIMIT 200""",
            (customer_id, status, status),
        ).fetchall()


class DataRequestIn(BaseModel):
    status: Literal["done", "refused"]
    # For a deletion: remove the contact and their conversations, or keep the
    # conversation shells with everything that identifies the person removed.
    mode: Literal["delete", "anonymise"] = "delete"


@router.post("/help-centre/data-requests/{request_id}")
def handle_data_request(customer_id: str, request_id: str, body: DataRequestIn, user: UserDep) -> dict:
    """Close a request from the help centre. "done" carries it out through the
    business's data governance (ADR 0030): a download is recorded as a subject
    export, a deletion erases or anonymises the contact. A legal hold refuses it."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _help_admin(conn, user, customer_id)
        row = conn.execute(
            "SELECT * FROM ss_data_requests WHERE id = %s AND customer_id = %s AND status = 'open' FOR UPDATE",
            (request_id, customer_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "No open request with that id.")
        try:
            subject_id, status, reason = _carry_out(conn, customer_id, row, body, user.actor)
        except governance.DataError as e:
            raise HTTPException(getattr(e, "code", 400), str(e)) from e
        row = conn.execute(
            """UPDATE ss_data_requests SET status = %s, handled_by = %s, handled_at = now(), reason = %s,
                      subject_request_id = %s
               WHERE id = %s RETURNING *""",
            (status, user.actor, reason, subject_id, request_id),
        ).fetchone()
        audit.record(conn, user.actor, f"commai.help.data_request.{status}", request_id, customer_id)
    return row


def _carry_out(conn, customer_id: str, row: dict, body: DataRequestIn, actor: str) -> tuple:
    """(subject request id, status, reason) after doing what the person asked."""
    if body.status != "done" or row["contact_id"] is None:
        return None, body.status, ""
    if row["kind"] == "delete":
        out = governance.subject_erase(conn, customer_id, row["contact_id"], body.mode, actor)
        req = out.get("request") or {}
        return req.get("id"), ("refused" if out.get("refused") else "done"), out.get("reason", "")
    governance.subject_export(conn, customer_id, row["contact_id"], actor)
    last = conn.execute(
        """SELECT id FROM commai_subject_requests WHERE customer_id = %s AND contact_id = %s AND kind = 'export'
           ORDER BY created_at DESC LIMIT 1""",
        (customer_id, row["contact_id"]),
    ).fetchone()
    return (last["id"] if last else None), "done", ""


@router.get("/help-centre/data-requests/{request_id}/export")
def export_data_request(customer_id: str, request_id: str, user: UserDep) -> dict:
    """The person's data to send them: their record, addresses and
    customer-facing messages. Private notes are never included."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, errors():
        _help_admin(conn, user, customer_id)
        r = conn.execute(
            "SELECT * FROM ss_data_requests WHERE id = %s AND customer_id = %s", (request_id, customer_id)
        ).fetchone()
        if r is None or r["contact_id"] is None:
            raise HTTPException(404, "Request not found.")
        audit.record(conn, user.actor, "commai.help.data_export", request_id, customer_id)
        return enduser.export_contact(conn, customer_id, r["contact_id"])


# ---- public help centre ----------------------------------------------------------------------


def _token(request: Request) -> str | None:
    return request.headers.get(HELP_HEADER) or request.cookies.get(HELP_COOKIE) or None


def _write(request: Request) -> None:
    if request.headers.get("x-requested-with") != CSRF_VALUE:
        raise HTTPException(403, "This request did not come from the help centre.")


def _ctx(conn, slug: str, request: Request) -> tuple[dict, dict | None]:
    c = helpcentre.find(conn, slug)
    return c, enduser.session(conn, c, _token(request))


def _set_cookie(response: Response, request: Request, slug: str, s: dict) -> None:
    response.set_cookie(
        HELP_COOKIE,
        s["token"],
        max_age=enduser.SESSION_HOURS * 3600,
        path=f"/api/v1/commai/help/{slug}",
        httponly=True,
        samesite="strict",
        secure=request.app.state.settings.cookie_secure,
    )


@public.get("")
def help_home(slug: str, request: Request) -> dict:
    """The public page: articles by category, hours, channels and what is switched on."""
    with db.tx() as conn, errors():
        c, s = _ctx(conn, slug, request)
        return {**helpcentre.home(conn, c), "signed_in": enduser.me(conn, s) if s else None}


@public.get("/articles/{article_id}")
def help_article(slug: str, article_id: str) -> dict:
    with db.tx() as conn, errors():
        return helpcentre.article(conn, helpcentre.find(conn, slug), article_id)


@public.get("/search")
def help_search(slug: str, q: str = Query("", max_length=200)) -> list[dict]:
    with db.tx() as conn, errors():
        return helpcentre.search(conn, helpcentre.find(conn, slug), q)


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    client_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@public.post("/ask")
def help_ask(slug: str, body: AskIn, request: Request) -> dict:
    """Ask the business's AI agent. It answers only from approved content and
    passes anything else to a person."""
    _write(request)
    with db.tx() as conn, errors():
        c, s = _ctx(conn, slug, request)
        if not helpcentre.hit(conn, c["customer_id"], "ask", _client_ip(request) or "-"):
            raise HTTPException(429, "Too many questions. Try again in a few minutes.")
    with db.tx() as conn, errors():
        c, s = _ctx(conn, slug, request)
        return helpcentre.ask(conn, c, body.question, body.client_id, s)


class ContactFormIn(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=255)
    message: str = Field(min_length=1, max_length=4000)
    client_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@public.post("/contact", status_code=201)
def help_contact(slug: str, body: ContactFormIn, request: Request) -> dict:
    _write(request)
    with db.tx() as conn, errors():
        c, s = _ctx(conn, slug, request)
        if not helpcentre.hit(conn, c["customer_id"], "contact", _client_ip(request) or "-"):
            raise HTTPException(429, "Too many messages. Try again in a few minutes.")
    with db.tx() as conn, errors():
        c, s = _ctx(conn, slug, request)
        return helpcentre.contact(
            conn, c, name=body.name, email=body.email, message=body.message, client_id=body.client_id, session=s
        )


class SignInIn(BaseModel):
    email: str = Field(min_length=1, max_length=255)


@public.post("/signin", status_code=202)
def help_signin(slug: str, body: SignInIn, request: Request) -> dict:
    """Email a one-time sign-in link. The answer is the same whether or not
    the address is known."""
    _write(request)
    base = request.app.state.settings.public_url or str(request.base_url).rstrip("/")
    with db.tx() as conn, errors():
        c = helpcentre.find(conn, slug)
        enduser.request_link(conn, c, body.email, _client_ip(request), base)
    return {"message": enduser.SIGNIN_ANSWER}


class LinkIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


@public.post("/signin/link")
def help_redeem(slug: str, body: LinkIn, request: Request, response: Response) -> dict:
    """Use the emailed link (once). Sets the help centre session cookie."""
    _write(request)
    with db.tx() as conn, errors():
        c = helpcentre.find(conn, slug)
        s = enduser.redeem_link(conn, c, body.token)
    _set_cookie(response, request, c["slug"], s)
    return s


class BusinessTokenIn(BaseModel):
    user_token: str = Field(min_length=10, max_length=4000)


@public.post("/signin/token")
def help_signin_token(slug: str, body: BusinessTokenIn, request: Request, response: Response) -> dict:
    """Sign in with a token the business's own website signed (as for website chat)."""
    _write(request)
    with db.tx() as conn, errors():
        c = helpcentre.find(conn, slug)
        s = enduser.signin_with_token(conn, c, body.user_token)
    _set_cookie(response, request, c["slug"], s)
    return s


@public.post("/signout", status_code=204)
def help_signout(slug: str, request: Request, response: Response) -> None:
    _write(request)
    with db.tx() as conn, errors():
        c = helpcentre.find(conn, slug)
        enduser.sign_out(conn, c, _token(request))
    response.delete_cookie(HELP_COOKIE, path=f"/api/v1/commai/help/{c['slug']}")


def _signed(conn, slug: str, request: Request) -> tuple[dict, dict]:
    c = helpcentre.find(conn, slug)
    return c, enduser.require(conn, c, _token(request))


@public.get("/me")
def help_me(slug: str, request: Request) -> dict:
    with db.tx() as conn, errors():
        _, s = _signed(conn, slug, request)
        return enduser.me(conn, s)


@public.get("/me/conversations")
def help_conversations(slug: str, request: Request) -> list[dict]:
    with db.tx() as conn, errors():
        _, s = _signed(conn, slug, request)
        return enduser.conversations(conn, s)


@public.get("/me/conversations/{conversation_id}")
def help_conversation(slug: str, conversation_id: str, request: Request) -> dict:
    with db.tx() as conn, errors():
        _, s = _signed(conn, slug, request)
        return enduser.conversation(conn, s, conversation_id)


class EndUserReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    client_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@public.post("/me/conversations/{conversation_id}/messages", status_code=201)
def help_reply(slug: str, conversation_id: str, body: EndUserReplyIn, request: Request) -> dict:
    _write(request)
    with db.tx() as conn, errors():
        _, s = _signed(conn, slug, request)
        return enduser.reply(conn, s, conversation_id, body.body, body.client_id)


@public.get("/me/bookings")
def help_bookings(slug: str, request: Request) -> list[dict]:
    with db.tx() as conn, errors():
        c, s = _signed(conn, slug, request)
        if not c["settings"]["show"]["bookings"]:
            return []
        return enduser.bookings(conn, s)


class RescheduleIn(BaseModel):
    start: str = Field(min_length=10, max_length=40, description="New start, like 2026-10-12T14:00:00Z")


@public.post("/me/bookings/{run_id}/reschedule")
def help_reschedule(slug: str, run_id: str, body: RescheduleIn, request: Request) -> dict:
    _write(request)
    with db.tx() as conn, errors():
        c, s = _signed(conn, slug, request)
        return enduser.reschedule(conn, c, s, run_id, body.start)


@public.post("/me/bookings/{run_id}/cancel")
def help_cancel(slug: str, run_id: str, request: Request) -> dict:
    _write(request)
    with db.tx() as conn, errors():
        c, s = _signed(conn, slug, request)
        return enduser.cancel(conn, c, s, run_id)


@public.get("/me/preferences")
def help_preferences(slug: str, request: Request) -> dict:
    with db.tx() as conn, errors():
        _, s = _signed(conn, slug, request)
        return enduser.preferences(conn, s)


class PreferenceIn(BaseModel):
    receive: bool


@public.put("/me/preferences/{identity_id}")
def help_set_preference(slug: str, identity_id: str, body: PreferenceIn, request: Request) -> dict:
    _write(request)
    with db.tx() as conn, errors():
        c, s = _signed(conn, slug, request)
        out = enduser.set_preference(conn, s, identity_id, body.receive)
        audit.record(
            conn,
            f"contact:{s['contact_id']}",
            "commai.help.opt_in" if body.receive else "commai.help.opt_out",
            identity_id,
            c["customer_id"],
        )
        return out


class LanguageIn(BaseModel):
    language: str = Field(default="", max_length=12)


@public.put("/me/language")
def help_set_language(slug: str, body: LanguageIn, request: Request) -> dict:
    _write(request)
    with db.tx() as conn, errors():
        _, s = _signed(conn, slug, request)
        return enduser.set_language(conn, s, body.language)


@public.get("/me/data-requests")
def help_data_requests(slug: str, request: Request) -> list[dict]:
    with db.tx() as conn, errors():
        _, s = _signed(conn, slug, request)
        return enduser.my_requests(conn, s)


class EndUserDataIn(BaseModel):
    kind: Literal["download", "delete"]
    detail: str = Field(default="", max_length=1000)


@public.post("/me/data-requests", status_code=201)
def help_data_request(slug: str, body: EndUserDataIn, request: Request) -> dict:
    _write(request)
    with db.tx() as conn, errors():
        c, s = _signed(conn, slug, request)
        return enduser.data_request(conn, c, s, body.kind, body.detail)
