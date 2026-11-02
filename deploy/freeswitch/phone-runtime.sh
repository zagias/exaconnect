#!/bin/sh
# GHSA-xjxm-9qx6-w3fj: <=1.11.1 must reject SIP REFER. No override switch.
# guard needs only BusyBox applets. Signed probes additionally need Python 3,
# or curl + openssl; their absence leaves telephone AI readiness unavailable.
# These commands run inside the PBX container, never against a Docker socket.
set -eu
umask 077
LC_ALL=C
export LC_ALL

fail() { echo "xakore: phone runtime $1" >&2; return 1; }

# Accept released versions only. Do not mistake rc/dev builds, arbitrary output
# or an image tag for the actual running binary's version.
parse_version() {
  awk -v framing="${1:-single}" '
    function decimal(s, width) {
      return s ~ /^[0-9]+$/ && length(s) <= width && (s == "0" || substr(s,1,1) != "0")
    }
    function hex(s) { return s ~ /^[0-9a-f]+$/ && length(s) >= 7 && length(s) <= 40 }
    function stamp(s, year, month, day, days) {
      if (length(s) != 16 || s !~ /^20[0-9]+T[0-9]+Z$/ || substr(s,9,1) != "T") return 0
      year=substr(s,1,4)+0; month=substr(s,5,2)+0; day=substr(s,7,2)+0
      if (month < 1 || month > 12) return 0
      days=31
      if (month == 4 || month == 6 || month == 9 || month == 11) days=30
      if (month == 2) days=(year % 4 == 0 ? 29 : 28)
      return day >= 1 && day <= days && substr(s,10,2)+0 <= 23 && substr(s,12,2)+0 <= 59 && substr(s,14,2)+0 <= 59
    }
    function valid(s, compact, human, p, bits, date, sha, release, meta, n, words, candidate, expected) {
      sub(/^FreeSWITCH [Vv]ersion:? /, "", s)
      normalised=s; compact=s; human=""; bits=""; date=""; sha=""; release=""
      p=index(s," (")
      if (p) {
        compact=substr(s,1,p-1); human=substr(s,p+2)
        if (substr(human,length(human),1) != ")") return 0
        human=substr(human,1,length(human)-1)
        if (index(human,"(") || index(human,")")) return 0
      }
      if (compact ~ /~(32bit|64bit)$/) {
        bits=substr(compact,length(compact)-4); sub(/~(32bit|64bit)$/, "", compact)
      }
      n=index(compact,"+git~")
      if (n) {
        if (split(substr(compact,n+5),meta,"~") != 2 || !stamp(meta[1]) || !hex(meta[2])) return 0
        date=meta[1]; sha=meta[2]; compact=substr(compact,1,n-1)
      }
      n=index(compact,"-release")
      if (n) {
        release=substr(compact,n); compact=substr(compact,1,n-1)
        if (release != "-release") {
          if (split(release,meta,"-") != 4 || meta[2] != "release" || meta[3] !~ /^[0-9]+$/ || length(meta[3]) > 20 || !hex(meta[4])) return 0
          if (date && meta[4] != sha) return 0
        }
      }
      if (split(compact,numbers,/[.]/) != 3 || !decimal(numbers[1],3) || !decimal(numbers[2],3) || !decimal(numbers[3],6)) return 0
      if (p) {
        if (!bits) return 0
        if (date) {
          candidate=human
          if (substr(candidate,1,4) != "git " && release && substr(candidate,1,length(release)) == release) candidate=substr(candidate,length(release)+1)
          if (split(candidate,words," ") != 5 || words[1] != "git" || !hex(words[2]) || substr(sha,1,length(words[2])) != words[2]) return 0
          expected="git " words[2] " " substr(date,1,4) "-" substr(date,5,2) "-" substr(date,7,2) " " substr(date,10,2) ":" substr(date,12,2) ":" substr(date,14,2) "Z " bits
          if (candidate != expected) return 0
        } else if (human != " " bits && human != release " " bits) return 0
      }
      return 1
    }
    {
      sub(/\r$/, "")
      if (++lines != 1) {
        # fs_cli -x version appends one empty terminal record. Only that
        # command boundary enables it; a second nonempty version never passes.
        if (framing == "fs_cli" && lines == 2 && $0 == "") next
        bad=1; next
      }
      if (length($0) > 320 || $0 ~ /[[:cntrl:]]/ || !valid($0)) { bad=1; next }
      version=normalised
    }
    END { if (bad || !version) exit 1; print version }
  '
}

