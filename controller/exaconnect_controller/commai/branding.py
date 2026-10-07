"""White-label branding and custom domains (ADR 0025).

A partner (and, optionally, one of its businesses) sets a product name, a logo,
two colours and a support email. The portal applies it at run time for that
partner's people and businesses; the chat widget uses the business's brand, or
its partner's. With no brand, everything is ExaCarib's own look (CLAUDE.md).

Checks:
- Colours are #RRGGBB. The brand colour carries white text (buttons) and is
  used for links on white, so it needs 4.5:1 contrast against white; the ink
  (text) colour needs 4.5:1 against white too (WCAG 2.2 AA).
- The logo is PNG, JPEG or WebP (never SVG: it can carry script), at most
  256 KB, and its bytes must match its declared type. PNGs are also checked
  for size (at most 2000 by 2000 pixels).

Custom domains are proved with a DNS TXT record
``_exacarib-challenge.<domain>  "exacarib-verify=<token>"``, checked through a
resolver behind an interface (simulated by default). Caddy asks
``tls_allowed`` before issuing a certificate, so certificates are issued only
for verified domains.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import struct
import urllib.parse
import urllib.request
from typing import Any, Protocol

import psycopg

MAX_LOGO_BYTES = 256 * 1024
MAX_LOGO_PX = 2000
LOGO_TYPES = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/webp": (b"RIFF",),
}
MIN_CONTRAST = 4.5
DEFAULT_INK = "#10213D"
CHALLENGE_PREFIX = "_exacarib-challenge."
TXT_PREFIX = "exacarib-verify="
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$")
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class BrandError(Exception):
    def __init__(self, message: str, code: int = 422):
        super().__init__(message)
        self.code = code


# ---- checks --------------------------------------------------------------------------------


def _luminance(hex_colour: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two #RRGGBB colours (1 to 21)."""
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def check_colour(value: str, what: str) -> str:
    if not _HEX.match(value or ""):
        raise BrandError(f"The {what} colour must look like #155EEF.")
    ratio = contrast(value, "#FFFFFF")
    if ratio < MIN_CONTRAST:
        raise BrandError(
            f"The {what} colour {value} has {ratio:.1f}:1 contrast with white; it needs at least {MIN_CONTRAST}:1 "
            "so text stays readable. Choose a darker shade."
        )
    return value.upper()


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not 1 <= len(name) <= 60:
        raise BrandError("The product name is 1 to 60 characters.")
    if "exacarib" in name.lower().replace(" ", ""):
        raise BrandError("A partner's product name can't use the ExaCarib name.")
    return name


def check_email(email: str) -> str:
    email = (email or "").strip()
    if email and not _EMAIL.match(email):
        raise BrandError("Give a support email address like help@example.com.")
    return email


