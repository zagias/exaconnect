"""Every connector the catalogue offers (ADR 0034).

Importing a connector module registers it (and declares its go-live
capability). Add one line here for each new connector file; the catalogue,
the portal's Integrations catalogue page and the docs read from the registry.
"""

# ruff: noqa: F401, I001
from . import google_calendar, hubspot, simulated  # phase 2
from . import zendesk  # helpdesk (ADR 0035)
from . import freshdesk  # helpdesk (ADR 0035)
from . import servicenow  # helpdesk (ADR 0035)
from . import slack  # team chat (ADR 0035)
from . import teams  # team chat (ADR 0035)
from . import shopify  # commerce (ADR 0035)
from . import stripe  # payments (ADR 0035)
from . import google_drive  # knowledge (ADR 0035)
from . import onedrive  # knowledge (ADR 0035)
from . import caldav, carddav  # standards (ADR 0034)
from . import salesforce  # CRM (ADR 0034)
from . import dynamics365, pipedrive, zoho_crm  # CRM (ADR 0034)
from . import gmail, microsoft365  # email and Outlook calendar (ADR 0034)
from . import calendly  # scheduling (ADR 0034)
from . import rest_generic  # a business's own REST API from OpenAPI (ADR 0034)
