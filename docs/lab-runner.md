# Lab runner

The lab runner lets the lab host test new commits without anyone at the
keyboard. Dudley approved it on 2026-10-01.

- A systemd timer (`exaconnect-lab-runner.timer`) runs `lab/runner/run.sh` every minute.
- When the work branch has a new head, the runner checks it out in the repo on
  the lab host and runs `make lab-ci` (`lab/ci/run.sh`). That redeploys the lab
  only if its definition changed, rebuilds the controller and agent, re-seeds,
  waits for routing, and runs `lab/scripts/check-routing.sh` and every
  executable `lab/ci/checks/*.sh`.
- The log is cleaned and pushed to the `lab-results` branch: `latest.log`,
  `latest.json` (commit, exit code, ok/FAIL counts) and `runs/<commit>.*` for the
  last 50 runs.

## What leaves the host

Only the cleaned log. Before pushing, the runner drops every line that looks
like it carries a password, secret, token or key, masks `sk-…` and WireGuard-key
shaped strings, and replaces the host's public addresses with `[host]`. The
repository is public, so treat `lab-results` as public too; making the
repository private is a one-click change in GitHub settings.

## Access it needs

- A deploy key that works for this repository only (`/root/.ssh/exaconnect_lab_runner`).
  GitHub deploy keys are read-only or read-write; the runner needs write to
  push `lab-results`, and its script pushes nothing else.
- It runs as root because containerlab, WireGuard and Docker need it.
- It executes whatever is pushed to the work branch. Only people with write
  access to the repository can push there.

## Install, check, remove

```
cd /root/exaconnect && git pull && sudo lab/runner/install.sh   # prints the deploy key to add on GitHub
systemctl list-timers exaconnect-lab-runner.timer               # next run
journalctl -u exaconnect-lab-runner -n 50                       # runner output
sudo lab/runner/install.sh --remove                             # stop and delete it
```

The installer can also store the DeepInfra key for "Ask your network": it asks
at a hidden prompt and writes `EXA_LLM_API_KEY` to `.env` only.
