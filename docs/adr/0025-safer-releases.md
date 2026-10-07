# ADR 0025: Health-checked releases with automatic rollback

Date: 2026-10-07 · Status: accepted

## Context

The lab runner deploys every push to the work branch straight onto the server that
also serves connect.exacarib.com. A commit that broke sign-in or the portal stayed
live until someone noticed, and going back meant pushing a revert and waiting for a
full lab run. Main is not the deploy branch yet (PR #1 waits on Dudley).

## Decision

`deploy/release/release.sh` (`make release`, used by `lab/scripts/up.sh`):

1. Tags the running images (`exaconnect-controller`, `exaconnect-web`) as
   `rel-<commit>` before building anything.
2. Takes an encrypted database backup with deploy/backup when
   `/etc/exaconnect/backup.env` exists on the server.
3. Builds and starts this commit: database, controller, agent gateway, public portal.
4. Checks it: controller health, an admin sign-in and the Overview API, the public
   site and portal, the agent gateway, and that every agent reporting before the
   release reports to the new version.
5. If a check fails, re-tags the previous images and starts them without a build,
   checks again and records the release as rolled back, with the reason.

`make rollback [TO=<commit>]` goes back by hand to the previous release or any kept
one; `make releases` lists them. The last five releases' images are kept.

Every release and rollback is a row in `releases`, shown in Admin > Releases with
the running commit (baked into the image as `EXA_BUILD_COMMIT`).

## Consequences

* A bad commit is live only for the minutes the build and checks take, then the
  last good release is back, and the lab run fails loudly.
* Rollback swaps code, not data. The schema only adds tables and columns, so the
  previous code runs on the newer schema; the backup taken before the release is
  there if data must go back too (`deploy/backup/restore.sh`).
* Releasing from main later is a runner setting (`EXA_RUNNER_BRANCH=main`) once
  PR #1 is merged; nothing else changes.
* A broken lab check does not roll back a release: the checks above are about
  serving people; lab checks report test failures.
