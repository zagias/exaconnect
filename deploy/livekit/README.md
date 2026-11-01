# LiveKit: telephone AI

The telephone worker now has configured speech adapters, interruptible audio,
call-scoped controller sessions, actual SIP call transfer and bounded cleanup.
It uses the same Jibsy conversation agent as browser calls. It has **not been
validated with a paid speech provider or a real carrier**. Keep ordinary PBX
fallbacks in place until the operational acceptance below passes.

The voice compose profile is opt-in. Starting services or supplying keys does
not enable telephone AI. Readiness requires all of:

- A trusted, fresh PBX runtime receipt showing FreeSWITCH >= 1.11.2 and actual
  transfer support enabled. Older and unknown versions fail closed because of
  GHSA-xjxm-9qx6-w3fj. The receipt expires after five minutes.
- A measured TTS-to-STT random phrase loopback from the configured speech
  adapters, refreshed within 75 seconds.
- A random phrase played through the real LiveKit room and repeated by the
  prepared SIP caller, then recognised from that caller's audio. The proof is
  bound to the same room, SIP participant and call ID.
- A successful controller-issued LiveKit SIP transfer of a real participant,
  with its room, participant identity and SIP call ID verified against LiveKit.
  Transfer evidence expires after 24 hours.

Test-mode evidence is labelled `lab` and **cannot enable production**. There is
no environment flag or API boolean that marks the service ready. A transfer
receipt reports `accepted` and `human_answered: false`: SIP peer completion
alone does not prove that a person, rather than voicemail, answered.

## Settings

Read secrets from environment or a git-ignored `.env`; never commit them.

| Setting | Purpose |
| --- | --- |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Worker/server connection |
| `EXA_LIVEKIT_URL`, `EXA_LIVEKIT_API_KEY`, `EXA_LIVEKIT_API_SECRET` | Controller server API connection |
| `EXA_LIVEKIT_AGENT_SECRET` | Worker bootstrap credential, shared with controller |
| `EXA_CONTROLLER_URL` | Internal controller URL, default `http://controller:8000` |
| `EXA_PHONE_WORKER_ID` | Worker deployment identity, default `exacarib-phone` |
| `EXA_PHONE_STT_URL`, `EXA_PHONE_STT_KEY`, `EXA_PHONE_STT_MODEL` | Explicit OpenAI-compatible transcription provider |
| `EXA_PHONE_TTS_URL`, `EXA_PHONE_TTS_KEY`, `EXA_PHONE_TTS_MODEL`, `EXA_PHONE_TTS_VOICE` | Explicit OpenAI-compatible speech provider |
| `EXA_PHONE_LANGUAGE` | Recognition language, default `en` |
| `EXA_PHONE_HANDOVER_HOST` | Controller-only internal FreeSWITCH REFER host and port |
| `EXA_PBX_SECRET` | Trusted PBX runtime probe authentication |

No paid provider is selected by default. The installed OpenAI-compatible
plugins are adapters; compatibility, voices, latency and billing must be
checked against the chosen provider. Speech endpoints require HTTPS except
explicit isolated loopback fixture addresses. Provider keys are not included
in fingerprints, responses or application logs.

## Call routing and lifecycle

Carrier → Kamailio → FreeSWITCH → LiveKit SIP → one dispatched worker. The
bridge must preserve the real called DID in `sip.trunkPhoneNumber` and LiveKit
must supply `sip.callID`. The controller obtains these attributes directly from
LiveKit rather than trusting a worker-submitted number. The SIP dispatch rule
must explicitly dispatch `exacarib-phone`.

The internal REFER destination is
`sip:exa-ai-handover-<tenant>@<EXA_PHONE_HANDOVER_HOST>`. Only the established
call's own tenant transfer context may handle it; it must select that tenant's
configured fallback without invoking AI again. Do not expose this route in a
public dialplan. Trunk and gateway provisioning are separate prerequisites.

Review an operator-checked JSON list of `{customer_id, e164}` owned DIDs with:

```sh
python configure_sip.py --numbers-file owned-dids.json --freeswitch-address 10.0.0.2
```

This default is offline and prints the exact plan. `--apply` explicitly creates
missing SIP resources; conflicting existing trunks or dispatch rules are refused.
It requires the exact FreeSWITCH source IP, creates one restricted trunk per
tenant and explicitly dispatches each call to `exacarib-phone`. Server-set
`exa.customer_id`, `exa.tenant` and the tenant room prefix must agree with the
controller's DID owner. No existing resource is overwritten or deleted.

Normal call start requires an active evidence lease. It returns a random
capability bound to the business, conversation and actual SIP participant;
the bootstrap secret cannot read, turn or end arbitrary conversations. Turn
IDs deduplicate retried requests. Staff inbox takeover is polled every two
seconds. LiveKit AgentSession handles audio subscriptions, VAD, sample rates
and playback interruption. Stale replies are discarded after barge-in.

Calls stop after 60 seconds without caller input or 30 minutes total. Speech
and controller operations have deadlines. Disconnects, provider failure and
shutdown cancel and await background work, attempt a real transfer where
appropriate, and end the controller session once. A shared transfer-attempt lease prevents
concurrent REFER attempts. Media cleanup remains a durable pending obligation
after billing ends and is retried through temporary control-plane outages;
only a confirmed transfer, participant removal or valid Twirp not-found receipt
clears it. Unconfirmed transfer
failure produces a brief failure message and removes the SIP participant so
an answered caller is not stranded in an empty room. A durable controller watchdog checks missed worker heartbeats, attempts transfer
after worker loss and caps call billing at 30 minutes. A controller or PBX process
outage still needs the PBX's own independent call-duration protection.

## Validation

Build the worker only in its Linux image. It pins the Agents and speech plugin
contract together. The offline SDK check does not contact speech providers:

```sh
python check_sdk.py
```

After authorisation to use the configured speech provider, prepare one existing
isolated SIP test call and its fallback, then run inside the worker environment:

```sh
python acceptance.py --room '<existing-room>' --participant '<sip-identity>' \
  --sip-call-id '<actual-sip-call-id>' --watch
```

This does not originate a call. It generates speech for a random challenge,
transcribes that audio, then plays the challenge in the actual SIP room. The
prepared test caller must repeat the six words after playback. The recognised
caller audio is verified against the challenge before it opens a scoped test
conversation for the existing participant, asks the controller to transfer
that exact call, and closes the test conversation. `--watch` measures speech
again every 45 seconds to maintain the short readiness lease. It stops on a
failed check; evidence then expires. A fresh transfer acceptance is required
at least daily and whenever the deployment or fallback route changes. The
watch loop is bounded to 24 hours; it does not originate a replacement call.

`--speech-only` checks speech and can refresh an already established lease;
it cannot create transfer evidence. Provider fixtures must run against a
controller with `EXA_VOICE_TEST_MODE=1`, which stores lab-only evidence.

Before customer use, verify audible caller input/output, barge-in, caller
hangup, staff takeover, a real human fallback, provider timeout, worker loss,
LiveKit loss and the PBX's handling of each failure. Local lifecycle tests and
provider stubs do not prove carrier interoperability or a human answer.
