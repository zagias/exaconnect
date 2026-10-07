"""Partner directory and plain-English ordering (ADR 0011).

A request in plain English becomes a draft order: one to five actions
(a cloud circuit, a site circuit, a partner connection, a bandwidth change,
a change of internet breakout). The draft is drafted by the AI service when
one is configured, else by a rules parser, and checked here either way:
names are resolved against the customer's own sites, circuits, classes and
the directory, and anything missing or wrong is listed. Nothing changes
until a person confirms, and then every action applies or none does.
See docs/ordering-contract.md.
"""

from __future__ import annotations

import ipaddress
import json
import re
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import audit, fabric, internet

ACTIONS = ("cloud_circuit", "site_circuit", "partner_connection", "bandwidth", "internet_mode", "onramp")
MAX_ACTIONS = 5
DEFAULT_CLOUD_MBPS = 50
DEFAULT_SITE_MBPS = 20
MODE_WORDS = {"pop": "through ExaCarib's PoP", "local": "straight out of its own links", "off": "off"}
EXAMPLE = "Connect Kingston to our AWS VPC in us-east-1 at 50 Mbps for 10.100.0.0/16"


class OrderError(ValueError):
    pass


# ---- what the parsers may refer to ---------------------------------------


def context(conn: psycopg.Connection, customer_id: Any) -> dict[str, Any]:
    sites = conn.execute(
        "SELECT id, name, location, kind, internet_mode FROM sites WHERE customer_id = %s ORDER BY overlay_host",
        (customer_id,),
    ).fetchall()
    circuits = conn.execute(
        """SELECT id, name, kind, bandwidth_mbps, price_per_mbps_month FROM circuits
           WHERE customer_id = %s AND deleted_at IS NULL ORDER BY id""",
        (customer_id,),
    ).fetchall()
    classes = [r["name"] for r in conn.execute("SELECT name FROM app_classes WHERE customer_id = %s", (customer_id,))]
    partners = conn.execute(
        "SELECT * FROM partners WHERE listed ORDER BY name",
    ).fetchall()
    return {"sites": sites, "circuits": circuits, "classes": classes, "partners": partners}


def _ai_context(ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "sites": [{"name": s["name"], "location": s["location"]} for s in ctx["sites"] if s["kind"] == "site"],
        "circuits": [
            {"name": c["name"], "kind": c["kind"], "bandwidth_mbps": c["bandwidth_mbps"]} for c in ctx["circuits"]
        ],
        "classes": ctx["classes"],
        "cloud_providers": {k: v["name"] for k, v in fabric.PROVIDERS.items()},
        "partners": [
            {"slug": p["slug"], "name": p["name"], "kind": p["kind"], "regions": list(p["regions"])}
            for p in ctx["partners"]
        ],
    }


# ---- the rules parser ----------------------------------------------------

PROVIDER_WORDS = {
    "aws": ("aws", "amazon"),
    "azure": ("azure", "microsoft"),
    "gcp": ("gcp", "google cloud", "google"),
    "oracle": ("oracle", "oci"),
}
AWS_REGION = re.compile(
    r"\b(?:us|eu|ap|sa|ca|me|af|il|mx)-(?:north|south|east|west|central|northeast|southeast|northwest|southwest)-\d\b"
)
GCP_REGION = re.compile(r"\b(?:us|europe|asia|southamerica|northamerica|australia|me|africa)-[a-z]+\d\b")
AZURE_REGION = re.compile(
    r"\b((?:east|west|central|north|south)(?:\s?central)?\s?us(?:\s?\d)?|brazil\s?south|uk\s?south)\b"
)
CIDR = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}\b")
MBPS = re.compile(r"(\d+(?:\.\d+)?)\s*(g|m)(?:bit|b)?(?:ps|/s|its?)?\b")
VLANS = re.compile(r"\bvlans?\s*(\d{1,4})(?:\s*(?:and|to|,|/)\s*(?:vlan\s*)?(\d{1,4}))?")
NAMED = re.compile(
    r"\b(?:called|named)\s+[\"']?([a-z0-9][a-z0-9 ._-]{0,40}?)[\"']?(?=$|\s+(?:at|on|for|with|from|to)\b)"
)
CHANGE = re.compile(r"\b(increase|raise|upgrade|reduce|lower|decrease|downgrade|change|set|bump|drop|resize)\b")