def check_logo(data: bytes, content_type: str) -> tuple[bytes, str]:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in LOGO_TYPES:
        raise BrandError("The logo must be a PNG, JPEG or WebP image.", 415)
    if not data:
        raise BrandError("The logo file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise BrandError(f"The logo is too large: at most {MAX_LOGO_BYTES // 1024} KB.", 413)
    if not any(data.startswith(m) for m in LOGO_TYPES[ctype]) or (ctype == "image/webp" and data[8:12] != b"WEBP"):
        raise BrandError("The file is not the image type it says it is.", 415)
    if ctype == "image/png":
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise BrandError("The PNG file is damaged.", 415)
        w, h = struct.unpack(">II", data[16:24])
        if not (0 < w <= MAX_LOGO_PX and 0 < h <= MAX_LOGO_PX):
            raise BrandError(f"The logo is at most {MAX_LOGO_PX} by {MAX_LOGO_PX} pixels.")
    return data, ctype


def check_domain(domain: str) -> str:
    d = (domain or "").strip().lower().rstrip(".")
    if len(d) > 253 or "." not in d or re.fullmatch(r"[0-9.]+", d):
        raise BrandError("Give a domain name like chat.example.com.")
    if not all(_LABEL.match(label) for label in d.split(".")):
        raise BrandError("Give a domain name like chat.example.com.")
    if d == "exacarib.com" or d.endswith(".exacarib.com") or d.endswith(".invalid") or d == "localhost":
        raise BrandError("That domain can't be used for a partner brand.")
    public = urllib.parse.urlparse(os.environ.get("EXA_PUBLIC_URL", "")).hostname or ""
    if public and (d == public or d.endswith("." + public)):
        raise BrandError("That domain can't be used for a partner brand.")
    return d


# ---- brands -----------------------------------------------------------------------------------


BRAND_COLUMNS = (
    "id, partner_id, customer_id, product_name, colour, ink, support_email, logo_type, logo_sha, updated_by, updated_at"
)


def public_view(row: dict | None) -> dict | None:
    """What the portal and widget need. None means ExaCarib's own look."""
    if row is None:
        return None
    logo = f"/api/v1/commai/branding/logo/{row['id']}?v={row['logo_sha'][:12]}" if row.get("logo_sha") else None
    return {
        "id": str(row["id"]),
        "product_name": row["product_name"],
        "colour": row["colour"],
        "ink": row["ink"],
        "support_email": row["support_email"],
        "logo_url": logo,
    }


def get(conn: psycopg.Connection, partner_id: Any = None, customer_id: Any = None) -> dict | None:
    if partner_id is not None:
        return conn.execute(
            f"SELECT {BRAND_COLUMNS} FROM commai_whitelabel WHERE partner_id = %s", (partner_id,)
        ).fetchone()
    return conn.execute(
        f"SELECT {BRAND_COLUMNS} FROM commai_whitelabel WHERE customer_id = %s", (customer_id,)
    ).fetchone()


def save(
    conn: psycopg.Connection,
    *,
    partner_id: Any = None,
    customer_id: Any = None,
    product_name: str,
    colour: str,
    ink: str | None,
    support_email: str,
    actor: str,
) -> dict:
    name = check_name(product_name)
    colour = check_colour(colour, "brand")
    ink = check_colour(ink or DEFAULT_INK, "text")
    email = check_email(support_email)
    owner = "partner_id" if partner_id is not None else "customer_id"
    return conn.execute(
        f"""INSERT INTO commai_whitelabel ({owner}, product_name, colour, ink, support_email, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT ({owner}) DO UPDATE SET product_name = EXCLUDED.product_name, colour = EXCLUDED.colour,
              ink = EXCLUDED.ink, support_email = EXCLUDED.support_email, updated_by = EXCLUDED.updated_by,
              updated_at = now()
            RETURNING {BRAND_COLUMNS}""",
        (partner_id if partner_id is not None else customer_id, name, colour, ink, email, actor),
    ).fetchone()


def set_logo(conn: psycopg.Connection, brand_id: Any, data: bytes, content_type: str, actor: str) -> dict:
    data, ctype = check_logo(data, content_type)
    return conn.execute(
        f"""UPDATE commai_whitelabel SET logo = %s, logo_type = %s, logo_sha = %s, updated_by = %s, updated_at = now()
            WHERE id = %s RETURNING {BRAND_COLUMNS}""",
        (data, ctype, hashlib.sha256(data).hexdigest(), actor, brand_id),
    ).fetchone()


def for_customer(conn: psycopg.Connection, customer_id: Any) -> dict | None:
    """The business's own brand, else its managing partner's (when white-label is on)."""
    if customer_id is None:
        return None
    own = get(conn, customer_id=customer_id)
    if own:
        return own
    return conn.execute(
        f"""SELECT {", ".join("b." + c.strip() for c in BRAND_COLUMNS.split(","))}
            FROM commai_partner_links l JOIN commai_whitelabel b ON b.partner_id = l.partner_id
            WHERE l.customer_id = %s AND l.status = 'active' AND l.white_label""",
        (customer_id,),
    ).fetchone()


def for_user(conn: psycopg.Connection, user: Any) -> dict | None:
    """Partner people see their partner's brand; everyone else their business's (or none)."""
    if user.role != "customer":
        return None
    from . import partners

    m = partners.membership(conn, user.id)
    if m:
        b = get(conn, partner_id=m["id"])
        if b:
            return b
    return for_customer(conn, user.customer_id)


# ---- domains ------------------------------------------------------------------------------------


class Resolver(Protocol):
    name: str

    def txt(self, conn: psycopg.Connection, name: str) -> list[str]: ...


class SimulatedResolver:
    """Reads TXT records from commai_sim_dns. The default: no network."""

    name = "simulated"

    def txt(self, conn: psycopg.Connection, name: str) -> list[str]:
        return [
            r["value"] for r in conn.execute("SELECT value FROM commai_sim_dns WHERE name = %s", (name,)).fetchall()
        ]


class DohResolver:
    """DNS over HTTPS (JSON API, RFC 8484 style). Runs only with EXA_DNS_RESOLVER=doh;
    EXA_DOH_URL picks the service (default Cloudflare's)."""

    name = "doh"

    def txt(self, conn: psycopg.Connection, name: str) -> list[str]:
        if os.environ.get("EXA_DNS_RESOLVER") != "doh":
            raise BrandError("Real DNS lookups are not switched on (EXA_DNS_RESOLVER=doh).", 409)
        base = os.environ.get("EXA_DOH_URL", "https://cloudflare-dns.com/dns-query")
        url = f"{base}?{urllib.parse.urlencode({'name': name, 'type': 'TXT'})}"
        req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - fixed https service
            body = json.loads(r.read(200_000))
        return [
            a.get("data", "").strip('"').replace('" "', "") for a in body.get("Answer") or [] if a.get("type") == 16
        ]


def resolver() -> Resolver:
    return DohResolver() if os.environ.get("EXA_DNS_RESOLVER") == "doh" else SimulatedResolver()


def add_domain(conn: psycopg.Connection, brand_id: Any, purpose: str, domain: str, actor: str) -> dict:
    d = check_domain(domain)
    if conn.execute("SELECT 1 FROM commai_brand_domains WHERE domain = %s", (d,)).fetchone():
        raise BrandError("That domain is already registered.", 409)
    n = conn.execute("SELECT count(*) AS n FROM commai_brand_domains WHERE brand_id = %s", (brand_id,)).fetchone()["n"]
    if n >= 10:
        raise BrandError("A brand can have up to 10 domains.", 400)
    return conn.execute(
        """INSERT INTO commai_brand_domains (brand_id, purpose, domain, token, created_by)
           VALUES (%s, %s, %s, %s, %s) RETURNING *""",
        (brand_id, purpose, d, secrets.token_urlsafe(24), actor),
    ).fetchone()


def domain_view(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "purpose": row["purpose"],
        "domain": row["domain"],
        "status": row["status"],
        "last_error": row["last_error"],
        "last_checked_at": row["last_checked_at"],
        "verified_at": row["verified_at"],
        "dns": {
            "type": "TXT",
            "name": CHALLENGE_PREFIX + row["domain"],
            "value": TXT_PREFIX + row["token"],
        },
        "cname": {"name": row["domain"], "points_to": os.environ.get("EXA_PUBLIC_HOST", "connect.exacarib.com")},
    }


def verify(conn: psycopg.Connection, domain_id: Any, res: Resolver | None = None) -> dict:
    row = conn.execute("SELECT * FROM commai_brand_domains WHERE id = %s FOR UPDATE", (domain_id,)).fetchone()
    if row is None:
        raise BrandError("Domain not found.", 404)
    res = res or resolver()
    want = TXT_PREFIX + row["token"]
    try:
        found = res.txt(conn, CHALLENGE_PREFIX + row["domain"])
        ok, err = (
            want in found,
            "" if want in found else f"No TXT record {want} at {CHALLENGE_PREFIX}{row['domain']} yet.",
        )
    except BrandError:
        raise
    except Exception as e:  # the resolver failed: say so, try again later
        ok, err = False, f"DNS lookup failed: {type(e).__name__}"
    return conn.execute(
        """UPDATE commai_brand_domains SET status = %s, last_error = %s, last_checked_at = now(),
             verified_at = CASE WHEN %s THEN coalesce(verified_at, now()) ELSE NULL END
           WHERE id = %s RETURNING *""",
        ("verified" if ok else ("failed" if row["status"] == "verified" else "pending"), err, ok, domain_id),
    ).fetchone()


def tls_allowed(conn: psycopg.Connection, domain: str) -> bool:
    """Caddy's on-demand TLS 'ask': a certificate only for a verified domain."""
    return (
        conn.execute(
            "SELECT 1 FROM commai_brand_domains WHERE domain = %s AND status = 'verified'",
            ((domain or "").strip().lower().rstrip("."),),
        ).fetchone()
        is not None
    )


def for_host(conn: psycopg.Connection, host: str) -> dict | None:
    """The brand whose verified portal domain this is (for the sign-in page)."""
    h = (host or "").split(":")[0].strip().lower()
    return conn.execute(
        f"""SELECT {", ".join("b." + c.strip() for c in BRAND_COLUMNS.split(","))}
            FROM commai_brand_domains d JOIN commai_whitelabel b ON b.id = d.brand_id
            WHERE d.domain = %s AND d.status = 'verified' AND d.purpose = 'portal'""",
        (h,),
    ).fetchone()
