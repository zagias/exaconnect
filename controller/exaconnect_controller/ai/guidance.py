"""Evidence-backed advisory choices, rendered without model-written product claims.

The model chooses relevant topics, follow-ups and scoped observations. Product
access and supported design controls come from these verified records. Shared
diagnosis selects actual scoped evidence; it does not emit model capability prose.
"""

from __future__ import annotations

import datetime as dt
import json
import re


def site_intent(question: str) -> str | None:
    """Direct site checks never authorise configuration changes."""
    if not re.search(r"\b(?:sites?|branches?)\b", question, re.I):
        return None
    if re.search(
        r"\b(?:voice|voip|sip|calls?|phone|video|teams|zoom|applications?|services?|routing|latency|jitter|loss|sla)\b",
        question,
        re.I,
    ):
        return None
    if re.search(r"\b(?:restore|recover)\b", question, re.I):
        return "restore"
    if re.search(
        r"\b(?:yesterday|previous|historical|history|ago|were|was|since|during|why|explain)\b|\blast\s+(?:week|month|night|year)\b",
        question,
        re.I,
    ):
        return None
    if re.search(
        r"\b(?:keep|stay|remain|ensure|design|prevent|avoid|plan|propose|improve)\b|\b(?:will|would|should)\s+(?:my|our|the)\s+(?:sites?|branches?)\b",
        question,
        re.I,
    ):
        return None
    if re.search(
        r"\b(?:delete|reset|disable|enable|apply|create|configure|set|change|update|remove)\b", question, re.I
    ):
        return None
    if re.search(
        r"\b(?:status|health|online|offline|reporting|reachable|unreachable)\b|\b(?:are|is)\b.*\b(?:up|down|working)\b",
        question,
        re.I,
    ):
        return "status"
    return None


def site_status_answer(context: dict) -> str:
    sites = context.get("sites", [])
    facts = example_note(context)
    organisation = str(context.get("account", {}).get("organisation") or "this organisation").replace("\n", " ")[:120]
    checked_at = context.get("now_utc")
    when = (
        dt.datetime.fromisoformat(str(checked_at)).astimezone(dt.UTC).strftime("%d %b %Y at %H:%M UTC")
        if checked_at
        else None
    )
    facts.append(f"Checked site records for {organisation}" + (f" on {when}." if when else "."))
    if not sites:
        return "\n\n".join(
            facts
            + ["No sites are recorded here. Check that you have selected the right organisation. Nothing has changed."]
        )
    reporting = sum(site.get("last_seen") is not None and site.get("online") is True for site in sites)
    never = sum(site.get("last_seen") is None for site in sites)
    stale = len(sites) - reporting - never
    facts.append(
        f"{len(sites)} sites: {reporting} reporting within 30 seconds, "
        f"{stale} with older reports, {never} with no report recorded."
    )
    paths = [path for site in sites for path in site.get("paths", [])]
    measured = sum(
        any(path.get(key) is not None for key in ("latency_ms_last_min", "jitter_ms_last_min", "loss_pct_last_min"))
        for path in paths
    )
    facts.append(f"{measured} of {len(paths)} configured paths have measurements from the last minute.")
    attention = [site for site in sites if site.get("last_seen") is None or site.get("online") is not True]
    details = []
    for site in attention[:5]:
        name = str(site.get("name") or "Unnamed site").replace("\n", " ")[:120]
        status = "no agent report recorded" if site.get("last_seen") is None else f"last report {site['last_seen']}"
        details.append(f"{name}: {status}.")
    if details:
        facts.append("\n".join(details))
    if len(attention) > 5:
        facts.append(f"{len(attention) - 5} more sites need a reporting check; open Sites for the full list.")
    facts.append(
        "Connectivity is unverified where reports are missing. "
        "Agent reports alone do not confirm every service is working. Nothing has changed."
    )
    return "\n\n".join(facts)


