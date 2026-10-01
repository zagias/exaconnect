#!/usr/bin/env bash
# One lab runner pass, started every minute by exaconnect-lab-runner.timer.
# When the work branch has a new head, it checks it out, runs `make lab-ci`
# and pushes a cleaned log to the lab-results branch.
set -uo pipefail
REPO=${EXA_RUNNER_REPO:?}
BRANCH=${EXA_RUNNER_BRANCH:?}
KEY=${EXA_RUNNER_KEY:?}
STATE=${EXA_RUNNER_STATE:?}
RESULTS=$STATE/results
REMOTE=git@github.com:zagias/exaconnect.git
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes"

exec 9>"$STATE/lock"
flock -n 9 || exit 0
cd "$REPO" || exit 1

git fetch -q origin "$BRANCH" || exit 0
head=$(git rev-parse "origin/$BRANCH")
[[ $head == "$(cat "$STATE/last" 2>/dev/null)" ]] && exit 0

# The lab host runs exactly what is on the branch. .env, lab/.state and bin/
# are git-ignored, so secrets and agent state survive the reset.
git checkout -q -B "$BRANCH" "origin/$BRANCH"
git reset -q --hard "origin/$BRANCH"

log=$STATE/run.log
started=$(date -u +%FT%TZ)
timeout 80m make -s lab-ci >"$log" 2>&1
rc=$?
finished=$(date -u +%FT%TZ)
echo "$head" >"$STATE/last"

# Clean the log before it leaves the host: drop lines that look like they
# carry a secret, mask key-shaped strings and this host's public addresses.
mapfile -t addrs < <({
  hostname -I 2>/dev/null | tr ' ' '\n'
  curl -fsS --max-time 5 https://ifconfig.me 2>/dev/null
  echo
} | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' |
  grep -vE '^(10|127|172\.(1[6-9]|2[0-9]|3[01])|192\.168|100\.64)\.' | sort -u)
clean() {
  local s a
  s=$(grep -viE '(password|secret|api_key|apikey|token|private)[^ ]*[=:]' |
    sed -E 's/sk-[A-Za-z0-9_-]{8,}/[redacted]/g; s#[A-Za-z0-9+/]{43}=#[key]#g' | tail -n 6000)
  for a in "${addrs[@]}"; do s=${s//$a/[host]}; done
  printf '%s\n' "$s"
}

if [[ ! -d $RESULTS/.git ]]; then
  rm -rf "$RESULTS"
  git clone -q --no-checkout "$REMOTE" "$RESULTS" || exit 1
  git -C "$RESULTS" checkout -q lab-results 2>/dev/null || git -C "$RESULTS" checkout -q --orphan lab-results
fi
cd "$RESULTS" || exit 1
git fetch -q origin lab-results 2>/dev/null && git reset -q --hard origin/lab-results
git config user.name "ExaConnect lab runner"
git config user.email "lab-runner@exaconnect.invalid"
short=${head:0:7}
mkdir -p runs
clean <"$log" >"runs/$short.log"
cp "runs/$short.log" latest.log
summary=$(grep -E '^(ok|FAIL|SKIP) ' "runs/$short.log" | awk '{print $1}' | sort | uniq -c |
  awk '{printf "%s=%s ", $2, $1}')
jq -n --arg commit "$head" --arg started "$started" --arg finished "$finished" --argjson exit "$rc" \
  --arg summary "$summary" '{commit:$commit, started:$started, finished:$finished, exit:$exit, summary:$summary}' \
  >latest.json
cp latest.json "runs/$short.json"
# Keep the last 50 runs.
find runs -name '*.log' -printf '%T@ %p\n' | sort -rn | tail -n +51 | cut -d' ' -f2- |
  while read -r f; do rm -f "$f" "${f%.log}.json"; done
git add -A
git commit -q -m "lab run $short: exit $rc ${summary}" && git push -q origin HEAD:lab-results
