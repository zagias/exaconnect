"""Portal sign-in (CLAUDE.md §4.2, ADR 0017): local accounts with optional
two-step sign-in, Google and Microsoft sign-in and enterprise SSO through the
Keycloak gateway, and a secure HttpOnly session cookie for the portal. The
token is still returned in the body for the SDK, Terraform and lab scripts."""

from __future__ import annotations

import datetime as dt
import urllib.parse
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from .. import audit, db
from ..identity import mfa, oidc, passkeys, sessions, sso, totp
from ..security import hash_password, new_token, token_hash, verify_password
from .deps import API_KEY_PREFIX, UserDep

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(max_length=1024)


# Throttling: after this many failed sign-ins for one email (or from one
# address) in the window, further attempts are refused until it passes.
MAX_FAILURES = 10
FAILURE_WINDOW = "15 minutes"


def _client_ip(request: Request) -> str:
    # Behind the public proxy (deploy/caddy) the real address is in X-Forwarded-For.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "")


class UserOut(BaseModel):
    email: str
    role: str
    customer_id: str | None = None
    name: str = ""
    two_step: bool = False
    # None: the full account. A list: what a directory-provisioned account may do.
    scopes: list[str] | None = None


class LoginOut(BaseModel):
    token: str
    expires_at: dt.datetime
    user: UserOut


class MfaRequiredOut(BaseModel):
    mfa_required: bool = True
    challenge: str
    # Which second steps this account has: totp, passkey, recovery.
    methods: list[str] = []
    expires_in: int = mfa.CHALLENGE_MINUTES * 60


def _user_out(row: dict) -> UserOut:
    return UserOut(
        email=row["email"],
        role=row["role"],
        customer_id=str(row["customer_id"]) if row["customer_id"] else None,
        name=row.get("display_name") or "",
        two_step=bool(row.get("totp_enabled_at")),
        scopes=list(row["access_scopes"]) if row.get("access_scopes") is not None else None,
    )


def _throttle(email: str, ip: str) -> None:
    with db.tx() as conn:
        recent = conn.execute(
            f"""SELECT count(*) FILTER (WHERE lower(target) = lower(%s)) AS by_email,
                       count(*) FILTER (WHERE detail->>'ip' = %s) AS by_ip
                FROM audit_log WHERE action = 'login_failed' AND at > now() - interval '{FAILURE_WINDOW}'""",
            (email, ip),
        ).fetchone()
    if recent["by_email"] >= MAX_FAILURES or recent["by_ip"] >= MAX_FAILURES * 3:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed sign-ins. Try again in 15 minutes.")


def _failed(email: str, ip: str, reason: str, **detail) -> None:
    # Its own transaction, so the record survives the error response.
    with db.tx() as conn:
        audit.record(conn, f"user:{email}", "login_failed", email, detail={"ip": ip, "reason": reason, **detail})


def _signed_in(request: Request, response: Response, row: dict, via: str, **detail) -> LoginOut:
    with db.tx() as conn:
        token, expires = sessions.start(conn, row["id"], request.app.state.settings.session_hours, via)
        audit.record(
            conn,
            f"user:{row['email']}",
            "login",
            customer_id=row["customer_id"],
            detail={"via": via, "ip": _client_ip(request), **detail},
        )
        on = mfa.two_step_on(conn, row)
    sessions.set_cookie(response, request, token, expires)
    out = _user_out(row)
    out.two_step = on
    return LoginOut(token=token, expires_at=expires, user=out)


SSO_ONLY = "Your organisation signs in with its own single sign-on. Use Continue with your work email."


