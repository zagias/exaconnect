"""Partners: resellers and managed-service providers (ADR 0031).

A partner looks after several businesses. The model keeps every existing
tenant check in force:

- A partner asks to manage a business (`request_link`) naming the scopes it
  wants. Someone at the business with commai:admin accepts, choosing which of
  those scopes to grant, or declines; they can change or revoke it at any time.
- A partner person "switches" into a linked business through a delegate
  account of that business (role customer, `access_scopes` = what was granted).
  So the partner reaches that one business only, with only those scopes, and
  every existing tenant check applies unchanged. Revoking disables the
  delegates and ends their sessions and keys.
- Delegates can never accept, change or revoke partner links, and never
  approve OAuth apps: those stay with the business's own people.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ..identity import sessions
from .access import SCOPES

UNUSABLE_PASSWORD = "!delegate"  # verify_password can never match this


class PartnerError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def membership(conn: psycopg.Connection, user_id: Any) -> dict | None:
    """The partner this person belongs to, with their role there."""
    return conn.execute(
        """SELECT p.*, m.role AS member_role FROM commai_partner_members m
           JOIN commai_partners p ON p.id = m.partner_id WHERE m.user_id = %s AND p.active""",
        (user_id,),
    ).fetchone()


def delegation(conn: psycopg.Connection, user_id: Any) -> dict | None:
    """When this account is a partner's delegate: the link, partner and the real person."""
    return conn.execute(
        """SELECT d.*, l.customer_id, l.partner_id, l.status AS link_status, p.name AS partner_name
           FROM commai_partner_delegates d JOIN commai_partner_links l ON l.id = d.link_id
           JOIN commai_partners p ON p.id = l.partner_id WHERE d.delegate_user_id = %s""",
        (user_id,),
    ).fetchone()


def is_delegate(conn: psycopg.Connection, user_id: Any) -> bool:
    return (
        conn.execute("SELECT 1 FROM commai_partner_delegates WHERE delegate_user_id = %s", (user_id,)).fetchone()
        is not None
    )


def check_scopes(scopes: list[str]) -> list[str]:
    bad = sorted(set(scopes) - set(SCOPES))
    if bad or not scopes:
        raise PartnerError(f"Scopes are {', '.join(SCOPES)}." + (f" Not {', '.join(bad)}." if bad else ""), 422)
    return sorted(set(scopes))


# ---- partners ----------------------------------------------------------------------------


def create(conn: psycopg.Connection, name: str, kind: str, markup_pct: float, contact_email: str, actor: str) -> dict:
    if conn.execute("SELECT 1 FROM commai_partners WHERE lower(name) = lower(%s)", (name,)).fetchone():
        raise PartnerError("A partner with that name already exists.", 409)
    return conn.execute(
        """INSERT INTO commai_partners (name, kind, markup_pct, contact_email, created_by)
           VALUES (%s, %s, %s, %s, %s) RETURNING *""",
        (name, kind, markup_pct, contact_email, actor),
    ).fetchone()


def add_member(conn: psycopg.Connection, partner_id: Any, email: str, role: str) -> dict:
    u = conn.execute(
        "SELECT id, role, email FROM users WHERE lower(email) = lower(%s) AND disabled_at IS NULL", (email,)
    ).fetchone()
    if u is None:
        raise PartnerError("No account with that email. Create the account first.", 404)
    if u["role"] != "customer":
        raise PartnerError("Partner people use a customer account (not an admin or carrier account).", 422)
    if is_delegate(conn, u["id"]):
        raise PartnerError("That is a delegate account, not a person.", 422)
    other = conn.execute("SELECT partner_id FROM commai_partner_members WHERE user_id = %s", (u["id"],)).fetchone()
    if other and str(other["partner_id"]) != str(partner_id):
        raise PartnerError("That person already belongs to another partner.", 409)
    conn.execute(
        """INSERT INTO commai_partner_members (partner_id, user_id, role) VALUES (%s, %s, %s)
           ON CONFLICT (partner_id, user_id) DO UPDATE SET role = EXCLUDED.role""",
        (partner_id, u["id"], role),
    )
    return {"user_id": str(u["id"]), "email": u["email"], "role": role}


