"""REST API under /api/v1."""

from fastapi import APIRouter

from .. import __version__
from . import admin, agent, ai, auth, metering, settings, views

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
router.include_router(ai.router)