@router.post("/login", response_model=LoginOut | MfaRequiredOut)
def login(body: LoginIn, request: Request, response: Response) -> LoginOut | MfaRequiredOut:
    ip = _client_ip(request)
    _throttle(body.email, ip)
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE lower(email) = lower(%s)", (body.email,)).fetchone()
        forced = sso.requires_sso(conn, body.email) if row is None or row["role"] != "admin" else None
    if row is None or not verify_password(body.password, row["password_hash"]):
        _failed(body.email, ip, "password")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That email and password don't match.")
    if row["disabled_at"] is not None:
        _failed(body.email, ip, "disabled")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been switched off. Ask your administrator.")
    if forced:
        _failed(body.email, ip, "sso_required")
        raise HTTPException(status.HTTP_403_FORBIDDEN, SSO_ONLY)
    with db.tx() as conn:
        second = mfa.methods(conn, row) if mfa.two_step_on(conn, row) else None
    if second:
        with db.tx() as conn:
            challenge = mfa.new_challenge(conn, row["id"])
            audit.record(
                conn,
                f"user:{row['email']}",
                "login.password_ok",
                customer_id=row["customer_id"],
                detail={"ip": ip, "next": "two_step"},
            )
        return MfaRequiredOut(challenge=challenge, methods=second)
    return _signed_in(request, response, row, "password")


class MfaLoginIn(BaseModel):
    challenge: str = Field(max_length=200)
    code: str = Field(max_length=40)


@router.post("/login/mfa")
def login_mfa(body: MfaLoginIn, request: Request, response: Response) -> LoginOut:
    """The second step: a code from the authenticator app, or a recovery code."""
    ip = _client_ip(request)
    with db.tx() as conn:
        ch = mfa.open_challenge(conn, body.challenge)
        row = conn.execute("SELECT * FROM users WHERE id = %s", (ch["user_id"],)).fetchone() if ch else None
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That sign-in has expired. Start again.")
    _throttle(row["email"], ip)
    with db.tx() as conn:
        kind = mfa.check_code(conn, row, body.code) if row["disabled_at"] is None else None
        if kind:
            mfa.close_challenge(conn, body.challenge)
    if not kind:
        _failed(row["email"], ip, "two_step")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That code isn't right. Try the newest code in your app.")
    return _signed_in(request, response, row, "password+totp" if kind == "totp" else "password+recovery")


@router.get("/me")
def me(user: UserDep) -> UserOut:
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = %s", (user.id,)).fetchone()
        on = mfa.two_step_on(conn, row)
    out = _user_out(row)
    out.two_step = on
    out.scopes = list(user.scopes) if user.scopes is not None else None
    return out


@router.post("/logout", status_code=204)
def logout(user: UserDep, request: Request, response: Response) -> None:
    token, _ = sessions.request_token(request)
    with db.tx() as conn:
        if token:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash(token),))
        audit.record(conn, user.actor, "logout", customer_id=user.customer_id)
    sessions.clear_cookie(response, request)


class PasswordIn(BaseModel):
    current: str = Field(max_length=1024)
    new: str = Field(min_length=12, max_length=1024)


@router.post("/password", status_code=204)
def change_password(body: PasswordIn, user: UserDep, request: Request, response: Response) -> None:
    """Change your own password. Other sessions for the account are signed out;
    a portal (cookie) session gets a fresh session token."""
    token, from_cookie = sessions.request_token(request)
    with db.tx() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = %s", (user.id,)).fetchone()
        if not verify_password(body.current, row["password_hash"]):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your current password is not right.")
        conn.execute(
            "UPDATE users SET password_hash = %s, updated_at = now() WHERE id = %s", (hash_password(body.new), user.id)
        )
        conn.execute("DELETE FROM sessions WHERE user_id = %s AND token_hash <> %s", (user.id, token_hash(token or "")))
        if from_cookie:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash(token or ""),))
            new, expires = sessions.start(conn, user.id, request.app.state.settings.session_hours, "password")
        audit.record(conn, user.actor, "user.change_password", user.email, user.customer_id)
    if from_cookie:
        sessions.set_cookie(response, request, new, expires)


# ---- Two-step sign-in (TOTP and recovery codes) ------------------------------


class CodeIn(BaseModel):
    code: str = Field(max_length=40)


