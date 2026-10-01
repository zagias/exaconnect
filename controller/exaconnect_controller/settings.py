"""Controller settings, read from the environment (see /.env.example). Never hard-code secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env("EXA_DATABASE_URL"))
    environment: str = field(default_factory=lambda: _env("EXA_ENV", "dev"))
    data_dir: str = field(default_factory=lambda: _env("EXA_DATA_DIR", "/data"))
    # Shared with the TLS proxy; agent endpoints only trust requests carrying it.
    proxy_secret: str = field(default_factory=lambda: _env("EXA_PROXY_SECRET"))
    # How agents reach the controller (shown with enrolment tokens).
    agent_url: str = field(default_factory=lambda: _env("EXA_AGENT_URL", "https://172.30.0.5:8443"))
    # Extra names/IPs for the agent-facing TLS certificate, comma separated.
    tls_sans: str = field(default_factory=lambda: _env("EXA_TLS_SANS", "controller,localhost,127.0.0.1,172.30.0.5"))
    admin_email: str = field(default_factory=lambda: _env("EXA_ADMIN_EMAIL"))
    admin_password: str = field(default_factory=lambda: _env("EXA_ADMIN_PASSWORD"))
    session_hours: int = field(default_factory=lambda: int(_env("EXA_SESSION_HOURS", "12")))


def get_settings() -> Settings:
    return Settings()
