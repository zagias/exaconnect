"""How a business looks on its help centre (ADR 0037).

Read only through `branding()`. The business's name always shows (its customers
know it by that). Colour: the help centre's own setting, else the business's
white-label brand (ADR 0031), else its first website chat key's colour. The logo
is the business's own white-label logo. The "runs on" line names the managing
partner's product when that partner white-labels the business, else ExaCarib.
"""

from __future__ import annotations

import re
from typing import Any

import psycopg

DEFAULT_COLOUR = "#155EEF"
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _contrast_ok(hex_colour: str) -> bool:
    """White text on this colour meets WCAG AA for normal text (4.5:1)."""

    def lin(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    lum = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    return (1.05) / (lum + 0.05) >= 4.5


def branding(conn: psycopg.Connection, customer_id: Any) -> dict:
    """{"name", "colour", "logo_url", "platform"}. The colour is always one white
    text can sit on legibly; a colour that fails falls back to ExaCarib blue."""
    from .. import branding as whitelabel

    row = conn.execute(
        """SELECT c.name, h.settings->>'colour' AS help_colour,
                  (SELECT w.settings->>'colour' FROM widget_keys w WHERE w.customer_id = c.id AND w.active
                   ORDER BY w.created_at LIMIT 1) AS widget_colour
           FROM customers c LEFT JOIN help_centres h ON h.customer_id = c.id WHERE c.id = %s""",
        (customer_id,),
    ).fetchone()
    if row is None:
        return {"name": "", "colour": DEFAULT_COLOUR, "logo_url": "", "platform": "ExaCarib"}
    own = whitelabel.public_view(whitelabel.get(conn, customer_id=customer_id)) or {}
    partner = conn.execute(
        """SELECT b.product_name FROM commai_partner_links l JOIN commai_whitelabel b ON b.partner_id = l.partner_id
           WHERE l.customer_id = %s AND l.status = 'active' AND l.white_label LIMIT 1""",
        (customer_id,),
    ).fetchone()
    platform = partner["product_name"] if partner else "ExaCarib"
    colours = (row["help_colour"], own.get("colour"), row["widget_colour"])
    colour = next((c for c in colours if c and HEX.match(c)), DEFAULT_COLOUR)
    if not _contrast_ok(colour):
        colour = DEFAULT_COLOUR
    return {"name": row["name"], "colour": colour, "logo_url": own.get("logo_url") or "", "platform": platform}