DESIGN = {
    "diversity": {
        "advice": "Check that terrestrial links have independent last-mile routes, upstreams and failure domains.",
        "question": "Which last-mile routes, upstreams and shared facilities do the proposed carriers use?",
    },
    "power": {
        "advice": "Size backup power for the edge, access equipment and satellite terminal, including outage duration.",
        "question": "How long must each branch run on backup power, including its access equipment and LEO terminal?",
    },
    "application_sla": {
        "advice": "Classify POS and voice traffic and agree latency, jitter, loss and capacity limits. "
        "Connect's routing engine chooses the path; this assistant can't pin it.",
        "question": "What are the payment provider's requirements, voice quality limits and peak concurrent demand?",
    },
    "storm_mode": {
        "advice": "Storm Mode keeps configured satellite paths warm for allowed traffic classes. "
        "It needs working links and agents; it doesn't buy or provision service.",
        "question": "Which sites and traffic classes should be allowed onto the configured satellite backup?",
    },
    "satellite_readiness": {
        "advice": "Check LEO coverage, sky view, power, performance, data limits and supplier terms "
        "before relying on it. "
        "GEO isn't eligible for real-time traffic.",
        "question": "What LEO service, clear-sky location, measured performance and data allowance "
        "can each site support?",
    },
    "satellite_costs": {
        "advice": "Budget satellite allowance and overage alongside terrestrial charges using verified supplier terms. "
        "A partner name or catalogue listing doesn't confirm supply or price.",
        "question": "What are the verified satellite allowance/overage terms and the monthly resilience budget?",
    },
    "pos_security": {
        "advice": "Segment POS traffic and use the payment provider's required outbound connections. "
        "Don't add inbound port forwarding without a specific, verified need.",
        "question": "Which documented outbound destinations and segmentation requirements "
        "does the payment provider require?",
    },
    "pos_sessions": {
        "advice": "Check payment destinations, fixed public IP or VPN requirements, "
        "and transaction recovery after failover. "
        "Don't assume payment sessions survive a path change.",
        "question": "Does the payment provider require a fixed public IP or VPN, "
        "and how should interrupted transactions recover?",
    },
    "voice_dependencies": {
        "advice": "Check the voice provider's signalling, media, NAT and failover requirements before changing policy. "
        "WAN resilience alone doesn't prove the phone service is ready.",
        "question": "Which voice service is in use, and what does its provider require "
        "for signalling, media and failover?",
    },
    "voice_failover": {
        "advice": "Distinguish new-call recovery from ongoing-call continuity. Changes to public IP, NAT "
        "or media paths "
        "can interrupt established calls; prove the required behaviour with the actual voice provider.",
        "question": "Is voice hosted or branch-based, and must failover preserve ongoing calls "
        "or only restore new calls?",
    },
    "commissioning": {
        "advice": "Install links and enrol agents before relying on monitoring or failover. "
        "A site record or draft circuit doesn't prove working connectivity.",
        "question": "Which links and edge agents are installed and commissioned, and which are only planned?",
    },
    "failure_validation": {
        "advice": "Agree an authorised test for brownouts, link loss, recovery, satellite limits "
        "and controller outage; "
        "measure application continuity before claiming resilience.",
        "question": "What outage and application-interruption limits should an authorised failover test prove?",
    },
    "shadow_mode": {
        "advice": "Use shadow mode to inspect routing decisions before enabling active steering; review measured "
        "results and hold times before an authorised change.",
        "question": "Should the initial commissioning period observe decisions in shadow mode before active steering?",
    },
    "dependencies": {
        "advice": "Include DNS, authentication, payment-acquirer and application endpoints in the failure analysis; "
        "multiple WAN links do not remove those dependencies.",
        "question": "Which DNS, authentication and application endpoints remain single points of failure?",
    },
}

CONSTRAINTS = {
    "storm_mode": {
        "uses_configured_satellite": True,
        "requires_installed_links_and_agents": True,
        "purchases_or_provisions_service": False,
        "satellite_classes": "SLA allow_satellite, or the separate organisation bulk-satellite setting",
        "geo_realtime_allowed": False,
        "source": "storm/service.py; routing/maps.py; routing/engine.py; desired.py",
    },
    "steering": {
        "path_selection": "routing_engine_with_class_SLA_and_eligible_paths",
        "assistant_can_pin_path": False,
        "assistant_controls": ["traffic_classification", "sla_limits", "storm_mode", "shadow_mode"],
        "source": "ai/act.py; routing/maps.py",
    },
    "pos_security": {
        "default_connection_direction": "outbound_to_verified_payment_provider",
        "unsolicited_inbound_port_forwarding": False,
        "requires": "segmentation and documented payment-provider requirements; no compliance guarantee",
    },
    "supply": {"catalogue_or_partner_implies_supply": False, "coverage_price_fulfilment_guaranteed": False},
}

