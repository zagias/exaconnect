# FreeSWITCH security and telephone AI activation

The prepared source requires a released FreeSWITCH **1.11.3 or later** for
telephone AI. This is a version floor for the known issues below, not a claim
that an arbitrary image is secure, compatible, or operationally accepted.

Official sources, reviewed 2 November 2026:

- [REFER injection, fixed in 1.11.2](https://github.com/signalwire/freeswitch/security/advisories/GHSA-xjxm-9qx6-w3fj).
- [Inbound MESSAGE API dispatch, fixed in 1.11.3](https://github.com/signalwire/freeswitch/security/advisories/GHSA-mg63-h38w-8p9p).
  The published workaround is `enable-chat=false` on profiles that do not need
  inbound SIP chat. Authentication alone does not close this route.
- [Official 1.11.3 release](https://github.com/signalwire/freeswitch/releases/tag/v1.11.3),
  commit `ef32e205295e29f034f1453ad245ba5efb07b94a`.

The startup guard disables REFER **and inbound SIP MESSAGE chat** in every
provided profile, and in stock first-boot templates, on affected/unknown builds.
It refuses ambiguous security parameters or unwritable profiles. A patched
build with an explicit unsafe `enable-chat-api-proto=true` cannot obtain a
transfer-enabled startup receipt. Neither mitigation nor configuration enables
telephone AI: real speech, participant-bound SIP transfer and fresh signed PBX
evidence are still required. Accepted transfer does not confirm a human answered.

## Narrow guard-only update proposal (not applied)

The recorded live 1.10.12 instance had `disable-transfer=true` in all four
profiles and no `enable-chat` parameter. Thus its existing REFER workaround
does **not** establish protection against the MESSAGE advisory. Updating it
disables SIP-initiated blind/attended transfers and inbound SIP MESSAGE chat;
normal audio calling, mail, web messaging and the LiveKit readiness guard have
separate responsibilities. A brief phone-service restart requires explicit
approval for this additional restriction.

Before that approval is exercised, prepare a private backup of the current
Compose files, bind sources, rendered phone configuration and certificate/data
mount metadata. Record the exact service/container/image identities without
printing environment values, directory passwords or raw configurations. Abort
if service identity, source hashes, mounts or active-call count drift.

At the approved cutover, require zero active channels, stop only that exact
FreeSWITCH service, and take a fresh private image snapshot of its stopped
writable layer. Preserve every existing volume, CA, environment setting,
network, profile, port and start command. Use that exact local image snapshot
and the reviewed guard's read-only bind; do not pull a new image, build, restart
dependencies, or use a broad release command for this guard-only mitigation.
The existing start wrapper remains byte-identical. The approved executor must
target only FreeSWITCH with `--no-deps --no-build --pull never`.

After restart, require the exact expected image and source bindings, startup
guard marker, successful PBX status and direct `phone_runtime_check.py` evidence
that all configured profiles disable both REFER and inbound chat, including all
currently loaded profile names. Do not send exploit payloads or paid calls.
Readiness remains false on 1.10.12. A failed or incomplete proof is a failed
cutover; never automatically restore a transfer/chat-enabled vulnerable state.
If the replacement has already been attempted, preserve private failure logs
and use operator recovery with the new protective guard retained.

## Patched image candidate (no image has been selected or pulled)

Use an independently built/reviewed **runtime** image at an immutable digest,
with official source provenance at 1.11.3 or a later supported release. Do not
guess a container tag. The upstream `docker/release/Dockerfile` at the 1.11.3
tag is a legacy Jessie/1.6 recipe, and the current
`.github/docker/debian/bookworm/amd64/public.release.Dockerfile` produces Debian
artifacts using a private repository credential; neither is a verified drop-in
runtime replacement for this deployment. Do not copy credentials into build
arguments or logs.

`prepare_upgrade.py --image <immutable image digest>` prints an **unapplied**
Compose override that changes only the FreeSWITCH image and prohibits pulling.
It makes no Docker, network or service request. The selected image must first
be available locally through a separately authorised build/pull. The override
does not prove its binary version. In an isolated Linux project, retain the
existing guarded start/config mounts and check the actual binary grammar,
loaded modules, Sofia profiles, directory/dialplan, Verto/TLS certificates,
codec/RTP behaviour, PBX authorisation and transfer/media cleanup. Keep both
security workarounds active until those checks and the agreed call acceptance
are complete. Production image replacement needs its own reviewed approval;
the guard-only restart approval does not authorise a binary upgrade.

## Safe read-only operator evidence

Read only version output, `status`, active-channel count and Sofia profile
names. Parse each current profile XML privately for exactly one active
`disable-transfer=true` and `enable-chat=false` on an affected build; comments,
missing values or duplicates are not evidence. Check stock first-boot templates
if the stock entrypoint can copy them. Do not print full XML or environment
values. Directory checks must reject FreeSWITCH `-ERR` replies; `list_users`
is the directory API used by the existing private inspection helper. None of
these observations sends a SIP MESSAGE, REFER, model prompt, email or call.
