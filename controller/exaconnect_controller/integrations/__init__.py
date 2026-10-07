"""Connect integrations (ADR 0026): the event catalogue, one publish point,
the standards layer (CloudEvents webhooks, Prometheus, OTLP, syslog, SNMP,
RESTCONF, TMF and MEF LSO Sonata) and connectors as thin profiles on top."""

from . import delivery, publish  # noqa: F401 - registers the job handlers