# ---- links -------------------------------------------------------------------------------


LINK_COLUMNS = """l.id, l.partner_id, l.customer_id, l.status, l.requested_scopes, l.scopes, l.markup_pct,
  l.white_label, l.note, l.requested_by, l.requested_at, l.decided_by, l.decided_at, l.revoked_by, l.revoked_at"""


def request_link(
    conn: psycopg.Connection, partner_id: Any, customer_id: Any, scopes: list[str], note: str, actor: str
) -> dict:
    scopes = check_scopes(scopes)
    cust = conn.execute("SELECT id FROM customers WHERE id = %s AND sandbox_of IS NULL", (customer_id,)).fetchone()
    if cust is None:
        raise PartnerError("No business with that id. Ask the business for its id in Jibsy settings.", 404)
    if conn.execute(
        """SELECT 1 FROM commai_partner_links WHERE partner_id = %s AND customer_id = %s
           AND status IN ('pending', 'active')""",
        (partner_id, customer_id),
    ).fetchone():
        raise PartnerError("There is already an open request or link with this business.", 409)
    return conn.execute(
        """INSERT INTO commai_partner_links AS l (partner_id, customer_id, requested_scopes, note, requested_by)
           VALUES (%s, %s, %s, %s, %s) RETURNING """
        + LINK_COLUMNS,
        (partner_id, customer_id, scopes, note, actor),
    ).fetchone()


def get_link(conn: psycopg.Connection, link_id: Any, customer_id: Any = None, partner_id: Any = None) -> dict:
    row = conn.execute(
        f"""SELECT {LINK_COLUMNS}, p.name AS partner_name, p.kind AS partner_kind, c.name AS customer_name
            FROM commai_partner_links l JOIN commai_partners p ON p.id = l.partner_id
            JOIN customers c ON c.id = l.customer_id
            WHERE l.id = %s AND (%s::uuid IS NULL OR l.customer_id = %s::uuid)
              AND (%s::uuid IS NULL OR l.partner_id = %s::uuid)""",
        (link_id, customer_id, customer_id, partner_id, partner_id),
    ).fetchone()
    if row is None:
        raise PartnerError("Link not found.", 404)
    return row


def decide(
    conn: psycopg.Connection, customer_id: Any, link_id: Any, accept: bool, scopes: list[str] | None, actor: str
) -> dict:
    link = get_link(conn, link_id, customer_id=customer_id)
    if link["status"] != "pending":
        raise PartnerError("This request has already been answered.", 409)
    if not accept:
        conn.execute(
            "UPDATE commai_partner_links SET status = 'declined', decided_by = %s, decided_at = now() WHERE id = %s",
            (actor, link_id),
        )
        return get_link(conn, link_id)
    granted = check_scopes(scopes if scopes is not None else list(link["requested_scopes"]))
    extra = set(granted) - set(link["requested_scopes"])
    if extra:
        raise PartnerError(f"The partner did not ask for {', '.join(sorted(extra))}.", 422)
    if conn.execute(
        "SELECT 1 FROM commai_partner_links WHERE customer_id = %s AND status = 'active'", (customer_id,)
    ).fetchone():
        raise PartnerError("Another partner already manages this business. Revoke that link first.", 409)
    conn.execute(
        """UPDATE commai_partner_links SET status = 'active', scopes = %s, decided_by = %s, decided_at = now()
           WHERE id = %s""",
        (granted, actor, link_id),
    )
    return get_link(conn, link_id)


def change_scopes(conn: psycopg.Connection, customer_id: Any, link_id: Any, scopes: list[str]) -> dict:
    link = get_link(conn, link_id, customer_id=customer_id)
    if link["status"] != "active":
        raise PartnerError("Only an active link can be changed.", 409)
    granted = check_scopes(scopes)
    extra = set(granted) - set(link["requested_scopes"])
    if extra:
        raise PartnerError(f"The partner did not ask for {', '.join(sorted(extra))}.", 422)
    conn.execute("UPDATE commai_partner_links SET scopes = %s WHERE id = %s", (granted, link_id))
    # Delegates read their scopes on every request, so this takes effect at once.
    conn.execute(
        """UPDATE users SET access_scopes = %s, updated_at = now()
           WHERE id IN (SELECT delegate_user_id FROM commai_partner_delegates WHERE link_id = %s)""",
        (granted, link_id),
    )
    return get_link(conn, link_id)


