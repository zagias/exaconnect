"""CommAI: ExaCarib's AI communications and customer-service platform.

It runs inside the controller and shares Connect's customers, users, roles,
audit log and API keys. See docs/adr/0016-commai-foundation.md.
"""

# Importing the modules registers their job handlers, channels and connectors.
from . import actions, inbox, jobs, webhooks  # noqa: F401
