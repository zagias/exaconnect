# Protected carrier ingress: prepared source, not qualified service

This mechanism has offline tests only. Do not redirect a number or claim TLS,
SRTP, reachability or staffed answering from a rendered configuration.

The controller emits a candidate DID policy with same-customer carrier binding,
canonical source CIDRs, approved dispatcher set/destination and a revision
covering the target and carrier policy. All organisation-owned DIDs fail closed
in ordinary candidate public XML. A separate approved local generation supplies
their actual public/private routes. Protected DIDs remain denied in public.

The approved generation contains public/private fragments, frozen tenant routing
contexts and frozen IVR routing. XML transfers, loopback contexts and forced
transfer contexts use generation-specific names. Signed controller tenant fields
retain the original tenant. Registrations, queue membership and media resources
remain live; this is not a complete business snapshot. Existing AI readiness and
fallback checks retain their existing dependencies.

The SBC tests received transport, never Via or a caller assertion. Protected
requests, early CANCEL and replies require actual TLS and their approved peer
identity. Carrier identity is bound to a DID/selected-set-specific CIDR group.
The unpublished private mTLS hop uses SBC5091/PBX5092, exact source IPs and fixed
certificate subjects. Every incoming request loses private assertions before the
SBC creates its own revision assertion. No public context uses it to grant access.

Protected outbound requires the private authenticated PBX leg, mandatory SRTP on
the carrier B-leg and approved TLS destinations on every next-set attempt.
Protected destinations bypass mutable candidate dispatcher targets/health state;
the approved endpoint is tried directly and subsequent approved sets are tried
on eligible failures. Legacy dispatcher behavior remains unchanged. A forged
X-Exa-Route/media header on the legacy listener cannot select an approved protected
set. The broad legacy Compose subnet still is not an authenticated PBX identity.

Carrier-network isolation is not provider-account authentication. Twilio permits
an account to select a specific origination URI user part, and its published
multi-tenant security warning says shared IPs do not identify the customer's
account. Thus the current TLS/CIDR/DID checks alone do not prove that an INVITE
belongs to this organisation's Twilio trunk. Production cutover remains blocked
on an additional privately provisioned account/trunk-origin proof.

The Elastic SIP Trunking origination documentation supports static X-headers
through the origination URI query. The SBC now requires exactly one
X-Jibsy-Trunk-Token on a protected initial INVITE/retransmission, checks its
domain-separated per-set private verifier under TLS+CIDR, and strips it before
FS. A missing verifier or missing/duplicate/wrong header is a denial.

The user-run provision_origin_auth.py prepares a256-bit bearer, private URL and
private hash include only when run in the separate private root process. It
prints no material and never contacts a provider/service. It has NOT been run
for a real trunk. Its output must remain outside every controller-writable path
and never enter candidate JSON, approved manifests, hashes/receipts or debug
logs. Bind the private verifier include to
/etc/kamailio/private-origination-auth.cfg and privately provision the URL in
Twilio only after native qualification/root review. Do not run this provisioner
in a model-visible terminal or read its output through an agent. No digest
origination authentication or trustworthy AccountSid SIP-header claim is made.

## Offline preparation

Use the existing Python loader's prepare subcommand with exact paths:

    python3 protected_policy_loader.py prepare \
      --directory <candidate-kamailio-directory> \
      --freeswitch-directory <candidate-freeswitch-directory> \
      --peers <reviewed-private-peer-json> \
      --expected-policy <exact-controller-policy-sha256> \
      --approved-root <shared-directory>/voice-approved

It prepares a content-addressed generation and full bundle digest. It does not
switch current, contact a service or create credentials. Review the concrete
generation before activation. Candidate changes stay pending and do not
immediately revoke the last approved generation. Missing/mismatched renders fail
preparation.

## Paired installation and activation

This is an operator procedure, not an automatic deployment script:

1. Retain the actual patched PBX and existing REFER/MESSAGE restrictions. Install
   the candidate public guard/includes before changing policy or redirecting a
   DID. Old deployed XML does not acquire these guards automatically.
2. Use a separate root-owned host directory such as
   /etc/exaconnect/voice-approved, outside controller-data and EVERY directory
   writable by the controller. Mount it read-only into both peers using the
   unapplied protected-ingress.yml.example. Do not expose it to the controller.
   The directory and all host parents must be root-owned and not group/world
   writable; the activator enforces this. Hashes do not grant operator approval
   if the controller can replace the entire approved root.
   Mount a shared DIRECTORY into both peers. Individual
   file binds can retain an old inode after host rename. Do not mount current or
   generation members separately. Both peers must see identical members and the
   changed directory symlink.
3. Bind actual exact private peer IPs in the templates. Define
   WITH_PROTECTED_INGRESS and substitute PROTECTED_SBC_IP in the operator-owned
   kamailio-local.cfg. Append private TLS domains to tls.cfg while preserving
   public domains. Never publish5091/5092.
4. Provision peer certificates/keys through the separate private operator process.
   SBC CN is jibsy-sbc.internal; PBX CN is jibsy-pbx.internal; include actual private
   IP SANs. Each peer trust file trusts ONLY the intended dedicated peer
   certificate/authority, never public CA roots or a stand-in. Kamailio5.8 chain
   verification alone does not verify hostnames. Explicit protected-pbx server_id
   selection through the per-message TLS XAVP is required on private outbound
   TLS connections; do not leak a worker-global selector through connection reuse.
