# Bounded isolated native qualification (unexecuted)

This is a test recipe for the root operator. No result is recorded here.
Offline Python tests do not satisfy these cases. Use only the already cached,
root-pinned Kamailio image and actual patched FreeSWITCH image aca9...; substitute
their COMPLETE sha256 image IDs in the commands. Never resolve or pull a tag.
No production mounts, account data, keys, public IPs, provider hosts or live DIDs
belong in this fixture. Use synthetic +18685550100/+18685550101 only.

## Fixed boundary

- Unique run label: jibsy.protected.qual=<reviewed-run-id>.
- A newly created Docker bridge with --internal and no host publications.
- Example unused subnet172.29.94.0/24; first prove it does not overlap any existing
  Docker/host route, or select another reviewed private test subnet.
- SBC172.29.94.2; PBX172.29.94.3; carrier-A172.29.94.10;
  carrier-B172.29.94.11; attacker172.29.94.12.
- Existing cached images only: --pull=never on EVERY run/create.
- Root-owned dedicated Linux tmpfs at /run/jibsy-protected-qual-<run-id>, mode0700,
  no persistent secret output. Verify mount type tmpfs
  before issuing synthetic credentials. A fresh test CA/leaf key is generated
  only there. Key bytes never go into chat, logs or the evidence archive.
- No published ports, host networking, production network attachment or writable
  source mounts. Containers get only their own synthetic private keys read-only.
  CA private key stays on the tmpfs operator host, not in either service.
- Root operator controls the approved directory. Controller fixture cannot write
  it or its parent. Mount it read-only to both service containers as a DIRECTORY.

Before execution, pin source/fixture/guard hashes and record image IDs, architecture
and native version strings. Keep the same guards and runtime restrictions.
Never substitute the old source image pin for the actual patched PBX image.

## Setup commands (review and bind before running)

These commands are deliberately parameterised; root supplies exact cached IDs,
an unused test subnet and a unique run-id. Do not paste production values.

    docker image inspect <KAMAILIO_SHA256_ID> <FREESWITCH_ACA9_SHA256_ID>
    docker network inspect <all-existing-network-IDs>
    ip route show table all
    mkdir -m 0700 /run/jibsy-protected-qual-<run-id>
    mount -t tmpfs -o size=64m,mode=0700,nosuid,nodev,noexec \
      tmpfs /run/jibsy-protected-qual-<run-id>
    findmnt -n -o FSTYPE,TARGET /run/jibsy-protected-qual-<run-id>
    docker network create --internal \
      --label jibsy.protected.qual=<run-id> \
      --subnet 172.29.94.0/24 jibsy-protected-qual-<run-id>

Use the existing Linux OpenSSL executable. In the verified tmpfs, generate a
fresh short-lived CA and distinct SBC/PBX/wrong-role/carrier test certificates.
SBC CN=jibsy-sbc.internal with SAN IP172.29.94.2; PBX CN=jibsy-pbx.internal with
SAN IP172.29.94.3. Use TLS1.2+, separate leaf keys, serverAuth/clientAuth EKUs as
appropriate. Include negative leaves: wrong subject signed by the test CA,
expired leaf, untrusted leaf and missing certificate. Restrict the normal private
trust files to the intended peer. Do not print PEMs, SDES attributes or key logs.

Create stopped containers from exact cached images with --pull=never,
--network=none, unique names and the run label, then copy only their packaged
default configuration into the tmpfs fixture. No production volume is inspected.
Remove those stopped copy-only containers by their exact names after copying.

Bind the new source templates to TEST addresses and local paths:
- Kamailio public-in-test SIP5060/TLS5061 advertises172.29.94.2, never publichost.
- WITH_PROTECTED_INGRESS and PROTECTED_SBC_IP172.29.94.2.
- Legacy TO_PBX172.29.94.3:5080 remains a separate comparison path.
- New private mTLS profiles use only5091/5092 and synthetic keys.
- Private FS ACL allows only172.29.94.2/32, with both transfer/chat disabled.
- Synthetic staff extension answers with a local fixed-codec echo/tone and hangs
  up after at most15s. No outside bridge exists in the fixture.
