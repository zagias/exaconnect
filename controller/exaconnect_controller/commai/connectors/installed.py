"""Every connector the catalogue offers (ADR 0028).

Importing a connector module registers it (and declares its go-live
capability). Add one line here for each new connector file; the catalogue,
the portal's Integrations catalogue page and the docs read from the registry.
"""

# ruff: noqa: F401, I001
from . import google_calendar, hubspot, simulated  # phase 2
from . import caldav, carddav  # standards (ADR 0028)
from . import salesforce  # CRM (ADR 0028)
from . import dynamics365, pipedrive, zoho_crm  # CRM (ADR 0028)
from . import gmail, microsoft365  # email and Outlook calendar (ADR 0028)
from . import calendly  # scheduling (ADR 0028)
from . import rest_generic  # a business's own REST API from OpenAPI (ADR 0028)
