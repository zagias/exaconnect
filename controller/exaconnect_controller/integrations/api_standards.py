"""TM Forum (TMF621, TMF622, TMF688) and MEF LSO Sonata APIs (ADR 0026).

Paths follow each standard's own layout under /api/v1. TMF678 Customer
Bill is not served: billing is outside Connect (see docs/integrations.md).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response
from psycopg.types.json import Jsonb

from .. import audit, db
from ..api.deps import UserDep
from . import notices, standards
from .api import IntegrationIn, create_integration, delivery
from .standards import StandardsError

router = APIRouter()

TT = "/tmf-api/troubleTicket/v4/troubleTicket"
PO = "/tmf-api/productOrderingManagement/v4/productOrder"
EV = "/tmf-api/event/v4"
SON = "/mefApi/sonata"


def _base(request: Request, path: str) -> str:
    return f"/api/v1{path}"


def _err(e: StandardsError | notices.NoticeError) -> HTTPException:
    return HTTPException(getattr(e, "code", 400), str(e))


def _buyer(user, buyer_id: str | None) -> Any:
    """Who is ordering: a customer user's own organisation, or the buyerId an admin names."""
    if user.role == "customer":
        if buyer_id and str(buyer_id) != str(user.customer_id):
            raise HTTPException(403, "You can only order for your own organisation.")
        return user.customer_id
    if user.role == "admin":
        if not buyer_id:
            raise HTTPException(400, "Say which organisation with buyerId.")
        return buyer_id
    raise HTTPException(403, "Not available for this account.")


# ---- TMF621 Trouble Ticket ---------------------------------------------------------


def _tickets(conn, user, ticket_id: int | None = None) -> list[dict]:
    q = "SELECT * FROM connect_notices WHERE (%(id)s::bigint IS NULL OR id = %(id)s)"
    args: dict[str, Any] = {"id": ticket_id}
    own: set[str] | None = None
    if user.role == "carrier":
        q += " AND carrier_id = %(k)s AND kind <> 'trouble'"
        args["k"] = user.carrier_id
    elif user.role == "customer":
        own = {str(r["id"]) for r in conn.execute("SELECT id FROM links WHERE customer_id = %s", (user.customer_id,))}
        q += " AND (customer_id = %(c)s OR (kind <> 'trouble' AND link_ids && %(own)s::uuid[]))"
        args.update(c=user.customer_id, own=list(own))
    elif user.role != "admin":
        raise HTTPException(403, "Not available for this account.")
    rows = conn.execute(q + " ORDER BY id DESC LIMIT 200", args).fetchall()
    if own is not None:
        for r in rows:
            r["link_ids"] = [x for x in r["link_ids"] if str(x) in own]
    return rows


@router.get(TT, tags=["tmf621"])
def tt_list(request: Request, user: UserDep) -> list[dict]:
    with db.tx() as conn:
        return [standards.ticket(r, _base(request, TT)) for r in _tickets(conn, user)]


@router.get(TT + "/{ticket_id}", tags=["tmf621"])
def tt_get(ticket_id: int, request: Request, user: UserDep) -> dict:
    with db.tx() as conn:
        rows = _tickets(conn, user, ticket_id)
    if not rows:
        raise HTTPException(404, "Trouble ticket not found.")
    return standards.ticket(rows[0], _base(request, TT))


TT_EXAMPLE = {
    "name": "Planned fibre work, Spanish Town",
    "description": "Splicing after road works.",
    "severity": "Minor",
    "ticketType": "Maintenance",
    "@type": "MaintenanceTroubleTicket",
    "plannedStartDate": "2026-10-10T02:00:00Z",
    "plannedEndDate": "2026-10-10T04:00:00Z",
    "externalIdentifier": [{"id": "CHG-20431", "owner": "carrier"}],
    "relatedEntity": [{"id": "<your link id>", "role": "affectedLink", "@referredType": "Link"}],
}