- Candidate carrier-A has TLS host172.29.94.10:5061, source /32, protected mode.
  Carrier-B uses172.29.94.11 and a separate DID/customer/source group.
- Render a protected policy and matching owned-public/protected/dialplan/IVR
  fragments from the actual source helper. Use the loader prepare subcommand.
  Review/pin the resulting exact full bundle digest before fixture activation.
  Privately provision ONLY a synthetic per-set origination token/verifier in
  the tmpfs fixture. The correct initial INVITE includes that header; the
  verifier include is separate from every candidate/approved bundle. Native
  missing/duplicate/wrong-token cases use the same allowed carrier source IP.

Mount the actual reviewed frozen guard/start COPY, preserving production copies.
Current production guard/start are51b35/8a78. Do not replace or relax them. Stage
a separate reviewed five-profile inventory/acceptance copy only if the current
qualification code assumes four; retain its complete version floor, expiry,
signature and transfer/chat rules. The fifth profile is included in every native
profile/receipt assertion. Any guard or receipt incompatibility is a FAIL and
blocks production activation.

Run each service with an internal IP, --pull=never, its tmpfs config/secret
mounts, the run label, NO -p/--publish and no production mounts. Give it only
the permissions its normal native binary needs; do not use --privileged.
Synthetic clients can run the existing host Python/OpenSSL inside an isolated
client container network namespace via nsenter, using only the tmpfs fixtures.
No package/image download or host network listener is allowed.

## Native parser and configuration phase

1. In the isolated Kamailio container, run actual kamailio -c against the complete
   bound configuration. Capture exit status and a sanitised error summary only.
2. On the actual patched FS binary, run the staged guard before startup. Start
   the synthetic five-profile configuration. Record native version, profile names
   and parsed disable-transfer=true/enable-chat=false evidence for ALL five.
3. Prepare/activate the approved test generation using the new loader, pointing
   to the two isolated service names and the tmpfs approved root. Production
   commands and production current symlinks are never passed to the helper.
4. Confirm both peers see the same generation directory and current symlink.
   Read back loaded public/private/frozen contexts and pinned IVR names. The
   loader performs checks but record the actual native command outcomes too.
5. Verify with docker inspect that the test network is internal, no service has
   Ports/PortBindings, no production network or mount is attached, and only
   labelled exact test resources exist.

## Wire matrix: every negative case needs absence-of-delivery proof

Use bounded synthetic SIP transactions (Max-Forwards10, unique IDs, short timeouts)
and count what the expected local peer actually receives. A403 alone is not proof
that a different branch did not relay. Keep captures on the isolated bridge only.
Do not retain raw SDP security attributes in deliverable logs.