def _me_row(conn, user) -> dict:
    return conn.execute("SELECT * FROM users WHERE id = %s FOR UPDATE", (user.id,)).fetchone()


@router.get("/two-step")
def two_step_status(user: UserDep) -> dict:
    with db.tx() as conn:
        row = conn.execute("SELECT totp_enabled_at, totp_pending FROM users WHERE id = %s", (user.id,)).fetchone()
        return {
            "enabled": row["totp_enabled_at"] is not None,
            "enabled_at": row["totp_enabled_at"],
            "pending": row["totp_pending"] is not None and row["totp_enabled_at"] is None,
            "recovery_codes_left": mfa.recovery_left(conn, user.id),
            "passkeys_available": passkeys.AVAILABLE,
            "passkeys": conn.execute(
                "SELECT id, name, created_at, last_used_at FROM passkeys WHERE user_id = %s ORDER BY created_at",
                (user.id,),
            ).fetchall(),
        }


@router.post("/two-step/start")
def two_step_start(user: UserDep) -> dict:
    """A new authenticator secret. Two-step is on only after /two-step/confirm."""
    if user.via == "key":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Set up two-step sign-in from the portal, not with an API key.")
    secret = totp.new_secret()
    with db.tx() as conn:
        row = _me_row(conn, user)
        if row["totp_enabled_at"] is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Two-step sign-in is already on. Turn it off first.")
        conn.execute("UPDATE users SET totp_pending = %s WHERE id = %s", (secret, user.id))
        audit.record(conn, user.actor, "two_step.start", user.email, user.customer_id)
    return {"secret": secret, "otpauth_uri": totp.otpauth_uri(secret, user.email)}


@router.post("/two-step/confirm")
def two_step_confirm(body: CodeIn, user: UserDep) -> dict:
    """Prove the app works with one code; returns ten recovery codes, shown once."""
    with db.tx() as conn:
        row = _me_row(conn, user)
        if not row["totp_pending"] or row["totp_enabled_at"] is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Start setting up two-step sign-in first.")
        step = totp.verify(row["totp_pending"], body.code)
        if step is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That code isn't right. Check the time on your phone.")
        conn.execute(
            """UPDATE users SET totp_secret = totp_pending, totp_pending = NULL, totp_enabled_at = now(),
                 totp_last_step = %s WHERE id = %s""",
            (step, user.id),
        )
        codes = mfa.new_recovery_codes(conn, user.id)
        audit.record(conn, user.actor, "two_step.enable", user.email, user.customer_id)
    return {"enabled": True, "recovery_codes": codes}


