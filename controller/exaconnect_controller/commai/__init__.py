"""CommAI: ExaCarib's AI communications and customer-service platform.

It runs inside the controller and shares Connect's customers, users, roles,
audit log and API keys. See docs/adr/0016-commai-foundation.md.
"""

# Importing the modules registers their job handlers, channels, connectors and
# diagnostic checks: the AI agents (ADR 0019, registers "ai.respond"), the
# channels (ADR 0018), voice (ADR 0021) and automation (ADR 0020).
from . import (  # noqa: F401  # noqa: F401  (ADR 0026)
    actions,
    ai,
    automation,
    csat,
    diagnostics,
    golive,
    i18n,
    inbox,
    jobs,
    team,
    usage,
    voice,
    webhooks,
)
from .channels import checks, email, messaging, widget  # noqa: F401
