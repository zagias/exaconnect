"""Enterprise administration and data governance API (ADR 0024):
organisation and business calendars, roles, security settings, data
governance and unusual-use alerts. Every write is audited."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import AdminDep, UserDep
from .. import access
from ..enterprise import calendar, governance, protect, roles, security
from .common import errors

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: enterprise"])
# ExaCarib-wide views (admins only). Named `public` so the module loader includes it.
public = APIRouter(prefix="/enterprise", tags=["commai: enterprise"])


@contextmanager
def _errors() -> Iterator[None]:
    with errors():
        try:
            yield
        except governance.DataError as e:
            raise HTTPException(e.code, str(e)) from e
        except (calendar.CalendarError, roles.RoleError, security.SecurityError) as e:
            raise HTTPException(422, str(e)) from e


def _admin(conn, user, customer_id: str) -> None:
    """Business settings: the commai:admin scope and not an internal seat."""
    access.require_business_admin(user)
    if access.seat(conn, user, customer_id) == "internal":
        raise HTTPException(403, "Your seat can't change organisation settings.")


def _one(row: dict | None, what: str) -> dict:
    if row is None:
        raise HTTPException(404, f"{what} not found.")
    return row


# ==== organisation ===============================================================================


class LocationIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    country: str = Field(default="", max_length=2, description="ISO 3166-1 alpha-2, for public holidays")
    timezone: str = Field(default="America/Port_of_Spain", max_length=64)
    address: str = Field(default="", max_length=500)
    is_primary: bool = False
    after_hours_team_id: str | None = None


class HoursIn(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0 is Monday")
    opens: dt.time
    closes: dt.time


class BrandIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    location_id: str | None = None


class HolidayIn(BaseModel):
    country: str = Field(min_length=2, max_length=2)
    day: dt.date
    name: str = Field(default="", max_length=120)


class ClosureIn(BaseModel):
    location_id: str | None = None
    starts_at: dt.datetime
    ends_at: dt.datetime
    reason: str = Field(default="", max_length=200)


class TeamPlaceIn(BaseModel):
    location_id: str | None = None
    brand_id: str | None = None


def _check_team(conn, customer_id: str, team_id: str | None) -> None:
    if (
        team_id
        and not conn.execute(
            "SELECT 1 FROM commai_teams WHERE id = %s AND customer_id = %s", (team_id, customer_id)
        ).fetchone()
    ):
        raise HTTPException(400, "That team is not in this organisation.")


def _check_location(conn, customer_id: str, location_id: str | None) -> None:
    if (
        location_id
        and not conn.execute(
            "SELECT 1 FROM commai_locations WHERE id = %s AND customer_id = %s", (location_id, customer_id)
        ).fetchone()
    ):
        raise HTTPException(400, "That location is not in this organisation.")


@router.get("/organisation")
def get_organisation(customer_id: str, user: UserDep) -> dict:
    """Locations with opening hours and whether they are open now, brands,
    public holidays, closures, and which location and brand each team serves."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, _errors():
        locs = conn.execute(
            "SELECT * FROM commai_locations WHERE customer_id = %s ORDER BY is_primary DESC, name", (customer_id,)
        ).fetchall()
        hours = conn.execute(
            "SELECT location_id, weekday, opens, closes FROM commai_opening_hours WHERE customer_id = %s"
            " ORDER BY weekday, opens",
            (customer_id,),
        ).fetchall()
        state = {s["location_id"]: s for s in calendar.status(conn, customer_id)}
        for loc in locs:
            loc["hours"] = [h for h in hours if h["location_id"] == loc["id"]]
            loc["now"] = state.get(str(loc["id"]))
        return {
            "locations": locs,
            "brands": conn.execute(
                "SELECT * FROM commai_brands WHERE customer_id = %s ORDER BY name", (customer_id,)
            ).fetchall(),
            "holidays": conn.execute(
                "SELECT * FROM commai_holidays WHERE customer_id = %s ORDER BY day", (customer_id,)
            ).fetchall(),
            "closures": conn.execute(
                "SELECT * FROM commai_closures WHERE customer_id = %s AND ends_at > now() - interval '30 days'"
                " ORDER BY starts_at",
                (customer_id,),
            ).fetchall(),
            "teams": conn.execute(
                "SELECT id, name, location_id, brand_id FROM commai_teams WHERE customer_id = %s ORDER BY name",
                (customer_id,),
            ).fetchall(),
        }


