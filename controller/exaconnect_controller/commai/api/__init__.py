"""CommAI API under /api/v1/commai (ADR 0016). Each module owns its router."""

from fastapi import APIRouter

from . import golive, inbox, platform

router = APIRouter(prefix="/commai")
router.include_router(inbox.router)
router.include_router(inbox.live)
router.include_router(platform.router)
router.include_router(golive.router)

# Module routers. Each module's file defines `router` (and optionally `public`
# for unauthenticated endpoints such as the website widget and provider webhooks).
from . import (  # noqa: E402
    ai,
    automation,
    channels,
    voice,
)

for _m in (channels, ai, automation, voice):
    router.include_router(_m.router)
    if hasattr(_m, "public"):
        router.include_router(_m.public)
