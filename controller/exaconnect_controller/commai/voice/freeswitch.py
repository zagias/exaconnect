"""FreeSWITCH configuration from the database (ADR 0021).

`gather()` reads one business's phone system; `render()` turns it into XML
files. Rendering is a pure function of that data: the same data always gives
byte-for-byte the same files (sorted, no timestamps), so a change shows up as
a clean diff and tests can pin the output.

Files per business, under <tenant>/ (the tenant is the business's SIP domain
prefix):

  directory.xml   the SIP domain and one user per extension (a1-hash, never
                  the password), with forwarding, do not disturb and voicemail
  dialplan.xml    the business's context: extensions, ring groups, queues,
                  menus with business hours, the AI agent with its fallback,
                  emergency numbers and blocked destinations
  public.xml      inbound numbers into that context
  ivr.xml         the voice menus
  queues.xml, agents.xml, tiers.xml   mod_callcenter fragments

deploy/freeswitch/ includes these from /exacarib/freeswitch/*/. Not live:
there is no SIP provider account yet, so the gateway ("exacarib_sip": the
provider, or Kamailio with EXA_VOICE_EDGE=kamailio) has nowhere to send
outside calls. Calls between extensions, menus and queues work without it.
Every outside call except an emergency call is authorised by the controller
first (voice/pbx.py) and refused when the controller can't be reached. The AI
agent is a transfer with a fallback, so basic calling never depends on the AI
services.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import psycopg
from psycopg.types.json import Jsonb

from .. import jobs
from . import emergency, pbx
from .billing import EMERGENCY, fraud_limits
from .common import domain as tenant_domain
from .common import now

WDAY = {"sun": 1, "mon": 2, "tue": 3, "wed": 4, "thu": 5, "fri": 6, "sat": 7}  # FreeSWITCH numbering
GATEWAY = "exacarib_sip"
AI_GATEWAY = "exacarib_ai"
PLUS = "^\\+?"  # an optional leading + in a dial plan expression


def a(v: Any) -> str:
    return quoteattr(str(v))


def gather(conn: psycopg.Connection, cid: Any) -> dict:
    """Everything the renderer needs, as plain sorted data."""

    def rows(sql, *args):
        return conn.execute(sql, (cid, *args)).fetchall()

    dom = tenant_domain(cid)
    users = rows(
        """SELECT v.id::text AS id, v.name, v.email, v.extension, v.sip_password, v.forward_to, v.dnd,
                  v.voicemail_to_email, v.voicemail_greeting, coalesce(s.name, '') AS site
           FROM voice_users v LEFT JOIN voice_sites s ON s.id = v.site_id
           WHERE v.customer_id = %s AND v.status = 'active' ORDER BY v.extension"""
    )
    tz = conn.execute("SELECT timezone FROM commai_settings WHERE customer_id = %s", (cid,)).fetchone()
    return {
        "customer_id": str(cid),
        "domain": dom,
        "tenant": dom.split(".")[0],
        "timezone": tz["timezone"] if tz else "America/Port_of_Spain",
        "users": [
            {
                **{k: v for k, v in u.items() if k != "sip_password"},
                "a1": hashlib.md5(f"{u['extension']}:{dom}:{u['sip_password']}".encode()).hexdigest(),
            }  # noqa: S324 - SIP digest needs MD5
            for u in users
        ],
        "numbers": rows(
            """SELECT id::text AS id, e164, target_type, target_id::text AS target_id FROM voice_numbers
               WHERE customer_id = %s AND status = 'active' ORDER BY e164"""
        ),
        "ring_groups": rows(
            """SELECT id::text AS id, name, extension, strategy, members::text[] AS members, ring_seconds
               FROM voice_ring_groups WHERE customer_id = %s ORDER BY extension"""
        ),
        "queues": rows(
            """SELECT id::text AS id, name, extension, strategy, members::text[] AS members, max_wait_s
               FROM voice_queues WHERE customer_id = %s ORDER BY extension"""
        ),
        "hours": rows(
            "SELECT id::text AS id, name, timezone, schedule, holidays FROM voice_hours"
            " WHERE customer_id = %s ORDER BY name"
        ),
        "menus": rows(
            """SELECT id::text AS id, name, extension, greeting, options, hours_id::text AS hours_id, closed_target
               FROM voice_menus WHERE customer_id = %s ORDER BY extension"""
        ),
        "ai_rules": rows(
            """SELECT id::text AS id, name, condition, hours_id::text AS hours_id, number_id::text AS number_id,
                      fallback FROM voice_ai_rules WHERE customer_id = %s AND enabled ORDER BY name"""
        ),
        "blocked_prefixes": sorted(fraud_limits(conn, cid)["blocked_prefixes"]),
        "emergency_numbers": sorted(emergency.business_numbers(conn, cid)),  # each island's (ADR 0027)
        # The controller check before outside calls (voice/pbx.py) and, for
        # emergency calls, which never wait for it, every carrier switched on.
        "controller_url": controller_url(),
        "emergency_sets": pbx.emergency_sets(conn, cid),
    }


def controller_url() -> str:
    """Where FreeSWITCH reaches the controller: EXA_PBX_CONTROLLER_URL, else the compose service."""
    return (os.environ.get("EXA_PBX_CONTROLLER_URL") or "http://controller:8000").rstrip("/")


def _ext_of(data: dict, kind: str, target_id: str | None) -> str | None:
    if kind == "ai":
        return "exa-ai"
    coll = {"user": "users", "ring_group": "ring_groups", "queue": "queues", "menu": "menus"}.get(kind)
    if not coll:
        return None
    for item in data[coll]:
        if item["id"] == target_id:
            return item["extension"]
    return None


def _dial(user_ext: str, dom: str) -> str:
    return f"user/{user_ext}@{dom}"


def _directory(data: dict) -> str:
    dom, ctx = data["domain"], data["tenant"]
    out = [
        "<include>",
        f"  <domain name={a(dom)}>",
        "    <params>",
        '      <param name="dial-string" value="{^^:sip_invite_domain=${dialed_domain}:'
        'presence_id=${dialed_user}@${dialed_domain}}${sofia_contact(*/${dialed_user}@${dialed_domain})}"/>',
        "    </params>",
        "    <variables>",
        f'      <variable name="exa_customer" value={a(data["customer_id"])}/>',
        f'      <variable name="user_context" value={a(ctx)}/>',
        "    </variables>",
        "    <groups>",
        '      <group name="default">',
        "        <users>",
    ]
    for u in data["users"]:
        out += [
            f"          <user id={a(u['extension'])}>",
            "            <params>",
            f'              <param name="a1-hash" value={a(u["a1"])}/>',
        ]
        if u["voicemail_to_email"] and u["email"]:
            out += [
                f'              <param name="vm-mailto" value={a(u["email"])}/>',
                '              <param name="vm-email-all-messages" value="true"/>',
                '              <param name="vm-attach-file" value="true"/>',
            ]
        out += [
            "            </params>",
            "            <variables>",
            f'              <variable name="effective_caller_id_name" value={a(u["name"])}/>',
            f'              <variable name="effective_caller_id_number" value={a(u["extension"])}/>',
            f'              <variable name="user_context" value={a(ctx)}/>',
            f'              <variable name="exa_site" value={a(u["site"])}/>',
        ]
        if u["voicemail_greeting"]:
            out.append(f'              <variable name="exa_vm_greeting" value={a(u["voicemail_greeting"])}/>')
        out += ["            </variables>", "          </user>"]
    out += ["        </users>", "      </group>", "    </groups>", "  </domain>", "</include>", ""]
    return "\n".join(out)


def _hours_by_id(data: dict) -> dict:
    return {h["id"]: h for h in data["hours"]}


def _open_conditions(hours: dict) -> list[str]:
    """One <condition> set per opening span (FreeSWITCH matches any of the extensions)."""
    spans = []
    for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        for start, end in (hours["schedule"] or {}).get(day, []):
            spans.append((WDAY[day], start, end))
    return [f'wday="{w}" time-of-day="{s}-{e}"' for w, s, e in spans]


def _hours_block(name: str, ext_expr: str, hours: dict, open_to: str, closed_to: str, ctx: str) -> list[str]:
    """Route by business hours: holidays closed, then each opening span, else closed."""
    out = [
        f'    <extension name={a(name + "-tz")} continue="true">',
        f'      <condition field="destination_number" expression={a(ext_expr)}>',
        f'        <action application="set" data={a("timezone=" + hours["timezone"])} inline="true"/>',
        "      </condition>",
        "    </extension>",
    ]
    if hours["holidays"]:
        days = "|".join(hours["holidays"])
        out += [
            f"    <extension name={a(name + '-holiday')}>",
            f'      <condition field="destination_number" expression={a(ext_expr)}/>',
            f'      <condition field="${{strftime_tz(${{timezone}} %Y-%m-%d)}}" expression={a("^(" + days + ")$")}>',
            f'        <action application="transfer" data={a(closed_to + " XML " + ctx)}/>',
            "      </condition>",
            "    </extension>",
        ]
    for i, cond in enumerate(_open_conditions(hours)):
        out += [
            f"    <extension name={a(f'{name}-open-{i}')}>",
            f'      <condition field="destination_number" expression={a(ext_expr)}/>',
            f"      <condition {cond}>",
            f'        <action application="transfer" data={a(open_to + " XML " + ctx)}/>',
            "      </condition>",
            "    </extension>",
        ]
    out += [
        f"    <extension name={a(name + '-closed')}>",
        f'      <condition field="destination_number" expression={a(ext_expr)}>',
        f'        <action application="transfer" data={a(closed_to + " XML " + ctx)}/>',
        "      </condition>",
        "    </extension>",
    ]
    return out


def _fallback_target(data: dict, fallback: str) -> str:
    if fallback == "ring_group" and data["ring_groups"]:
        return data["ring_groups"][0]["extension"]
    if fallback == "queue" and data["queues"]:
        return data["queues"][0]["extension"]
    return "exa-voicemail-main"


def _dialplan(data: dict) -> str:
    dom, ctx = data["domain"], data["tenant"]
    hours = _hours_by_id(data)
    users = {u["id"]: u for u in data["users"]}
    out = ["<include>", f"  <context name={a(ctx)}>"]

    # Emergency numbers first, never blocked and never waiting for the
    # controller: with carriers on, Kamailio gets every one of them to try.
    em = "|".join(sorted(EMERGENCY | set(data.get("emergency_numbers") or ())))
    out += [
        '    <extension name="emergency">',
        f'      <condition field="destination_number" expression={a("^(" + em + ")$")}>',
        '        <action application="set" data="exa_emergency=true"/>',
        '        <action application="set" data="effective_caller_id_number=${exa_emergency_callback}"/>',
    ]
    if data.get("emergency_sets"):
        sets = ",".join(str(n) for n in data["emergency_sets"])
        out.append(f'        <action application="set" data={a("sip_h_X-Exa-Route=" + sets)}/>')
    out += [
        f'        <action application="bridge" data={a("sofia/gateway/" + GATEWAY + "/$1")}/>',
        "      </condition>",
        "    </extension>",
    ]

    # Premium and high-risk destinations are refused here as well as by the controller.
    if data["blocked_prefixes"]:
        expr = "^\\+?(" + "|".join(data["blocked_prefixes"]) + ")\\d*$"
        out += [
            '    <extension name="blocked-destinations">',
            f'      <condition field="destination_number" expression={a(expr)}>',
            '        <action application="playback" data="ivr/ivr-call_cannot_be_completed_as_dialed.wav"/>',
            '        <action application="hangup" data="CALL_REJECTED"/>',
            "      </condition>",
            "    </extension>",
        ]

    out += [
        '    <extension name="check-voicemail">',
        '      <condition field="destination_number" expression="^\\*97$">',
        '        <action application="answer"/>',
        f'        <action application="voicemail" data={a("check default " + dom + " ${caller_id_number}")}/>',
        "      </condition>",
        "    </extension>",
        '    <extension name="exa-voicemail-main">',
        '      <condition field="destination_number" expression="^exa-voicemail-main$">',
        '        <action application="answer"/>',
        '        <action application="playback" data="voicemail/vm-not_available_no_voicemail.wav"/>',
        '        <action application="hangup"/>',
        "      </condition>",
        "    </extension>",
    ]

    # The AI agent. If the AI service doesn't answer, the call falls back.
    ai_fallback = _fallback_target(data, data["ai_rules"][0]["fallback"]) if data["ai_rules"] else "exa-voicemail-main"
    out += [
        '    <extension name="exa-ai">',
        '      <condition field="destination_number" expression="^exa-ai$">',
        '        <action application="set" data="continue_on_fail=true"/>',
        '        <action application="set" data="hangup_after_bridge=true"/>',
        '        <action application="set" data="call_timeout=8"/>',
        f'        <action application="bridge" data={a("sofia/gateway/" + AI_GATEWAY + "/" + data["tenant"])}/>',
        f'        <action application="transfer" data={a(ai_fallback + " XML " + ctx)}/>',
        "      </condition>",
        "    </extension>",
    ]

    for u in data["users"]:
        ext = u["extension"]
        out += [
            f"    <extension name={a('user-' + ext)}>",
            f'      <condition field="destination_number" expression={a("^" + ext + "$")}>',
            f'        <action application="set" data={a("dialed_extension=" + ext)}/>',
            '        <action application="set" data="call_timeout=25"/>',
            '        <action application="set" data="continue_on_fail=true"/>',
            '        <action application="set" data="hangup_after_bridge=true"/>',
        ]
        fwd = u["forward_to"]
        if u["dnd"]:
            pass  # do not disturb: straight to voicemail
        elif fwd and fwd.startswith("+"):
            # Back through this context, so the forwarded call is checked like any outside call.
            out += [
                f'        <action application="export" data={a("nolocal:exa_forwarded_by=" + ext)}/>',
                f'        <action application="bridge" data={a("loopback/" + fwd + "/" + ctx)}/>',
            ]
        elif fwd:
            out.append(f'        <action application="bridge" data={a(_dial(fwd, dom))}/>')
        else:
            out.append(f'        <action application="bridge" data={a(_dial(ext, dom))}/>')
        out += [
            '        <action application="answer"/>',
            f'        <action application="voicemail" data={a("default " + dom + " " + ext)}/>',
            "      </condition>",
            "    </extension>",
        ]

    for g in data["ring_groups"]:
        members = [users[m]["extension"] for m in g["members"] if m in users]
        sep = "," if g["strategy"] == "simultaneous" else "|"
        dial = sep.join(f"[leg_timeout={g['ring_seconds']}]{_dial(m, dom)}" for m in members)
        out += [
            f"    <extension name={a('ring-group-' + g['extension'])}>",
            f'      <condition field="destination_number" expression={a("^" + g["extension"] + "$")}>',
            '        <action application="set" data="continue_on_fail=true"/>',
            '        <action application="set" data="hangup_after_bridge=true"/>',
        ]
        if dial:
            out.append(f'        <action application="bridge" data={a(dial)}/>')
        out += [
            '        <action application="transfer" data="exa-voicemail-main XML ' + escape(ctx) + '"/>',
            "      </condition>",
            "    </extension>",
        ]

    for q in data["queues"]:
        out += [
            f"    <extension name={a('queue-' + q['extension'])}>",
            f'      <condition field="destination_number" expression={a("^" + q["extension"] + "$")}>',
            '        <action application="answer"/>',
            f'        <action application="callcenter" data={a(q["id"] + "@" + dom)}/>',
            "      </condition>",
            "    </extension>",
        ]

    for m in data["menus"]:
        open_to = f"exa-menu-{m['extension']}"
        if m["hours_id"] and m["hours_id"] in hours:
            closed = (
                _ext_of(data, m["closed_target"].get("type", ""), m["closed_target"].get("id")) or "exa-voicemail-main"
            )
            out += _hours_block(
                f"menu-{m['extension']}", "^" + m["extension"] + "$", hours[m["hours_id"]], open_to, closed, ctx
            )
        else:
            out += [
                f"    <extension name={a('menu-' + m['extension'])}>",
                f'      <condition field="destination_number" expression={a("^" + m["extension"] + "$")}>',
                f'        <action application="transfer" data={a(open_to + " XML " + ctx)}/>',
                "      </condition>",
                "    </extension>",
            ]
        out += [
            f"    <extension name={a(open_to)}>",
            f'      <condition field="destination_number" expression={a("^" + open_to + "$")}>',
            '        <action application="answer"/>',
            '        <action application="sleep" data="500"/>',
            f'        <action application="ivr" data={a(menu_name(data, m))}/>',
            "      </condition>",
            "    </extension>",
        ]

    # Inbound numbers arrive from public.xml as exa-in-<digits>. AI rules apply first.
    for n in data["numbers"]:
        digits = n["e164"].lstrip("+")
        name = f"exa-in-{digits}"
        target = _ext_of(data, n["target_type"], n["target_id"]) or "exa-voicemail-main"
        rule = next((r for r in data["ai_rules"] if r["number_id"] in (None, n["id"])), None)
        if rule and rule["condition"] == "always":
            target = "exa-ai"
        if rule and rule["condition"] == "after_hours" and rule["hours_id"] in hours:
            out += _hours_block(name, "^" + name + "$", hours[rule["hours_id"]], target, "exa-ai", ctx)
            continue
        out += [
            f"    <extension name={a(name)}>",
            f'      <condition field="destination_number" expression={a("^" + name + "$")}>',
        ]
        if rule and rule["condition"] in ("no_answer", "busy") and target not in ("exa-ai",):
            out += [
                '        <action application="set" data="continue_on_fail=true"/>',
                '        <action application="set" data="call_timeout=20"/>',
                f'        <action application="export" data={a("exa_ai_on=" + rule["condition"])}/>',
            ]
        out += [
            f'        <action application="transfer" data={a(target + " XML " + ctx)}/>',
            "      </condition>",
            "    </extension>",
        ]

    out += _outbound(data)
    out += ["  </context>", "</include>", ""]
    return "\n".join(out)


OUTSIDE = "^\\+?(\\d{7,15})$"


def _outbound(data: dict) -> list[str]:
    """Outside calls: ask the controller first (voice/pbx.py), then bridge.

    The check runs inline while the dial plan is hunted, so the extensions after
    it can match on its answer: "NO <code>" is refused, anything that is not
    "OK" (the controller is down or refused the digest) fails closed, and "OK
    <set ids> <caller id>" sets X-Exa-Route for Kamailio and the caller id.
    The PBX's secret never appears here: ${exa_pbx_secret} is FreeSWITCH's own
    global variable, from EXA_PBX_SECRET in its environment (vars.xml)."""
    t = data["tenant"]
    fields = f"{t}:${{user_name}}:${{exa_forwarded_by}}:$1:${{uuid}}"
    digest = f"${{md5(${{exa_pbx_secret}}:{fields}:${{exa_pbx_secret}})}}"
    url = (
        f"{data['controller_url']}/api/v1/commai/internal/voice/authorise"
        f"?tenant={t}&ext=${{user_name}}&fwd=${{exa_forwarded_by}}&to=$1&call=${{uuid}}"
    )
    ask = f"exa_auth=${{curl({url} connect-timeout 2 timeout 4 append_headers {pbx.HEADER}:{digest} get)}}"

    def ext(name: str, auth_expr: str | None, actions: list[str], cont: bool = False) -> list[str]:
        lines = [f"    <extension name={a(name)}{' continue=' + a('true') if cont else ''}>"]
        if auth_expr is None:
            lines.append(f'      <condition field="destination_number" expression={a(OUTSIDE)}>')
        else:
            lines += [
                f'      <condition field="destination_number" expression={a(OUTSIDE)}/>',
                f'      <condition field="${{exa_auth}}" expression={a(auth_expr)}>',
            ]
        return lines + [f"        {x}" for x in actions] + ["      </condition>", "    </extension>"]

    refuse = '<action application="playback" data="ivr/ivr-call_cannot_be_completed_as_dialed.wav"/>'
    return [
        "    <!-- Outside calls: the controller authorises each one (voice/pbx.py). -->",
        *ext(
            "outbound-authorise",
            None,
            [
                '<action application="set" data="exa_outbound=true" inline="true"/>',
                '<action application="set" data="exa_dest=$1" inline="true"/>',
                f'<action application="set" data={a(ask)} inline="true"/>',
            ],
            cont=True,
        ),
        *ext(
            "outbound-refused",
            "^NO (\\S+)",
            [
                '<action application="log" data="NOTICE outside call refused by the controller: $1"/>',
                '<action application="set" data="exa_refused=$1"/>',
                refuse,
                '<action application="hangup" data="CALL_REJECTED"/>',
            ],
        ),
        *ext(
            "outbound-unavailable",
            "^(?!OK )",
            [
                '<action application="log" data="WARNING outside call refused: no answer from the controller"/>',
                '<action application="set" data="exa_refused=controller_unreachable"/>',
                refuse,
                '<action application="hangup" data="SERVICE_UNAVAILABLE"/>',
            ],
        ),
        *ext(
            "outbound-route",
            "^OK (\\d+(,\\d+)*) ",
            ['<action application="set" data="sip_h_X-Exa-Route=$1"/>'],
            cont=True,
        ),
        *ext(
            "outbound-caller-id",
            "^OK \\S+ (\\+\\d{7,15})$",
            ['<action application="set" data="effective_caller_id_number=$1"/>'],
            cont=True,
        ),
        *ext(
            "outbound",
            "^OK ",
            [f'<action application="bridge" data={a("sofia/gateway/" + GATEWAY + "/+${exa_dest}")}/>'],
        ),
    ]


def _public(data: dict) -> str:
    out = ["<include>"]
    for n in data["numbers"]:
        digits = n["e164"].lstrip("+")
        out += [
            f"  <extension name={a('in-' + digits)}>",
            f'    <condition field="destination_number" expression={a(PLUS + digits + "$")}>',
            f'      <action application="set" data={a("domain_name=" + data["domain"])}/>',
            f'      <action application="transfer" data={a("exa-in-" + digits + " XML " + data["tenant"])}/>',
            "    </condition>",
            "  </extension>",
        ]
    out += ["</include>", ""]
    return "\n".join(out)


def menu_name(data: dict, m: dict) -> str:
    return f"{data['tenant']}_menu_{m['extension']}"


def _ivr(data: dict) -> str:
    out = ["<include>"]
    for m in data["menus"]:
        greet = "say:" + (m["greeting"] or "Please choose an option.")
        out += [
            f"  <menu name={a(menu_name(data, m))} greet-long={a(greet)} greet-short={a(greet)}"
            ' invalid-sound="ivr/ivr-that_was_an_invalid_entry.wav" exit-sound="voicemail/vm-goodbye.wav"'
            ' tts-engine="flite" tts-voice="slt" timeout="10000" max-failures="3" max-timeouts="3" digit-len="1">'
        ]
        for key, spec in sorted(m["options"].items()):
            dest = _ext_of(data, spec.get("type", ""), spec.get("id"))
            if dest:
                out.append(
                    f'    <entry action="menu-exec-app" digits={a(key)} '
                    f"param={a('transfer ' + dest + ' XML ' + data['tenant'])}/>"
                )
        out.append("  </menu>")
    out += ["</include>", ""]
    return "\n".join(out)


def _callcenter(data: dict) -> dict[str, str]:
    users = {u["id"]: u for u in data["users"]}
    dom = data["domain"]
    queues, agents, tiers = ["<include>"], ["<include>"], ["<include>"]
    seen_agents = set()
    for q in data["queues"]:
        queues += [
            f"  <queue name={a(q['id'] + '@' + dom)}>",
            f'    <param name="strategy" value={a(q["strategy"])}/>',
            '    <param name="moh-sound" value="$${hold_music}"/>',
            f'    <param name="max-wait-time" value={a(q["max_wait_s"])}/>',
            '    <param name="tier-rules-apply" value="false"/>',
            "  </queue>",
        ]
        for level, m in enumerate(q["members"], start=1):
            if m not in users:
                continue
            agent = f"{users[m]['extension']}@{dom}"
            if agent not in seen_agents:
                seen_agents.add(agent)
            tiers.append(f'  <tier agent={a(agent)} queue={a(q["id"] + "@" + dom)} level={a(level)} position="1"/>')
    for agent in sorted(seen_agents):
        agents.append(
            f'  <agent name={a(agent)} type="callback" contact={a("[call_timeout=15]user/" + agent)}'
            ' status="Available" max-no-answer="3" wrap-up-time="10" reject-delay-time="10"'
            ' busy-delay-time="60"/>'
        )
    for x in (queues, agents, tiers):
        x += ["</include>", ""]
    return {"queues.xml": "\n".join(queues), "agents.xml": "\n".join(agents), "tiers.xml": "\n".join(tiers)}


def render(data: dict) -> dict[str, str]:
    """{relative path: file content}, deterministic for the same data."""
    t = data["tenant"]
    files = {
        f"{t}/directory.xml": _directory(data),
        f"{t}/dialplan.xml": _dialplan(data),
        f"{t}/public.xml": _public(data),
        f"{t}/ivr.xml": _ivr(data),
    }
    files.update({f"{t}/{k}": v for k, v in _callcenter(data).items()})
    return dict(sorted(files.items()))


def digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def write(files: dict[str, str], root: str) -> None:
    """Write files under root, each one atomically."""
    for rel, content in files.items():
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content)
        os.replace(tmp, path)


def output_dir() -> str:
    """Where rendered files go: EXA_FREESWITCH_DIR (set in deploy/docker-compose.yml)."""
    return os.environ.get("EXA_FREESWITCH_DIR", "")


def render_business(conn: psycopg.Connection, cid: Any, root: str | None = None) -> dict:
    files = render(gather(conn, cid))
    d = digest(files)
    root = output_dir() if root is None else root
    written = ""
    if root:
        write(files, root)
        written = root
    conn.execute(
        """INSERT INTO voice_pbx_renders (customer_id, digest, files, written_to, rendered_at)
           VALUES (%s, %s, %s, %s, %s) ON CONFLICT (customer_id) DO UPDATE SET digest = EXCLUDED.digest,
             files = EXCLUDED.files, written_to = EXCLUDED.written_to, rendered_at = EXCLUDED.rendered_at""",
        (cid, d, Jsonb(files), written, now()),
    )
    return {"digest": d, "files": files, "written_to": written}


@jobs.handler("voice.render")
def _render_job(conn: psycopg.Connection, job: dict):
    render_business(conn, job["payload"]["customer_id"])
    return None
