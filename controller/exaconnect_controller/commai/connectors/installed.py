"""Every connector the catalogue offers (ADR 0028).

Importing a connector module registers it (and declares its go-live
capability). Add one line here for each new connector file; the catalogue,
the portal's Integrations catalogue page and the docs read from the registry.
"""

# ruff: noqa: F401
from . import google_calendar, hubspot, simulated  # phase 2
