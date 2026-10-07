# shellcheck shell=bash
# Shared netem helpers for the lab scripts. Sourced, not executed.
#
# Each underlay router (carrier-a, carrier-b, sat) has eth1 toward the PoP and
# eth2/eth3 toward site-a/site-b. A packet crosses exactly one egress interface
# per direction, so shaping every interface shapes both directions.
set -euo pipefail
LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAB_NAME="${LAB_NAME:-exaconnect}"
STATE_DIR="$LAB_DIR/.state"
mkdir -p "$STATE_DIR"

# Settings: the environment wins, then what apply-profiles.sh last saved (so
# a fault or restore script keeps a GEO lab GEO), then profiles.env.
#   SAT_PROFILE=leo|geo      the satellite router's profile
#   LOSS_BOTH_WAYS=0|1       1 splits loss across both directions (ADR 0002)
env_sat="${SAT_PROFILE:-}"
env_both="${LOSS_BOTH_WAYS:-}"
unset SAT_PROFILE LOSS_BOTH_WAYS
if [[ -f $STATE_DIR/settings ]]; then
  # shellcheck source=/dev/null
  source "$STATE_DIR/settings"
fi
[[ -n $env_sat ]] && SAT_PROFILE=$env_sat
[[ -n $env_both ]] && LOSS_BOTH_WAYS=$env_both
# shellcheck source=lab/netem/profiles.env
source "$LAB_DIR/netem/profiles.env"
case "$SAT_PROFILE" in leo|geo) ;; *) echo "SAT_PROFILE must be leo or geo, not '$SAT_PROFILE'" >&2; exit 2 ;; esac
case "$LOSS_BOTH_WAYS" in 0|1) ;; *) echo "LOSS_BOTH_WAYS must be 0 or 1, not '$LOSS_BOTH_WAYS'" >&2; exit 2 ;; esac

# save_settings keeps the current settings for later scripts.
save_settings() {
  printf 'SAT_PROFILE=%s\nLOSS_BOTH_WAYS=%s\n' "$SAT_PROFILE" "$LOSS_BOTH_WAYS" > "$STATE_DIR/settings"
}

LINKS=(carrier-a carrier-b sat)

node() { echo "clab-$LAB_NAME-$1"; }

check_link() {
  local l
  for l in "${LINKS[@]}"; do [[ $l == "$1" ]] && return 0; done
  echo "unknown link '$1' (expected: ${LINKS[*]})" >&2
  exit 2
}

# profile <link> -> "delay jitter loss"
profile() {
  case "$1" in
    carrier-a) echo "$CARRIER_A" ;;
    carrier-b) echo "$CARRIER_B" ;;
    sat) if [[ ${SAT_PROFILE} == geo ]]; then echo "$SAT_GEO"; else echo "$SAT_LEO"; fi ;;
  esac
}

# current <link> -> "delay jitter loss" (last applied, or the profile)
current() { cat "$STATE_DIR/$1" 2>/dev/null || profile "$1"; }

# one_way_loss <round-trip loss %> -> the loss % per direction that gives it
# (1 - (1 - p)^2 = L, so p = 1 - sqrt(1 - L)); 100 stays 100.
one_way_loss() {
  awk "BEGIN{l=$1/100; if (l >= 1) {print 100} else {printf \"%.4f\", 100 * (1 - sqrt(1 - l))}}"
}

# shape <link> <rtt_delay_ms> <rtt_jitter_ms> <loss_pct>
# Loss goes once, toward the PoP (eth1), unless LOSS_BOTH_WAYS=1 splits it
# across both directions so the round trip still sees about <loss_pct>.
shape() {
  local link=$1 d=$2 j=$3 l=$4 n dev half_d half_j loss each
  check_link "$link"
  n=$(node "$link")
  half_d=$(awk "BEGIN{print $d/2}")
  half_j=$(awk "BEGIN{print $j/2}")
  each=$(one_way_loss "$l")
  for dev in eth1 eth2 eth3; do
    loss=0
    if [[ $LOSS_BOTH_WAYS == 1 ]]; then
      loss=$each
    elif [[ $dev == eth1 ]]; then
      loss=$l
    fi
    local args=(delay "${half_d}ms")
    awk "BEGIN{exit !($j > 0)}" && args+=("${half_j}ms" distribution normal)
    args+=(loss "${loss}%" limit 10000)
    docker exec "$n" tc qdisc replace dev "$dev" root netem "${args[@]}"
  done
  echo "$d $j $l" > "$STATE_DIR/$link"
  printf '%-10s rtt %6sms  jitter %5sms  loss %5s%%\n' "$link" "$d" "$j" "$l"
}

# ramp <link> <field: delay|loss> <from> <to> <seconds>
ramp() {
  local link=$1 field=$2 from=$3 to=$4 secs=$5 step=2 i steps v d j l
  read -r d j l < <(current "$link")
  steps=$(( secs / step )); (( steps < 1 )) && steps=1
  for i in $(seq 1 "$steps"); do
    v=$(awk "BEGIN{printf \"%.2f\", $from + ($to - $from) * $i / $steps}")
    case "$field" in
      delay) shape "$link" "$v" "$j" "$l" ;;
      loss) shape "$link" "$d" "$j" "$v" ;;
    esac
    (( i < steps )) && sleep "$step"
  done
  return 0
}
