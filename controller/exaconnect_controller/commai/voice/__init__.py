"""Jibsy voice (ADR 0021, stage 5 in ADR 0033): phone system, provisioning,
billing, numbers by country, porting, emergency addresses, carriers and fraud.

Importing the package registers the voice job handlers and event types.
"""

from . import (  # noqa: F401
    billing,
    bulk,
    carriers,
    config,
    countries,
    emergency,
    fraud,
    freeswitch,
    porting,
    provisioning,
    selfservice,
)
