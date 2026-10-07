"""Countries: the capability matrix and the SMS rules the gateway enforces (ADR 0023).

Every country is declared in the go-live registry (kind "country", key the ISO
code) and starts off. The matrix says what research suggests is possible there
for numbers, porting, SMS, WhatsApp, calling and emergency calling. **None of it
is verified**: every entry carries ``verified: False`` and the screens say
"Unverified research" until someone who knows the local rules has checked it
and an ExaCarib admin records that criterion as met.

The SMS gateway enforces, for each destination country:

- the country must be switched on for the business (go-live registry);
- the sending number's type (long code, toll-free, short code, alphanumeric)
  must be one the country accepts, and registered where the country requires it
  (US 10DLC, toll-free verification);
- quiet hours in the recipient's time zone, where a rule exists, for messages
  the business starts (a reply within 24 hours of the person's own message is
  not affected);
- a per-country rate per minute.

Each refusal says which rule, in plain words.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from .. import channels, golive
from . import messaging

RESEARCH_AS_OF = "2026-10-07"
UNVERIFIED = "Unverified research"
DEFAULT_RATE_PER_MINUTE = 60
SENDER_TYPES = ("long_code", "toll_free", "short_code", "alphanumeric")
SENDER_LABEL = {
    "long_code": "an ordinary phone number (long code)",
    "toll_free": "a toll-free number",
    "short_code": "a short code",
    "alphanumeric": "a name (alphanumeric sender ID)",
}
TOLL_FREE_NPA = {"800", "833", "844", "855", "866", "877", "888"}
# Canadian area codes, so +1 numbers that are not Caribbean can be told apart
# from the United States. Anything else on +1 is treated as the US.
CANADA_NPA = {
    "204", "226", "236", "249", "250", "257", "263", "289", "306", "343", "354", "365", "367", "368", "382",
    "387", "403", "416", "418", "428", "431", "437", "438", "450", "460", "468", "474", "506", "514", "519",
    "548", "579", "581", "584", "587", "600", "604", "613", "622", "639", "647", "672", "683", "705", "709",
    "742", "753", "778", "780", "782", "807", "819", "825", "867", "873", "879", "902", "905", "942",
}  # fmt: skip

COUNTRY_CRITERIA = {
    "matrix-reviewed": "The capability matrix for this country checked against current sources by someone who "
    "knows the local rules; entries confirmed or corrected.",
    "regulatory": "Local telecoms, consent and data-protection rules reviewed (regulator, registration, "
    "marketing consent).",
    "sms-rules": "SMS sender types, registration, quiet hours and rate set for this country and tested "
    "through the gateway.",
    "carrier-route": "An SMS route with a primary and a fallback carrier is in place for this country, "
    "and failover has been tested.",
}


def _fact(summary: str, **more: Any) -> dict:
    return {"summary": summary, "verified": False, **more}


def _caribbean(
    code: str,
    name: str,
    dial: str,
    tz: str,
    emergency: str,
    *,
    data_law: str = "",
    numbers: str = "",
    porting: str = "",
    extra: list[str] | None = None,
) -> dict:
    """The common shape for the smaller Caribbean markets, where little is confirmed."""
    restrictions = [
        "Marketing messages need the person's consent; check the local rules before any campaign.",
        *(extra or []),
    ]
    if data_law:
        restrictions.append(data_law)
    return {
        "name": name,
        "region": "Caribbean",
        "dial_code": dial,
        "timezones": [tz],
        "numbers": _fact(
            numbers
            or "Local numbers may be available through the national operators; no number provider for "
            "CommAI is arranged yet."
        ),
        "porting": _fact(
            porting or "Not known whether numbers can be moved between operators. Check with the regulator."
        ),
        "sms": _fact(
            "Messages from an international number usually arrive; whether a name (alphanumeric sender ID) "
            "is shown depends on the operator.",
            sender_id_types=["long_code", "alphanumeric"],
            registration={},
            quiet_hours=None,
            quiet_hours_note="No quiet-hours rule found in research. Check locally.",
            rate_per_minute=DEFAULT_RATE_PER_MINUTE,
        ),
        "whatsapp": _fact(
            "WhatsApp Business Platform appears to be available; not on Meta's restricted list as far as known."
        ),
        "calling": _fact("Needs a number from a voice carrier that serves this country; none arranged yet."),
        "emergency": _fact(f"Emergency numbers reported as {emergency}. CommAI does not place emergency calls."),
        "restrictions": _fact("; ".join(restrictions)),
    }


def _nanp_caribbean(code: str, name: str, area: str, tz: str, emergency: str, **kw: Any) -> dict:
    return _caribbean(code, name, f"+1 {area}", tz, emergency, **kw)


US_ZONES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Anchorage",
    "Pacific/Honolulu",
]  # fmt: skip
CA_ZONES = [
    "America/St_Johns", "America/Halifax", "America/Toronto", "America/Winnipeg", "America/Edmonton",
    "America/Vancouver",
]  # fmt: skip

MATRIX: dict[str, dict] = {
    "TT": _nanp_caribbean(
        "TT", "Trinidad and Tobago", "868", "America/Port_of_Spain", "999 (police), 990 (fire and ambulance)",
        data_law="Data Protection Act 2011 is only partly in force (to confirm).",
    ),
    "JM": _nanp_caribbean(
        "JM", "Jamaica", "876 / 658", "America/Jamaica", "119 (police), 110 (fire and ambulance)",
        data_law="Data Protection Act 2020 (to confirm what applies to messaging).",
        porting="Number portability is reported to exist between mobile operators (to confirm).",
    ),
    "BB": _nanp_caribbean(
        "BB", "Barbados", "246", "America/Barbados", "211 (police), 311 (fire), 511 (ambulance)",
        data_law="Data Protection Act 2019 (to confirm).",
    ),
    "BS": _nanp_caribbean(
        "BS", "The Bahamas", "242", "America/Nassau", "911 or 919",
        data_law="Data Protection (Privacy of Personal Information) Act 2003 (to confirm).",
    ),
    "GY": _caribbean(
        "GY", "Guyana", "+592", "America/Guyana", "911 (police), 912 (fire), 913 (ambulance)",
    ),
    "LC": _nanp_caribbean("LC", "Saint Lucia", "758", "America/St_Lucia", "911 or 999"),
    "VC": _nanp_caribbean("VC", "Saint Vincent and the Grenadines", "784", "America/St_Vincent", "911 or 999"),
    "GD": _nanp_caribbean("GD", "Grenada", "473", "America/Grenada", "911"),
    "AG": _nanp_caribbean("AG", "Antigua and Barbuda", "268", "America/Antigua", "911 or 999"),
    "KN": _nanp_caribbean("KN", "Saint Kitts and Nevis", "869", "America/St_Kitts", "911"),
    "DM": _nanp_caribbean("DM", "Dominica", "767", "America/Dominica", "999"),
    "BZ": _caribbean("BZ", "Belize", "+501", "America/Belize", "911 or 90"),
    "SR": _caribbean(
        "SR", "Suriname", "+597", "America/Paramaribo", "115",
        extra=["Dutch is the official language: check message copy and consent wording."],
    ),
    "CW": _caribbean(
        "CW", "Curaçao", "+599 9", "America/Curacao", "911 (police), 912 (ambulance)",
        extra=["+599 is shared with Bonaire, Sint Eustatius and Saba; numbers are told apart by the next digit."],
    ),
    "AW": _caribbean("AW", "Aruba", "+297", "America/Aruba", "911"),
    "DO": _nanp_caribbean(
        "DO", "Dominican Republic", "809 / 829 / 849", "America/Santo_Domingo", "911",
        data_law="Law 172-13 on personal data (to confirm).",
        extra=["Spanish copy expected."],
        porting="Number portability is reported to exist (to confirm).",
    ),
    "PR": {
        **_nanp_caribbean(
            "PR", "Puerto Rico", "787 / 939", "America/Puerto_Rico", "911",
            extra=["A US territory: US federal messaging rules (TCPA, carrier registration) are expected to apply."],
        ),
        "numbers": _fact("US numbers (local and toll-free) are expected to be available as in the United States."),
        "porting": _fact("US number portability is expected to apply."),
        "sms": _fact(
            "Expected to follow US carrier rules: ordinary numbers need 10DLC registration, toll-free numbers "
            "need verification, and names (alphanumeric sender IDs) are not supported.",
            sender_id_types=["long_code", "toll_free", "short_code"],
            registration={
                "long_code": "10DLC brand and campaign registration",
                "toll_free": "Toll-free verification",
            },
            quiet_hours={"from": "08:00", "until": "20:00"},
            quiet_hours_note="US federal and state rules on telemarketing hours (the narrowest, 08:00 to 20:00, "
            "is used). To confirm for service messages.",
            rate_per_minute=DEFAULT_RATE_PER_MINUTE,
        ),
    },
    "KY": _nanp_caribbean(
        "KY", "Cayman Islands", "345", "America/Cayman", "911",
        data_law="Data Protection Act 2017 (to confirm).",
    ),
    "VG": _nanp_caribbean("VG", "British Virgin Islands", "284", "America/Tortola", "911 or 999"),
    "TC": _nanp_caribbean("TC", "Turks and Caicos Islands", "649", "America/Grand_Turk", "911"),
    "BM": {
        **_nanp_caribbean(
            "BM", "Bermuda", "441", "Atlantic/Bermuda", "911",
            data_law="Personal Information Protection Act 2016 (to confirm).",
        ),
        "region": "Atlantic",
    },
    "HT": _caribbean(
        "HT", "Haiti", "+509", "America/Port-au-Prince", "114 (police), 115 (ambulance), 116 (fire)",
        extra=["French and Haitian Creole copy expected.", "Check service reliability and any payment restrictions."],
    ),
    # Reference markets: better documented, still unverified here.
    "US": {
        "name": "United States",
        "region": "Reference",
        "dial_code": "+1",
        "timezones": US_ZONES,
        "numbers": _fact("Local, mobile-capable and toll-free numbers are widely available from providers."),
        "porting": _fact("Numbers can be ported between providers (local number portability)."),
        "sms": _fact(
            "Messages from ordinary numbers need 10DLC brand and campaign registration (The Campaign Registry); "
            "toll-free numbers need verification; short codes need their own approval. Names (alphanumeric "
            "sender IDs) are not supported.",
            sender_id_types=["long_code", "toll_free", "short_code"],
            registration={
                "long_code": "10DLC brand and campaign registration",
                "toll_free": "Toll-free verification",
            },
            quiet_hours={"from": "08:00", "until": "20:00"},
            quiet_hours_note="Federal (TCPA) and state telemarketing-hours rules; the narrowest common window "
            "is used and checked in every US time zone, because the recipient's own zone is not known from "
            "the number. To confirm for service messages.",
            rate_per_minute=DEFAULT_RATE_PER_MINUTE,
        ),
        "whatsapp": _fact("WhatsApp Business Platform is available."),
        "calling": _fact("Needs a US number from a voice carrier; none arranged yet."),
        "emergency": _fact("911. Calls over the internet carry address duties (Kari's Law, RAY BAUM'S Act)."),
        "restrictions": _fact(
            "TCPA consent rules and the carriers' messaging guidelines (CTIA); opt-out words must be honoured."
        ),
    },
    "CA": {
        "name": "Canada",
        "region": "Reference",
        "dial_code": "+1",
        "timezones": CA_ZONES,
        "numbers": _fact("Local and toll-free numbers are available from providers."),
        "porting": _fact("Numbers can be ported between providers."),
        "sms": _fact(
            "Ordinary numbers and short codes work; toll-free numbers need verification; names (alphanumeric "
            "sender IDs) are not supported.",
            sender_id_types=["long_code", "toll_free", "short_code"],
            registration={"toll_free": "Toll-free verification"},
            quiet_hours={"from": "09:00", "until": "21:30"},
            quiet_hours_note="CRTC telemarketing hours on weekdays (weekend hours are shorter and not yet "
            "modelled). To confirm for service messages.",
            rate_per_minute=DEFAULT_RATE_PER_MINUTE,
        ),
        "whatsapp": _fact("WhatsApp Business Platform is available."),
        "calling": _fact("Needs a Canadian number from a voice carrier; none arranged yet."),
        "emergency": _fact("911."),
        "restrictions": _fact("Canada's Anti-Spam Legislation (CASL): consent and identification in messages."),
    },
    "GB": {
        "name": "United Kingdom",
        "region": "Reference",
        "dial_code": "+44",
        "timezones": ["Europe/London"],
        "numbers": _fact("Geographic, mobile and non-geographic numbers are available from providers."),
        "porting": _fact("Numbers can be ported between providers."),
        "sms": _fact(
            "Ordinary numbers, short codes and names (alphanumeric sender IDs) are all used. A voluntary "
            "sender-ID protection registry exists.",
            sender_id_types=["long_code", "short_code", "alphanumeric"],
            registration={},
            quiet_hours=None,
            quiet_hours_note="No statutory quiet hours for SMS found in research.",
            rate_per_minute=DEFAULT_RATE_PER_MINUTE,
        ),
        "whatsapp": _fact("WhatsApp Business Platform is available."),
        "calling": _fact("Needs a UK number from a voice carrier; none arranged yet."),
        "emergency": _fact("999 and 112."),
        "restrictions": _fact("PECR and UK GDPR: consent for marketing messages, and data-protection duties."),
    },
}  # fmt: skip

for _code, _row in MATRIX.items():
    _row["research"] = {"status": UNVERIFIED, "as_of": RESEARCH_AS_OF}
    golive.declare("country", _code, _row["name"], COUNTRY_CRITERIA, _row)


# ---- numbers -------------------------------------------------------------------------


def country_of(number: str) -> str:
    """ISO code for an E.164 number, telling the US and Canada apart by area code."""
    code = messaging.country_of(number)
    if code == "US/CA":
        digits = re.sub(r"\D", "", number)
        return "CA" if digits[1:4] in CANADA_NPA else "US"
    return code


def sender_kind(sender: str) -> str:
    s = (sender or "").strip()
    if re.search(r"[A-Za-z]", s):
        return "alphanumeric"
    digits = re.sub(r"\D", "", s)
    if len(digits) <= 6:
        return "short_code"
    if digits.startswith("1") and digits[1:4] in TOLL_FREE_NPA:
        return "toll_free"
    return "long_code"


def name_of(code: str) -> str:
    row = MATRIX.get(code)
    return row["name"] if row else (code or "this country")


# ---- rules ---------------------------------------------------------------------------


def sms_rules(conn: psycopg.Connection, code: str) -> dict | None:
    """The researched SMS rules with ExaCarib's adjustments on top, or None if not in the matrix."""
    row = MATRIX.get(code)
    if row is None:
        return None
    rules = {k: row["sms"].get(k) for k in ("sender_id_types", "registration", "quiet_hours", "rate_per_minute")}
    rules["timezones"] = row["timezones"]
    o = conn.execute("SELECT rules FROM country_sms_overrides WHERE country = %s", (code,)).fetchone()
    if o:
        rules.update({k: v for k, v in (o["rules"] or {}).items() if k in rules and k != "timezones"})
    return rules


