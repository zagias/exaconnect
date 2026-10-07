"""QR code images, drawn on the server (ADR 0033).

The portal posts a softphone sign-in link (shown once) and gets an SVG to
scan. The text is not stored or logged. POST, so the link never sits in a
URL or a server log.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ...api.deps import UserDep
from .. import access, qr

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: qr"])


class QRIn(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    label: str = Field(default="QR code", max_length=100)


@router.post("/qr", response_class=Response, responses={200: {"content": {"image/svg+xml": {}}}})
def qr_svg(customer_id: str, body: QRIn, user: UserDep) -> Response:
    access.check(user, customer_id, "commai:read")
    try:
        out = qr.svg(body.text, label=body.label)
    except qr.QRError as e:
        raise HTTPException(422, str(e)) from e
    return Response(out, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