def create_ticket(conn, user, body: dict, source: str, carrier_id: Any = None) -> dict:
    if user.role == "carrier" or carrier_id:
        f = standards.from_ticket(body)
        return notices.create(
            conn,
            carrier_id=carrier_id or user.carrier_id,
            actor=user.actor,
            source=source,
            raw=body,
            **f,
        )
    if user.role != "customer":
        raise HTTPException(400, "Admins post tickets for a carrier with carrierId.")
    links = [str(e.get("id")) for e in body.get("relatedEntity") or [] if e.get("id")]
    own = {str(r["id"]) for r in conn.execute("SELECT id FROM links WHERE customer_id = %s", (user.customer_id,))}
    if any(x not in own for x in links):
        raise HTTPException(403, "A ticket can only name your own links.")
    name = str(body.get("name") or body.get("description") or "").strip()
    if not name:
        raise HTTPException(422, "A trouble ticket needs a name or description.")
    row = conn.execute(
        """INSERT INTO connect_notices (customer_id, kind, status, severity, title, description, link_ids, source, raw,
                                        created_by)
           VALUES (%s, 'trouble', 'open', %s, %s, %s, %s::uuid[], %s, %s, %s) RETURNING *""",
        (
            user.customer_id,
            standards.SEV_IN.get(str(body.get("severity") or "").lower(), "warning"),
            name[:200],
            str(body.get("description") or "")[:4000],
            links,
            source,
            Jsonb(body),
            user.actor,
        ),
    ).fetchone()
    audit.record(conn, user.actor, "ticket.create", row["title"], user.customer_id, {"id": row["id"]})
    return row


@router.post(TT, status_code=201, tags=["tmf621"])
def tt_create(
    request: Request,
    user: UserDep,
    body: dict = Body(..., examples=[TT_EXAMPLE]),
    carrierId: str | None = Query(default=None, description="Admins only."),  # noqa: N803
) -> dict:
    """Carriers: a fault or maintenance on your own links (Maintenance tickets carry plannedStartDate and
    plannedEndDate). Organisations: a trouble ticket to ExaCarib."""
    if carrierId and user.role != "admin":
        raise HTTPException(403, "Only admins name a carrier.")
    with db.tx() as conn:
        try:
            row = create_ticket(conn, user, body, "tmf621", carrierId)
        except (notices.NoticeError, StandardsError, ValueError) as e:
            raise HTTPException(getattr(e, "code", 422), str(e)) from None
    return standards.ticket(row, _base(request, TT))


@router.patch(TT + "/{ticket_id}", tags=["tmf621"])
def tt_patch(ticket_id: int, request: Request, user: UserDep, body: dict = Body(...)) -> dict:
    with db.tx() as conn:
        rows = _tickets(conn, user, ticket_id)
        if not rows:
            raise HTTPException(404, "Trouble ticket not found.")
        n = rows[0]
        if user.role == "customer" and n["kind"] != "trouble":
            raise HTTPException(403, "Only the carrier changes its notices.")
        try:
            changes = standards.ticket_changes(body)
            if user.role == "customer" and changes.get("status") not in (None, "closed", "cancelled"):
                raise HTTPException(403, "You can close or cancel your ticket; ExaCarib updates its progress.")
            notices.update(
                conn,
                conn.execute("SELECT * FROM connect_notices WHERE id = %s", (ticket_id,)).fetchone(),
                changes,
                user.actor,
            )
        except (notices.NoticeError, StandardsError, ValueError) as e:
            raise HTTPException(getattr(e, "code", 422), str(e)) from None
        rows = _tickets(conn, user, ticket_id)
    return standards.ticket(rows[0], _base(request, TT))


# ---- TMF622 Product Ordering --------------------------------------------------------

PO_EXAMPLE = {
    "description": "Cloud circuit for Kingston",
    "productOrderItem": [
        {
            "id": "1",
            "action": "add",
            "productOffering": {"id": "cloud-circuit"},
            "product": {
                "productCharacteristic": [
                    {"name": "provider", "value": "aws"},
                    {"name": "region", "value": "us-east-1"},
                    {"name": "site", "value": "kingston"},
                    {"name": "bandwidthMbps", "value": 50},
                    {"name": "cloudPrefixes", "value": ["10.100.0.0/16"]},
                    {"name": "peerAddress", "value": "203.0.113.10"},
                    {"name": "psk", "value": "<pre-shared key>"},
                ]
            },
        }
    ],
}


