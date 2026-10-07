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
    channels_global,
    connectors_more,
    developer,
    enterprise,
    inbox_extras,
    languages,
    partners,
    quality,
    regions,
    selfservice,
    team,
    voice,
    voice_global,
)

for _m in (
    channels,
    ai,
    automation,
    voice,
    partners,
    regions,
    developer,
    enterprise,
    channels_global,
    voice_global,
    selfservice,
    languages,
    quality,
    team,
    connectors_more,
    inbox_extras,
):
    router.include_router(_m.router)
    if hasattr(_m, "public"):
        router.include_router(_m.public)

# Deprecated endpoints carry Deprecation and Sunset headers (ADR 0025).
from .. import apipolicy  # noqa: E402

apipolicy.apply(router)
