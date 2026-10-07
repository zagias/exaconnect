"""REST API under /api/v1."""

from fastapi import APIRouter

from .. import __version__
from . import (
    admin,
    agent,
    ai,
    auth,
    billing,
    circuits,
    internet,
    metering,
    ordering,
    protection,
    scim,
    security,
    settings,
    sso,
    traffic,
    views,
)

router = APIRouter()


@router.get("/version", tags=["ops"])
def version() -> dict[str, str]:
    return {"service": "exaconnect-controller", "version": __version__}


router.include_router(auth.router)
router.include_router(admin.router)
router.include_router(agent.router)
router.include_router(views.router)
router.include_router(settings.router)
router.include_router(metering.router)
router.include_router(billing.router)
router.include_router(ai.router)
router.include_router(traffic.router)
router.include_router(circuits.router)
router.include_router(internet.router)
router.include_router(ordering.router)
router.include_router(protection.router)
router.include_router(security.router)
router.include_router(sso.router)
router.include_router(scim.router)

from ..commai.api import router as commai_router  # noqa: E402

router.include_router(commai_router)