SELECTION_LIMITS = {
    "feature_ids": 2,
    "controls": 3,
    "questions": 4,
    "observations": 2,
    "offer_codes": 2,
    "evidence_ids": 6,
    "account_topics": 4,
}
MAX_SELECTION_ITEMS = 64

CONTRACT = {
    "kind": "account | design | analysis | proposal",
    "feature_ids": "up to 2 verified feature IDs relevant to the question",
    "controls": "up to 3 relevant supported_design IDs, in priority order",
    "questions": "up to 4 supported_design IDs whose follow-up question is useful; none if already answered",
    "observations": "up to 2 of inventory, satellite, shadow_mode",
    "offer_codes": "up to 2 exact live catalogue codes; empty if no relevant verified offer",
    "evidence_ids": "for analysis: 1 to 6 exact scoped_evidence IDs relevant to the question",
    "account_topics": "choose relevant unresolved topics: access, subscriptions, configuration, offers",
    "selection_policy": {
        "display_limits": SELECTION_LIMITS,
        "maximum_supplied_per_field": MAX_SELECTION_ITEMS,
        "order": "Most relevant IDs first. Validate all IDs before removing duplicates or valid extras.",
        "unknown_ids": "The whole advisory selection is rejected; unsupported IDs are never silently dropped.",
    },
}


def _selection(advice: dict, name: str, allowed) -> list[str]:
    values = advice.get(name, [])
    if not isinstance(values, list) or len(values) > MAX_SELECTION_ITEMS:
        raise ValueError("invalid selection")
    # Validate the complete selection before bounding it: an unknown ID at the
    # end must not disappear behind the presentation limit.
    if any(not isinstance(value, str) or value not in allowed for value in values):
        raise ValueError("unverified selection")
    return list(dict.fromkeys(values))[: SELECTION_LIMITS[name]]


def selection_metadata(text: str) -> dict:
    """Counts only, for an advisory selection already validated by render()."""
    advice = json.loads(text)["advice"]
    changes = []
    for name, maximum in SELECTION_LIMITS.items():
        values = advice.get(name, [])
        selected = min(len(dict.fromkeys(values)), maximum)
        if selected != len(values):
            changes.append({"field": name, "supplied": len(values), "selected": selected})
    return {"normalised": bool(changes), "changes": changes}


def account_facts(context: dict, feature_ids: list[str], offer_codes: list[str], topics=None) -> list[str]:
    account = context.get("account", {})
    topics = {"access", "subscriptions", "configuration", "offers"} if topics is None else set(topics)
    facts = []
    for product, label in (("connect", "Connect"), ("commai", "Jibsy")) if "access" in topics else []:
        evidence = account.get("product_access", {}).get(product, {})
        if evidence.get("operator_access"):
            facts.append(
                f"Your admin role lets you review {label}; this doesn't show what the organisation has bought."
            )
        elif evidence.get("member_access"):
            facts.append(f"You have {label} access.")
        elif evidence.get("organisation_access"):
            facts.append(f"The organisation has {label} access; an owner or admin can give you app access.")
        else:
            facts.append(f"{label} isn't enabled for this organisation.")
    if "subscriptions" in topics:
        if not account.get("subscription_evidence", {}).get("active_recorded"):
            facts.append("No active subscription is recorded. App access doesn't confirm a purchase or payment.")
        else:
            facts.append("An active subscription is recorded. Payment hasn't been checked.")
    if "configuration" in topics:
        facts.append("I haven't checked which services are connected or working.")
    features = {feature["id"]: feature for feature in context.get("verified_features", [])}
    for feature_id in feature_ids:
        feature = features[feature_id]
        if feature.get("availability", {}).get("permitted_now"):
            facts.append(feature["name"] + ": " + feature["description"])
        else:
            facts.append(feature["name"] + " isn't available with your access and permissions.")
    offers = {offer["code"]: offer for offer in context.get("catalogue", [])}
    for code in offer_codes:
        offer = offers[code]
        facts.append(f"The catalogue lists {offer['name']}: " + (offer.get("price_text") or "Price needs review."))
    if "offers" in topics and not offers:
        facts.append("There isn't a verified offer or price here. Availability and fulfilment still need review.")
    elif "offers" in topics or offer_codes:
        facts.append("A catalogue listing doesn't confirm coverage, supply or fulfilment.")
    upgrade = context.get("upgrade_guidance", {})
    if topics.intersection({"access", "offers"}) and upgrade.get("can_request"):
        facts.append(
            "You can request access in Apps & plans or discuss availability in Quotes. "
            "Nothing has been requested or bought."
        )
    elif topics.intersection({"access", "offers"}):
        facts.append("Ask an owner or admin to review app access or a quote.")
    return facts


