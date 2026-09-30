"""REST API under /api/v1. Inventory, enrolment and desired state arrive in M2."""

from fastapi import APIRouter

from .. import __version__

router = APIRouter()


@router.get("/version", tags=["ops"])
def version() -> dict[str, str]:
    return {"service": "exaconnect-controller", "version": __version__}