def _save_location(conn, customer_id: str, body: LocationIn, location_id: str | None, actor: str) -> dict:
    calendar.zone(body.timezone)
    _check_team(conn, customer_id, body.after_hours_team_id)
    if body.is_primary:
        conn.execute(
            "UPDATE commai_locations SET is_primary = false WHERE customer_id = %s AND id IS DISTINCT FROM %s",
            (customer_id, location_id),
        )
    vals = (
        body.name.strip(),
        body.country.upper(),
        body.timezone,
        body.address,
        body.is_primary,
        body.after_hours_team_id,
    )
    if location_id is None:
        row = conn.execute(
            """INSERT INTO commai_locations (customer_id, name, country, timezone, address, is_primary,
                                             after_hours_team_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (customer_id, name) DO NOTHING RETURNING *""",
            (customer_id, *vals),
        ).fetchone()
        if row is None:
            raise HTTPException(409, "There is already a location with that name.")
        # The first location is the primary one.
        if not conn.execute(
            "SELECT 1 FROM commai_locations WHERE customer_id = %s AND is_primary", (customer_id,)
        ).fetchone():
            row = conn.execute(
                "UPDATE commai_locations SET is_primary = true WHERE id = %s RETURNING *", (row["id"],)
            ).fetchone()
    else:
        row = _one(
            conn.execute(
                """UPDATE commai_locations SET name = %s, country = %s, timezone = %s, address = %s, is_primary = %s,
                          after_hours_team_id = %s WHERE id = %s AND customer_id = %s RETURNING *""",
                (*vals, location_id, customer_id),
            ).fetchone(),
            "Location",
        )
    audit.record(
        conn,
        actor,
        "commai.org.location." + ("create" if location_id is None else "update"),
        row["name"],
        customer_id,
        body.model_dump(),
    )
    return row


