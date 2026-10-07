"""Shared accounts (ADR 0023): the people in an organisation, invitations,
roles, leaving, handing on ownership, and switching organisation.

Who may do what:
- any member (viewers too) sees the organisation's people;
- owners and admins (and ExaCarib admins) invite, change roles and remove;
- only owners make owners, change an owner's role or hand ownership on;
- everyone may leave, except the last owner.
Every write is audited."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .. import audit, db
from ..identity import orgs, sessions, sso
from ..security import hash_password, token_hash
from .deps import ORG_ROLES, User, UserDep, check_customer, current_user, require_org_manager

router = APIRouter(tags=["organisations"])


def _fail(e: orgs.OrgError) -> HTTPException:
    return HTTPException(e.status, str(e))


def _by_role(user: User) -> str | None:
    """The actor's role in the organisation; None for ExaCarib admins (who may do anything)."""
    return None if user.role == "admin" else user.org_role


# ---- the people in an organisation ---------------------------------------------------


@router.get("/orgs/{customer_id}/members")
def list_members(customer_id: str, user: UserDep) -> dict:
    check_customer(user, customer_id)
    with db.tx() as conn:
        org = conn.execute("SELECT id, name FROM customers WHERE id = %s", (customer_id,)).fetchone()
        if org is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Organisation not found.")
        people = orgs.members(conn, customer_id)
    manage = user.role == "admin" or user.org_role in orgs.MANAGER_ROLES
    return {
        "organisation": org,
        "your_role": user.org_role if user.role == "customer" else None,
        "can_manage": manage,
        "members": [{**p, "you": str(p["user_id"]) == str(user.id)} for p in people],
    }


class RoleIn(BaseModel):
    role: str = Field(max_length=10)


@router.patch("/orgs/{customer_id}/members/{user_id}")
def change_role(customer_id: str, user_id: str, body: RoleIn, user: UserDep) -> dict:
    require_org_manager(user, customer_id)
    if body.role not in ORG_ROLES:
        raise HTTPException(422, f"Roles are {', '.join(ORG_ROLES)}.")
    with db.tx() as conn:
        try:
            before = orgs.membership(conn, customer_id, user_id) if _uuid_ok(user_id) else None
            m = orgs.set_role(conn, customer_id, user_id, body.role, by_role=_by_role(user))
        except orgs.OrgError as e:
            raise _fail(e) from e
        email = conn.execute("SELECT email FROM users WHERE id = %s", (user_id,)).fetchone()["email"]
        audit.record(
            conn,
            user.actor,
            "org.member.role",
            email,
            customer_id,
            {"from": before["role"] if before else None, "to": m["role"]},
        )
    return {"user_id": user_id, "email": email, "role": m["role"]}


@router.delete("/orgs/{customer_id}/members/{user_id}", status_code=204)
def remove_member(customer_id: str, user_id: str, user: UserDep) -> None:
    require_org_manager(user, customer_id)
    leaving = str(user_id) == str(user.id)
    with db.tx() as conn:
        try:
            m = orgs.remove(conn, customer_id, user_id, by_role=_by_role(user), leaving=leaving)
        except orgs.OrgError as e:
            raise _fail(e) from e
        email = conn.execute("SELECT email FROM users WHERE id = %s", (user_id,)).fetchone()["email"]
        audit.record(
            conn,
            user.actor,
            "org.member.leave" if leaving else "org.member.remove",
            email,
            customer_id,
            {"role": m["role"]},
        )


@router.post("/orgs/{customer_id}/leave", status_code=204)
def leave(customer_id: str, user: UserDep) -> None:
    """Leave an organisation you belong to (not necessarily the one you are acting for)."""
    if user.role != "customer":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only organisation members can leave.")
    with db.tx() as conn:
        try:
            m = orgs.remove(conn, customer_id, user.id, by_role=None, leaving=True) if _uuid_ok(customer_id) else None
        except orgs.OrgError as e:
            raise _fail(e) from e
        if m is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "You are not a member of that organisation.")
        audit.record(conn, user.actor, "org.member.leave", user.email, customer_id, {"role": m["role"]})


class TransferIn(BaseModel):
    user_id: str = Field(max_length=64)


