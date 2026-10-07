"""REST API under /api/v1."""

from fastapi import APIRouter, Depends

from .. import __version__
from . import (
    admin,
    agent,
    ai,
    auth,
    circuits,
    deps,
    internet,
    inventory_admin,
    metering,
    ordering,
    orgs,
    protection,
    releases,
    scim,
    security,
    settings,
    sso,
    traffic,
    views,
)

router = APIRouter()
# Connect's screens and API need the organisation to hold the Connect plan
# (ADR 0023). Sign-in, organisations, SSO and SCIM are shared by both plans, and
# the agent router authenticates nodes, not people.
_connect = [Depends(deps.require_product("connect"))]


@router.get("/version", tags=["ops"])
def version() -> dict[str, str]:
    return {"service": "exaconnect-controller", "version": __version__, "commit": releases.build_commit()}


router.include_router(auth.router)
router.include_router(admin.router, dependencies=_connect)
router.include_router(inventory_admin.router, dependencies=_connect)
router.include_router(agent.router)
router.include_router(views.router, dependencies=_connect)
router.include_router(settings.shared)
router.include_router(settings.router, dependencies=_connect)
router.include_router(metering.router, dependencies=_connect)
router.include_router(ai.router, dependencies=_connect)
router.include_router(traffic.router, dependencies=_connect)
router.include_router(circuits.router, dependencies=_connect)
router.include_router(internet.router, dependencies=_connect)
router.include_router(ordering.router, dependencies=_connect)
router.include_router(protection.router, dependencies=_connect)
router.include_router(releases.router)
router.include_router(security.router, dependencies=_connect)
router.include_router(sso.router)
router.include_router(orgs.router)
router.include_router(scim.router)

from ..commai.api import router as commai_router  # noqa: E402

router.include_router(commai_router)
