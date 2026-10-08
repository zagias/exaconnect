# ADR 0013: API keys, a Python SDK and a Terraform provider

Date: 2026-10-04. Status: accepted.

## Context

Step 6 of ExaConnect Fabric. Large organisations run their networks from
code: a cloud team that builds a VPC in Terraform wants the ExaConnect
circuit to it in the same plan, and operations teams script reports. Until
now the only way in was a portal session that lasts hours.

## Decision

**API keys** made by a person on the Account screen. A key acts as that
person, with their role and organisation and nothing more, so there is no
second permission model to get wrong. Keys start with `exa_` so they are
easy to spot in a leaked file and for secret scanners; only a SHA-256 hash
is stored, and the first 12 characters are kept to tell keys apart. Keys
may expire, can be revoked at once, and record when they were last used.
Creating and revoking are audited.

**A Python SDK** (`sdk/python`) that is a thin layer over the REST API: one
method per endpoint, JSON in and out, the API's own plain-English errors.
A thin client cannot drift from the API, and the API's contracts in
`docs/` stay the single description. It depends only on `httpx`; its
tests run against the real controller in the controller's test suite.

**A Terraform provider** (`terraform/`, Go, terraform-plugin-framework) for
what organisations want declared rather than clicked: cloud and site
circuits (including resilient pairs), firewall rules, port forwards,
internet breakout per site and traffic rules. Pre-shared keys are
write-only, as in the API. Acceptance tests run against a local controller.

## Consequences

- A leaked key is as powerful as its owner until revoked; the portal says
  so and shows each key's last use. Read-only keys can come later if asked
  for.
- The provider and SDK are not yet published to the Terraform Registry or
  PyPI; that needs ExaCarib accounts there (Dudley's call). Until then they
  install from the repository.
- Contract: `docs/automation-contract.md`.
- The provider's module needs Go 1.25.8 (the agent stays on its own
  `go.mod`). Current terraform-plugin-framework and terraform-plugin-testing
  require it, and older testing releases can no longer download Terraform
  (an expired signing key in hc-install 0.9.3 and earlier). CI reads the
  version from `terraform/go.mod`.
- The provider reads a list and filters it, because circuits, firewall
  rules, port forwards and traffic rules have no single-item GET yet. Empty
  strings for optional circuit fields (`a_site_id`, `class_name`) now mean
  "none" on create and update.
- Firewall rule order is set by `position` on create or the order
  endpoint, so `position` is read-only in Terraform; use `depends_on` where
  order matters.