@router.post("/organisation/locations", status_code=201)
def create_location(customer_id: str, body: LocationIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        return _save_location(conn, customer_id, body, None, user.actor)


@router.put("/organisation/locations/{location_id}")
def update_location(customer_id: str, location_id: str, body: LocationIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        return _save_location(conn, customer_id, body, location_id, user.actor)


@router.delete("/organisation/locations/{location_id}", status_code=204)
def delete_location(customer_id: str, location_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = _one(
            conn.execute(
                "DELETE FROM commai_locations WHERE id = %s AND customer_id = %s RETURNING name",
                (location_id, customer_id),
            ).fetchone(),
            "Location",
        )
        audit.record(conn, user.actor, "commai.org.location.delete", row["name"], customer_id)


@router.put("/organisation/locations/{location_id}/hours")
def set_hours(customer_id: str, location_id: str, body: list[HoursIn], user: UserDep) -> list[dict]:
    """Replace a location's opening hours. An empty list means open all the time."""
    access.check(user, customer_id, "commai:admin")
    if len(body) > 50:
        raise HTTPException(422, "Use at most 50 opening-hour intervals.")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        _check_location(conn, customer_id, location_id)
        conn.execute("DELETE FROM commai_opening_hours WHERE location_id = %s", (location_id,))
        for h in body:
            if h.closes <= h.opens:
                raise HTTPException(422, "Each interval must close after it opens (use 23:59 for midnight).")
            conn.execute(
                """INSERT INTO commai_opening_hours (location_id, customer_id, weekday, opens, closes)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                (location_id, customer_id, h.weekday, h.opens, h.closes),
            )
        audit.record(conn, user.actor, "commai.org.hours.set", location_id, customer_id, {"intervals": len(body)})
        return conn.execute(
            "SELECT weekday, opens, closes FROM commai_opening_hours WHERE location_id = %s ORDER BY weekday, opens",
            (location_id,),
        ).fetchall()


@router.post("/organisation/brands", status_code=201)
def create_brand(customer_id: str, body: BrandIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        _check_location(conn, customer_id, body.location_id)
        row = conn.execute(
            """INSERT INTO commai_brands (customer_id, name, location_id) VALUES (%s, %s, %s)
               ON CONFLICT (customer_id, name) DO NOTHING RETURNING *""",
            (customer_id, body.name.strip(), body.location_id),
        ).fetchone()
        if row is None:
            raise HTTPException(409, "There is already a brand with that name.")
        audit.record(conn, user.actor, "commai.org.brand.create", row["name"], customer_id)
        return row


@router.delete("/organisation/brands/{brand_id}", status_code=204)
def delete_brand(customer_id: str, brand_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = _one(
            conn.execute(
                "DELETE FROM commai_brands WHERE id = %s AND customer_id = %s RETURNING name", (brand_id, customer_id)
            ).fetchone(),
            "Brand",
        )
        audit.record(conn, user.actor, "commai.org.brand.delete", row["name"], customer_id)


@router.post("/organisation/holidays", status_code=201)
def add_holiday(customer_id: str, body: HolidayIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = conn.execute(
            """INSERT INTO commai_holidays (customer_id, country, day, name) VALUES (%s, %s, %s, %s)
               ON CONFLICT (customer_id, country, day) DO UPDATE SET name = EXCLUDED.name RETURNING *""",
            (customer_id, body.country.upper(), body.day, body.name),
        ).fetchone()
        audit.record(
            conn,
            user.actor,
            "commai.org.holiday.add",
            f"{row['country']} {row['day']}",
            customer_id,
            {"name": body.name},
        )
        return row


@router.delete("/organisation/holidays/{holiday_id}", status_code=204)
def delete_holiday(customer_id: str, holiday_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = _one(
            conn.execute(
                "DELETE FROM commai_holidays WHERE id = %s AND customer_id = %s RETURNING country, day",
                (holiday_id, customer_id),
            ).fetchone(),
            "Holiday",
        )
        audit.record(conn, user.actor, "commai.org.holiday.delete", f"{row['country']} {row['day']}", customer_id)


@router.post("/organisation/closures", status_code=201)
def add_closure(customer_id: str, body: ClosureIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    if body.ends_at <= body.starts_at:
        raise HTTPException(422, "A closure must end after it starts.")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        _check_location(conn, customer_id, body.location_id)
        row = conn.execute(
            """INSERT INTO commai_closures (customer_id, location_id, starts_at, ends_at, reason, created_by)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
            (customer_id, body.location_id, body.starts_at, body.ends_at, body.reason, user.actor),
        ).fetchone()
        audit.record(conn, user.actor, "commai.org.closure.add", str(row["id"]), customer_id, {"reason": body.reason})
        return row


@router.delete("/organisation/closures/{closure_id}", status_code=204)
def delete_closure(customer_id: str, closure_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        _one(
            conn.execute(
                "DELETE FROM commai_closures WHERE id = %s AND customer_id = %s RETURNING id", (closure_id, customer_id)
            ).fetchone(),
            "Closure",
        )
        audit.record(conn, user.actor, "commai.org.closure.delete", closure_id, customer_id)


@router.put("/organisation/teams/{team_id}")
def place_team(customer_id: str, team_id: str, body: TeamPlaceIn, user: UserDep) -> dict:
    """Tie a team to a location (its calendar) and a brand."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        _check_location(conn, customer_id, body.location_id)
        if (
            body.brand_id
            and not conn.execute(
                "SELECT 1 FROM commai_brands WHERE id = %s AND customer_id = %s", (body.brand_id, customer_id)
            ).fetchone()
        ):
            raise HTTPException(400, "That brand is not in this organisation.")
        row = _one(
            conn.execute(
                """UPDATE commai_teams SET location_id = %s, brand_id = %s WHERE id = %s AND customer_id = %s
                   RETURNING id, name, location_id, brand_id""",
                (body.location_id, body.brand_id, team_id, customer_id),
            ).fetchone(),
            "Team",
        )
        audit.record(conn, user.actor, "commai.org.team.place", row["name"], customer_id, body.model_dump())
        return row


@router.get("/organisation/due")
def preview_due(
    customer_id: str,
    user: UserDep,
    minutes: float = Query(60, gt=0, le=60 * 24 * 365),
    team_id: str | None = None,
    start: dt.datetime | None = None,
) -> dict:
    """When a target of this many minutes, starting now (or at `start`), falls due in business time."""
    access.check(user, customer_id, "commai:read")
    begin = start or dt.datetime.now(dt.UTC)
    if begin.tzinfo is None:
        begin = begin.replace(tzinfo=dt.UTC)
    with db.tx() as conn, _errors():
        due, _ = calendar.due(conn, customer_id, team_id, begin, dt.timedelta(minutes=minutes), dt.timedelta(0))
    return {"start": begin, "due": due, "clock_due": begin + dt.timedelta(minutes=minutes)}


# ==== roles ======================================================================================


class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    permissions: list[str] = Field(default_factory=list, max_length=20)


class AssignIn(BaseModel):
    user_id: str
    role_id: str
    team_id: str | None = None


@router.get("/roles")
def list_roles(customer_id: str, user: UserDep) -> dict:
    """The permission list, built-in and custom roles, who has which, and each person's rights."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        people = conn.execute(
            """SELECT u.id, u.email, u.display_name, COALESCE(m.seat, 'agent') AS seat, u.access_scopes,
                      u.disabled_at IS NOT NULL AS disabled
               FROM users u LEFT JOIN commai_members m ON m.user_id = u.id AND m.customer_id = u.customer_id
               WHERE u.customer_id = %s AND u.role = 'customer' ORDER BY u.email""",
            (customer_id,),
        ).fetchall()
        for p in people:
            eff = roles.effective(conn, p["id"], customer_id)
            p["has_roles"] = eff["has_roles"]
            # Without roles, a person's rights are those of the built-in role for their seat.
            p["builtin"] = (
                "custom" if eff["has_roles"] else ("internal" if p["seat"] == "internal" else "business_admin")
            )
            p["permissions"] = eff["permissions"]
            p["team_permissions"] = eff["teams"]
        return {
            "permissions": [{"key": k, "label": v} for k, v in roles.PERMISSIONS.items()],
            "roles": conn.execute(
                """SELECT id, key, name, description, permissions, customer_id IS NULL AS builtin
                   FROM commai_roles WHERE customer_id = %s OR customer_id IS NULL
                   ORDER BY customer_id IS NOT NULL, name""",
                (customer_id,),
            ).fetchall(),
            "assignments": conn.execute(
                """SELECT a.id, a.user_id, u.email, a.role_id, r.name AS role_name, a.team_id, t.name AS team_name,
                          a.created_by, a.created_at
                   FROM commai_role_assignments a JOIN users u ON u.id = a.user_id
                   JOIN commai_roles r ON r.id = a.role_id LEFT JOIN commai_teams t ON t.id = a.team_id
                   WHERE a.customer_id = %s ORDER BY u.email, r.name""",
                (customer_id,),
            ).fetchall(),
            "people": people,
        }


@router.get("/roles/me")
def my_permissions(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        if user.role == "admin":
            return {"has_roles": False, "permissions": sorted(roles.PERMISSIONS), "teams": {}}
        return roles.effective(conn, user.id, customer_id)


@router.post("/roles", status_code=201)
def create_role(customer_id: str, body: RoleIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        perms = roles.check_permissions(body.permissions)
        row = conn.execute(
            """INSERT INTO commai_roles (customer_id, key, name, description, permissions, created_by)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING *""",
            (customer_id, roles.slug(body.name), body.name.strip(), body.description, perms, user.actor),
        ).fetchone()
        if row is None:
            raise HTTPException(409, "There is already a role with that name.")
        audit.record(conn, user.actor, "commai.roles.create", row["name"], customer_id, {"permissions": perms})
        return row


@router.put("/roles/{role_id}")
def update_role(customer_id: str, role_id: str, body: RoleIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        role = roles.get_role(conn, customer_id, role_id)
        if role["customer_id"] is None:
            raise HTTPException(409, "Built-in roles can't be changed. Make a custom role instead.")
        perms = roles.check_permissions(body.permissions)
        before = roles.security_holders(conn, customer_id)
        row = conn.execute(
            """UPDATE commai_roles SET name = %s, description = %s, permissions = %s, updated_at = now()
               WHERE id = %s RETURNING *""",
            (body.name.strip(), body.description, perms, role_id),
        ).fetchone()
        if before:
            roles.guard_lockout(conn, customer_id)
        audit.record(
            conn,
            user.actor,
            "commai.roles.update",
            row["name"],
            customer_id,
            {"before": role["permissions"], "after": perms},
        )
        return row


@router.delete("/roles/{role_id}", status_code=204)
def delete_role(customer_id: str, role_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        role = roles.get_role(conn, customer_id, role_id)
        if role["customer_id"] is None:
            raise HTTPException(409, "Built-in roles can't be deleted.")
        before = roles.security_holders(conn, customer_id)
        conn.execute("DELETE FROM commai_roles WHERE id = %s", (role_id,))
        if before:
            roles.guard_lockout(conn, customer_id)
        audit.record(conn, user.actor, "commai.roles.delete", role["name"], customer_id)


@router.post("/roles/assignments", status_code=201)
def assign_role(customer_id: str, body: AssignIn, user: UserDep) -> dict:
    """Give a person a role, across the business or for one team."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        role = roles.get_role(conn, customer_id, body.role_id)
        if not conn.execute(
            "SELECT 1 FROM users WHERE id = %s AND customer_id = %s AND role = 'customer'", (body.user_id, customer_id)
        ).fetchone():
            raise HTTPException(400, "That person is not in this organisation.")
        _check_team(conn, customer_id, body.team_id)
        before = roles.security_holders(conn, customer_id)
        row = conn.execute(
            """INSERT INTO commai_role_assignments (customer_id, user_id, role_id, team_id, created_by)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING *""",
            (customer_id, body.user_id, body.role_id, body.team_id, user.actor),
        ).fetchone()
        if row is None:
            raise HTTPException(409, "That person already has that role there.")
        if before:
            roles.guard_lockout(conn, customer_id)
        email = conn.execute("SELECT email FROM users WHERE id = %s", (body.user_id,)).fetchone()["email"]
        audit.record(
            conn,
            user.actor,
            "commai.roles.assign",
            email,
            customer_id,
            {"role": role["name"], "team_id": body.team_id},
        )
        return row


@router.delete("/roles/assignments/{assignment_id}", status_code=204)
def unassign_role(customer_id: str, assignment_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        before = roles.security_holders(conn, customer_id)
        row = _one(
            conn.execute(
                """DELETE FROM commai_role_assignments a USING users u, commai_roles r
                   WHERE a.id = %s AND a.customer_id = %s AND u.id = a.user_id AND r.id = a.role_id
                   RETURNING u.email, r.name AS role_name, a.team_id""",
                (assignment_id, customer_id),
            ).fetchone(),
            "Role assignment",
        )
        if before:
            roles.guard_lockout(conn, customer_id)
        audit.record(
            conn,
            user.actor,
            "commai.roles.unassign",
            row["email"],
            customer_id,
            {"role": row["role_name"], "team_id": str(row["team_id"] or "")},
        )


# ==== security ===================================================================================


class SecurityIn(BaseModel):
    require_two_step: bool = False
    session_hours: int | None = Field(default=None, ge=1, le=720)
    ip_allowlist: list[str] = Field(default_factory=list, max_length=security.MAX_CIDRS)
    sso_logout: bool = True
    thresholds: dict[str, float] = Field(default_factory=dict)


@router.get("/security")
def get_security(customer_id: str, user: UserDep, request: Request) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        s = security.get(conn, customer_id)
        people = conn.execute(
            """SELECT u.id, u.email, u.totp_enabled_at IS NOT NULL
                      OR EXISTS (SELECT 1 FROM passkeys p WHERE p.user_id = u.id) AS two_step,
                      u.provisioned_by
               FROM users u WHERE u.customer_id = %s AND u.role = 'customer' AND u.disabled_at IS NULL
               ORDER BY u.email""",
            (customer_id,),
        ).fetchall()
        return {
            **s,
            "your_ip": security.client_ip(request),
            "thresholds_effective": protect.thresholds(conn, customer_id),
            "sso_connections": security.sso_logout_status(conn, customer_id),
            "people": people,
            "without_two_step": sum(1 for p in people if not p["two_step"]),
        }


@router.put("/security")
def set_security(customer_id: str, body: SecurityIn, user: UserDep, request: Request) -> dict:
    """Two-step for everyone, session lifetime, IP allow-list, SSO sign-out and alert thresholds."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        cidrs = security.normalise_cidrs(body.ip_allowlist)
        ip = security.client_ip(request)
        if cidrs and user.role != "admin" and not security.ip_allowed(ip, cidrs):
            raise HTTPException(
                409,
                f"Your own address ({ip or 'unknown'}) is not in that list, so it would lock you out. Add it first.",
            )
        if body.require_two_step and user.role != "admin":
            me = conn.execute("SELECT * FROM users WHERE id = %s", (user.id,)).fetchone()
            from ...identity import mfa

            if not mfa.two_step_on(conn, me):
                raise HTTPException(409, "Set up two-step sign-in for yourself first, then require it for everyone.")
        bad = [k for k in body.thresholds if k not in protect.DEFAULTS]
        if bad:
            raise HTTPException(422, f"Unknown threshold: {', '.join(bad)}.")
        before = security.get(conn, customer_id)
        row = conn.execute(
            """INSERT INTO commai_security_settings
                 (customer_id, require_two_step, session_hours, ip_allowlist, sso_logout, thresholds, updated_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (customer_id) DO UPDATE SET require_two_step = EXCLUDED.require_two_step,
                 session_hours = EXCLUDED.session_hours, ip_allowlist = EXCLUDED.ip_allowlist,
                 sso_logout = EXCLUDED.sso_logout, thresholds = EXCLUDED.thresholds,
                 updated_by = EXCLUDED.updated_by, updated_at = now()
               RETURNING *""",
            (
                customer_id,
                body.require_two_step,
                body.session_hours,
                cidrs,
                body.sso_logout,
                Jsonb(body.thresholds),
                user.actor,
            ),
        ).fetchone()
        changed = {
            k: {"before": before.get(k), "after": row[k]}
            for k in ("require_two_step", "session_hours", "ip_allowlist", "sso_logout", "thresholds")
            if before.get(k) != row[k]
        }
        audit.record(
            conn, user.actor, "commai.security.update", "security", customer_id, {"changed": changed, "ip": ip}
        )
        return row


@router.get("/security/audit")
def security_audit(
    customer_id: str,
    user: UserDep,
    kind: Literal["signin", "failed", "settings"] = "signin",
    limit: int = Query(100, ge=1, le=500),
    before: int | None = None,
) -> list[dict]:
    """Sign-ins, failed sign-ins and setting changes for this organisation."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        return security.audit_view(conn, customer_id, kind, limit, before)


@router.get("/security/alerts")
def list_alerts(customer_id: str, user: UserDep, status: Literal["open", "all"] = "open") -> list[dict]:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        return conn.execute(
            """SELECT * FROM commai_security_alerts WHERE customer_id = %s AND (%s = 'all' OR status = 'open')
               ORDER BY created_at DESC LIMIT 200""",
            (customer_id, status),
        ).fetchall()


@router.post("/security/alerts/{alert_id}/acknowledge")
def acknowledge_alert(customer_id: str, alert_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = _one(
            conn.execute(
                """UPDATE commai_security_alerts SET status = 'acknowledged', acknowledged_by = %s,
                          acknowledged_at = now() WHERE id = %s AND customer_id = %s RETURNING *""",
                (user.actor, alert_id, customer_id),
            ).fetchone(),
            "Alert",
        )
        audit.record(conn, user.actor, "commai.alert.acknowledge", row["kind"], customer_id, {"alert_id": alert_id})
        return row


@router.post("/security/watch")
def run_watch(customer_id: str, user: UserDep) -> list[dict]:
    """Look for unusual use now, rather than waiting for the five-minute check."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        return protect.watch(conn, customer_id)


@router.get("/security/keys")
def list_keys(customer_id: str, user: UserDep) -> list[dict]:
    """The organisation's API keys, where they have been used from, and any lock."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        return protect.keys(conn, customer_id)


@router.post("/security/keys/{key_id}/unlock")
def unlock_key(customer_id: str, key_id: int, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = _one(protect.unlock_key(conn, customer_id, key_id), "API key")
        audit.record(conn, user.actor, "commai.security.key_unlock", row["prefix"], customer_id, {"name": row["name"]})
        return row


# ==== data governance ===========================================================================


class RetentionIn(BaseModel):
    rules: dict[str, int | None] = Field(default_factory=dict, description="category: days, or null to keep")
    hold_all: bool = False
    hold_reason: str = Field(default="", max_length=300)


class HoldIn(BaseModel):
    on: bool
    reason: str = Field(default="", max_length=300)


class EraseIn(BaseModel):
    mode: Literal["anonymise", "delete"]
    confirm: str = Field(description="The contact's id again, to confirm")


class ExportIn(BaseModel):
    include_notes: bool = True


@router.get("/data")
def get_data(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        return {
            "categories": [{"key": k, "label": v} for k, v in governance.CATEGORIES.items()],
            "rules": governance.rules(conn, customer_id),
            "hold": governance.data_settings(conn, customer_id),
            "held_contacts": conn.execute(
                """SELECT id, name, email, legal_hold_reason FROM contacts WHERE customer_id = %s AND legal_hold
                   ORDER BY name""",
                (customer_id,),
            ).fetchall(),
            "runs": conn.execute(
                "SELECT * FROM commai_retention_runs WHERE customer_id = %s ORDER BY started_at DESC LIMIT 20",
                (customer_id,),
            ).fetchall(),
            "requests": conn.execute(
                """SELECT r.*, c.name AS contact_name FROM commai_subject_requests r
                   LEFT JOIN contacts c ON c.id = r.contact_id
                   WHERE r.customer_id = %s ORDER BY r.created_at DESC LIMIT 50""",
                (customer_id,),
            ).fetchall(),
        }


@router.put("/data/retention")
def set_retention(customer_id: str, body: RetentionIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        before = governance.rules(conn, customer_id)
        after = governance.set_rules(conn, customer_id, body.rules, user.actor)
        conn.execute(
            """INSERT INTO commai_data_settings (customer_id, hold_all, hold_reason, updated_by)
               VALUES (%s, %s, %s, %s) ON CONFLICT (customer_id) DO UPDATE SET hold_all = EXCLUDED.hold_all,
                 hold_reason = EXCLUDED.hold_reason, updated_by = EXCLUDED.updated_by, updated_at = now()""",
            (customer_id, body.hold_all, body.hold_reason if body.hold_all else "", user.actor),
        )
        audit.record(
            conn,
            user.actor,
            "commai.data.retention_update",
            "retention",
            customer_id,
            {"before": before, "after": after, "hold_all": body.hold_all},
        )
        governance.schedule_retention(conn, customer_id)
        return {"rules": after, "hold": governance.data_settings(conn, customer_id)}


@router.post("/data/retention/run", status_code=202)
def run_retention_now(customer_id: str, user: UserDep) -> dict:
    """Queue a retention run now (it also runs once a day)."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        job = governance.schedule_retention(conn, customer_id, trigger="manual")
        if job is None:
            raise HTTPException(409, "Set a retention period for at least one kind of data first.")
        audit.record(conn, user.actor, "commai.data.retention_requested", str(job), customer_id)
        return {"job_id": job}


@router.put("/data/contacts/{contact_id}/hold")
def set_hold(customer_id: str, contact_id: str, body: HoldIn, user: UserDep) -> dict:
    """Legal hold: while on, nothing about this contact is deleted or anonymised."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = governance.set_hold(conn, customer_id, contact_id, body.on, body.reason)
        audit.record(
            conn,
            user.actor,
            "commai.data.legal_hold_" + ("on" if body.on else "off"),
            contact_id,
            customer_id,
            {"reason": body.reason},
        )
        return row


@router.get("/data/contacts/{contact_id}/export")
def export_subject(
    customer_id: str,
    contact_id: str,
    user: UserDep,
    format: Literal["json", "zip"] = "json",
    include_notes: bool = False,
):
    """Everything held about one contact. Private notes only when asked for and
    the caller may read notes."""
    access.check(user, customer_id, "commai:admin")
    if include_notes:
        access.require_scope(user, "commai:notes")
        access.require_permission(user, customer_id, "notes")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        data = governance.subject_export(conn, customer_id, contact_id, user.actor, include_notes)
        if format == "zip":
            blob = governance.subject_zip(conn, customer_id, data)
            return Response(
                blob,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="contact-{contact_id}.zip"'},
            )
    return data


@router.post("/data/contacts/{contact_id}/erase")
def erase_subject(customer_id: str, contact_id: str, body: EraseIn, user: UserDep) -> dict:
    """Anonymise or delete one contact on request. Refused under legal hold."""
    access.check(user, customer_id, "commai:admin")
    if body.confirm != contact_id:
        raise HTTPException(422, "Type the contact's id to confirm.")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        out = governance.subject_erase(conn, customer_id, contact_id, body.mode, user.actor)
    if out["refused"]:
        raise HTTPException(409, out["reason"])
    return out


@router.post("/data/exports", status_code=202)
def request_export(customer_id: str, body: ExportIn, user: UserDep) -> dict:
    """A full export of the organisation's data, built in the background."""
    access.check(user, customer_id, "commai:admin")
    if body.include_notes:
        access.require_scope(user, "commai:notes")
        access.require_permission(user, customer_id, "notes")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = governance.request_export(conn, customer_id, user.actor, body.include_notes)
        audit.record(conn, user.actor, "commai.data.export_requested", str(row["id"]), customer_id, body.model_dump())
        return {k: v for k, v in row.items() if k != "data"}


@router.get("/data/exports")
def list_exports(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        return conn.execute(
            """SELECT id, status, include_notes, size, counts, error, requested_by, created_at, ready_at, expires_at
               FROM commai_exports WHERE customer_id = %s ORDER BY created_at DESC LIMIT 20""",
            (customer_id,),
        ).fetchall()


@router.get("/data/exports/{export_id}/download")
def download_export(customer_id: str, export_id: str, user: UserDep) -> Response:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn, _errors():
        _admin(conn, user, customer_id)
        row = _one(
            conn.execute(
                "SELECT * FROM commai_exports WHERE id = %s AND customer_id = %s AND expires_at > now()",
                (export_id, customer_id),
            ).fetchone(),
            "Export",
        )
        if row["status"] != "ready":
            raise HTTPException(409, "That export is not ready yet.")
        audit.record(conn, user.actor, "commai.data.export_download", export_id, customer_id)
    return Response(
        bytes(row["data"]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="exacarib-export-{export_id}.zip"'},
    )


@router.get("/data/processing")
def processing(customer_id: str, user: UserDep, request: Request) -> dict:
    """Processing locations and subprocessors, generated from configuration."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return governance.processing(conn, request.app.state.settings, customer_id)


# ==== ExaCarib-wide ==============================================================================


@public.get("/alerts")
def all_alerts(user: AdminDep, status: Literal["open", "all"] = "open") -> list[dict]:
    """Unusual-use alerts across every business (ExaCarib admins)."""
    with db.tx() as conn:
        return conn.execute(
            """SELECT a.*, c.name AS customer_name FROM commai_security_alerts a
               JOIN customers c ON c.id = a.customer_id
               WHERE (%s = 'all' OR a.status = 'open') ORDER BY a.created_at DESC LIMIT 500""",
            (status,),
        ).fetchall()


@public.post("/watch")
def watch_all(user: AdminDep) -> dict:
    """Run the unusual-use check for every business now (it also runs every five minutes)."""
    raised = 0
    with db.tx() as conn:
        for c in conn.execute("SELECT id FROM customers").fetchall():
            raised += len(protect.watch(conn, c["id"]))
    return {"alerts_raised": raised}