@router.post("/orgs/{customer_id}/transfer-ownership")
def transfer_ownership(customer_id: str, body: TransferIn, user: UserDep) -> dict:
    """Hand ownership to another member. You become an admin."""
    check_customer(user, customer_id)
    if user.role != "admin" and user.org_role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an owner can hand on ownership.")
    with db.tx() as conn:
        try:
            if not _uuid_ok(body.user_id):
                raise orgs.OrgError("That person is not a member of this organisation.", 404)
            orgs.transfer(conn, customer_id, user.id if user.role == "customer" else None, body.user_id)
        except orgs.OrgError as e:
            raise _fail(e) from e
        email = conn.execute("SELECT email FROM users WHERE id = %s", (body.user_id,)).fetchone()["email"]
        audit.record(conn, user.actor, "org.transfer_ownership", email, customer_id, {"from": user.email})
    return {"owner": email}


def _uuid_ok(v: str) -> bool:
    import uuid

    try:
        uuid.UUID(str(v))
    except ValueError:
        return False
    return True


# ---- invitations ----------------------------------------------------------------------


class InviteIn(BaseModel):
    email: str = Field(max_length=255)
    role: str = Field(default="member", max_length=10)


@router.get("/orgs/{customer_id}/invites")
def list_invites(customer_id: str, user: UserDep) -> list[dict]:
    require_org_manager(user, customer_id)
    with db.tx() as conn:
        return orgs.pending(conn, customer_id)


@router.post("/orgs/{customer_id}/invites", status_code=201)
def create_invite(customer_id: str, body: InviteIn, user: UserDep, request: Request) -> dict:
    """Invite someone by email. No email is sent yet: the link is shown once,
    here, for you to pass on. It works once and expires in 7 days."""
    require_org_manager(user, customer_id)
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM customers WHERE id = %s", (customer_id,)).fetchone() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Organisation not found.")
        try:
            row, token = orgs.invite(conn, customer_id, body.email, body.role, user.actor)
        except orgs.OrgError as e:
            raise _fail(e) from e
        audit.record(conn, user.actor, "org.invite.create", row["email"], customer_id, {"role": row["role"]})
    path = f"/invite/{token}"
    base = request.app.state.settings.public_url
    return {**row, "token": token, "path": path, "url": f"{base}{path}" if base else path, "emailed": False}


@router.delete("/orgs/{customer_id}/invites/{invite_id}", status_code=204)
def revoke_invite(customer_id: str, invite_id: str, user: UserDep) -> None:
    require_org_manager(user, customer_id)
    with db.tx() as conn:
        try:
            row = orgs.revoke(conn, customer_id, invite_id)
        except orgs.OrgError as e:
            raise _fail(e) from e
        audit.record(conn, user.actor, "org.invite.revoke", row["email"], customer_id, {"role": row["role"]})


@router.get("/invites/{token}")
def show_invite(token: str) -> dict:
    """What an invitation is for. Works signed out: the link is the secret."""
    with db.tx() as conn:
        try:
            row = orgs.lookup(conn, token)
        except orgs.OrgError as e:
            raise _fail(e) from e
        needs_sso = bool(sso.requires_sso(conn, row["email"]))
    return {
        "organisation": row["organisation"],
        "email": row["email"],
        "role": row["role"],
        "expires_at": row["expires_at"],
        "has_account": row["has_account"],
        "sso_required": needs_sso,
    }


class AcceptIn(BaseModel):
    # Only for someone without an account: the password they choose, and their name.
    password: str | None = Field(default=None, min_length=12, max_length=1024)
    name: str = Field(default="", max_length=200)


def _signed_in_user(request: Request) -> User | None:
    if not request.headers.get("authorization") and not request.cookies.get(sessions.COOKIE):
        return None
    try:
        return current_user(request, request.headers.get("authorization"))
    except HTTPException as e:
        if e.status_code == status.HTTP_401_UNAUTHORIZED:
            return None  # an old session: treat as signed out
        raise


@router.post("/invites/{token}/accept")
def accept_invite(token: str, body: AcceptIn, request: Request, response: Response) -> dict:
    """Join the organisation. Signed in: the invitation must be for your email.
    Signed out and new to ExaCarib: choose a password and you are signed in."""
    user = _signed_in_user(request)
    if user is not None:
        return _accept_existing(token, user, request)
    return _accept_new(token, body, request, response)