def _scrub(body: dict) -> dict:
    """Never keep secrets (pre-shared keys) in stored documents."""
    import copy

    out = copy.deepcopy(body)
    for it in out.get("productOrderItem") or out.get("quoteItem") or []:
        prod = it.get("product") or {}
        for c in prod.get("productCharacteristic") or []:
            if c.get("name") in ("psk",):
                c["value"] = "[secret]"
        cfg = prod.get("productConfiguration") or {}
        if "psk" in cfg:
            cfg["psk"] = "[secret]"
    return out


@router.post(PO, status_code=201, tags=["tmf622"])
def po_create(
    request: Request,
    user: UserDep,
    body: dict = Body(..., examples=[PO_EXAMPLE]),
    buyerId: str | None = Query(default=None),  # noqa: N803
) -> dict:
    """Place an order. Items are checked like any order; a valid order is confirmed at once."""
    cid = _buyer(user, buyerId)
    try:
        actions, inputs = standards.tmf622_items(body)
    except StandardsError as e:
        raise _err(e) from None
    with db.tx() as conn:
        doc_body = {
            **_scrub(body),
            "orderDate": standards.now_iso(),
            "@type": "ProductOrder",
        }
        try:
            order = standards.place(conn, cid, actions, inputs, user.actor, str(body.get("description") or "TMF622"))
        except StandardsError as e:
            doc = standards.store(conn, "tmf622", cid, "rejected", {**doc_body, "note": [{"text": str(e)}]})
            return {**doc, "state": "rejected", "href": f"{_base(request, PO)}/{doc['id']}"}
        doc = standards.store(conn, "tmf622", cid, "acknowledged", doc_body, order["id"])
        row = standards.load(conn, "tmf622", None, doc["id"])
        return standards.tmf622_view(row, order, _base(request, PO))


@router.get(PO, tags=["tmf622"])
def po_list(request: Request, user: UserDep, buyerId: str | None = Query(default=None)) -> list[dict]:  # noqa: N803
    cid = _buyer(user, buyerId)
    with db.tx() as conn:
        docs = conn.execute(
            "SELECT * FROM connect_std_documents WHERE kind = 'tmf622' AND customer_id = %s ORDER BY created_at DESC",
            (cid,),
        ).fetchall()
        orders = {
            o["id"]: o
            for o in conn.execute(
                "SELECT * FROM orders WHERE id = ANY(%s)", ([d["order_id"] for d in docs if d["order_id"]],)
            )
        }
    return [standards.tmf622_view(d, orders.get(d["order_id"]), _base(request, PO)) for d in docs]


@router.get(PO + "/{order_id}", tags=["tmf622"])
def po_get(order_id: str, request: Request, user: UserDep, buyerId: str | None = Query(default=None)) -> dict:  # noqa: N803
    cid = None if user.role == "admin" and not buyerId else _buyer(user, buyerId)
    with db.tx() as conn:
        doc = standards.load(conn, "tmf622", cid, order_id)
        if doc is None:
            raise HTTPException(404, "Product order not found.")
        order = (
            conn.execute("SELECT * FROM orders WHERE id = %s", (doc["order_id"],)).fetchone()
            if doc["order_id"]
            else None
        )
    return standards.tmf622_view(doc, order, _base(request, PO))


# ---- TMF688 Event --------------------------------------------------------------------


