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
    # Seconds between routing engine passes; 0 turns the background loop off (tests).
    routing_interval_s: float = field(default_factory=lambda: float(_env("EXA_ROUTING_INTERVAL_S", "10")))
    # Hurricane watch: NHC's active storms feed, checked every interval; "" turns it off.
    nhc_url: str = field(default_factory=lambda: _env("EXA_NHC_URL", "https://www.nhc.noaa.gov/CurrentStorms.json"))
    nhc_interval_s: float = field(default_factory=lambda: float(_env("EXA_NHC_INTERVAL_S", "900")))
    # Disaster watch (ADR 0009): earthquakes, GDACS multi-hazard alerts and tsunami
    # messages, checked every interval. Any URL set to "" turns that feed off.
    usgs_url: str = field(
        default_factory=lambda: _env(
            "EXA_USGS_URL", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
        )
    )
    gdacs_url: str = field(
        default_factory=lambda: _env("EXA_GDACS_URL", "https://www.gdacs.org/gdacsapi/api/events/geteventlist/MAP")
    )
    tsunami_urls: str = field(
        default_factory=lambda: _env(
            "EXA_TSUNAMI_URLS",
            "https://www.tsunami.gov/events/xml/PHEBAtom.xml,https://www.tsunami.gov/events/xml/PAAQAtom.xml",
        )
    )
    hazard_interval_s: float = field(default_factory=lambda: float(_env("EXA_HAZARD_INTERVAL_S", "600")))
    # "Ask your network": any OpenAI-compatible endpoint. Off until a key is set.
    llm_api_key: str = field(default_factory=lambda: _env("EXA_LLM_API_KEY"), repr=False)
    llm_base_url: str = field(
        default_factory=lambda: _env("EXA_LLM_BASE_URL", "https://api.deepinfra.com/v1/openai").rstrip("/")
    )
    llm_model: str = field(default_factory=lambda: _env("EXA_LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash"))
    llm_questions_per_hour: int = field(default_factory=lambda: int(_env("EXA_LLM_QUESTIONS_PER_HOUR", "30")))


def get_settings() -> Settings:
    return Settings()