def check_override(rules: dict) -> dict:
    """Validate an admin's adjustment. Raises ValueError with a plain reason."""
    out: dict = {}
    for k, v in rules.items():
        if k == "rate_per_minute":
            if not isinstance(v, int) or not 0 <= v <= 100_000:
                raise ValueError("rate_per_minute is a whole number from 0.")
        elif k == "sender_id_types":
            if not isinstance(v, list) or not set(v) <= set(SENDER_TYPES):
                raise ValueError(f"sender_id_types are some of {', '.join(SENDER_TYPES)}.")
        elif k == "quiet_hours":
            if v is not None and not (
                isinstance(v, dict)
                and set(v) == {"from", "until"}
                and all(isinstance(t, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t) for t in v.values())
                and v["from"] < v["until"]
            ):
                raise ValueError(
                    'quiet_hours is null or {"from": "HH:MM", "until": "HH:MM"} (the hours sending is allowed).'
                )
        elif k == "registration":
            if not isinstance(v, dict) or not set(v) <= set(SENDER_TYPES):
                raise ValueError("registration maps a sender type to what must be registered.")
        else:
            raise ValueError(f"Unknown rule {k}.")
        out[k] = v
    return out


def _business_initiated(conv: dict, now: dt.datetime) -> bool:
    last = conv.get("last_inbound_at")
    return last is None or now - last > messaging.WINDOW


