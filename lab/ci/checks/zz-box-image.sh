#!/usr/bin/env bash
# shellcheck disable=SC2031  # REPO and fail are set only by lib.sh, in this shell
# The ExaCarib box image (ADR 0047), end to end: the image published on GitHub for
# this code (.github/workflows/box-image.yml) boots in a virtual machine on this host,
# and deploy/uat/box-console.mjs sets it up the way someone at a branch would: the
# setup code from the box's screen, the ports, joining Connect with the portal's
# box setup code. Then the portal shows the box, and the controller has its ports.
# Named zz- to run last: it may wait for GitHub to finish building the image.
# shellcheck source=lab/ci/lib.sh
source "$(dirname "$0")/../lib.sh"
# shellcheck source=lab/ci/box-lib.sh
source "$(dirname "$0")/../box-lib.sh"

echo "-- box image"
[[ -s deploy/public/site.env ]] || { skip "no deploy/public/site.env"; exit 0; }
# shellcheck disable=SC1091
host=$(. deploy/public/site.env && echo "${EXA_PUBLIC_HOST:-}")
[[ -n $host ]] || { skip "no public address (EXA_PUBLIC_HOST)"; exit 0; }

# 1. The image release for this code: built from a commit between the last change to
#    the box's files and this one. GitHub may still be building it.
paths=(agent deploy/box deploy/systemd/exa-agent.service deploy/systemd/exa-box.service .github/workflows/box-image.yml)
want=$(git -C "$REPO" log -1 --format=%H -- "${paths[@]}")
api_gh="https://api.github.com/repos/zagias/exaconnect/releases?per_page=20"
rel=""
for _ in $(seq 1 50); do
  while read -r tag commit; do
    if git -C "$REPO" merge-base --is-ancestor "$want" "$commit" 2>/dev/null &&
      git -C "$REPO" merge-base --is-ancestor "$commit" HEAD 2>/dev/null; then
      rel=$tag
      break
    fi
  done < <(curl -fsS --max-time 20 "$api_gh" | jq -r '.[] | select(.tag_name | startswith("box-v")) |
      "\(.tag_name) \(.body | capture("Commit: (?<c>[0-9a-f]{40})").c // "")"' 2>/dev/null)
  [[ -n $rel ]] && break
  sleep 30
done
if [[ -z $rel ]]; then
  bad "no box image release on GitHub for this code (wanted one built after ${want:0:7})"
  exit 1
fi
ok "image release $rel is for this code"
ver=${rel#box-v}
cache=$REPO/lab/.state/box-image
mkdir -p "$cache"
img=$cache/exacarib-box-$ver-amd64.qcow2
if [[ ! -s $img ]]; then
  rm -f "$cache"/*.qcow2
  dl=https://github.com/zagias/exaconnect/releases/download/$rel
  if ! curl -fsSL --max-time 900 -o "$img.part" "$dl/exacarib-box-$ver-amd64.qcow2" ||
    ! (cd "$cache" && curl -fsSL -o sums "$dl/exacarib-box-$ver-amd64.sha256" &&
      mv "$img.part" "$img" && grep qcow2 sums | sha256sum -c --quiet); then
    rm -f "$img" "$img.part"
    bad "could not download the image from $dl"
    exit 1
  fi
fi
ok "downloaded and checked exacarib-box-$ver-amd64.qcow2"

# 2. Boot it, in a container with QEMU (KVM when this host has it).
qimg=exaconnect/qemu:1
if ! docker image inspect "$qimg" >/dev/null 2>&1; then
  printf 'FROM debian:bookworm-slim\nRUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends qemu-system-x86 qemu-utils curl openssl ca-certificates procps >/dev/null && rm -rf /var/lib/apt/lists/*\n' |
    docker build -q -t "$qimg" - >/dev/null 2>&1 || { bad "could not build the QEMU image"; exit 1; }
fi
vm=exa-box-vm
tag=$(openssl rand -hex 3)
secrets=$(mktemp -d)
chmod 700 "$secrets"
trap 'docker rm -f "$vm" >/dev/null 2>&1; rm -rf "$secrets"' EXIT
docker rm -f "$vm" >/dev/null 2>&1
kvm=()
[[ -e /dev/kvm ]] && kvm=(--device /dev/kvm)
docker run -d --name "$vm" --network host "${kvm[@]}" -e LAN_PORT=9443 -e WAN_PORT=9444 \
  -v "$cache:/img:ro" -v "$REPO/deploy/box/test-image.sh:/test-image.sh:ro" "$qimg" sleep infinity >/dev/null ||
  { bad "could not start the QEMU container"; exit 1; }
out=$(docker exec "$vm" /test-image.sh start "/img/${img##*/}" /vm 2>&1)
while IFS= read -r line; do
  case $line in
    "PASS "*) ok "image: ${line#PASS }" ;;
    "FAIL "*) bad "image: ${line#FAIL }" ;;
    *) note "$line" ;;
  esac
done <<<"$out"
code=$(docker exec "$vm" cat /vm/setup-code 2>/dev/null)
[[ -n $code ]] || exit 1

# 3. Set it up and join Connect, in a browser on this host (the "laptop" on both sides).
box_location "$tag" 192.168.50.0/24 "$host" || exit 1
echo "BOX_SETUP_CODE=$code" >>"$secrets/env"
unset code
box_walkthrough image --network host --add-host "$host:127.0.0.1" \
  -e BOX_BASE=https://127.0.0.1:9443 -e WAN_URL=https://127.0.0.1:9444 -e WAN_IFACE=eth0 -e WAN_MODE=dhcp \
  -e LAN_IFACE=eth1 -e PORTAL_BASE="https://$host"

# 4. What the controller knows.
box_controller_checks eth0
if [[ $fail != 0 ]]; then
  note "box screen:"
  docker exec "$vm" sh -c "tr -d '\r' </vm/serial.log | grep -v 'Setup code' | tail -n 20" 2>&1 | sed 's/^/      /'
fi
docker exec "$vm" /test-image.sh stop /vm >/dev/null 2>&1
[[ -n ${node:-} ]] && api POST "/nodes/$node/revoke" >/dev/null
exit "$fail"