patched() {
  printf '%s\n' "$1" | awk -F '[.+ -]' '
    { exit !(($1+0 > 1) || ($1+0 == 1 && ($2+0 > 11 || ($2+0 == 11 && $3+0 >= 2)))) }
  '
}

runtime_version() (
  # POSIX pipelines cannot report a failed binary when the parser succeeds.
  # Preserve its exit status and exact output (including extra blank lines).
  # The shipped image has no /tmp before its stock entrypoint. The guard has
  # already created its private state directory; probes use that same directory.
  version_parent=$1
  shift
  version_file=$(mktemp "$version_parent/version.XXXXXX") || exit 1
  trap 'rm -f "$version_file"' 0 1 2 15
  # One filesystem block is ample for the <=320-byte line plus CR/LF. A noisy
  # command cannot fill the container filesystem before the timeout expires.
  ulimit -f 1 || exit 1
  if ! timeout 5 "$@" > "$version_file" 2>/dev/null; then exit 1; fi
  version_bytes=$(wc -c < "$version_file") || exit 1
  [ "$version_bytes" -le 322 ] || exit 1
  framing=single
  if [ "$#" -eq 3 ] && [ "$1" = fs_cli ] && [ "$2" = -x ] && [ "$3" = version ]; then framing=fs_cli; fi
  parse_version "$framing" < "$version_file"
)

# This intentionally supports the shipped, line-oriented profile XML. Any
# ambiguous/multiline transfer parameter or settings element fails closed.
# Comments are ignored for validation and preserved in the rewritten file.
profile_xml() {
  awk -v mode="$1" '
    function active(s, out, p) {
      out=""
      while (length(s)) {
        if (comment) {
          p=index(s,"-->"); if (!p) return out
          s=substr(s,p+3); comment=0
        } else {
          p=index(s,"<!--")
          if (!p) return out s
          out=out substr(s,1,p-1); s=substr(s,p+4); comment=1
        }
      }
      return out
    }
    {
      live=active($0)
      if (live ~ /<profile[ \t]/) {
        if (live !~ /^[ \t]*<profile[ \t]+[^>]+>[ \t]*$/ || ++profiles != 1) bad=1
      }
      if (live ~ /<\/profile/) {
        if (live !~ /^[ \t]*<\/profile>[ \t]*$/ || ++ended != 1 || inside) bad=1
      }
      if (live ~ /<settings/) {
        if (live !~ /^[ \t]*<settings>[ \t]*$/ || inside || ++opened != 1) bad=1
        inside=1
      }
      if (live ~ /disable-transfer/) {
        if (!inside || live !~ /^[ \t]*<param[ \t]+name="disable-transfer"[ \t]+value="(true|false)"[ \t]*\/>[ \t]*$/) bad=1
        if (live ~ /value="true"/) disabled=1
        if (mode == "disable") next
      }
      if (live ~ /<\/settings/) {
        if (live !~ /^[ \t]*<\/settings>[ \t]*$/ || !inside || ++closed != 1) bad=1
        if (mode == "disable") print "    <param name=\"disable-transfer\" value=\"true\"/>"
        inside=0
      }
      if (mode == "disable") print $0
    }
    END {
      if (comment || bad || profiles != 1 || ended != 1 || opened != 1 || closed != 1 || inside) exit 1
      if (mode == "enabled" && disabled) exit 1
    }
  ' "$2"
}

