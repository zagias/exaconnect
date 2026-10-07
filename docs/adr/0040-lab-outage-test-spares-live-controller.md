# 0040: The lab's controller-outage test never stops the live controller

Date: 7 October 2026. Status: accepted.

## Context

The lab runs on the same VPS as the live site (connect.exacarib.com). Demo
step 7 (`lab/ci/checks/m7-outage.sh`) stopped the controller and its proxy for
about two minutes to show that agents keep forwarding without it. Because the
lab runner runs every check on each merge, that took the live portal down
(signing in answered 502) and cut real sites off the agent gateway on every
merge.

## Options

1. **A second controller and database just for the lab**, with the lab
   agents enrolled against it. It's the cleanest separation, but it doubles the
   controller and TimescaleDB load on a host that is already overloaded and
   failing timing checks. It also means re-pointing every lab check, which
   reads the database and API of the one controller.
2. **Run step 7 only off the live host** (CI or a local lab). That's safe, but
   the step would no longer be tested where the rest of the lab runs.
3. **Cut the agents off from the controller instead of stopping it.** Each
   lab agent gets an `unreachable` route to the controller's lab address
   (172.30.0.5). To an agent, that's the same as a dead controller: its polls
   and telemetry fail, it logs `controller silent`, holds the last good state,
   and fails over on BFD by itself. Deleting the route brings the controller
   back, and the agents reconcile.

## Decision

Option 3. It tests exactly what demo step 7 promises from the sites' side,
adds no load and costs nothing. The live controller, the portal, the public
agent gateway (8443) and real sites carry on throughout. The check now also
confirms that the live controller answered during the test. A trap removes the
routes whatever happens, so a failed run never leaves the agents cut off.

If the lab moves to its own host, stopping the controller for real is fine
again, and option 1 becomes cheap.
