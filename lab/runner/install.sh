#!/usr/bin/env bash
# Installs the lab runner on the lab host (once, as root, from the repo):
#   sudo lab/runner/install.sh
# Dudley approved this on 2026-10-01 so the lab can be tested unattended.
# The runner checks the work branch every minute. When the branch moves it
# rebuilds, redeploys and tests the lab (make lab-ci) and pushes a cleaned log
# to the lab-results branch. See docs/lab-runner.md. Remove it with:
#   sudo lab/runner/install.sh --remove
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO=$(pwd)
BRANCH=${EXA_RUNNER_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}
KEY=/root/.ssh/exaconnect_lab_runner
STATE=/var/lib/exaconnect-runner
UNIT=exaconnect-lab-runner

[[ $(id -u) == 0 ]] || { echo "run as root (sudo)"; exit 1; }

if [[ ${1:-} == --remove ]]; then
  systemctl disable --now "$UNIT.timer" 2>/dev/null || true
  rm -f "/etc/systemd/system/$UNIT.service" "/etc/systemd/system/$UNIT.timer"
  systemctl daemon-reload
  rm -rf "$STATE" "$KEY" "$KEY.pub"
  echo "Lab runner removed. Also delete the 'lab runner' deploy key on GitHub."
  exit 0
fi

install -d -m 700 /root/.ssh "$STATE"

# 1. A deploy key for this repository only. On GitHub it needs write access,
#    and the runner only ever pushes the lab-results branch.
[[ -f $KEY ]] || ssh-keygen -q -t ed25519 -N "" -C "exaconnect-lab-runner" -f "$KEY"
grep -q "^github.com " /root/.ssh/known_hosts 2>/dev/null ||
  ssh-keyscan -t ed25519 github.com >>/root/.ssh/known_hosts 2>/dev/null

# 2. Optional: the DeepInfra key for "Ask your network", typed at a hidden
#    prompt and kept only in .env (git-ignored, mode 600). Enter skips it.
lab/scripts/init-env.sh >/dev/null
if [[ -t 0 ]] && ! grep -q '^EXA_LLM_API_KEY=.' .env; then
  read -r -s -p "DeepInfra API key for Ask your network (Enter to skip): " llm_key
  echo
  if [[ -n ${llm_key:-} ]]; then
    sed -i '/^EXA_LLM_API_KEY=/d' .env
    printf 'EXA_LLM_API_KEY=%s\n' "$llm_key" >>.env
    echo "Saved to .env."
  fi
  unset llm_key
fi

# 3. The systemd service and timer.
cat >"/etc/systemd/system/$UNIT.service" <<EOF
[Unit]
Description=ExaConnect lab runner (tests new commits on $BRANCH)
After=network-online.target docker.service

[Service]
Type=oneshot
Environment=EXA_RUNNER_REPO=$REPO EXA_RUNNER_BRANCH=$BRANCH EXA_RUNNER_KEY=$KEY EXA_RUNNER_STATE=$STATE
ExecStart=$REPO/lab/runner/run.sh
TimeoutStartSec=90min
EOF
cat >"/etc/systemd/system/$UNIT.timer" <<EOF
[Unit]
Description=Check for new ExaConnect commits every minute

[Timer]
OnBootSec=2min
OnUnitInactiveSec=60s

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now "$UNIT.timer" >/dev/null

echo
echo "Lab runner installed for branch $BRANCH."
echo "Now add this deploy key on GitHub: repo Settings > Deploy keys > Add deploy key,"
echo "title 'lab runner', paste the line below, tick 'Allow write access', Add key."
echo
cat "$KEY.pub"
