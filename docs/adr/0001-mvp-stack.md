# ADR 0001: MVP stack and repository layout

Date: 2026-09-30 · Status: accepted

## Context

CLAUDE.md §3 sets the stack. A first draft in this session used a Go
controller and five Parallels VMs; the brief arrived before anything was
committed and replaced it.

## Decision

Follow the brief: Go agent, Python/FastAPI controller, PostgreSQL 16 with
TimescaleDB, React/TypeScript/Vite portal, containerlab lab in one Ubuntu 24.04
arm64 VM, Docker Compose for the controller.

Details chosen where the brief is silent:

* The agent is its own Go module (`agent/go.mod`), standard library only for M0.
* The controller requires Python ≥ 3.11 so it runs on stock tooling, and CI and
  the container image use 3.12 as the brief asks.
* One lab node image (`lab/images/node`, based on `quay.io/frrouting/frr:10.2.1`)
  serves sites, the PoP and the underlay routers, so there is one thing to build.
* The controller joins the containerlab management network, so agents reach it
  by name (`controller:8000`) without port forwarding.

## Consequences

Two languages to maintain, as the brief intends. The single node image carries
FRR on the carrier routers, where it is unused; that costs disk, not behaviour.