guard() {
  config_dir=${1:-/etc/freeswitch}
  state_dir=${2:-/run/xakore-phone-runtime}
  vanilla_dir=${3:-/usr/share/freeswitch/conf/vanilla}
  mkdir -p "$state_dir" || return 1
  rm -f "$state_dir/guard" || return 1
  version=unknown
  if command -v timeout >/dev/null 2>&1; then
    version=$(runtime_version "$state_dir" freeswitch -version) || version=unknown
  fi
  transfer_enabled=true
  safe=false
  if [ "$version" != unknown ] && patched "$version"; then safe=true; fi
  # The stock entrypoint copies vanilla defaults on its first boot. Guard those
  # source profiles too, so that later copy cannot overwrite our mitigation.
  # Do not initialise/copy the defaults here or alter provided configuration.
  set -- "$config_dir"
  if [ "$safe" = false ] && [ ! -f "$config_dir/freeswitch.xml" ]; then
    set -- "$config_dir" "$vanilla_dir"
  fi
  found=false
  for profile_root in "$@"; do
    source_found=false
    for profile in "$profile_root"/sip_profiles/*.xml; do
      [ -f "$profile" ] || continue
      found=true
      source_found=true
      if [ -L "$profile" ]; then fail "profile symlinks are not supported"; return 1; fi
      if [ "$safe" = false ]; then
        tmp=$(mktemp "$profile.phone-runtime.XXXXXX") || return 1
        if ! cp -p "$profile" "$tmp"; then rm -f "$tmp"; return 1; fi
        if ! profile_xml disable "$profile" > "$tmp"; then
          rm -f "$tmp"; fail "could not safely disable transfers in a SIP profile"; return 1
        fi
        # Replacing only after complete validation avoids a partially written XML
        # file. Failure (including a read-only mount) stops the PBX entrypoint.
        if ! mv -f "$tmp" "$profile"; then rm -f "$tmp"; fail "could not write guarded SIP profile"; return 1; fi
        transfer_enabled=false
      elif ! profile_xml enabled "$profile"; then
        # A patched binary can start with custom/disabled profiles, but those do
        # not establish that transfers are available to the phone worker.
        transfer_enabled=false
      fi
    done
    if [ "$profile_root" = "$vanilla_dir" ] && [ "$source_found" = false ]; then
      fail "has no first-boot SIP profile templates to guard"; return 1
    fi
  done
  if [ "$found" = false ]; then
    if [ "$safe" = false ]; then fail "has no SIP profiles to guard"; return 1; fi
    transfer_enabled=false
  fi
  tmp=$(mktemp "$state_dir/guard.XXXXXX") || return 1
  printf '%s\n%s\n' "$version" "$transfer_enabled" > "$tmp" || return 1
  mv -f "$tmp" "$state_dir/guard" || return 1
  if [ "$transfer_enabled" = false ]; then echo "xakore: SIP transfers unavailable; phone AI remains disabled" >&2; fi
}

probe_python() {
  timeout 8 python3 - <<'PY'
import datetime
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

try:
    payload = {
        "instance_id": "main",
        "version": os.environ["EXA_PHONE_RUNTIME_VERSION"],
        "transfer_enabled": os.environ["EXA_PHONE_TRANSFER_ENABLED"] == "true",
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "nonce": str(uuid.uuid4()),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(os.environ["EXA_PBX_SECRET"].encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        "http://controller:8000/api/v1/commai/internal/voice/phone-runtime", body,
        headers={"Content-Type": "application/json", "X-Exa-PBX-Auth": signature}, method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=5) as response:
        if not 200 <= response.status < 300:
            raise ValueError("not accepted")
        if len(response.read(4097)) > 4096:
            raise ValueError("response too large")
except Exception:
    # No response body, exception, key, signature or request is logged.
    print("xakore: phone runtime receipt unavailable", file=sys.stderr)
    sys.exit(1)
PY
}

probe_curl() (
  # Do not pass the HMAC secret in openssl command arguments. Construct the
  # standard HMAC pads in a private temporary directory and hash via stdin.
  for dependency in curl openssl od awk date mktemp; do command -v "$dependency" >/dev/null 2>&1 || exit 1; done
  tmp=$(mktemp -d /tmp/xakore-phone-probe.XXXXXX) || exit 1
  trap 'rm -rf "$tmp"' 0 1 2 15
  printf '%s' "$EXA_PBX_SECRET" > "$tmp/key"
  if [ "$(wc -c < "$tmp/key")" -gt 64 ]; then
    openssl dgst -sha256 -binary < "$tmp/key" > "$tmp/short-key" || exit 1
    mv "$tmp/short-key" "$tmp/key"
  fi
  for pad in 54 92; do
    od -v -An -tu1 "$tmp/key" | awk -v pad="$pad" '
      function bxor(a,b, r,p) { r=0;p=1;while(a || b){if(a%2 != b%2)r+=p;a=int(a/2);b=int(b/2);p*=2}return r }
      { for(i=1;i<=NF;i++) bytes[++n]=$i }
      END { for(i=1;i<=64;i++) printf "%c", bxor(bytes[i]+0,pad) }
    ' > "$tmp/pad-$pad"
  done
  nonce=$(openssl rand -hex 16 | awk '{print substr($0,1,8) "-" substr($0,9,4) "-4" substr($0,14,3) "-8" substr($0,18,3) "-" substr($0,21,12)}')
  [ "${#nonce}" -eq 36 ] || exit 1
  checked_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  printf '{"checked_at":"%s","instance_id":"main","nonce":"%s","transfer_enabled":%s,"version":"%s"}' \
    "$checked_at" "$nonce" "$EXA_PHONE_TRANSFER_ENABLED" "$EXA_PHONE_RUNTIME_VERSION" > "$tmp/body"
  cat "$tmp/pad-54" "$tmp/body" | openssl dgst -sha256 -binary > "$tmp/inner" || exit 1
  signature=$(cat "$tmp/pad-92" "$tmp/inner" | openssl dgst -sha256 -binary | od -v -An -tx1 | tr -d ' \n')
  [ "${#signature}" -eq 64 ] || exit 1
  # -q is first: ignore .curlrc. No proxy, redirects, external URL or unbounded
  # connection; the plaintext hop exists only on the private Compose network.
  code=$(curl -q --silent --fail --output /dev/null --write-out '%{http_code}' --noproxy '*' --proxy '' \
    --proto '=http' --max-redirs 0 --connect-timeout 3 --max-time 5 \
    --header 'Content-Type: application/json' --header "X-Exa-PBX-Auth: $signature" \
    --data-binary "@$tmp/body" \
    http://controller:8000/api/v1/commai/internal/voice/phone-runtime 2>/dev/null) || exit 1
  case "$code" in 2??) ;; *) exit 1 ;; esac
)

probe() {
  state_dir=${1:-/run/xakore-phone-runtime}
  [ -n "${EXA_PBX_SECRET:-}" ] || { fail "probe has no signing secret"; return 1; }
  if [ ! -f "$state_dir/guard" ] || [ -L "$state_dir/guard" ]; then
    fail "probe has no startup guard"; return 1
  fi
  command -v timeout >/dev/null 2>&1 || { fail "probe needs a command timeout"; return 1; }
  version=$(runtime_version "$state_dir" fs_cli -x version) || { fail "cannot verify running version"; return 1; }
  startup_version=$(sed -n '1p' "$state_dir/guard")
  transfer_enabled=$(sed -n '2p' "$state_dir/guard")
  case "$transfer_enabled" in true|false) ;; *) fail "has an invalid startup guard"; return 1 ;; esac
  if [ "$version" != "$startup_version" ] || ! patched "$version"; then transfer_enabled=false; fi
  EXA_PHONE_RUNTIME_VERSION=$version
  EXA_PHONE_TRANSFER_ENABLED=$transfer_enabled
  export EXA_PHONE_RUNTIME_VERSION EXA_PHONE_TRANSFER_ENABLED
  if command -v python3 >/dev/null 2>&1; then probe_python
  elif command -v curl >/dev/null 2>&1 && command -v openssl >/dev/null 2>&1; then
    probe_curl || { fail "receipt unavailable"; return 1; }
  else fail "probe needs Python 3 or curl and openssl; readiness stays unavailable"; return 1
  fi
}

case "${1:-}" in
  guard) guard "${2:-/etc/freeswitch}" "${3:-/run/xakore-phone-runtime}" "${4:-/usr/share/freeswitch/conf/vanilla}" ;;
  probe) probe "${2:-/run/xakore-phone-runtime}" ;;
  loop) while :; do probe "${2:-/run/xakore-phone-runtime}" || :; sleep 60; done ;;
  *) fail "expects guard, probe or loop"; exit 2 ;;
esac