def _mbps(clause: str) -> int | None:
    m = MBPS.search(clause)
    if not m:
        return None
    v = float(m.group(1)) * (1000 if m.group(2) == "g" else 1)
    return int(round(v))


def _has(clause: str, word: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])", clause) is not None


def _sites_in(clause: str, ctx: dict) -> list[str]:
    """Site names mentioned by name or location, in the order they appear."""
    found: list[tuple[int, str]] = []
    for s in ctx["sites"]:
        if s["kind"] != "site":
            continue
        for word in (s["name"], s["location"]):
            if not word:
                continue
            m = re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])", clause)
            if m:
                found.append((m.start(), s["name"]))
                break
    out: list[str] = []
    for _, name in sorted(found):
        if name not in out:
            out.append(name)
    return out


def _circuit_in(clause: str, ctx: dict) -> str | None:
    best = None
    for c in ctx["circuits"]:
        if c["name"].lower() in clause and (best is None or len(c["name"]) > len(best)):
            best = c["name"]
    return best


def _provider_in(clause: str) -> str | None:
    for p, words in PROVIDER_WORDS.items():
        if any(_has(clause, w) for w in words):
            return p
    return None


def _partner_in(clause: str, ctx: dict) -> dict | None:
    for p in ctx["partners"]:
        if p["kind"] == "service" and (_has(clause, p["name"]) or _has(clause, p["slug"])):
            return p
    return None


def _region(clause: str, provider: str) -> str:
    if provider == "aws":
        m = AWS_REGION.search(clause)
    elif provider == "gcp":
        m = GCP_REGION.search(clause)
    elif provider == "azure":
        m = AZURE_REGION.search(clause)
        return m.group(1).replace(" ", "") if m else ""
    else:
        m = re.search(r"\bregion\s+([a-z0-9-]{2,30})", clause)
        return m.group(1) if m else ""
    return m.group(0) if m else ""


def _class_in(clause: str, ctx: dict) -> str | None:
    for c in ctx["classes"]:
        if re.search(r"\b(?:as|in|class|for)\s+" + re.escape(c.lower()) + r"\b", clause):
            return c
    return None


def _all_sites(clause: str) -> bool:
    return bool(re.search(r"\b(all|every|each) (?:of )?(?:our |my )?(sites|offices|branches)\b", clause))


def _named(clause: str) -> dict[str, str]:
    m = NAMED.search(clause)
    return {"name": m.group(1).strip()} if m else {}


def _clauses(text: str) -> list[str]:
    parts = re.split(
        r";|\n|\.\s+|\bthen\b|\balso\b|,\s*and\s+(?=(?:connect|join|link|send|turn|increase|raise|set|change|reduce|lower|upgrade|add|order|get)\b)",
        text.lower(),
    )
    return [p.strip(" .,") for p in parts if p and p.strip(" .,")]