def observations(context: dict, selected: list[str]) -> list[str]:
    sites = context.get("sites", [])
    facts = []
    if "inventory" in selected:
        links = sum(len(site.get("paths", [])) for site in sites)
        reporting = sum(bool(site.get("online")) for site in sites)
        facts.append(
            f"There are {len(sites)} recorded sites, {links} configured paths and {reporting} agents reporting."
        )
    if "satellite" in selected:
        count = sum(path.get("underlay_type") in ("leo", "geo") for site in sites for path in site.get("paths", []))
        facts.append(
            f"There are {count} configured satellite paths. Coverage, performance and terms haven't been checked."
        )
    if "shadow_mode" in selected and "shadow_mode" in context.get("customer", {}):
        mode = "on" if context["customer"]["shadow_mode"] else "off"
        facts.append(f"Shadow mode is {mode}.")
    return facts


def attach_evidence(context: dict) -> None:
    """Give the model stable references to existing, scoped source facts."""
    evidence = []
    for index, decision in enumerate(context.get("routing_decisions_last_7_days_newest_first", [])):
        mode = "Shadow decision" if decision.get("shadow") else "Recorded decision"
        evidence.append(
            {
                "id": f"decision:{index}",
                "text": f"{mode} at {decision.get('time')}, {decision.get('site')}, "
                f"{decision.get('class_name')}: {decision.get('reason')}",
            }
        )
    for index, site in enumerate(context.get("sites", [])):
        if site.get("last_seen") is not None:
            evidence.append(
                {
                    "id": f"agent:{index}",
                    "text": f"{site.get('name')}: agent last seen {site['last_seen']}; "
                    f"currently reporting: {'yes' if site.get('online') else 'no'}.",
                }
            )
        for number, path in enumerate(site.get("paths", [])):
            metrics = {
                name: path.get(name) for name in ("latency_ms_last_min", "jitter_ms_last_min", "loss_pct_last_min")
            }
            if any(value is not None for value in metrics.values()):
                evidence.append(
                    {
                        "id": f"path:{index}:{number}",
                        "text": f"{site.get('name')}, {path.get('path')}, last minute: "
                        f"latency {metrics['latency_ms_last_min']} ms, "
                        f"jitter {metrics['jitter_ms_last_min']} ms, "
                        f"loss {metrics['loss_pct_last_min']}%; missing values are not measured.",
                    }
                )
    for index, event in enumerate(context.get("events_last_24h_newest_first", [])):
        evidence.append(
            {
                "id": f"event:{index}",
                "text": f"Recorded event at {event.get('time')}: {event.get('kind')}, node {event.get('node')}.",
            }
        )
    context["scoped_evidence"] = evidence[:80]


def readiness_note(context: dict) -> list[str]:
    sites = context.get("sites", [])
    paths = sum(len(site.get("paths", [])) for site in sites)
    reporting = sum(bool(site.get("online")) for site in sites)
    if sites and not paths:
        return [
            "There are no configured paths here. A previous agent report doesn't prove working connectivity. "
            "Treat this as a proposed design."
        ]
    if paths and not reporting:
        return [
            f"There are {paths} configured paths but no agents reporting. "
            "Working connectivity and failover still need checking."
        ]
    return []


