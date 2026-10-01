"""AI features beyond SLA routing (ADR 0006).

- storms: NHC hurricane advisories near a customer's sites suggest Storm Mode.
- billshock: forecast each link's month-end 95th percentile against its commit.
- anomaly: flag a carrier path that is worse than usual for the hour.
- ask: "Ask your network", an LLM answering questions from the customer's own data.

The first three run in the controller with no external AI service. Each
raises insights (one open row per key) that the portal and the LLM read.
"""
