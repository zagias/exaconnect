"""FastAPI application factory. Run with: uvicorn exaconnect_controller.main:app"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__, db, pki
from .api import router as api_router
from .settings import Settings, get_settings

log = logging.getLogger("exaconnect")


def bootstrap_admin(settings: Settings) -> None:
    """Create the first admin from EXA_ADMIN_EMAIL / EXA_ADMIN_PASSWORD if there is none."""
    if not settings.admin_email or not settings.admin_password:
        return
    from . import audit
    from .security import hash_password

    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone():
            return
        conn.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (%s, %s, 'admin')",
            (settings.admin_email, hash_password(settings.admin_password)),
        )
        audit.record(conn, "system", "user.create", settings.admin_email, detail={"role": "admin"})
    log.info("created admin user %s", settings.admin_email)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks: list[asyncio.Task] = []
        if settings.database_url:
            db.init(settings.database_url)
            bootstrap_admin(settings)
            if settings.routing_interval_s > 0:
                from .routing import runner

                tasks.append(asyncio.create_task(runner.loop(settings.routing_interval_s)))
                from .ai import runner as ai_runner

                tasks.append(asyncio.create_task(ai_runner.loop(settings)))
        yield
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        db.close()

    app = FastAPI(
        title="ExaConnect controller",
        version=__version__,
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.ca = pki.load_or_create(settings.data_dir, [s.strip() for s in settings.tls_sans.split(",") if s.strip()])

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(api_router, prefix="/api/v1")
    return app


def __getattr__(name: str):
    # `uvicorn exaconnect_controller.main:app` builds the app on first access,
    # so importing this module (tests, seed) has no side effects.
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