def example_note(context: dict) -> list[str]:
    return ["Example data."] if context.get("account", {}).get("example") else []


def brief(facts: list[str], questions: list[str] | None = None) -> str:
    """Keep complete factual statements; never truncate a qualification mid-sentence."""
    selected = []
    words = 0
    for fact in facts:
        size = len(fact.split())
        if size + words <= 140:
            selected.append(fact)
            words += size
    return "\n\n".join(selected + (questions or []))


def proposal_answer(plan: dict | None, context: dict) -> str:
    facts = example_note(context)
    facts.append(
        "Proposed changes only. Nothing has been applied; review the exact revision before approving and applying it."
    )
    facts.extend(readiness_note(context))
    if plan:
        facts.extend(plan.get("summary") or [])
    return brief(facts)


def render(text: str, answer: str, actions: list[dict], context: dict, scope: str) -> tuple[str, list[dict], str]:
    """Render selected source facts, controls and questions, never raw claims.

    Unknown selectors never become text or an executable action. Account scope
    cannot be turned into network analysis to bypass the entitlement projection.
    """
    try:
        document = json.loads(text)
        advice = document.get("advice", {}) if isinstance(document, dict) else {}
        if not isinstance(advice, dict):
            raise ValueError("invalid advice")
        kind = advice.get("kind")
        if scope != "account" and actions and kind not in ("account", "design"):
            return proposal_answer(None, context), actions, "reviewed_typed_proposal"
        if kind not in ("account", "design", "analysis") or (scope == "account" and kind != "account"):
            raise ValueError("missing advice contract")
        if set(advice) - {
            "kind",
            "feature_ids",
            "controls",
            "questions",
            "observations",
            "offer_codes",
            "evidence_ids",
            "account_topics",
        }:
            raise ValueError("unknown advice field")
        features = {feature["id"] for feature in context.get("verified_features", [])}
        offers = {offer["code"] for offer in context.get("catalogue", [])}
        feature_ids = _selection(advice, "feature_ids", features)
        offer_codes = _selection(advice, "offer_codes", offers)
        controls = _selection(advice, "controls", DESIGN)
        questions = _selection(advice, "questions", DESIGN)
        selected = _selection(advice, "observations", {"inventory", "satellite", "shadow_mode"})
        evidence = {record["id"]: record["text"] for record in context.get("scoped_evidence", [])}
        evidence_ids = _selection(advice, "evidence_ids", evidence)
        topics = _selection(advice, "account_topics", {"access", "subscriptions", "configuration", "offers"})
        if kind == "account":
            if controls or questions or selected or evidence_ids:
                raise ValueError("account scope does not include network data")
            chosen_topics = topics if "account_topics" in advice else None
            facts = example_note(context) + account_facts(context, feature_ids, offer_codes, chosen_topics)
            if not facts:
                raise ValueError("empty account advice selection")
        else:
            if feature_ids or offer_codes:
                raise ValueError("network advice cannot imply product access or a supplier order")
            if kind == "analysis" and not evidence_ids:
                raise ValueError("analysis requires selected actual evidence")
            if not (controls or questions or selected or evidence_ids):
                raise ValueError("empty network advice selection")
            facts = example_note(context) + readiness_note(context) + observations(context, selected)
            facts.extend(evidence[record] for record in evidence_ids)
            facts.extend(DESIGN[control]["advice"] for control in controls)
            if not facts:
                raise ValueError("empty design advice")
        return brief(facts, [DESIGN[question]["question"] for question in questions]), [], "verified_advisory_selection"
    except (ValueError, TypeError, AttributeError, KeyError):
        if scope == "account":
            available = [
                feature["id"]
                for feature in context.get("verified_features", [])
                if feature.get("availability", {}).get("permitted_now")
            ][:2]
            fallback = example_note(context) + account_facts(context, available, [])
        else:
            fallback = example_note(context) + readiness_note(context) + observations(context, ["inventory"])
            fallback.extend(DESIGN[control]["advice"] for control in ("application_sla", "storm_mode", "pos_security"))
        return brief(fallback), [], "unverified_advice_replaced_with_grounded_facts"