def revoke(conn: psycopg.Connection, link_id: Any, actor: str, customer_id: Any = None, partner_id: Any = None) -> dict:
    """Either side can end a link. Delegates are switched off at once."""
    link = get_link(conn, link_id, customer_id=customer_id, partner_id=partner_id)
    if link["status"] not in ("pending", "active"):
        raise PartnerError("This link has already ended.", 409)
    conn.execute(
        "UPDATE commai_partner_links SET status = 'revoked', revoked_by = %s, revoked_at = now() WHERE id = %s",
        (actor, link_id),
    )
    for d in conn.execute(
        "SELECT delegate_user_id FROM commai_partner_delegates WHERE link_id = %s", (link_id,)
    ).fetchall():
        conn.execute("UPDATE users SET disabled_at = now() WHERE id = %s", (d["delegate_user_id"],))
        sessions.end_all(conn, d["delegate_user_id"], revoke_keys=True)
        conn.execute(
            """UPDATE commai_oauth_grants SET revoked_at = now(), revoked_by = %s
               WHERE user_id = %s AND revoked_at IS NULL""",
            (actor, d["delegate_user_id"]),
        )
    return get_link(conn, link_id)


# ---- switching -------------------------------------------------------------------------------


def delegate_for(conn: psycopg.Connection, partner: dict, user: Any, customer_id: Any) -> dict:
    """The delegate account this partner person uses in this business (made on first switch)."""
    link = conn.execute(
        """SELECT * FROM commai_partner_links WHERE partner_id = %s AND customer_id = %s AND status = 'active'""",
        (partner["id"], customer_id),
    ).fetchone()
    if link is None:
        raise PartnerError("This business has not accepted a link with your partner account.", 403)
    row = conn.execute(
        """SELECT u.* FROM commai_partner_delegates d JOIN users u ON u.id = d.delegate_user_id
           WHERE d.link_id = %s AND d.partner_user_id = %s""",
        (link["id"], user.id),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """INSERT INTO users (email, password_hash, role, customer_id, access_scopes, display_name, provisioned_by)
               VALUES (%s, %s, 'customer', %s, %s, %s, 'partner') RETURNING *""",
            (
                f"{user.email}#partner:{link['id']}",
                UNUSABLE_PASSWORD,
                customer_id,
                list(link["scopes"]),
                f"{user.email} ({partner['name']})",
            ),
        ).fetchone()
        conn.execute(
            "INSERT INTO commai_partner_delegates (link_id, partner_user_id, delegate_user_id) VALUES (%s, %s, %s)",
            (link["id"], user.id, row["id"]),
        )
    conn.execute("UPDATE commai_partner_delegates SET last_switch_at = now() WHERE delegate_user_id = %s", (row["id"],))
    return row


def start_session(conn: psycopg.Connection, user_id: Any, hours: int, via: str) -> tuple[str, dt.datetime]:
    return sessions.start(conn, user_id, hours, via)


# ---- consolidated view and billing --------------------------------------------------------------


def _month(period: str | None) -> tuple[dt.datetime, dt.datetime]:
    now = dt.datetime.now(dt.UTC)
    if period:
        try:
            y, m = (int(x) for x in period.split("-"))
            start = dt.datetime(y, m, 1, tzinfo=dt.UTC)
        except ValueError as e:
            raise PartnerError("Give the month as YYYY-MM.", 422) from e
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    return start, end


