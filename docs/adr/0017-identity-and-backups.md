# ADR 0017: Sign-in, single sign-on, provisioning and backups

Date: 2026-10-06. Status: accepted.

## Context

CommAI phases 0 to 2 need the sign-in the architecture describes: one gateway
in front of every ExaCarib product, Google and Microsoft sign-in, enterprise
single sign-on per business, two-step sign-in, sessions in a secure cookie
instead of browser storage, and user provisioning from a business's
directory. Phase 0 also needs database backups with a restore that is tested,
not assumed. Connect had local accounts with a bearer token kept in
`localStorage`.

## Decision

**Sessions.** `/auth/login` still returns the token in the body (the SDK,
Terraform and lab scripts use Bearer tokens unchanged) and also sets
`exa_session`: HttpOnly, SameSite=Lax, Path=/, Secure unless
`EXA_COOKIE_SECURE=0`. The portal no longer stores a token; it sends
`credentials: same-origin` and learns who is signed in from `/auth/me`. A
cookie-authenticated POST, PUT, PATCH or DELETE must carry
`X-Requested-With: exa-portal`, which a cross-site form cannot set. Logout
clears the cookie; a password change from the portal rotates it and ends every
other session.

**Two-step sign-in.** TOTP (RFC 6238, standard library, tested against the RFC
vectors), with a confirm-by-code enrolment, ten single-use recovery codes
stored as scrypt hashes, and disable only with a code. A used time step can't
be replayed. Passkeys (WebAuthn, the Duo Labs `webauthn` library) are a second
step too; the first passkey or authenticator app comes with recovery codes.
With two-step on, the password step returns `{"mfa_required", "challenge",
"methods"}`: a five-minute, single-use challenge that allows five tries and
counts towards the existing sign-in throttle. The TOTP secret has to be
readable to check codes, so it is stored as is; backups are encrypted.

**Gateway.** Keycloak (self-hosted, no licence cost) runs under the compose
profile `sso` with its own Postgres, so the gateway's data never mixes with
the controller's. The realm import (`deploy/keycloak/exacarib-realm.json`)
creates the portal client (confidential, PKCE S256), a service-account client
allowed only to manage identity providers, Google and Microsoft brokers, and a
mapper that puts the identity provider used into the ID token. The controller
does the authorization-code flow with PKCE itself and verifies the ID token
with `cryptography` (RS256 against the JWKS; issuer, audience, expiry, nonce).
State, nonce and verifier are stored server-side and used once. Google and
Microsoft sign-in only match an existing account by verified email; they never
create one.

Microsoft is off by default (`EXA_OIDC_IDPS=google`). Keycloak's Microsoft
broker takes the account's `mail` attribute, which a tenant admin can set to
any address, so the realm ships with that email untrusted, and the controller
refuses unverified emails. Turning it on needs a decision: accept that risk,
or send businesses on Microsoft Entra ID through enterprise SSO, which is tied
to their own tenant.

**Enterprise SSO.** A business admin adds a SAML 2.0 (metadata XML or URL) or
OpenID Connect (discovery URL, client ID, secret) connection. It is pushed to
Keycloak as a hidden identity provider; the client secret goes to Keycloak and
is never stored by the controller. A test sign-in records the result without
creating a session; only a tested connection can be switched on. Email-domain
claims route nobody until an ExaCarib admin approves them, one owner per
domain, and public mailbox domains can't be claimed. `POST /auth/sso/discover`
gives the sign-in screen its email-first answer. With `require_sso`, password
and Google/Microsoft sign-in are refused for that domain (ExaCarib admins are
exempt so ExaCarib can't be locked out). A business's provider can only sign
in people from its approved domains who belong to that business; someone new
from such a domain gets a customer account with member rights. The Keycloak
admin API sits behind a `KeycloakAdmin` interface with a simulated
implementation used whenever its service-account settings are missing.

**Provisioning.** SCIM 2.0 at `/api/v1/scim/v2` (Users, Groups,
ServiceProviderConfig, ResourceTypes, Schemas; `eq` filters; PATCH; paging)
with per-business bearer tokens kept hashed and shown once. Created people
are customer accounts of that business with an unusable password. Switching
someone off, or deleting them, ends their sessions and revokes their API keys
in the same transaction; a deleted account is kept, switched off and hidden
from SCIM so history still names who did what. An existing hand-made account
of the same business is linked, not duplicated, and keeps its rights.

**Rights from groups.** Each directory group becomes a CommAI team and maps to
a seat. Provisioned people get member rights only: CommAI read, write and
notes, no network API, no business settings, and they can't make a stronger
API key (an account limit works like a scoped key, ADR 0016). A mapping that
grants business-admin rights (the full organisation account) stays pending
until a different business admin, or an ExaCarib admin, approves it. Leaving
the group removes the rights again.

**Backups.** `deploy/backup/backup.sh` takes `pg_dump -Fc` inside one
repeatable-read snapshot and counts key tables in that same snapshot, then
encrypts with `openssl enc -aes-256-cbc -pbkdf2` (200,000 iterations) using
`EXA_BACKUP_PASSPHRASE`, writes a checksum, keeps 14 days and, if
`EXA_BACKUP_RCLONE_REMOTE` is set, copies off-site with rclone.
`restore-test.sh` restores into a scratch database (TimescaleDB pre/post
restore when the dump has it), compares the counts and drops the scratch
database. A systemd timer runs both nightly.

Every sign-in, failed sign-in, two-step change, SSO setting change, domain
decision, SCIM write and mapping change is audited.

## Consequences

- Not live until Dudley creates the Google and Microsoft OAuth apps (free) and
  sets the gateway's values in `.env` (`EXA_OIDC_*`, `EXA_PUBLIC_URL`,
  `EXA_KEYCLOAK_ADMIN_CLIENT_*`, `KEYCLOAK_*`, `GOOGLE_*`, `MICROSOFT_*`). Until
  then the sign-in screen shows no provider buttons and enterprise SSO
  connections are kept by the simulated gateway (the screen says so).
- Tests use a fake issuer (an RSA key made in the test) and a software
  passkey authenticator. The Keycloak REST client, the realm import and the
  TimescaleDB restore path are written from the published documentation and
  have not been run against a live Keycloak or TimescaleDB here.
- The off-site copy waits on Dudley choosing and configuring an rclone remote.
- `openssl enc` CBC has no authentication tag; the checksum file catches
  corruption, not a deliberate edit. Keep backups somewhere only ExaCarib can
  write.
- Signing out of ExaCarib does not yet sign out of the company's provider.
- A business-level "require two-step for everyone" setting is not built.
- `EXA_COOKIE_SECURE=1` is the default. Browsers accept Secure cookies on
  `http://localhost`; if one does not over the lab's SSH tunnel, set it to 0
  there only.