def _sent_last_minute(conn: psycopg.Connection, customer_id: Any, country: str, now: dt.datetime) -> int:
    rows = conn.execute(
        """SELECT ci.address, count(*) AS n FROM messages m
           JOIN conversations c ON c.id = m.conversation_id
           JOIN contact_identities ci ON ci.id = c.identity_id
           WHERE m.customer_id = %s AND c.channel = 'sms' AND m.direction = 'out'
             AND m.status NOT IN ('blocked', 'failed') AND m.created_at > %s
           GROUP BY ci.address""",
        (customer_id, now - dt.timedelta(minutes=1)),
    ).fetchall()
    return sum(r["n"] for r in rows if country_of(r["address"]) == country)


def check_sms(
    conn: psycopg.Connection,
    conv: dict,
    to: str,
    sender: str,
    *,
    count_limits: bool = True,
    now: dt.datetime | None = None,
) -> str:
    """Raise channels.SendBlocked if an SMS to `to` from `sender` may not go now.
    Returns the destination country. `count_limits` is False when a queued
    message is delivered (it was counted when it was accepted)."""
    now = now or dt.datetime.now(dt.UTC)
    customer_id = conv["customer_id"]
    code = country_of(to)
    rules = sms_rules(conn, code)
    if rules is None:
        raise channels.SendBlocked(
            f"SMS to {code or 'this number'} is not offered: the country is not in CommAI's country list yet."
        )
    name = name_of(code)
    if not golive.enabled(conn, "country", code, customer_id):
        raise channels.SendBlocked(
            f"SMS to {name} is not switched on for this business yet. ExaCarib switches a country on once its "
            "go-live checks pass."
        )
    kind = sender_kind(sender)
    allowed = rules.get("sender_id_types") or []
    if kind not in allowed:
        raise channels.SendBlocked(
            f"{name} does not accept SMS from {SENDER_LABEL[kind]}. Allowed: "
            + ", ".join(SENDER_LABEL[k] for k in allowed if k in SENDER_LABEL)
            + "."
        )
    need = (rules.get("registration") or {}).get(kind)
    if need:
        ok = conn.execute(
            """SELECT 1 FROM sms_sender_registrations WHERE customer_id = %s AND sender = %s AND country = %s
               AND status = 'approved'""",
            (customer_id, sender, code),
        ).fetchone()
        if not ok:
            raise channels.SendBlocked(
                f"SMS to {name} from {sender} needs {need} first. Ask for it under Countries; ExaCarib records "
                "the outcome."
            )
    qh = rules.get("quiet_hours")
    if qh and _business_initiated(conv, now):
        for tz in rules["timezones"]:
            local = now.astimezone(ZoneInfo(tz))
            hhmm = local.strftime("%H:%M")
            if not qh["from"] <= hhmm < qh["until"]:
                raise channels.SendBlocked(
                    f"Quiet hours in {name} ({tz}, now {hhmm}): messages you start may go out between "
                    f"{qh['from']} and {qh['until']} in the recipient's time zone. Replies within 24 hours of "
                    "the person's own message are not affected."
                )
    if count_limits:
        rate = rules.get("rate_per_minute")
        if rate is not None and _sent_last_minute(conn, customer_id, code, now) >= int(rate):
            raise channels.SendBlocked(f"The SMS rate for {name} ({rate} a minute) is reached. Try again in a minute.")
    return code


def matrix_for(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    """The customer's view: what is available where, with the research marked as such."""
    out = []
    for code, row in sorted(MATRIX.items(), key=lambda kv: kv[1]["name"]):
        out.append(
            {
                "code": code,
                "name": row["name"],
                "region": row["region"],
                "dial_code": row["dial_code"],
                "timezones": row["timezones"],
                "available": golive.enabled(conn, "country", code, customer_id),
                "research": row["research"],
                "numbers": row["numbers"],
                "porting": row["porting"],
                "sms": {**row["sms"], **(sms_rules(conn, code) or {})},
                "whatsapp": row["whatsapp"],
                "calling": row["calling"],
                "emergency": row["emergency"],
                "restrictions": row["restrictions"],
            }
        )
    return out
