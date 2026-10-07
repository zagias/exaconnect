"""Countries for voice numbers (ADR 0033).

What CommAI voice needs to know about each country: its calling code, the
area codes numbers come from, and which number ranges the simulated provider
hands out. The country capability matrix itself (kind "country" in the
go-live registry) belongs to the countries module; voice declares only its
own criteria, as features "voice-numbers-XX" and "voice-porting-XX".

Simulated numbers are never real subscribers:
- North American Numbering Plan countries use 555-0100 to 555-0199 in each
  area code, which the plan sets aside for fiction;
- the UK uses 020 7946 0000 to 0999, which Ofcom sets aside for drama;
- Guyana and Belize have no published fictional range, so the simulated
  numbers start with 0 after the country code, which no real number there does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from .. import golive
from .common import VoiceError, digits


@dataclass(frozen=True)
class Country:
    code: str
    name: str
    calling: str  # country calling code, digits
    areas: tuple[str, ...]  # area codes numbers are ordered in ('' when there are none)
    sim: str  # how simulated numbers are built: "nanp", "gb" or "zero"
    porting: bool = True


COUNTRIES: dict[str, Country] = {
    c.code: c
    for c in (
        Country("TT", "Trinidad and Tobago", "1", ("868",), "nanp"),
        Country("JM", "Jamaica", "1", ("876", "658"), "nanp"),
        Country("BB", "Barbados", "1", ("246",), "nanp"),
        Country("BS", "The Bahamas", "1", ("242",), "nanp"),
        Country("GY", "Guyana", "592", ("",), "zero"),
        Country("LC", "Saint Lucia", "1", ("758",), "nanp"),
        Country("VC", "Saint Vincent and the Grenadines", "1", ("784",), "nanp"),
        Country("GD", "Grenada", "1", ("473",), "nanp"),
        Country("AG", "Antigua and Barbuda", "1", ("268",), "nanp"),
        Country("KN", "Saint Kitts and Nevis", "1", ("869",), "nanp"),
        Country("DM", "Dominica", "1", ("767",), "nanp"),
        Country("BZ", "Belize", "501", ("",), "zero"),
        Country("US", "United States", "1", ("305", "786", "212"), "nanp"),
        Country("CA", "Canada", "1", ("416", "647"), "nanp"),
        Country("GB", "United Kingdom", "44", ("20",), "gb"),
    )
}

# North American area codes outside the US and Canada, to tell which country
# a +1 number is in (for fraud rules and emergency notices).
NANP_AREAS: dict[str, str] = {
    "868": "TT", "876": "JM", "658": "JM", "246": "BB", "242": "BS", "758": "LC", "784": "VC",
    "473": "GD", "268": "AG", "869": "KN", "767": "DM", "809": "DO", "829": "DO", "849": "DO",
    "284": "VG", "340": "VI", "345": "KY", "441": "BM", "649": "TC", "664": "MS", "721": "SX",
    "787": "PR", "939": "PR", "264": "AI", "671": "GU", "670": "MP", "684": "AS",
}  # fmt: skip
CANADA_AREAS = {
    "204", "226", "236", "249", "250", "263", "289", "306", "343", "354", "365", "367", "368", "382", "403",
    "416", "418", "428", "431", "437", "438", "450", "468", "474", "506", "514", "519", "548", "579", "581",
    "584", "587", "600", "604", "613", "639", "647", "672", "683", "705", "709", "742", "753", "778", "780",
    "782", "807", "819", "825", "867", "873", "879", "902", "905",
}  # fmt: skip
OTHER_CALLING = {
    "44": "GB", "592": "GY", "501": "BZ", "53": "CU", "52": "MX", "57": "CO", "58": "VE", "597": "SR",
    "509": "HT", "31": "NL", "33": "FR", "34": "ES", "49": "DE", "91": "IN", "86": "CN", "297": "AW",
    "599": "CW",
}  # fmt: skip

NUMBER_CRITERIA = {
    "numbering": "Numbers here come from a carrier contract, with the numbering rules (formats, local presence, "
    "proof of address) written down.",
    "emergency": "An emergency test call from a number here reached the right emergency centre, and the island's "
    "emergency rules and the notice users see were confirmed with the regulator or carrier.",
    "test-calls": "Inbound and outbound test calls passed on the live provider for a number here.",
    "regulatory": "Any licence or registration needed to offer phone numbers here is in place, or written advice "
    "says none is needed.",
}
PORTING_CRITERIA = {
    "process": "The port process here (documents, notice periods, cut-over windows) is written down and one real "
    "port has completed.",
    "rollback": "Rolling a port back to the losing carrier has been agreed with the provider and tested.",
}

for _c in COUNTRIES.values():
    golive.declare(
        "feature",
        f"voice-numbers-{_c.code}",
        f"Voice numbers in {_c.name}",
        NUMBER_CRITERIA,
        {"module": "voice", "country": _c.code, "areas": [a for a in _c.areas if a]},
    )
    golive.declare(
        "feature",
        f"voice-porting-{_c.code}",
        f"Number porting in {_c.name}",
        PORTING_CRITERIA,
        {"module": "voice", "country": _c.code},
    )


def get(code: str) -> Country:
    c = COUNTRIES.get(str(code or "").upper())
    if c is None:
        raise VoiceError(f"CommAI voice doesn't offer numbers in {code or 'that country'} yet.", 422)
    return c


def require(conn: psycopg.Connection, code: str, customer_id: Any, what: str = "numbers") -> Country:
    """Numbers (or porting) in this country must be switched on for this business.
    When the countries module has declared the country itself, it must be on too."""
    c = get(code)
    key = f"voice-{what}-{c.code}"
    if not golive.enabled(conn, "feature", key, customer_id):
        label = "Voice numbers" if what == "numbers" else "Number porting"
        raise VoiceError(
            f"{label} in {c.name} are not switched on yet. ExaCarib switches them on once the written "
            "go-live checks for that country pass.",
            409,
        )
    row = conn.execute("SELECT 1 FROM commai_capabilities WHERE kind = 'country' AND key = %s", (c.code,)).fetchone()
    if row and not golive.enabled(conn, "country", c.code, customer_id):
        raise VoiceError(f"CommAI is not switched on in {c.name} yet.", 409)
    return c


def available(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    """Every country with whether numbers and porting are on for this business."""
    out = []
    for c in COUNTRIES.values():
        out.append(
            {
                "code": c.code,
                "name": c.name,
                "calling_code": c.calling,
                "areas": [a for a in c.areas if a],
                "numbers": golive.enabled(conn, "feature", f"voice-numbers-{c.code}", customer_id),
                "porting": golive.enabled(conn, "feature", f"voice-porting-{c.code}", customer_id),
            }
        )
    return out


def country_of(number: str) -> str:
    """The ISO country of an E.164 number ('' when unknown)."""
    d = digits(number)
    if d.startswith("1") and len(d) >= 4:
        area = d[1:4]
        if area in NANP_AREAS:
            return NANP_AREAS[area]
        return "CA" if area in CANADA_AREAS else "US"
    for n in (3, 2, 1):
        if d[:n] in OTHER_CALLING:
            return OTHER_CALLING[d[:n]]
    return ""


def sim_range(c: Country, area: str) -> list[str]:
    """The simulated provider's numbers for a country and area (fictional, see above)."""
    if c.sim == "nanp":
        return [f"+1{area}55501{i:02d}" for i in range(100)]
    if c.sim == "gb":
        return [f"+4420794600{i:02d}" for i in range(100)]
    return [f"+{c.calling}0000{i:03d}" for i in range(100)]


def check_area(c: Country, area: str | None) -> str:
    area = digits(area or "") or c.areas[0]
    if area not in c.areas:
        raise VoiceError(f"Numbers in {c.name} come from area code {', '.join(a for a in c.areas if a)}.", 422)
    return area