def _require_code(conn, user, code: str) -> None:
    row = _me_row(conn, user)
    if row["totp_enabled_at"] is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Two-step sign-in is not on.")
    if not mfa.check_code(conn, row, code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That code isn't right.")


@router.post("/two-step/disable", status_code=204)
def two_step_disable(body: CodeIn, user: UserDep) -> None:
    with db.tx() as conn:
        _require_code(conn, user, body.code)
        conn.execute(
            """UPDATE users SET totp_secret = NULL, totp_pending = NULL, totp_enabled_at = NULL,
                 totp_last_step = NULL WHERE id = %s""",
            (user.id,),
        )
        if not passkeys.count(conn, user.id):
            conn.execute("DELETE FROM mfa_recovery_codes WHERE user_id = %s", (user.id,))
        audit.record(conn, user.actor, "two_step.disable", user.email, user.customer_id)


@router.post("/two-step/recovery-codes")
def two_step_new_codes(body: CodeIn, user: UserDep) -> dict:
    """Ten new recovery codes; the old ones stop working."""
    with db.tx() as conn:
        _require_code(conn, user, body.code)
        codes = mfa.new_recovery_codes(conn, user.id)
        audit.record(conn, user.actor, "two_step.recovery_codes", user.email, user.customer_id)
    return {"recovery_codes": codes}


# ---- Passkeys (WebAuthn) ----------------------------------------------------


def _passkeys_ready() -> None:
    if not passkeys.AVAILABLE:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Passkeys are not available on this controller.")


class PasskeyIn(BaseModel):
    ticket: str = Field(max_length=200)
    credential: dict
    name: str = Field(default="Passkey", max_length=60)


@router.post("/passkeys/options")
def passkey_options(user: UserDep, request: Request) -> dict:
    """Options for navigator.credentials.create(). Post the result to /auth/passkeys."""
    _passkeys_ready()
    if user.via == "key":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Add passkeys from the portal, not with an API key.")
    rp_id, _ = passkeys.rp(request.app.state.settings, request)
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = %s", (user.id,)).fetchone()
        try:
            ticket, options = passkeys.registration_options(conn, row, rp_id)
        except passkeys.PasskeyError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return {"ticket": ticket, "options": options}


@router.post("/passkeys", status_code=201)
def passkey_add(body: PasskeyIn, user: UserDep, request: Request) -> dict:
    _passkeys_ready()
    rp_id, origin = passkeys.rp(request.app.state.settings, request)
    with db.tx() as conn:
        try:
            pk = passkeys.register(conn, user.id, body.ticket, body.credential, body.name, rp_id, origin)
        except passkeys.PasskeyError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
        # The first second step also gets recovery codes, shown once.
        codes = mfa.new_recovery_codes(conn, user.id) if not mfa.recovery_left(conn, user.id) else None
        audit.record(conn, user.actor, "passkey.add", user.email, user.customer_id, {"name": pk["name"]})
    return {**pk, "recovery_codes": codes}


@router.delete("/passkeys/{passkey_id}", status_code=204)
def passkey_remove(passkey_id: int, user: UserDep) -> None:
    with db.tx() as conn:
        row = conn.execute(
            "DELETE FROM passkeys WHERE id = %s AND user_id = %s RETURNING name", (passkey_id, user.id)
        ).fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Passkey not found.")
        me_row = conn.execute("SELECT * FROM users WHERE id = %s", (user.id,)).fetchone()
        if not mfa.two_step_on(conn, me_row):
            conn.execute("DELETE FROM mfa_recovery_codes WHERE user_id = %s", (user.id,))
        audit.record(conn, user.actor, "passkey.remove", user.email, user.customer_id, {"name": row["name"]})


class ChallengeIn(BaseModel):
    challenge: str = Field(max_length=200)


@router.post("/login/passkey/options")
def login_passkey_options(body: ChallengeIn, request: Request) -> dict:
    """The second step with a passkey: options for navigator.credentials.get()."""
    _passkeys_ready()
    rp_id, _ = passkeys.rp(request.app.state.settings, request)
    with db.tx() as conn:
        ch = mfa.peek_challenge(conn, body.challenge)
        if ch is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That sign-in has expired. Start again.")
        try:
            ticket, options = passkeys.signin_options(conn, ch["user_id"], rp_id)
        except passkeys.PasskeyError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return {"ticket": ticket, "options": options}


class PasskeyLoginIn(BaseModel):
    challenge: str = Field(max_length=200)
    ticket: str = Field(max_length=200)
    credential: dict


@router.post("/login/passkey")
def login_passkey(body: PasskeyLoginIn, request: Request, response: Response) -> LoginOut:
    _passkeys_ready()
    ip = _client_ip(request)
    rp_id, origin = passkeys.rp(request.app.state.settings, request)
    with db.tx() as conn:
        ch = mfa.open_challenge(conn, body.challenge)
        row = conn.execute("SELECT * FROM users WHERE id = %s", (ch["user_id"],)).fetchone() if ch else None
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That sign-in has expired. Start again.")
    _throttle(row["email"], ip)
    try:
        with db.tx() as conn:
            if row["disabled_at"] is not None:
                raise passkeys.PasskeyError("disabled")
            passkeys.verify_signin(conn, row["id"], body.ticket, body.credential, rp_id, origin)
            mfa.close_challenge(conn, body.challenge)
    except passkeys.PasskeyError as e:
        _failed(row["email"], ip, "passkey")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That passkey didn't work. Try again.") from e
    return _signed_in(request, response, row, "password+passkey")


# ---- Google, Microsoft and enterprise SSO through the gateway ----------------

SOCIAL = {"google": "Sign in with Google", "microsoft": "Sign in with Microsoft"}
STATE_MINUTES = 10


@router.get("/providers")
def providers(request: Request) -> dict:
    """Which sign-in buttons the portal shows. None until the gateway is set up."""
    s = request.app.state.settings
    out = []
    if oidc.configured(s):
        for idp in (i.strip() for i in s.oidc_idps.split(",")):
            if idp in SOCIAL:
                out.append({"id": idp, "label": SOCIAL[idp], "start_url": f"/api/v1/auth/oidc/start?idp={idp}"})
    return {"password": True, "providers": out, "sso": oidc.configured(s)}


def _safe_next(next_path: str) -> str:
    p = (next_path or "/").strip()
    if not p.startswith("/") or p.startswith("//") or "\\" in p or any(c in p for c in "\r\n"):
        return "/"
    return p[:500]


def begin(request: Request, idp: str, purpose: str, next_path: str, connection_id=None, requested_by: str = "") -> str:
    """Store state, nonce and PKCE verifier; return the gateway's authorisation URL."""
    s = request.app.state.settings
    state, nonce = new_token(), new_token()
    verifier, challenge = oidc.pkce()
    with db.tx() as conn:
        conn.execute("DELETE FROM oidc_states WHERE expires_at < now() - interval '1 day'")
        conn.execute(
            f"""INSERT INTO oidc_states (state_hash, nonce, verifier, idp, purpose, connection_id, next_path,
                                         requested_by, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now() + interval '{STATE_MINUTES} minutes')""",
            (token_hash(state), nonce, verifier, idp, purpose, connection_id, _safe_next(next_path), requested_by),
        )
    try:
        return oidc.authorize_url(s, state, nonce, challenge, idp, prompt="login" if purpose == "test" else None)
    except oidc.OidcError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e


@router.get("/oidc/start")
def oidc_start(request: Request, idp: Annotated[str, Query(max_length=80)], next: str = "/") -> RedirectResponse:
    if not oidc.configured(request.app.state.settings):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Single sign-on is not set up on this controller.")
    connection_id = None
    if idp not in SOCIAL:
        with db.tx() as conn:
            c = conn.execute(
                "SELECT id FROM sso_connections WHERE alias = %s AND status = 'enabled'", (idp,)
            ).fetchone()
        if c is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "That sign-in option is not available.")
        connection_id = c["id"]
    return RedirectResponse(begin(request, idp, "signin", next, connection_id), status.HTTP_302_FOUND)