5. Install the new TLS-only Sofia profile, exact SBC /32 ACL and private context.
   Keep disable-transfer=true and enable-chat=false. This adds a fifth profile
   to the known four: acceptance evidence must honestly include it. Do not ignore
   it or weaken the startup/runtime guards. Current production guard/start
   remain the frozen51b35/8a78 payloads: this source does not replace them.
   Stage and review their five-profile inventory expansion separately, preserve
   every version/receipt restriction, and prove interaction with the approved
   generation reload before installing this profile.
6. Complete the native qualification below using the exact installed binaries.
7. The activate subcommand takes the already staged bundle, approved-root, exact
   reviewed full bundle digest and actual peer container names. It closes the
   activation gate, validates native Kamailio config and both runtime mounts,
   stages the edge map, switches directory indirection, reloads FS and reads back
   exact loaded routing contexts. It publishes the edge pointer last, records
   completion and opens the gate. No provider or credential change is performed.

The startup verifier checks seven fixed members against SHA256SUMS, whose digest
is the generation directory name, plus the completed activation receipt. It
rehydrates this persistent local generation after restart without a controller
call. Incoming standard calling has no added controller admission dependency.

Missing/corrupt members, receipt mismatch and failed activation keep new SIP
work closed. ACK/BYE/CANCEL retain cleanup paths with protected peer checks.
Activation temporarily closes new SIP work, including legacy calls; schedule
that bounded maintenance step. Recovery is explicit reactivation of a reviewed
generation. Do not claim immediate database revocation or automatic fallback.

## Required native Linux qualification

Use isolated local fake carriers/PBX, non-public bindings and ephemeral
test-only certificates. No provider account, paid calls or production keys.

- Parse the actual full Kamailio config on its exact5.8 build; load all actual
  FreeSWITCH1.11.3 profiles. Prove both guards on every loaded profile.
- Correct private subject/source works. Missing, invalid, expired, wrong-subject
  or wrong-source certificates fail at both peers. Verify actual selected TLS
  client certificate/trust domain. Plain UDP/TCP cannot reach private context.
- Correct protected DID/carrier/TLS works. Wrong carrier, UDP/TCP, overlapping
  other-tenant CIDRs, malformed DIDs and duplicate case-variant assertions fail.
  Direct public/PBX access and forged Route/Contact aliases cannot bypass it.
  Shared Twilio source IP without the correct per-set bearer still fails.
  Test missing, duplicate/case-variant, malformed and wrong bearer; a verifier
  must not work as the bearer or authenticate another set. Pin private verifier
  configuration evidence privately, without recording its value or hash here.
- Prove protected ACK/re-INVITE/UPDATE/BYE continuity. Prove early UDP/wrong-peer
  CANCEL cannot cancel protected INVITEs. The protectedtx retention3600s must
  exceed all actual retained early transactions; qualify actual TM timers.
  Include copied Call-ID/From-tag with different Via branches across carriers,
  original transaction retransmissions over a different transport, and matched
  CANCEL with altered caller headers. TM's own internal transaction identity
  supplies the retransmission guard; CANCEL reads the matched original INVITE.
- Inject matching-branch fake183/200/error replies over UDP and from wrong
  sources/subjects. They must be dropped before SDP/media keys reach a peer.
- From another application peer, inject a known protected route set on legacy
  SIP with no media header, forged header, duplicates and mixed lists. All fail
  before delivery. Mutated candidate TLS/UDP destinations cannot redirect an
  approved protected call; exercise every approved next-set failure.
- Prove directory indirection visibility through both actual mounts and exact
  xml_locate readback after reload. Reject stale legacy/protected render pairs,
  missing/corrupt members, partial/failed/concurrent activation and restart
  during activation.
- Stop the controller with a valid approved legacy generation and prove incoming
  standard calls still reach staff; repeat for protected calling. Pending
  candidates must not replace approved targets/IVR. Missing/corrupt approved
  policy must fail closed.
- Require AES_CM_128_HMAC_SHA1_80. Reject RTP/plain SDP, wrong suites and downgrade
  re-INVITEs before useful media. Prove fixed voice codec, DTMF, NAT/two-way audio,
  hangup/timeout and staffed answering separately.

Twilio public origination uses sip:<actual-host>:5061;transport=tls. These private
templates are not a public endpoint. Keep provider REFER disabled. AI handover
policy remains a separate review on the guarded PBX.

Primary implementation references:
- https://www.kamailio.org/docs/modules/5.8.x/modules/ipops.html
- https://www.twilio.com/docs/sip-trunking#origination-settings
- https://www.twilio.com/docs/voice/api/sip-security
- https://www.kamailio.org/docs/modules/stable/modules/tls.html
- https://raw.githubusercontent.com/kamailio/kamailio/5.8/src/modules/tls/tls_server.c
- https://raw.githubusercontent.com/kamailio/kamailio/5.8/src/modules/tm/t_lookup.c
- https://developer.signalwire.com/freeswitch/users-and-endpoints/sip-profiles/
- https://developer.signalwire.com/freeswitch/media-and-codecs/handling/
