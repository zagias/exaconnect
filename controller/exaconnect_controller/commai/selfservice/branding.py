"""How a business looks on its help centre (ADR 0031).

Read only through `branding()`. White-label branding is being added by another
part of phase 3; when its table lands, point this one function at it and the
help centre follows. Today it uses what exists: the business's name, the help
centre's own colour setting, else the colour of its first website chat key.
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
    """{"name", "colour", "logo_url"}. The colour is always one white text can
    sit on legibly; a colour that fails falls back to ExaCarib blue."""
    row = conn.execute(
        """SELECT c.name, h.settings->>'colour' AS help_colour,
                  (SELECT w.settings->>'colour' FROM widget_keys w WHERE w.customer_id = c.id AND w.active
                   ORDER BY w.created_at LIMIT 1) AS widget_colour
           FROM customers c LEFT JOIN help_centres h ON h.customer_id = c.id WHERE c.id = %s""",
        (customer_id,),
    ).fetchone()
    if row is None:
        return {"name": "", "colour": DEFAULT_COLOUR, "logo_url": ""}
    colour = next((c for c in (row["help_colour"], row["widget_colour"]) if c and HEX.match(c)), DEFAULT_COLOUR)
    if not _contrast_ok(colour):
        colour = DEFAULT_COLOUR
    return {"name": row["name"], "colour": colour, "logo_url": ""}
