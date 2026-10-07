"""Every connector the catalogue offers (ADR 0028).

Importing a connector module registers it (and declares its go-live
capability). Add one line here for each new connector file; the catalogue,
the portal's Integrations catalogue page and the docs read from the registry.
"""

# ruff: noqa: F401, I001
from . import google_calendar, hubspot, simulated  # phase 2
from . import zendesk  # helpdesk (ADR 0029)
from . import freshdesk  # helpdesk (ADR 0029)
from . import servicenow  # helpdesk (ADR 0029)
from . import slack  # team chat (ADR 0029)
from . import teams  # team chat (ADR 0029)
from . import shopify  # commerce (ADR 0029)
from . import stripe  # payments (ADR 0029)