def parse_rules(text: str, ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """Best-effort actions from plain English, without an AI service."""
    actions: list[dict[str, Any]] = []
    for clause in _clauses(text):
        sites = _sites_in(clause, ctx)
        mbps = _mbps(clause)
        if re.search(r"\binternet\b|\bbreak ?out\b", clause):
            mode = None
            if re.search(
                r"straight out|\blocal(ly)?\b|direct(ly)?|own (?:links|carriers?|lines?)|break ?out at", clause
            ):
                mode = "local"
            elif re.search(r"through (?:the |exacarib'?s? )?pop|via (?:the )?pop|backhaul|through exacarib", clause):
                mode = "pop"
            elif re.search(
                r"\b(turn|switch) off\b|\bno internet\b|\bblock (?:the )?internet\b|\bdisable\b|\bcut off\b", clause
            ):
                mode = "off"
            if mode:
                for site in sites or [None]:
                    actions.append({"action": "internet_mode", "site": site, "mode": mode})
                continue
        circuit = _circuit_in(clause, ctx)
        if circuit and mbps and CHANGE.search(clause):
            actions.append({"action": "bandwidth", "circuit": circuit, "bandwidth_mbps": mbps})
            continue
        partner = _partner_in(clause, ctx)
        if partner:
            actions.append(
                {
                    "action": "partner_connection",
                    "partner": partner["slug"],
                    "site": sites[0] if sites and not _all_sites(clause) else None,
                    "bandwidth_mbps": mbps,
                }
            )
            continue
        provider = _provider_in(clause)
        if provider:
            actions.append(
                {
                    "action": "cloud_circuit",
                    "provider": provider,
                    "region": _region(clause, provider),
                    "site": sites[0] if sites and not _all_sites(clause) else None,
                    "bandwidth_mbps": mbps,
                    "cloud_prefixes": CIDR.findall(clause),
                    "class_name": _class_in(clause, ctx),
                    **_named(clause),
                }
            )
            continue
        if len(sites) >= 2:
            vlans = VLANS.search(clause)
            a_vlan = int(vlans.group(1)) if vlans else None
            b_vlan = int(vlans.group(2)) if vlans and vlans.group(2) else a_vlan
            actions.append(
                {
                    "action": "site_circuit",
                    "a_site": sites[0],
                    "b_site": sites[1],
                    "a_vlan": a_vlan,
                    "b_vlan": b_vlan,
                    "bandwidth_mbps": mbps,
                    **_named(clause),
                }
            )
    return actions[:MAX_ACTIONS]


# ---- the AI parser -------------------------------------------------------

AI_SYSTEM = """You turn a customer's request for ExaConnect, ExaCarib's connectivity platform, into JSON actions.
Reply with one JSON object and nothing else: {"actions": [...]} with at most 5 actions, each one of:
{"action":"cloud_circuit","provider":"aws|azure|gcp|oracle","region":"...",
 "site":"<site name, or null for all sites>","bandwidth_mbps":int|null,"cloud_prefixes":["10.0.0.0/16"],
 "class_name":"<class or null>","name":"<short name or null>"}
{"action":"site_circuit","a_site":"<site name>","b_site":"<site name>","a_vlan":int|null,"b_vlan":int|null,
 "bandwidth_mbps":int|null,"name":null}
{"action":"partner_connection","partner":"<partner slug>","site":"<site name or null>","bandwidth_mbps":int|null}
{"action":"bandwidth","circuit":"<existing circuit name>","bandwidth_mbps":int}
{"action":"internet_mode","site":"<site name>","mode":"pop|local|off"}
Use only names from the context: map places to the site whose location matches. internet_mode: "pop" sends a site's
internet through ExaCarib's PoP, "local" straight out of its own carrier links, "off" none. Never invent values the
customer did not give: use null. If the request is not something these actions can do, reply {"actions": []}.
Never include passwords, keys or secrets."""


def parse_ai(text: str, ctx: dict[str, Any], settings: Any) -> list[dict[str, Any]]:
    """Actions from the AI service. Raises ask.AskError when it can't answer."""
    from .ai import ask as ask_mod

    user = "Context (JSON):\n" + json.dumps(_ai_context(ctx), separators=(",", ":")) + "\n\nRequest: " + text
    out = ask_mod.chat(
        AI_SYSTEM,
        user,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        max_tokens=600,
        temperature=0,
        timeout_s=45,
    )
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise ask_mod.AskError("The AI service did not draft an order.")
    try:
        doc = json.loads(m.group(0))
    except json.JSONDecodeError:
        raise ask_mod.AskError("The AI service drafted an order in an unexpected shape.") from None
    acts = doc.get("actions") if isinstance(doc, dict) else None
    if not isinstance(acts, list):
        raise ask_mod.AskError("The AI service drafted an order in an unexpected shape.")
    return [a for a in acts if isinstance(a, dict)][:MAX_ACTIONS]


# ---- review: normalise, resolve names, list what's missing -----------------


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _site(ctx: dict, name: Any, problems: list[str]) -> dict | None:
    if not name:
        return None
    key = str(name).strip().lower()
    for s in ctx["sites"]:
        if s["kind"] == "site" and key in (s["name"].lower(), (s["location"] or "").lower()):
            return s
    problems.append(f"There is no site called {name}.")
    return None


def cidr_list(values: Any, problems: list[str]) -> list[str]:
    out = []
    for v in values or []:
        try:
            net = ipaddress.ip_network(str(v).strip(), strict=False)
        except ValueError:
            problems.append(f"{v} is not a subnet, like 10.100.0.0/16.")
            continue
        if net.version == 4:
            out.append(str(net))
    return sorted(set(out))


def _bandwidth(v: Any, default: int, problems: list[str], notes: list[str]) -> int:
    mbps = _int(v)
    if mbps is None:
        notes.append(f"{default} Mbps, as no speed was given")
        return default
    if not 1 <= mbps <= 1000:
        problems.append("Bandwidth is 1 to 1,000 Mbps on today's PoPs.")
    return mbps


def _unique_name(ctx: dict[str, Any], name: str, problems: list[str]) -> None:
    """A draft is checked against the circuits as they are now, so one made on
    the Fabric page since the draft was written is not created twice."""
    if any(c["name"].lower() == name.lower() for c in ctx["circuits"]):
        problems.append(f"You already have a circuit called {name}. Change that one, or give this one another name.")


def review(conn: psycopg.Connection, customer_id: Any, raw: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalised actions with a summary, the inputs still needed, problems
    and the change to the monthly charge."""
    ctx = context(conn, customer_id)
    partners = {p["slug"]: p for p in ctx["partners"]}
    actions: list[dict[str, Any]] = []
    summary: list[str] = []
    needs: list[dict[str, Any]] = []
    problems: list[str] = []
    estimate = Decimal(0)
    default_price = Decimal("2.0")
    if not raw:
        problems.append(f'Nothing here that can be ordered yet. Try, for example: "{EXAMPLE}".')
    if len(raw) > MAX_ACTIONS:
        problems.append(f"At most {MAX_ACTIONS} changes in one order.")
    for i, a in enumerate(raw[:MAX_ACTIONS]):
        kind = a.get("action")
        notes: list[str] = []
        if kind == "partner_connection":
            p = partners.get(str(a.get("partner") or "").lower()) or next(
                (x for x in ctx["partners"] if x["name"].lower() == str(a.get("partner") or "").lower()), None
            )
            if p is None:
                problems.append(f"{a.get('partner') or 'That partner'} is not in the partner directory.")
                continue
            if p["kind"] == "cloud":
                # A cloud partner is a cloud circuit to that provider.
                a = {**a, "action": "cloud_circuit", "provider": p["provider"], "partner": p["slug"]}
                kind = "cloud_circuit"
            else:
                site = _site(ctx, a.get("site"), problems)
                mbps = _bandwidth(a.get("bandwidth_mbps"), 10, problems, notes)
                price = Decimal(str(p["price_per_mbps_month"]))
                actions.append(
                    {
                        "action": "partner_connection",
                        "partner": p["slug"],
                        "site": site["name"] if site else a.get("site"),
                        "bandwidth_mbps": mbps,
                    }
                )
                summary.append(
                    f"Connect {site['name'] if site else 'all sites'} to {p['name']} at {mbps} Mbps"
                    + (f" ({'; '.join(notes)})" if notes else "")
                    + f". {p['name']} sets up its side, then ExaCarib completes the connection."
                )
                estimate += mbps * price
                continue
        if kind == "cloud_circuit":
            provider = str(a.get("provider") or "").lower()
            if provider not in fabric.PROVIDERS:
                problems.append("Say which cloud: AWS, Azure, Google Cloud or Oracle.")
                continue
            label = fabric.PROVIDERS[provider]["name"]
            site = _site(ctx, a.get("site"), problems)
            region = str(a.get("region") or "").strip()[:40]
            mbps = _bandwidth(a.get("bandwidth_mbps"), DEFAULT_CLOUD_MBPS, problems, notes)
            prefixes = cidr_list(a.get("cloud_prefixes"), problems)
            cls = a.get("class_name")
            if cls and cls not in ctx["classes"]:
                problems.append(f"There is no class called {cls}.")
            p = partners.get(str(a.get("partner") or ""))
            name = str(a.get("name") or "").strip()[:80] or (f"{label} {region}".strip() if region else label)
            _unique_name(ctx, name, problems)
            actions.append(
                {
                    "action": "cloud_circuit",
                    "name": name,
                    "provider": provider,
                    "region": region,
                    "site": site["name"] if site else a.get("site"),
                    "bandwidth_mbps": mbps,
                    "cloud_prefixes": prefixes,
                    "class_name": cls or None,
                    **({"partner": p["slug"]} if p else {}),
                }
            )
            summary.append(
                f"New circuit from {site['name'] if site else 'all sites'} to {label}"
                + (f" ({region})" if region else "")
                + f", {mbps} Mbps"
                + (f", for {', '.join(prefixes)}" if prefixes else "")
                + (f", as {cls} traffic" if cls else "")
                + (f" ({'; '.join(notes)})" if notes else "")
                + "."
            )
            where = fabric.PROVIDERS[provider]["where"]
            needs += [
                {
                    "action": i,
                    "field": "peer_address",
                    "label": f"The gateway's public address ({where})",
                    "secret": False,
                },
                {
                    "action": i,
                    "field": "peer_asn",
                    "label": "The gateway's ASN",
                    "secret": False,
                    "default": fabric.PROVIDERS[provider]["asn"],
                },
                {"action": i, "field": "psk", "label": f"Pre-shared key from {label}", "secret": True},
                {
                    "action": i,
                    "field": "inside_cidr",
                    "label": "Tunnel inside /30, if the console sets one",
                    "secret": False,
                    "optional": True,
                },
            ]
            estimate += mbps * (Decimal(str(p["price_per_mbps_month"])) if p else default_price)
        elif kind == "site_circuit":
            a_site = _site(ctx, a.get("a_site"), problems)
            b_site = _site(ctx, a.get("b_site"), problems)
            if a.get("a_site") is None or a.get("b_site") is None:
                problems.append("Say which two sites to join.")
            elif a_site and b_site and a_site["id"] == b_site["id"]:
                problems.append("A site circuit joins two different sites.")
            a_vlan, b_vlan = _int(a.get("a_vlan")), _int(a.get("b_vlan"))
            if a_vlan is None:
                problems.append("Say which VLAN to carry, like VLAN 100.")
            b_vlan = b_vlan or a_vlan
            for v in (a_vlan, b_vlan):
                if v is not None and not 1 <= v <= 4094:
                    problems.append("VLAN IDs are 1 to 4094.")
            mbps = _bandwidth(a.get("bandwidth_mbps"), DEFAULT_SITE_MBPS, problems, notes)
            an = a_site["name"] if a_site else a.get("a_site")
            bn = b_site["name"] if b_site else a.get("b_site")
            name = str(a.get("name") or "").strip()[:80] or f"{an} to {bn}"
            _unique_name(ctx, name, problems)
            actions.append(
                {
                    "action": "site_circuit",
                    "name": name,
                    "a_site": an,
                    "b_site": bn,
                    "a_vlan": a_vlan,
                    "b_vlan": b_vlan,
                    "bandwidth_mbps": mbps,
                }
            )
            vl = f"VLAN {a_vlan}" + (f" at {an} and VLAN {b_vlan} at {bn}" if b_vlan != a_vlan else " at both ends")
            summary.append(
                f"New layer 2 circuit joining {an} and {bn}, {vl}, {mbps} Mbps"
                + (f" ({'; '.join(notes)})" if notes else "")
                + "."
            )
            estimate += mbps * default_price
        elif kind == "bandwidth":
            want = str(a.get("circuit") or "").strip().lower()
            c = next((x for x in ctx["circuits"] if x["name"].lower() == want), None)
            if c is None:
                matches = [x for x in ctx["circuits"] if want and want in x["name"].lower()]
                c = matches[0] if len(matches) == 1 else None
            if c is None:
                problems.append(f"There is no circuit called {a.get('circuit') or 'that'}.")
                continue
            mbps = _int(a.get("bandwidth_mbps"))
            if mbps is None or not 1 <= mbps <= 1000:
                problems.append("Say the new speed, 1 to 1,000 Mbps.")
                continue
            if mbps == c["bandwidth_mbps"]:
                problems.append(f"{c['name']} already runs at {mbps} Mbps.")
            actions.append({"action": "bandwidth", "circuit": c["name"], "bandwidth_mbps": mbps})
            summary.append(
                f"Change {c['name']} from {c['bandwidth_mbps']} to {mbps} Mbps, billed by the hour from now."
            )
            estimate += (mbps - c["bandwidth_mbps"]) * Decimal(str(c["price_per_mbps_month"]))
        elif kind == "internet_mode":
            site = _site(ctx, a.get("site"), problems)
            mode = a.get("mode")
            if a.get("site") is None:
                problems.append("Say which site's internet to change.")
            if mode not in internet.MODES:
                problems.append("Internet goes through the PoP, straight out at the site, or off.")
                continue
            if site and site["internet_mode"] == mode:
                problems.append(f"{site['name']} already sends its internet {MODE_WORDS[mode]}.")
            actions.append({"action": "internet_mode", "site": site["name"] if site else a.get("site"), "mode": mode})
            if site:
                summary.append(
                    f"Send {site['name']}'s internet {MODE_WORDS[mode]} (now {MODE_WORDS[site['internet_mode']]})."
                )
        elif kind == "onramp":
            # Dedicated cloud on-ramps through the provider adapters (ADR 0026).
            from .integrations import onramps
            from .integrations.adapters.clouds import OnrampError

            try:
                r = onramps.review(conn, customer_id, a)
            except OnrampError as e:
                problems.append(str(e))
                continue
            ad = onramps.adapter(r["provider"])
            site_name = next((s["name"] for s in ctx["sites"] if s["id"] == r["site_id"]), None)
            actions.append(
                {"action": "onramp", "provider": r["provider"], "name": r["name"], "site": site_name, **r["detail"]}
            )
            summary.append(
                f"Order {ad.name} at {r['detail']['bandwidth_mbps']} Mbps"
                + (f" for {site_name}" if site_name else "")
                + (" (simulated until the provider is connected)" if not ad.live() else "")
                + "."
            )
            estimate += r["detail"]["bandwidth_mbps"] * onramps.PRICE_PER_MBPS_MONTH
        elif kind is not None:
            problems.append(f"Can't order '{kind}' here.")
    return {
        "actions": actions,
        "summary": summary,
        "needs": needs,
        "problems": list(dict.fromkeys(problems)),
        "monthly_estimate": float(estimate.quantize(Decimal("0.01"))),
    }


# ---- orders --------------------------------------------------------------


def create(conn: psycopg.Connection, customer_id: Any, raw: list[dict], engine: str, text: str, actor: str) -> int:
    r = review(conn, customer_id, raw)
    oid = conn.execute(
        """INSERT INTO orders (customer_id, engine, text, actions, created_by)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (customer_id, engine, text[:500], Jsonb(r["actions"]), actor),
    ).fetchone()["id"]
    audit.record(conn, actor, "order.draft", f"order {oid}", customer_id, {"engine": engine, "actions": r["actions"]})
    return oid


def view(conn: psycopg.Connection, order: dict) -> dict[str, Any]:
    out = {
        k: order[k]
        for k in (
            "id",
            "status",
            "engine",
            "text",
            "actions",
            "results",
            "created_by",
            "created_at",
            "confirmed_by",
            "confirmed_at",
        )
    }
    if order["status"] == "draft":
        r = review(conn, order["customer_id"], order["actions"])
        out.update({k: r[k] for k in ("summary", "needs", "problems", "monthly_estimate")})
    else:
        out.update(
            {
                "summary": [x.get("message", "") for x in order["results"]],
                "needs": [],
                "problems": [],
                "monthly_estimate": None,
            }
        )
    return out


def _site_id(conn: psycopg.Connection, customer_id: Any, name: str | None) -> str | None:
    if name is None:
        return None
    row = conn.execute("SELECT id FROM sites WHERE customer_id = %s AND name = %s", (customer_id, name)).fetchone()
    if row is None:
        raise OrderError(f"There is no site called {name}.")
    return str(row["id"])


def confirm(conn: psycopg.Connection, order: dict, inputs: list[dict[str, Any]], actor: str) -> None:
    """Apply every action, or none (the caller's transaction rolls back on OrderError)."""
    cid = order["customer_id"]
    if order["status"] != "draft":
        raise OrderError("This order is no longer a draft.")
    r = review(conn, cid, order["actions"])
    if r["problems"]:
        raise OrderError(r["problems"][0])
    results: list[dict[str, Any]] = []
    pending = False
    partners = {p["slug"]: p for p in conn.execute("SELECT * FROM partners").fetchall()}
    for i, a in enumerate(r["actions"]):
        given = inputs[i] if i < len(inputs) and isinstance(inputs[i], dict) else {}
        try:
            if a["action"] == "cloud_circuit":
                circuit_id = fabric.create(
                    conn,
                    cid,
                    {
                        "name": a["name"],
                        "kind": "cloud",
                        "provider": a["provider"],
                        "region": a["region"],
                        "a_site_id": _site_id(conn, cid, a["site"]),
                        "bandwidth_mbps": a["bandwidth_mbps"],
                        "cloud_prefixes": a["cloud_prefixes"],
                        "class_name": a["class_name"],
                        "peer_address": given.get("peer_address"),
                        "peer_asn": _int(given.get("peer_asn")) or fabric.PROVIDERS[a["provider"]]["asn"],
                        "psk": given.get("psk") or None,
                        "inside_cidr": given.get("inside_cidr") or None,
                    },
                    actor,
                )
                p = partners.get(a.get("partner", ""))
                if p is not None:
                    conn.execute(
                        "UPDATE circuits SET partner_id = %s, price_per_mbps_month = %s WHERE id = %s",
                        (p["id"], p["price_per_mbps_month"], circuit_id),
                    )
                results.append(
                    {
                        "action": i,
                        "ok": True,
                        "circuit_id": circuit_id,
                        "message": f"Created circuit {a['name']}. It comes up once the cloud side is set.",
                    }
                )
            elif a["action"] == "site_circuit":
                circuit_id = fabric.create(
                    conn,
                    cid,
                    {
                        "name": a["name"],
                        "kind": "site",
                        "a_site_id": _site_id(conn, cid, a["a_site"]),
                        "b_site_id": _site_id(conn, cid, a["b_site"]),
                        "a_vlan": a["a_vlan"],
                        "b_vlan": a["b_vlan"],
                        "bandwidth_mbps": a["bandwidth_mbps"],
                    },
                    actor,
                )
                results.append(
                    {"action": i, "ok": True, "circuit_id": circuit_id, "message": f"Created circuit {a['name']}."}
                )
            elif a["action"] == "partner_connection":
                p = partners[a["partner"]]
                pending = True
                results.append(
                    {
                        "action": i,
                        "ok": True,
                        "pending": True,
                        "message": f"Asked {p['name']} to connect; ExaCarib completes it once they're ready.",
                    }
                )
            elif a["action"] == "bandwidth":
                c = conn.execute(
                    "SELECT * FROM circuits WHERE customer_id = %s AND name = %s AND deleted_at IS NULL"
                    " ORDER BY id LIMIT 1 FOR UPDATE",
                    (cid, a["circuit"]),
                ).fetchone()
                if c is None:
                    raise OrderError(f"There is no circuit called {a['circuit']}.")
                fabric.update(conn, c, {"bandwidth_mbps": a["bandwidth_mbps"]}, actor)
                results.append(
                    {
                        "action": i,
                        "ok": True,
                        "circuit_id": c["id"],
                        "message": f"{a['circuit']} is now {a['bandwidth_mbps']} Mbps.",
                    }
                )
            elif a["action"] == "onramp":
                from .integrations import onramps
                from .integrations.adapters.clouds import OnrampError

                try:
                    o = onramps.create(conn, cid, a, actor, order_id=order["id"])
                except OnrampError as e:
                    raise OrderError(f"Change {i + 1}: {e}") from None
                results.append(
                    {
                        "action": i,
                        "ok": True,
                        "onramp_id": o["id"],
                        "message": f"Ordered {o['name']} ({o['status']}). {o['detail'].get('next_step', '')}".strip(),
                    }
                )
            elif a["action"] == "internet_mode":
                internet.set_mode(conn, cid, _site_id(conn, cid, a["site"]), a["mode"], actor)
                results.append(
                    {"action": i, "ok": True, "message": f"{a['site']}'s internet now goes {MODE_WORDS[a['mode']]}."}
                )
        except (fabric.CircuitError, internet.InternetError) as e:
            raise OrderError(f"Change {i + 1}: {e}") from None
    conn.execute(
        """UPDATE orders SET status = %s, results = %s, confirmed_by = %s, confirmed_at = now() WHERE id = %s""",
        ("pending_partner" if pending else "done", Jsonb(results), actor, order["id"]),
    )
    audit.record(conn, actor, "order.confirm", f"order {order['id']}", cid, {"results": results})


def cancel(conn: psycopg.Connection, order: dict, actor: str) -> None:
    if order["status"] not in ("draft", "pending_partner"):
        raise OrderError("Only a draft or an order waiting for a partner can be cancelled.")
    conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s", (order["id"],))
    audit.record(conn, actor, "order.cancel", f"order {order['id']}", order["customer_id"])


def complete(conn: psycopg.Connection, order: dict, details: dict[str, Any], actor: str) -> None:
    """ExaCarib enters a service partner's gateway: the pending connections become circuits."""
    if order["status"] != "pending_partner":
        raise OrderError("This order is not waiting for a partner.")
    cid = order["customer_id"]
    results = list(order["results"])
    partners = {p["slug"]: p for p in conn.execute("SELECT * FROM partners").fetchall()}
    for i, a in enumerate(order["actions"]):
        if a.get("action") != "partner_connection":
            continue
        p = partners.get(a["partner"])
        if p is None:
            raise OrderError("The partner is no longer in the directory.")
        try:
            circuit_id = fabric.create(
                conn,
                cid,
                {
                    "name": p["name"],
                    "kind": "cloud",
                    "provider": "other",
                    "region": "",
                    "a_site_id": _site_id(conn, cid, a.get("site")),
                    "bandwidth_mbps": a["bandwidth_mbps"],
                    "cloud_prefixes": details.get("prefixes") or [str(x) for x in p["prefixes"]],
                    "peer_address": details.get("peer_address"),
                    "peer_asn": _int(details.get("peer_asn")),
                    "psk": details.get("psk") or None,
                    "inside_cidr": details.get("inside_cidr") or None,
                },
                actor,
            )
        except fabric.CircuitError as e:
            raise OrderError(str(e)) from None
        conn.execute(
            "UPDATE circuits SET partner_id = %s, price_per_mbps_month = %s WHERE id = %s",
            (p["id"], p["price_per_mbps_month"], circuit_id),
        )
        results = [x for x in results if x.get("action") != i] + [
            {"action": i, "ok": True, "circuit_id": circuit_id, "message": f"Connected to {p['name']}."}
        ]
    conn.execute(
        "UPDATE orders SET status = 'done', results = %s WHERE id = %s",
        (Jsonb(sorted(results, key=lambda x: x.get("action", 0))), order["id"]),
    )
    audit.record(conn, actor, "order.complete", f"order {order['id']}", cid)