@router.post(EV + "/hub", status_code=201, tags=["tmf688"])
def hub_register(
    user: UserDep,
    body: dict = Body(
        ..., examples=[{"callback": "https://listener.example.org/tmf", "query": "eventType=path.down,carrier.fault"}]
    ),
) -> dict:
    """Register a listener: Connect POSTs TMF688 Event resources to the callback."""
    callback = str(body.get("callback") or "")
    if not callback:
        raise HTTPException(400, "A hub registration needs a callback.")
    query = str(body.get("query") or "")
    kinds = ["*"]
    for part in query.split("&"):
        k, _, v = part.partition("=")
        if k.strip() == "eventType" and v:
            kinds = [x.strip() for x in v.split(",") if x.strip()]
    with db.tx() as conn:
        row, _ = create_integration(
            conn,
            user,
            IntegrationIn(
                provider="tmf688",
                name="TMF688 listener",
                config={"query": query[:400]},
                secrets={"url": callback},
                event_types=kinds,
            ),
            origin="tmf688",
        )
    return {"id": str(row["id"]), "callback": callback, "query": query, "mode": delivery.mode_of(row)}


def _hub(conn, user, hub_id: int) -> dict:
    from .api import _get

    row = _get(conn, user, hub_id)
    if row["origin"] != "tmf688":
        raise HTTPException(404, "Hub registration not found.")
    return row


@router.get(EV + "/hub/{hub_id}", tags=["tmf688"])
def hub_get(hub_id: int, user: UserDep) -> dict:
    with db.tx() as conn:
        row = _hub(conn, user, hub_id)
    return {"id": str(row["id"]), "callback": row["config"].get("host", ""), "query": row["config"].get("query", "")}


@router.delete(EV + "/hub/{hub_id}", status_code=204, tags=["tmf688"])
def hub_delete(hub_id: int, user: UserDep) -> Response:
    with db.tx() as conn:
        row = _hub(conn, user, hub_id)
        conn.execute("DELETE FROM connect_integrations WHERE id = %s", (row["id"],))
        audit.record(conn, user.actor, "integration.delete", row["name"], row["customer_id"], {"id": row["id"]})
    return Response(status_code=204)