| Case | Action | Required native result |
|---|---|---|
| Private identity | Correct SBC/PBX mutual TLS and exact source | Negotiated TLS1.2+ and intended peer certificate role; allowed private request |
| Wrong private identity | Missing/untrusted/expired/wrong-subject certificate or wrong-source IP on5091/5092 | TLS/request refusal and no protected context entry |
| Reused TLS selection | Reuse private TLS connection, then external carrier connect in same worker | Correct per-message certificate/trust profile on both connections |
| Protected ingress | Carrier-A TLS to own DID | Private PBX receives one request with exact approved revision; public context never admits it |
| Carrier isolation | B/attacker to A DID, overlapping other-tenant source groups | Refusal, zero protected PBX delivery |
| Transport spoof | A over UDP/TCP with TLS Via/URI and mixed/duplicate case-variant ingress headers | Refusal, zero protected delivery |
| Shared provider source | Correct TLS/IP/DID with missing/wrong/duplicate token, other-set token or verifier-as-token | Refusal and zero private delivery; token/verifier absent downstream and from logs |
| Direct PBX bypass | Attacker connects to public/legacy/private FS paths with valid-looking revision | No protected tenant transfer |
| Protected route injection | Attacker on legacy PBX subnet sends known protected X-Exa-Route, absent/forged/duplicate media policy, mixed set lists | No carrier delivery, including next-set/address retries |
| Mutable candidate | Change candidate protected target to a different TLS or UDP host; reload legacy dispatcher | Approved protected target remains the only delivered target |
| Early CANCEL | Hold a protected INVITE ringing; send copied branch CANCEL over UDP/wrong carrier/private legacy path | No cancellation at callee; correct original TLS peer can cancel |
| Transaction collision | Same Call-ID/From-tag, different Via branches from A/B; then copied CANCEL | No policy replacement or cross-carrier cancellation |
| Cached retransmission | Replay an existing protected INVITE/negative ACK over another transport/source | No cached SDP/keying reply or transaction release to untrusted peer |
| Fake responses | Copy valid branch/Via into fake183/200/error over UDP/wrong source/role | Dropped before forwarded SDP or state change |
| Dialog continuity | Valid ACK/re-INVITE/UPDATE/BYE, then UDP/wrong-source/certificate copies and forged Contact alias/Route | Only authenticated original peer policy relays; cleanup succeeds |
| Failover | Approved protected A returns503/timeout, approved B succeeds; stale/missing/mixed policy candidates | Only approved TLS destinations; no downgrade or unauthorised set |
| Inbound media | Fixed PCMU/PCMA with required AES_CM_128_HMAC_SHA1_80 vs plain/wrong-suite/late-offer downgrade | Secure case negotiates; negative cases fail before useful media |
| Outbound media | PBX private protected gateway to fake carrier with same suite matrix | Mandatory carrier B-leg SRTP; ordinary browser/internal leg policy unchanged |
| Media reality | At most15s local generated audio, DTMF, bidirectional packets, BYE/timeouts | Actual decoded two-way audio and authenticated SRTP; counters/SDP alone insufficient |

## Local activation/outage matrix

- With a valid approved legacy generation, make one synthetic incoming staff call,
  stop ONLY the isolated controller fixture (or leave it absent), and repeat.
  Both reach the same staff target. Repeat protected standard calling.
- Change pending candidate target/AI/IVR and source/destination policy. Approved
  routing stays pinned until an explicit full bundle activation.
- Try protected policy with stale legacy XML and vice versa. Preparation rejects
  the pair. Try a private extension with an unconditional transfer inserted
  before a valid revision condition; preparation rejects it.
- Corrupt/remove each approved member; replace current/receipt inconsistently.
  New SIP work stays closed; correct peer cleanup remains possible.
- Inject failure at native parse, either runtime hash/mount visibility check,
  FS reload/readback and final pointer publication. No partial generation opens.
- Run two activations concurrently and restart each peer during the sequence.
  Locking/receipt/hash verification must recover one complete reviewed generation
  or stay closed. Never expose mixed public/private/edge revisions.
- Prove the active approved directory and its parent are inaccessible to the
  controller writer, even if it can rewrite every candidate file.
- Prove5-profile guard/receipt inventory remains truthful after loader reload and
  the preserved startup watcher; no hidden/ignored profile is acceptable.

## Evidence and cleanup

Record only source/image hashes, synthetic topology, bounded case IDs, command
statuses, sanitised TLS role/version/suite identifiers, delivery/audio assertions
and outstanding failures. Mark NOT RUN/FAIL honestly; never promote planned
cases to passing results. No production account resources or real phone numbers
are touched.

Stop/remove only containers carrying this exact run label and the exact recorded
network name; remove the test network, unmount this exact tmpfs and remove its
empty mountpoint. Capture cleanup verification. Do not use prune or wildcard
deletion. Keys disappear with the tmpfs and must never enter the evidence tar.

Passing this isolated matrix still does not prove public ingress, provider
certificate/codec interoperability, actual NAT/staff answer, or production
availability. Those remain separately bounded, authorised operational checks.