def _back(path: str, **q) -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}{urllib.parse.urlencode(q)}" if q else path, status.HTTP_302_FOUND)


@router.get("/oidc/callback")
def oidc_callback(
    request: Request,
    state: Annotated[str, Query(max_length=500)] = "",
    code: Annotated[str, Query(max_length=4000)] = "",
    error: Annotated[str, Query(max_length=200)] = "",
) -> RedirectResponse:
    s = request.app.state.settings
    ip = _client_ip(request)
    with db.tx() as conn:
        st = conn.execute(
            """UPDATE oidc_states SET used_at = now()
               WHERE state_hash = %s AND used_at IS NULL AND expires_at > now() RETURNING *""",
            (token_hash(state),),
        ).fetchone()
    if st is None:
        with db.tx() as conn:
            audit.record(conn, "anonymous", "login_failed", "", detail={"ip": ip, "reason": "oidc_state"})
        return _back("/", signin_error="That sign-in link has expired. Start again.")
    conn_row = None
    if st["connection_id"]:
        with db.tx() as conn:
            conn_row = conn.execute("SELECT * FROM sso_connections WHERE id = %s", (st["connection_id"],)).fetchone()

    def fail(message: str, reason: str, email: str = "") -> RedirectResponse:
        with db.tx() as conn:
            audit.record(
                conn,
                f"user:{email}" if email else "anonymous",
                "login_failed",
                email,
                conn_row["customer_id"] if conn_row else None,
                {"ip": ip, "reason": reason, "idp": st["idp"], "purpose": st["purpose"]},
            )
            if st["purpose"] == "test" and conn_row:
                _record_test(conn, conn_row, False, message, email)
        if st["purpose"] == "test":
            return _back(st["next_path"], sso_test="failed", message=message)
        return _back("/", signin_error=message)

    if error or not code:
        return fail("Sign-in was cancelled or refused by the identity provider.", f"idp_error:{error[:60]}")
    try:
        tokens = oidc.exchange(s, code, st["verifier"])
        claims = oidc.verify_id_token(s, tokens["id_token"], st["nonce"])
    except oidc.OidcError as e:
        return fail(str(e), "oidc_token")
    email = str(claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is not True:
        return fail("Your identity provider did not confirm your email address.", "email_unverified", email)
    came_from = claims.get("identity_provider")
    if came_from is not None and came_from != st["idp"]:
        return fail("You signed in with a different identity provider than the one chosen.", "idp_mismatch", email)

    if st["purpose"] == "test":
        if conn_row is None or came_from != conn_row["alias"]:
            return fail("The test did not come back through this connection.", "test_idp", email)
        with db.tx() as conn:
            domain_ok = email.rsplit("@", 1)[-1] in [
                r["domain"]
                for r in conn.execute(
                    "SELECT domain FROM sso_domains WHERE connection_id = %s AND status <> 'rejected'",
                    (conn_row["id"],),
                ).fetchall()
            ]
            note = (
                "Test sign-in worked." if domain_ok else "Test sign-in worked, but that email's domain is not listed."
            )
            _record_test(conn, conn_row, True, note, email)
            audit.record(
                conn,
                st["requested_by"] or "anonymous",
                "sso.test",
                conn_row["alias"],
                conn_row["customer_id"],
                {"ok": True, "email": email, "domain_listed": domain_ok},
            )
        return _back(st["next_path"], sso_test="ok", message=note)

    with db.tx() as conn:
        user = conn.execute("SELECT * FROM users WHERE lower(email) = %s", (email,)).fetchone()
        if conn_row is not None:
            # Enterprise SSO: the business's provider may only vouch for its own approved domains and people.
            if conn_row["status"] != "enabled" or came_from != conn_row["alias"]:
                user, why = None, "sso_connection"
            elif not sso.domain_approved_for(conn, conn_row["id"], email):
                user, why = None, "sso_domain"
            elif user is None:
                user, why = sso.provision(conn, conn_row, email, claims), "provisioned"
                audit.record(
                    conn,
                    f"sso:{conn_row['alias']}",
                    "user.create",
                    email,
                    conn_row["customer_id"],
                    {"via": "sso", "connection": str(conn_row["id"])},
                )
            elif user["role"] != "customer" or str(user["customer_id"]) != str(conn_row["customer_id"]):
                user, why = None, "sso_other_business"
            else:
                why = "ok"
        else:
            # Google or Microsoft: only for people who already have an account.
            forced = sso.requires_sso(conn, email) if user is not None and user["role"] != "admin" else None
            why = "no_account" if user is None else ("sso_required" if forced else "ok")
            if why != "ok":
                user = None
    messages = {
        "sso_connection": "That single sign-on connection is not switched on.",
        "sso_domain": "Your email's domain is not approved for this single sign-on.",
        "sso_other_business": "This account belongs to a different organisation.",
        "no_account": "No ExaCarib account uses this email. Ask your administrator to add you.",
        "sso_required": SSO_ONLY,
    }
    if user is None:
        return fail(messages[why], why, email)
    if user["disabled_at"] is not None:
        return fail("This account has been switched off. Ask your administrator.", "disabled", email)
    response = _back(st["next_path"])
    _signed_in(request, response, user, "sso" if conn_row else f"oidc:{st['idp']}", idp=st["idp"])
    return response


def _record_test(conn, conn_row: dict, ok: bool, message: str, email: str) -> None:
    from psycopg.types.json import Jsonb

    result = {"ok": ok, "message": message, "email": email, "at": dt.datetime.now(dt.UTC).isoformat()}
    conn.execute(
        """UPDATE sso_connections SET last_test = %s, tested_at = now(), updated_at = now(),
             status = CASE WHEN %s AND status = 'draft' THEN 'tested' ELSE status END WHERE id = %s""",
        (Jsonb(result), ok, conn_row["id"]),
    )


# ---- API keys (ADR 0013) ----------------------------------------------------

MAX_KEYS = 20
KEY_COLUMNS = "id, name, prefix, scopes, created_at, last_used_at, expires_at"
KEY_SCOPES = ("connect", "metrics", "commai:read", "commai:write", "commai:notes", "commai:admin")


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    days: int | None = Field(default=None, ge=1, le=730)
    # None: the key can do whatever you can. A list limits it (ADR 0016).
    scopes: list[str] | None = Field(default=None, max_length=len(KEY_SCOPES))


@router.get("/api-keys")
def list_keys(user: UserDep) -> list[dict]:
    """Your own active keys. The keys themselves are never shown again."""
    with db.tx() as conn:
        return conn.execute(
            f"""SELECT {KEY_COLUMNS} FROM api_keys WHERE user_id = %s AND revoked_at IS NULL
                AND (expires_at IS NULL OR expires_at > now()) ORDER BY created_at DESC""",
            (user.id,),
        ).fetchall()


@router.post("/api-keys", status_code=201)
def create_key(body: KeyIn, user: UserDep) -> dict:
    """A key that acts as you, for the SDK, Terraform or your own code. Shown once."""
    if user.scopes is not None:
        # A limited account (or key) can't make a key with more than it has.
        if body.scopes is None:
            body.scopes = list(user.scopes)
        elif set(body.scopes) - set(user.scopes):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "A key can't have scopes your account doesn't have.")
    if body.scopes is not None:
        bad = sorted(set(body.scopes) - set(KEY_SCOPES))
        if bad or not body.scopes:
            raise HTTPException(
                422, f"Scopes are {', '.join(KEY_SCOPES)}." + (f" Not {', '.join(bad)}." if bad else "")
            )
    token = API_KEY_PREFIX + new_token()
    with db.tx() as conn:
        active = conn.execute(
            """SELECT count(*) AS n FROM api_keys WHERE user_id = %s AND revoked_at IS NULL
               AND (expires_at IS NULL OR expires_at > now())""",
            (user.id,),
        ).fetchone()["n"]
        if active >= MAX_KEYS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"You have {MAX_KEYS} keys. Revoke one first.")
        row = conn.execute(
            f"""INSERT INTO api_keys (user_id, name, prefix, token_hash, scopes, expires_at)
                VALUES (%s, %s, %s, %s, %s,
                        CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(days => %s) END)
                RETURNING {KEY_COLUMNS}""",
            (user.id, body.name.strip(), token[:12], token_hash(token), body.scopes, body.days, body.days),
        ).fetchone()
        audit.record(
            conn,
            user.actor,
            "api_key.create",
            row["prefix"],
            user.customer_id,
            {"name": row["name"], "scopes": body.scopes},
        )
    return {**row, "token": token}


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_key(key_id: int, user: UserDep) -> None:
    with db.tx() as conn:
        row = conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE id = %s AND user_id = %s AND revoked_at IS NULL"
            " RETURNING prefix, name",
            (key_id, user.id),
        ).fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found.")
        audit.record(conn, user.actor, "api_key.revoke", row["prefix"], user.customer_id, {"name": row["name"]})
