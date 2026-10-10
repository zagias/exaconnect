#!/usr/bin/env bash
# The phone system's recorded prompts and hold music (FreeSWITCH's standard sound
# packs: voicemail, "the number you dialled", queue music). The image ships none,
# so voicemail hung up straight away and queues were silent. Downloaded once into
# deploy/freeswitch/sounds (git-ignored, mounted read-only), each pack checked against
# its pinned SHA-256 before it is unpacked. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")/../freeswitch"
BASE=https://files.freeswitch.org/releases/sounds
PACKS=(
  "en-us-callie-8000-1.0.51 e48a63bd69e6253d294ce43a941d603b02467feb5d92ee57a536ccc5f849a4a8"
  "en-us-callie-16000-1.0.51 324b1ab5ab754db5697963e9bf6a2f9c7aeb1463755e86bbb6dc4d6a77329da2"
  "music-8000-1.0.52 2491dcb92a69c629b03ea070d2483908a52e2c530dd77791f49a45a4d70aaa07"
  "music-16000-1.0.52 93e0bf31797f4847dc19a94605c039ad4f0763616b6d819f5bddbfb6dd09718a"
)
mkdir -p sounds
for pack in "${PACKS[@]}"; do
  read -r name sum <<<"$pack"
  grep -qsx "$name" sounds/.installed && continue
  tmp=$(mktemp)
  curl -fsSL --retry 3 -o "$tmp" "$BASE/freeswitch-sounds-$name.tar.gz"
  if ! echo "$sum  $tmp" | sha256sum -c --quiet -; then
    rm -f "$tmp"
    echo "Sound pack $name did not match its checksum; not installed" >&2
    exit 1
  fi
  tar -xzf "$tmp" -C sounds
  rm -f "$tmp"
  echo "$name" >>sounds/.installed
  echo "Sound pack $name installed"
done
