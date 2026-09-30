"""FastAPI application factory. Run with: uvicorn exaconnect_controller.main:app"""

from fastapi import FastAPI

from . import __version__
from .api import router as api_router
from .settings import get_settings


def create_app() -> FastAPI:
    app = FastAPI(
        title="ExaConnect controller",
        version=__version__,
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
    )

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(api_router, prefix="/api/v1")
    app.state.settings = get_settings()
    return app


app = create_app()
