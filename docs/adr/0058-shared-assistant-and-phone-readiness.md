# ADR 0058: One assistant and evidence before telephone activation

Status: review implementation; no production assistant/telephone rollout.

## A single workspace

The public website provides pre-sales coaching. Hub uses `/assistant` for
account advice, Connect network advice and authorised Jibsy diagnostics.
Old links retain the intended scope. Public website conversations are not
silently copied into an account. Jibsy diagnostics retain their original
seat, role and permission checks.

Account advice reads a safe projection of the active product catalogue and
current subscriptions. Example offers and private commercial fields are
excluded. Missing access means an owner/admin permission request, not an
invented upgrade price. Source capability does not establish entitlement or
provider readiness. Advice-only scope cannot issue configuration tools.

Connect proposals use an allow-list of typed actions. A dry-run validates the
whole proposal. A person reviews and approves a configuration-bound revision,
then separately applies that revision. Plans expire after 30 minutes. Current
permissions are checked again. Retries reuse an execution key and do not
repeat a completed change. Paid changes produce draft orders for normal
commercial review. Undo checks the post-apply configuration; later edits or
legacy plans without a digest require manual review.

Configuration review takes the existing Internet advisory lock before
nonblocking configuration locks. Concurrent manual editing returns a useful
conflict instead of forming a database deadlock. Pasted credentials stop
before audit storage, configuration reads or an external model call.

## Telephone calls

The worker uses the pinned LiveKit Agents SDK and explicit STT/TTS provider
configuration. No provider is chosen by default. It selects an actual SIP
participant, and the controller checks that participant's call identity and
owned DID. A bootstrap worker credential cannot operate arbitrary browser
conversations. Random call capabilities bind subsequent requests to one
customer and conversation.

Speech, silence, call duration and disconnects are bounded. Taking over a
conversation interrupts speech and requests a real SIP transfer. An accepted
transfer is not reported as proof that a person answered. A failed transfer
leads to bounded hangup. A durable controller watchdog handles missed worker
heartbeats and absolute call deadlines, including worker process loss.

Readiness depends on expiring operational evidence, not an environment flag:
actual provider speech, an actual SIP media challenge, a controller-observed
transfer, and a fresh authenticated patched PBX runtime receipt. Evidence is
scoped to the tenant and provider configuration. Test-mode evidence remains
lab-only. Browser dictation is labelled as dictation; it is not telephone AI.

## PBX transfer security

[FreeSWITCH advisory GHSA-xjxm-9qx6-w3fj](https://github.com/signalwire/freeswitch/security/advisories/GHSA-xjxm-9qx6-w3fj)
affects versions through 1.11.1. The current image is 1.10.12. Startup disables
SIP REFER on every unsafe or unknown profile before the stock entrypoint runs.
Ambiguous or uneditable profiles stop startup. This mitigation also disables
SIP-initiated blind/attended transfers; normal calling does not establish
that transfers are safe. A released binary at least 1.11.2 is required for
telephone AI acceptance. There is no bypass flag.

The startup mitigation uses BusyBox only. Runtime HMAC receipts additionally
need Python 3, or curl plus openssl CLI in the PBX image. Those optional tools
are not established by the current minimal image. Missing tools leave
telephone AI unavailable. The probe uses loopback `fs_cli` and the private
Compose controller endpoint, without a Docker socket or public event socket.

FreeSWITCH keeps the owned DID and selected fallback across its AI bridge.
The handover destination exists only inside the established tenant context,
requires the server-exported handover state, and routes only to a bounded
local fallback. No public route lets a new caller select a tenant by name.
The AI gateway example is not installed automatically.

## Release evidence still required

CI verifies real PostgreSQL conflicts/isolation, proposal lifecycle, worker
SDK compatibility, isolated SIP/media, routing and firewall behaviour.
Provider calls and human/PBX media acceptance require a separately authorised
operational run. These are not established by mocked tests, a successful
build, credential presence or a transfer API response alone. Full application
release requires a reviewed change set, migration/rollback plan and approval.
