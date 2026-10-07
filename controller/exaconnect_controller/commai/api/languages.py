"""Languages API (ADR 0026): catalogues, each person's language, the AI's reply
languages, reviewer sign-off, the widget's strings and the satisfaction form.

- Signed in: /customers/{customer_id}/languages (read; set my language; set the
  AI reply languages), /i18n/catalogues/{locale} (read), and ExaCarib admins
  sign a catalogue off at /i18n/catalogues/{locale}/review.
- Public: /i18n/widget/{public_key}?lang= (strings for a switched-on language
  only) and /csat/{token} (the one-page satisfaction form).
"""

from __future__ import annotations

import html
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import User, UserDep, require_admin
from .. import access, csat, i18n, inbox
from ..ai import language, runtime

router = APIRouter(tags=["commai: languages"])
public = APIRouter(tags=["commai: languages"])


def _err(e: i18n.LocaleError) -> HTTPException:
    return HTTPException(e.code, str(e))


@router.get("/customers/{customer_id}/languages")
def languages(customer_id: str, user: UserDep) -> dict:
    """Every interface language with its review status and whether it is offered
    here, the signed-in person's language, and the AI's reply languages."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        prof = runtime.profile(conn, customer_id)
        return {
            "source": i18n.SOURCE,
            "my_locale": i18n.user_locale(conn, user.id, customer_id),
            "locales": i18n.describe(conn, customer_id),
            "ai": {
                "business_language": prof["business_language"],
                "languages": prof["languages"],
                "options": [{"code": c, "name": n} for c, n in language.NAMES.items()],
            },
        }


class MyLocaleIn(BaseModel):
    locale: str = Field(min_length=2, max_length=10)


@router.put("/customers/{customer_id}/languages/me")
def set_my_locale(customer_id: str, body: MyLocaleIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    if user.via == "key":
        raise HTTPException(403, "Each person picks their own language when signed in.")
    try:
        with db.tx() as conn:
            loc = i18n.set_user_locale(conn, user.id, customer_id, body.locale)
            audit.record(conn, user.actor, "commai.languages.me", loc, customer_id)
    except i18n.LocaleError as e:
        raise _err(e) from e
    return {"locale": loc}


class AiLanguagesIn(BaseModel):
    languages: list[str] = Field(min_length=1, max_length=20)


@router.put("/customers/{customer_id}/languages/ai")
def set_ai_languages(customer_id: str, body: AiLanguagesIn, user: UserDep) -> dict:
    """The languages the AI agent answers customers in (the language picker)."""
    access.check(user, customer_id, "commai:admin")
    langs = sorted({x.strip().lower() for x in body.languages if x.strip()})
    unknown = [x for x in langs if x not in language.NAMES]
    if unknown:
        raise HTTPException(422, f"The AI can't answer in {', '.join(unknown)}.")
    with db.tx() as conn:
        if access.seat(conn, user, customer_id) == "internal":
            raise HTTPException(403, "Your seat can't change AI settings.")
        prof = runtime.profile(conn, customer_id)
        if prof["business_language"] not in langs:
            langs = sorted({*langs, prof["business_language"]})
        current = (inbox.settings(conn, customer_id)["config"] or {}).get("ai") or {}
        from psycopg.types.json import Jsonb

        conn.execute(
            """UPDATE commai_settings SET config = jsonb_set(config, '{ai}', %s), updated_at = now()
               WHERE customer_id = %s""",
            (Jsonb({**current, "languages": langs}), customer_id),
        )
        audit.record(
            conn, user.actor, "commai.languages.ai", "", customer_id, {"before": prof["languages"], "after": langs}
        )
    return {"languages": langs}


@router.get("/i18n/catalogues/{locale}")
def get_catalogue(locale: str, user: UserDep) -> dict:
    """The catalogue (English filling any key a draft lacks) and its review status."""
    try:
        with db.tx() as conn:
            rs = i18n.review_status(conn, locale)
        return {
            "locale": locale,
            "status": rs["status"],
            "machine_drafted": rs["status"] == "machine-drafted",
            "missing": i18n.missing_keys(locale),
            "messages": i18n.merged(locale),
        }
    except i18n.LocaleError as e:
        raise _err(e) from e


class ReviewIn(BaseModel):
    note: str = Field(default="", max_length=500)


@router.post("/i18n/catalogues/{locale}/review")
def sign_off(locale: str, body: ReviewIn, user: User = Depends(require_admin)) -> dict:
    """A reviewer signs off this exact version of a draft catalogue."""
    try:
        with db.tx() as conn:
            out = i18n.sign_off(conn, locale, user.actor, body.note)
            audit.record(conn, user.actor, "commai.i18n.review", locale, None, {"hash": i18n.catalogue_hash(locale)})
            return out
    except i18n.LocaleError as e:
        raise _err(e) from e


@router.delete("/i18n/catalogues/{locale}/review")
def withdraw_sign_off(locale: str, user: User = Depends(require_admin)) -> dict:
    try:
        with db.tx() as conn:
            out = i18n.withdraw(conn, locale)
            audit.record(conn, user.actor, "commai.i18n.review_withdrawn", locale, None)
            return out
    except i18n.LocaleError as e:
        raise _err(e) from e


# ---- public ------------------------------------------------------------------------------


@public.get("/i18n/widget/{public_key}")
def widget_strings(public_key: str, lang: str = "") -> JSONResponse:
    """The chat widget's words in the visitor's language, when that language is
    switched on for the business; English otherwise."""
    with db.tx() as conn:
        k = conn.execute(
            "SELECT customer_id FROM widget_keys WHERE public_key = %s AND active", (public_key,)
        ).fetchone()
        if k is None:
            return JSONResponse({"detail": "This chat key is not active."}, status_code=404)
        wanted = [x.strip() for x in lang.split(",") if x.strip()][:5]
        loc = i18n.SOURCE
        for w in wanted:
            cand = w if w in i18n.LOCALES else i18n.for_language(w.split("-")[0].lower())
            if i18n.available(conn, cand, k["customer_id"]):
                loc = cand
                break
        rs = i18n.review_status(conn, loc)
    return JSONResponse(
        {"locale": loc, "machine_drafted": rs["status"] == "machine-drafted", "strings": i18n.widget_strings(loc)},
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"},
    )


_PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
body{{margin:0;background:#F3F6FB;color:#10213D;
 font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}}
main{{max-width:480px;margin:40px auto;padding:24px;background:#fff;border:1px solid #DFE6EE;border-radius:8px}}
h1{{font-size:22px;font-weight:650;letter-spacing:-.01em;margin:0 0 8px}}
.r{{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}}
button{{font:inherit;border:1px solid #DFE6EE;background:#fff;border-radius:5px;padding:10px 12px;cursor:pointer}}
button:hover,button:focus{{border-color:#155EEF;outline:none}}
textarea{{width:100%;box-sizing:border-box;font:inherit;border:1px solid #DFE6EE;border-radius:5px;padding:8px}}
p.m{{color:#52647A;font-size:14px}}
</style></head><body><main>{body}</main></body></html>"""


