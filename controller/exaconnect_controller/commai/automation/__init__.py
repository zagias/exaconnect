"""Jibsy automation (ADR 0020): integration setup, the Google Calendar and
HubSpot connectors, the workflow engine, plain-English workflows and starter
packs, AI onboarding, the platform assistant, and outcome reports with cost
controls.

Importing this package registers its job handlers, events and diagnostic checks.
"""

from . import assistant, integrations, onboarding, reports, workflows  # noqa: F401
