"""Business calendars (ADR 0030): opening hours per location, public holidays
per country and one-off closures.

The inbox's service targets count business time only: a conversation that
arrives at 17:55 with a one-hour first-reply target on a location that closes
at 18:00 is due at 08:55 the next opening day, not at 18:55. A business with
no locations, or a location with no hours, holidays or closures, runs all the
time, exactly as before.

A conversation's location is its team's location, else the business's
primary location.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg

MAX_DAYS = 400  # never search further ahead than this for open time
END_OF_DAY = dt.time(23, 59)


class CalendarError(ValueError):
    """A bad calendar setting. The message is safe to show."""


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise CalendarError(
            f"{name or 'That'} is not a time zone we know. Use a name like America/Port_of_Spain."
        ) from e


def location_for(conn: psycopg.Connection, customer_id: Any, team_id: Any = None) -> dict | None:
    """The location whose calendar applies: the team's, else the primary one."""
    if team_id:
        row = conn.execute(
            """SELECT l.* FROM commai_teams t JOIN commai_locations l ON l.id = t.location_id
               WHERE t.id = %s AND t.customer_id = %s""",
            (team_id, customer_id),
        ).fetchone()
        if row:
            return row
    return conn.execute(
        "SELECT * FROM commai_locations WHERE customer_id = %s AND is_primary", (customer_id,)
    ).fetchone()


class Calendar:
    """One location's calendar, loaded once for a computation."""

    def __init__(self, conn: psycopg.Connection, location: dict):
        self.location = location
        self.tz = zone(location["timezone"])
        self.hours: dict[int, list[tuple[dt.time, dt.time]]] = {}
        for r in conn.execute(
            "SELECT weekday, opens, closes FROM commai_opening_hours WHERE location_id = %s ORDER BY weekday, opens",
            (location["id"],),
        ).fetchall():
            self.hours.setdefault(r["weekday"], []).append((r["opens"], r["closes"]))
        self.holidays: dict[dt.date, str] = {}
        if location["country"]:
            for r in conn.execute(
                "SELECT day, name FROM commai_holidays WHERE customer_id = %s AND country = %s",
                (location["customer_id"], location["country"]),
            ).fetchall():
                self.holidays[r["day"]] = r["name"]
        self.closures = [
            (r["starts_at"], r["ends_at"], r["reason"])
            for r in conn.execute(
                """SELECT starts_at, ends_at, reason FROM commai_closures
                   WHERE customer_id = %s AND (location_id IS NULL OR location_id = %s)
                     AND ends_at > now() - interval '1 day'
                   ORDER BY starts_at""",
                (location["customer_id"], location["id"]),
            ).fetchall()
        ]

    @property
    def always_open(self) -> bool:
        return not self.hours and not self.holidays and not self.closures

    def day_intervals(self, day: dt.date) -> list[tuple[dt.datetime, dt.datetime]]:
        """Open intervals on a local date, in UTC, after holidays and closures."""
        if day in self.holidays:
            return []
        if self.hours:
            spans = self.hours.get(day.weekday(), [])
        else:
            spans = [(dt.time(0, 0), END_OF_DAY)]
        out = []
        for opens, closes in spans:
            start = dt.datetime.combine(day, opens, self.tz)
            end = (
                dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 0), self.tz)
                if closes >= END_OF_DAY
                else dt.datetime.combine(day, closes, self.tz)
            )
            out.append((start.astimezone(dt.UTC), end.astimezone(dt.UTC)))
        for c_start, c_end, _ in self.closures:
            cut = []
            for s, e in out:
                if c_end <= s or c_start >= e:
                    cut.append((s, e))
                    continue
                if s < c_start:
                    cut.append((s, c_start))
                if c_end < e:
                    cut.append((c_end, e))
            out = cut
        return out

    def is_open(self, at: dt.datetime) -> bool:
        if self.always_open:
            return True
        local = at.astimezone(self.tz).date()
        for day in (local - dt.timedelta(days=1), local):
            if any(s <= at < e for s, e in self.day_intervals(day)):
                return True
        return False

    def add(self, start: dt.datetime, delta: dt.timedelta) -> dt.datetime:
        """start + delta, counting only open time."""
        if self.always_open:
            return start + delta
        remaining = delta
        day = start.astimezone(self.tz).date() - dt.timedelta(days=1)
        for _ in range(MAX_DAYS):
            for s, e in self.day_intervals(day):
                if e <= start:
                    continue
                s = max(s, start)
                if remaining <= e - s:
                    return s + remaining
                remaining -= e - s
            day += dt.timedelta(days=1)
        return start + delta  # never open: fall back to clock time rather than never due

    def next_open(self, at: dt.datetime) -> dt.datetime | None:
        if self.is_open(at):
            return at
        day = at.astimezone(self.tz).date()
        for _ in range(MAX_DAYS):
            for s, e in self.day_intervals(day):
                if e > at:
                    return max(s, at)
            day += dt.timedelta(days=1)
        return None


def calendar_for(conn: psycopg.Connection, customer_id: Any, team_id: Any = None) -> Calendar | None:
    loc = location_for(conn, customer_id, team_id)
    return Calendar(conn, loc) if loc else None


def due(
    conn: psycopg.Connection,
    customer_id: Any,
    team_id: Any,
    start: dt.datetime,
    first: dt.timedelta,
    resolve: dt.timedelta,
) -> tuple[dt.datetime, dt.datetime]:
    """Service-target due times, counting business time only."""
    cal = calendar_for(conn, customer_id, team_id)
    if cal is None:
        return start + first, start + resolve
    return cal.add(start, first), cal.add(start, resolve)


def after_hours(conn: psycopg.Connection, customer_id: Any, team_id: Any, at: dt.datetime | None = None) -> dict:
    """{"open": bool, "team_id": the team to route to, "reason": str}.

    While the team's location is closed, conversations go to that location's
    after-hours team if it has one; otherwise they wait in the team's queue
    and nobody is assigned until it opens."""
    at = at or dt.datetime.now(dt.UTC)
    cal = calendar_for(conn, customer_id, team_id)
    if cal is None or cal.is_open(at):
        return {"open": True, "team_id": team_id, "reason": ""}
    loc = cal.location
    other = loc.get("after_hours_team_id")
    if other and str(other) != str(team_id or ""):
        other_cal = calendar_for(conn, customer_id, other)
        return {
            "open": other_cal is None or other_cal.is_open(at),
            "team_id": other,
            "reason": f"outside hours at {loc['name']}: after-hours team",
        }
    return {"open": False, "team_id": team_id, "reason": f"outside hours at {loc['name']}: waits for opening"}


def status(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    """Each location, whether it is open now and when it next opens."""
    now = dt.datetime.now(dt.UTC)
    out = []
    for loc in conn.execute(
        "SELECT * FROM commai_locations WHERE customer_id = %s ORDER BY is_primary DESC, name", (customer_id,)
    ).fetchall():
        try:
            cal = Calendar(conn, loc)
        except CalendarError:
            out.append({"location_id": str(loc["id"]), "open": None, "next_open": None})
            continue
        is_open = cal.is_open(now)
        out.append(
            {
                "location_id": str(loc["id"]),
                "open": is_open,
                "next_open": None if is_open else cal.next_open(now),
                "always_open": cal.always_open,
            }
        )
    return out