def _accept_existing(token: str, user: User, request: Request) -> dict:
    if user.via == "key":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Accept invitations in the portal, not with an API key.")
    with db.tx() as conn:
        try:
            inv = orgs.lookup(conn, token)
        except orgs.OrgError as e:
            raise _fail(e) from e
        if inv["email"] != user.email.lower():
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"This invitation is for {inv['email']}. Sign in with that account to accept it.",
            )
        if user.role != "customer":
            raise HTTPException(status.HTTP_409_CONFLICT, "ExaCarib and carrier accounts can't join organisations.")
        if orgs.membership(conn, inv["customer_id"], user.id):
            raise HTTPException(status.HTTP_409_CONFLICT, "You are already a member of this organisation.")
        try:
            orgs.claim(conn, token, user.id)
        except orgs.OrgError as e:
            raise _fail(e) from e
        orgs.add(conn, inv["customer_id"], user.id, inv["role"], inv["invited_by"])
        # A person who had lost every organisation gets this one as their primary.
        conn.execute(
            "UPDATE users SET customer_id = %s, updated_at = now() WHERE id = %s AND customer_id IS NULL",
            (inv["customer_id"], user.id),
        )
        _switch_session(conn, request, user, inv["customer_id"])
        audit.record(conn, user.actor, "org.invite.accept", user.email, inv["customer_id"], {"role": inv["role"]})
    return {"organisation": inv["organisation"], "customer_id": str(inv["customer_id"]), "role": inv["role"]}


def _accept_new(token: str, body: AcceptIn, request: Request, response: Response) -> dict:
    with db.tx() as conn:
        try:
            inv = orgs.lookup(conn, token)
        except orgs.OrgError as e:
            raise _fail(e) from e
        if conn.execute("SELECT 1 FROM users WHERE lower(email) = %s", (inv["email"],)).fetchone():
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "You already have an account. Sign in, then open this link again."
            )
        if sso.requires_sso(conn, inv["email"]):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Your organisation signs in with its own single sign-on. Sign in that way, then open this link again.",
            )
        if not body.password:
            raise HTTPException(422, "Choose a password of at least 12 characters.")
        try:
            orgs.claim(conn, token, None)
        except orgs.OrgError as e:
            raise _fail(e) from e
        row = conn.execute(
            """INSERT INTO users (email, password_hash, role, customer_id, display_name)
               VALUES (%s, %s, 'customer', %s, %s) RETURNING *""",
            (inv["email"], hash_password(body.password), inv["customer_id"], body.name.strip()),
        ).fetchone()
        conn.execute("UPDATE org_invites SET accepted_by = %s WHERE id = %s", (row["id"], inv["id"]))
        orgs.add(conn, inv["customer_id"], row["id"], inv["role"], inv["invited_by"])
        actor = f"user:{row['email']}"
        audit.record(conn, actor, "user.create", row["email"], inv["customer_id"], {"via": "invite"})
        audit.record(conn, actor, "org.invite.accept", row["email"], inv["customer_id"], {"role": inv["role"]})
        tok, expires = sessions.start(conn, row["id"], request.app.state.settings.session_hours, "invite")
        conn.execute(
            "UPDATE sessions SET customer_id = %s WHERE token_hash = %s", (inv["customer_id"], token_hash(tok))
        )
        audit.record(conn, actor, "login", customer_id=inv["customer_id"], detail={"via": "invite"})
    sessions.set_cookie(response, request, tok, expires)
    return {
        "organisation": inv["organisation"],
        "customer_id": str(inv["customer_id"]),
        "role": inv["role"],
        "token": tok,
        "expires_at": expires,
    }


# ---- switching organisation ---------------------------------------------------------


class SwitchIn(BaseModel):
    customer_id: str = Field(max_length=64)


def _switch_session(conn, request: Request, user: User, customer_id) -> None:
    token, _ = sessions.request_token(request)
    conn.execute(
        "UPDATE sessions SET customer_id = %s WHERE token_hash = %s AND user_id = %s",
        (customer_id, token_hash(token or ""), user.id),
    )


@router.post("/auth/organisation")
def switch_organisation(body: SwitchIn, user: UserDep, request: Request) -> dict:
    """Act for another organisation you belong to, in this session."""
    if user.role != "customer":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only organisation members switch organisation.")
    if user.via == "key":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "An API key stays in the organisation it was made in.")
    with db.tx() as conn:
        m = orgs.membership(conn, body.customer_id, user.id) if _uuid_ok(body.customer_id) else None
        if m is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You are not a member of that organisation.")
        _switch_session(conn, request, user, body.customer_id)
        name = conn.execute("SELECT name FROM customers WHERE id = %s", (body.customer_id,)).fetchone()["name"]
        audit.record(conn, user.actor, "org.switch", name, body.customer_id, {"from": str(user.customer_id or "")})
    return {"customer_id": body.customer_id, "name": name, "role": m["role"]}
