# ADR 0003: Agent mTLS terminates at an nginx proxy

Date: 2026-10-01 · Status: accepted

## Context

CLAUDE.md §3 asks for HTTPS with mutual TLS between agent and controller.
Uvicorn can require client certificates, but then the portal and the API docs
on the same listener need certificates too, and uvicorn does not pass the
verified certificate to the app in a standard way.

## Decision

* The controller runs its own small CA (`controller/exaconnect_controller/pki.py`),
  kept in the controller's data volume. Agents send a CSR at enrolment and get
  a one-year client certificate with CN = node name.
* An nginx container (`deploy/nginx`) listens on 8443 with the controller's
  server certificate and `ssl_verify_client optional`.
  * `/api/v1/ca.pem` and `/api/v1/enrol` pass through without a client
    certificate (enrolment is protected by the one-time token, and the agent
    pins the CA by SHA-256 fingerprint before it trusts it).
  * Everything under `/api/v1/agent/` returns 403 unless the client
    certificate verified, and nginx forwards the verify result and the
    certificate serial in headers.
* The controller trusts those headers only when the request also carries the
  shared `X-Exa-Proxy` secret from `.env`, and maps the serial to the node.
* The controller's own port (8000, portal and API) stays on loopback; only the
  proxy faces the agents.

## Consequences

* The portal and admin API keep simple bearer-token sessions on 8000.
* Rotating a node's certificate is re-enrolment for the MVP. Revocation is
  "delete or re-enrol the node": the serial no longer maps to a node, so its
  old certificate gets 403.
* In Phase B the proxy's 8443 is the only port to open on the cloud VM. The
  server certificate's SANs come from `EXA_TLS_SANS`.