@router.get(EV + "/event", tags=["tmf688"])
def event_list(user: UserDep, limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
    """Recent Connect events as TMF688 Event resources (your organisation's, or your links' for a carrier)."""
    from . import cloudevents
    from .providers.webhook import tmf_event

    with db.tx() as conn:
        if user.role == "carrier":
            rows = conn.execute(
                "SELECT * FROM connect_events WHERE %s = ANY(carrier_ids) ORDER BY time DESC LIMIT %s",
                (user.carrier_id, limit),
            ).fetchall()
        elif user.role == "customer":
            rows = conn.execute(
                "SELECT * FROM connect_events WHERE customer_id = %s ORDER BY time DESC LIMIT %s",
                (user.customer_id, limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM connect_events ORDER BY time DESC LIMIT %s", (limit,)).fetchall()
    out = []
    for r in rows:
        ce = cloudevents.event(r)
        if user.role == "carrier":
            ce = delivery.carrier_view(ce)
        out.append(tmf_event(ce))
    return out


EV_EXAMPLE = {
    "eventId": "c-77812",
    "eventTime": "2026-10-07T12:00:00Z",
    "eventType": "TroubleTicketCreateEvent",
    "event": {
        "troubleTicket": {
            "id": "INC-77812",
            "name": "Fibre cut near Spanish Town",
            "severity": "Critical",
            "ticketType": "Fault",
            "relatedEntity": [{"id": "<your link id>", "role": "affectedLink", "@referredType": "Link"}],
        }
    },
}


@router.post(EV + "/event", status_code=201, tags=["tmf688"])
def event_inbound(user: UserDep, body: dict = Body(..., examples=[EV_EXAMPLE])) -> dict:
    """Carriers: send TroubleTicket events (create, status change, resolved) about your own links."""
    if user.role != "carrier":
        raise HTTPException(403, "Carriers only.")
    et = str(body.get("eventType") or "")
    tt = ((body.get("event") or {}).get("troubleTicket")) or {}
    if not tt:
        raise HTTPException(422, "The event must carry event.troubleTicket.")
    ext = str(tt.get("id") or "")
    with db.tx() as conn:
        existing = (
            conn.execute(
                "SELECT * FROM connect_notices WHERE carrier_id = %s AND external_id = %s FOR UPDATE",
                (user.carrier_id, ext),
            ).fetchone()
            if ext
            else None
        )
        try:
            if existing is None:
                if et and "Create" not in et:
                    raise HTTPException(404, f"No notice with the reference {ext} to change.")
                f = standards.from_ticket({**tt, "externalIdentifier": [{"id": ext}] if ext else []})
                row = notices.create(conn, carrier_id=user.carrier_id, actor=user.actor, source="tmf688", raw=body, **f)
            else:
                changes = standards.ticket_changes(tt)
                if "Resolved" in et:
                    changes["status"] = "resolved"
                row = notices.update(conn, existing, changes, user.actor)
        except (notices.NoticeError, StandardsError, ValueError) as e:
            raise HTTPException(getattr(e, "code", 422), str(e)) from None
    return {"eventId": body.get("eventId"), "notice": row["id"], "status": row["status"]}


# ---- MEF LSO Sonata --------------------------------------------------------------------

POQ = SON + "/productOfferingQualification/v7/productOfferingQualification"
QUOTE = SON + "/quoteManagement/v8/quote"
SPO = SON + "/productOrderingManagement/v10/productOrder"
STT = SON + "/troubleTicket/v4/troubleTicket"

SONATA_ITEM = {
    "id": "1",
    "action": "add",
    "product": {
        "productOffering": {"id": "cloud-onramp"},
        "productConfiguration": {
            "@type": "urn:exacarib:connect:cloud-onramp:v1",
            "provider": "aws_dx",
            "site": "kingston",
            "bandwidthMbps": 50,
            "awsAccountId": "123456789012",
            "region": "us-east-1",
        },
    },
}


def _items(body: dict, key: str) -> list[dict]:
    items = body.get(key) or []
    if not items:
        raise HTTPException(422, f"Give at least one {key}.")
    return items


@router.post(POQ, status_code=201, tags=["sonata"])
def poq_create(
    user: UserDep,
    body: dict = Body(
        ..., examples=[{"instantSyncQualification": True, "productOfferingQualificationItem": [SONATA_ITEM]}]
    ),
    buyerId: str | None = Query(default=None),  # noqa: N803
) -> dict:
    """Can this be provided? Each item is checked as an order would be (instant, synchronous)."""
    cid = _buyer(user, buyerId)
    out_items = []
    with db.tx() as conn:
        for it in _items(body, "productOfferingQualificationItem"):
            try:
                a, _ = standards.sonata_item(it)
                r = standards.review_one(conn, cid, a)
                problems = r["problems"]
            except StandardsError as e:
                problems = [str(e)]
            out_items.append(
                {
                    "id": it.get("id"),
                    "action": it.get("action", "add"),
                    "state": "done",
                    "qualificationResult": "unqualified" if problems else "qualified",
                    "terminationError": [{"value": p} for p in problems],
                    "product": it.get("product"),
                }
            )
        doc = standards.store(
            conn,
            "sonata_poq",
            cid,
            "done.ready",
            {
                "instantSyncQualification": True,
                "state": "done.ready",
                "requestedProductOfferingQualificationCompletionDate": None,
                "effectiveQualificationDate": standards.now_iso(),
                "productOfferingQualificationItem": out_items,
            },
        )
    return doc


@router.get(POQ + "/{doc_id}", tags=["sonata"])
def poq_get(doc_id: str, user: UserDep, buyerId: str | None = Query(default=None)) -> dict:  # noqa: N803
    return _doc("sonata_poq", doc_id, user, buyerId)


def _doc(kind: str, doc_id: str, user, buyer: str | None) -> dict:
    cid = None if user.role == "admin" and not buyer else _buyer(user, buyer)
    with db.tx() as conn:
        d = standards.load(conn, kind, cid, doc_id)
    if d is None:
        raise HTTPException(404, "Not found.")
    return d["body"]


@router.post(QUOTE, status_code=201, tags=["sonata"])
def quote_create(
    user: UserDep,
    body: dict = Body(..., examples=[{"instantSyncQuote": True, "quoteItem": [SONATA_ITEM]}]),
    buyerId: str | None = Query(default=None),  # noqa: N803
) -> dict:
    """A firm monthly price per item. An orderable quote can be referenced by a product order."""
    cid = _buyer(user, buyerId)
    items = []
    all_ok = True
    with db.tx() as conn:
        for it in _items(body, "quoteItem"):
            try:
                a, _ = standards.sonata_item(it)
                r = standards.review_one(conn, cid, a)
                problems, price = r["problems"], r["monthly_estimate"]
            except StandardsError as e:
                problems, price = [str(e)], 0
            all_ok = all_ok and not problems
            items.append(
                {
                    "id": it.get("id"),
                    "action": it.get("action", "add"),
                    "state": "unableToProvide" if problems else "approved.orderable",
                    "product": _scrub({"quoteItem": [it]})["quoteItem"][0].get("product"),
                    "quoteItemPrice": []
                    if problems
                    else [
                        {
                            "priceType": "recurring",
                            "recurringChargePeriod": "month",
                            "price": {"dutyFreeAmount": standards.money(price)},
                        }
                    ],
                    "terminationError": [{"value": p} for p in problems],
                }
            )
        state = "approved.orderable" if all_ok else "unableToProvide"
        doc = standards.store(
            conn,
            "sonata_quote",
            cid,
            state,
            {"instantSyncQuote": True, "state": state, "quoteDate": standards.now_iso(), "quoteItem": items},
        )
    return doc


@router.get(QUOTE + "/{doc_id}", tags=["sonata"])
def quote_get(doc_id: str, user: UserDep, buyerId: str | None = Query(default=None)) -> dict:  # noqa: N803
    return _doc("sonata_quote", doc_id, user, buyerId)


SONATA_STATE = {"done": "completed", "pending_partner": "inProgress", "cancelled": "cancelled", "failed": "failed"}


def _sonata_order_view(d: dict, order: dict | None) -> dict:
    body = dict(d["body"])
    if order is not None:
        body["state"] = SONATA_STATE.get(order["status"], body.get("state"))
        results = {r.get("action"): r for r in order["results"] or []}
        for n, it in enumerate(body.get("productOrderItem") or []):
            r = results.get(n) or {}
            it["state"] = (
                "completed" if r.get("ok") and not r.get("pending") else ("inProgress" if r else body["state"])
            )
            if r.get("message"):
                it["note"] = [{"text": r["message"]}]
    return body


@router.post(SPO, status_code=201, tags=["sonata"])
def sonata_order(
    user: UserDep,
    body: dict = Body(..., examples=[{"productOrderItem": [SONATA_ITEM]}]),
    buyerId: str | None = Query(default=None),  # noqa: N803
) -> dict:
    """Order. An item may reference an orderable quote item ({quoteId, quoteItemId}) instead of a product."""
    cid = _buyer(user, buyerId)
    actions, inputs = [], []
    with db.tx() as conn:
        for it in _items(body, "productOrderItem"):
            qi = it.get("quoteItem") or {}
            src = it
            if qi.get("quoteId"):
                q = standards.load(conn, "sonata_quote", cid, str(qi["quoteId"]))
                if q is None:
                    raise HTTPException(404, f"Quote {qi['quoteId']} not found.")
                match = [x for x in q["body"].get("quoteItem") or [] if str(x.get("id")) == str(qi.get("quoteItemId"))]
                if not match or match[0].get("state") != "approved.orderable":
                    raise HTTPException(422, "That quote item is not orderable.")
                if not it.get("product"):
                    src = {**it, "product": match[0]["product"]}
            try:
                a, i = standards.sonata_item(src)
            except StandardsError as e:
                raise _err(e) from None
            actions.append(a)
            inputs.append(i)
        doc_body = {**_scrub(body), "orderDate": standards.now_iso()}
        try:
            order = standards.place(conn, cid, actions, inputs, user.actor, "MEF LSO Sonata order")
        except StandardsError as e:
            return standards.store(
                conn, "sonata_order", cid, "rejected", {**doc_body, "state": "rejected", "note": [{"text": str(e)}]}
            )
        doc = standards.store(
            conn, "sonata_order", cid, "acknowledged", {**doc_body, "state": "acknowledged"}, order["id"]
        )
        d = standards.load(conn, "sonata_order", None, doc["id"])
    return _sonata_order_view(d, order)


@router.get(SPO, tags=["sonata"])
def sonata_orders(user: UserDep, buyerId: str | None = Query(default=None)) -> list[dict]:  # noqa: N803
    cid = _buyer(user, buyerId)
    with db.tx() as conn:
        docs = conn.execute(
            "SELECT * FROM connect_std_documents WHERE kind = 'sonata_order' AND customer_id = %s"
            " ORDER BY created_at DESC",
            (cid,),
        ).fetchall()
        orders = {
            o["id"]: o
            for o in conn.execute(
                "SELECT * FROM orders WHERE id = ANY(%s)", ([d["order_id"] for d in docs if d["order_id"]],)
            )
        }
    return [_sonata_order_view(d, orders.get(d["order_id"])) for d in docs]


@router.get(SPO + "/{doc_id}", tags=["sonata"])
def sonata_order_get(doc_id: str, user: UserDep, buyerId: str | None = Query(default=None)) -> dict:  # noqa: N803
    cid = None if user.role == "admin" and not buyerId else _buyer(user, buyerId)
    with db.tx() as conn:
        d = standards.load(conn, "sonata_order", cid, doc_id)
        if d is None:
            raise HTTPException(404, "Product order not found.")
        order = (
            conn.execute("SELECT * FROM orders WHERE id = %s", (d["order_id"],)).fetchone() if d["order_id"] else None
        )
    return _sonata_order_view(d, order)


def _sonata_ticket(n: dict) -> dict:
    t = standards.ticket(n, f"/api/v1{STT}")
    return {
        "id": t["id"],
        "href": t["href"],
        "description": n["description"] or n["title"],
        "issueType": "degraded" if n["severity"] != "critical" else "unavailable",
        "priority": {"critical": "high", "warning": "medium", "info": "low"}.get(n["severity"], "medium"),
        "severity": {"critical": "extensive", "warning": "significant", "info": "minor"}.get(n["severity"], "moderate"),
        "status": t["status"],
        "creationDate": t["creationDate"],
        "lastUpdate": t["lastUpdate"],
        "resolutionDate": t["resolutionDate"],
        "relatedEntity": t["relatedEntity"],
    }


@router.post(STT, status_code=201, tags=["sonata"])
def sonata_ticket_create(
    user: UserDep,
    body: dict = Body(
        ...,
        examples=[
            {
                "description": "Voice quality poor at Kingston",
                "severity": "significant",
                "priority": "high",
                "issueType": "degraded",
            }
        ],
    ),
) -> dict:
    if user.role != "customer":
        raise HTTPException(403, "Organisations raise Sonata trouble tickets.")
    sev = {"extensive": "critical", "significant": "warning", "moderate": "warning", "minor": "info"}
    with db.tx() as conn:
        row = create_ticket(
            conn,
            user,
            {
                "name": str(body.get("description") or "")[:200],
                "description": body.get("description"),
                "severity": sev.get(str(body.get("severity") or ""), "warning"),
                "relatedEntity": body.get("relatedEntity") or [],
            },
            "sonata",
        )
    return _sonata_ticket(row)


@router.get(STT + "/{ticket_id}", tags=["sonata"])
def sonata_ticket_get(ticket_id: int, user: UserDep) -> dict:
    with db.tx() as conn:
        rows = _tickets(conn, user, ticket_id)
    if not rows or rows[0]["kind"] != "trouble":
        raise HTTPException(404, "Trouble ticket not found.")
    return _sonata_ticket(rows[0])