def _t(msgs: dict, key: str, **v) -> str:
    return html.escape(msgs.get(key, key).format(**v))


def _csat_page(token: str, done: bool = False) -> HTMLResponse:
    with db.tx() as conn:
        s = csat.by_token(conn, token)
        loc = i18n.SOURCE
        if s:
            loc = i18n.for_language(s["language"] or "")
            if not i18n.available(conn, loc, s["customer_id"]):
                loc = i18n.SOURCE
    msgs = i18n.merged(loc)
    headers = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}
    if s is None:
        body = f"<h1>{_t(msgs, 'csat.title')}</h1><p>{_t(msgs, 'csat.gone')}</p>"
        return HTMLResponse(_PAGE.format(lang=loc, title=_t(msgs, "csat.title"), body=body), 404, headers=headers)
    biz = s["business"]
    if done or s["status"] == "answered":
        body = f"<h1>{_t(msgs, 'csat.title')}</h1><p>{_t(msgs, 'csat.thanks', business=biz)}</p>"
    else:
        buttons = "".join(
            f'<button type="submit" name="rating" value="{i}">{i} · {_t(msgs, f"csat.r{i}")}</button>'
            for i in range(1, 6)
        )
        body = (
            f"<h1>{_t(msgs, 'csat.title')}</h1><p>{_t(msgs, 'csat.question', business=biz)}</p>"
            f'<form method="post"><label>{_t(msgs, "csat.comment")}<textarea name="comment" rows="3" '
            f'maxlength="1000"></textarea></label><div class="r">{buttons}</div></form>'
        )
    return HTMLResponse(_PAGE.format(lang=loc, title=_t(msgs, "csat.title"), body=body), headers=headers)


@public.get("/csat/{token}", include_in_schema=False)
def csat_form(token: str) -> HTMLResponse:
    return _csat_page(token)


@public.post("/csat/{token}", include_in_schema=False)
async def csat_answer(token: str, request: Request) -> HTMLResponse:
    raw = (await request.body())[:20_000].decode("utf-8", "replace")
    form = {k: v[0] for k, v in parse_qs(raw).items()}
    try:
        rating = int(form.get("rating", "0"))
    except ValueError:
        rating = 0
    comment = form.get("comment", "")
    try:
        with db.tx() as conn:
            row = csat.answer(conn, token, rating, comment)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if row is None:
        return _csat_page(token)
    return _csat_page(token, done=True)
