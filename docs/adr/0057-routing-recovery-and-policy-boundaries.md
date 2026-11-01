# 0057 — Routing recovery and policy boundaries

Status: accepted, 1 November 2026

The controller's desired map and a file left by an earlier boot do not prove
what the current dataplane applied. A failed policy-rule batch can leave a
partial result; a controller returning 204 must not prevent a local retry.

The agent retries a failed cached map on its normal steering poll, including
when the controller is unavailable. It advances its SLA-change signature and
applied steering version only after a successful apply. While steering has an
apply error it omits actual steering from telemetry, allowing earlier actual
observations to age rather than reporting an unverified state as fresh.
Restoring last-good desired state follows the same rule: retry locally while
no current configuration has succeeded, and never acknowledge the file's
version just because it exists.

A successful FRR BFD read replaces the peer snapshot, removing retired
sessions. Read failure preserves the previous snapshot. For a tunnel with
multiple sessions, a known failure always outranks an initialising session;
Go map iteration order cannot change whether the tunnel is usable.

The controller persists the evaluation timestamp in steering.updated_at.
After a gap longer than the telemetry freshness allowance, or a backwards
clock jump, it restarts the preferred-path recovery clock and breach
persistence. Unobserved time does not establish five minutes of good health.
Hard-down failover still ignores the hold time and acts immediately.

Satellite remains established in BGP. Therefore a class with an excluded
satellite path must pause when no permitted path is usable, instead of
falling through to BGP and reaching the excluded path. This covers satellite
outside Storm Mode and GEO for real-time traffic, including mixed LEO/GEO
sites. Permitted LEO or business GEO in Storm Mode remains available.

InternetOff also restricts classification to corporate destinations. Its
desired state carries the authoritative corporate prefixes for a forwarding
guard independent of steering-map arrival order. The guard denies public
LAN traffic in both directions before established-connection acceptance;
the host's own management traffic remains outside the forwarding policy.

Unit fault tests cover retry after a failed rule batch, controller errors,
startup restoration failure, stale BFD sessions and deterministic BFD
aggregation. Routing tests cover controller gaps, emergency failover,
satellite exclusions and InternetOff map scope. Network namespace/lab tests
remain necessary to establish packet-loss and convergence bounds on Linux;
unit tests do not establish the production three-second failover target.
