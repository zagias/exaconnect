"""CommAI API under /api/v1/commai (ADR 0016). Each module owns its router."""

from fastapi import APIRouter, Depends

from ...api.deps import require_product
from . import inbox, platform

router = APIRouter(prefix="/commai")
# Signed-in endpoints need the organisation to hold the CommAI plan (ADR 0023).
# Public endpoints (the website widget, provider webhooks) and the live socket,
# which signs in by itself and checks the plan there, sit outside it.
_planned = APIRouter(dependencies=[Depends(require_product("commai"))])
_planned.include_router(inbox.router)
_planned.include_router(platform.router)
router.include_router(inbox.live)

# Module routers. Each module's file defines `router` (and optionally `public`
# for unauthenticated endpoints such as the website widget and provider webhooks).
from . import (  # noqa: E402
    ai,
    automation,
    channels,
    voice,
)

for _m in (channels, ai, automation, voice):
    _planned.include_router(_m.router)
    if hasattr(_m, "public"):
        router.include_router(_m.public)
router.include_router(_planned)
