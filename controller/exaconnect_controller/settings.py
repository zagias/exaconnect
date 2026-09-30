"""Controller settings, read from the environment (see /.env.example). Never hard-code secrets."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("EXA_DATABASE_URL", ""))
    environment: str = field(default_factory=lambda: os.environ.get("EXA_ENV", "dev"))


def get_settings() -> Settings:
    return Settings()