def customers_view(conn: psycopg.Connection, partner: dict, period: str | None = None) -> list[dict]:
    """Every business linked to this partner: health (when commai:read was granted) and usage.

    Pending, declined and revoked links show only the business's name and the
    request: no health or usage."""
    from .automation import reports

    start, end = _month(period)
    rows = conn.execute(
        f"""SELECT {LINK_COLUMNS}, c.name AS customer_name FROM commai_partner_links l
            JOIN customers c ON c.id = l.customer_id WHERE l.partner_id = %s
            ORDER BY l.status <> 'active', c.name""",
        (partner["id"],),
    ).fetchall()
    out = []
    for r in rows:
        item = {**r, "health": None, "usage": None}
        if r["status"] == "active":
            markup = Decimal(str(r["markup_pct"] if r["markup_pct"] is not None else partner["markup_pct"]))
            u = reports.usage_report(conn, r["customer_id"], start, end)
            base = Decimal(str(u["example_total"]))
            item["usage"] = {
                "meters": [{"meter": m["meter"], "quantity": m["quantity"]} for m in u["meters"]],
                "example_total": float(base),
                "markup_pct": float(markup),
                "example_total_with_markup": float((base * (1 + markup / 100)).quantize(Decimal("0.0001"))),
                "prices": u["prices"],
            }
            if "commai:read" in r["scopes"] or "commai:write" in r["scopes"]:
                item["health"] = health(conn, r["customer_id"])
        out.append(item)
    return out


def health(conn: psycopg.Connection, customer_id: Any) -> dict:
    """A short health summary: open work, failed deliveries and broken channels."""
    c = conn.execute(
        """SELECT count(*) FILTER (WHERE state NOT IN ('resolved', 'snoozed')) AS open,
                  count(*) FILTER (WHERE state NOT IN ('resolved', 'snoozed') AND assignee_id IS NULL
                                   AND handler <> 'ai') AS unassigned,
                  count(*) FILTER (WHERE state NOT IN ('resolved', 'snoozed') AND first_reply_at IS NULL
                                   AND first_reply_due < now()) AS overdue
           FROM conversations WHERE customer_id = %s""",
        (customer_id,),
    ).fetchone()
    dead = conn.execute(
        """SELECT count(*) AS n FROM jobs WHERE customer_id = %s AND status = 'dead'
           AND created_at > now() - interval '1 day'""",
        (customer_id,),
    ).fetchone()["n"]
    broken = conn.execute(
        "SELECT count(*) AS n FROM channel_accounts WHERE customer_id = %s AND status = 'broken'", (customer_id,)
    ).fetchone()["n"]
    state = "bad" if broken or dead else ("warn" if c["overdue"] or c["unassigned"] > 5 else "ok")
    return {
        "state": state,
        "open": c["open"],
        "unassigned": c["unassigned"],
        "overdue": c["overdue"],
        "failed_jobs_24h": dead,
        "broken_channels": broken,
    }


def record_statements(conn: psycopg.Connection, partner: dict, period: str, actor: str) -> list[dict]:
    """Write (or rewrite) the month's statement for every active link. No payment is taken."""
    from .automation import reports

    start, end = _month(period)
    out = []
    for r in conn.execute(
        "SELECT * FROM commai_partner_links WHERE partner_id = %s AND status = 'active'", (partner["id"],)
    ).fetchall():
        u = reports.usage_report(conn, r["customer_id"], start, end)
        markup = Decimal(str(r["markup_pct"] if r["markup_pct"] is not None else partner["markup_pct"]))
        base = Decimal(str(u["example_total"]))
        total = (base * (1 + markup / 100)).quantize(Decimal("0.0001"))
        lines = [{"meter": m["meter"], "quantity": m["quantity"], "unit_price": m["example_unit_price"],
                  "cost": m["example_cost"]} for m in u["meters"]]  # fmt: skip
        row = conn.execute(
            """INSERT INTO commai_partner_statements (partner_id, customer_id, period, base_amount, markup_pct,
                 total_amount, currency, lines, recorded_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (partner_id, customer_id, period) DO UPDATE SET base_amount = EXCLUDED.base_amount,
                 markup_pct = EXCLUDED.markup_pct, total_amount = EXCLUDED.total_amount, lines = EXCLUDED.lines,
                 recorded_by = EXCLUDED.recorded_by, recorded_at = now()
               RETURNING *""",
            (partner["id"], r["customer_id"], start.date(), base, markup, total, u["currency"], Jsonb(lines), actor),
        ).fetchone()
        out.append(row)
    return out
