"""CommAI: ExaCarib's AI communications and customer-service platform.

It runs inside the controller and shares Connect's customers, users, roles,
audit log and API keys. See docs/adr/0016-commai-foundation.md.
"""

# Importing the modules registers their job handlers, channels, connectors and
# diagnostic checks: the AI agents (ADR 0019, registers "ai.respond"), the
# channels (ADR 0018), voice (ADR 0021) and automation (ADR 0020).
# Partners, white-label, regions and the developer platform: ADR 0025.
from . import (  # noqa: F401
    actions,
    ai,
    apipolicy,
    automation,
    branding,
    diagnostics,
    enterprise,
    golive,
    inbox,
    jobs,
    oauth,
    partners,
    regions,
    sandbox,
    usage,
    voice,
    webhooks,
)
from .channels import checks, countries, email, messaging, sms_routing, social, whatsapp_cloud, widget  # noqa: F401
