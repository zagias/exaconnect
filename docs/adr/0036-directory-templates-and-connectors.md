# ADR 0036: Directory templates and connectors

Date: 2026-10-07. Status: accepted.

## Context

ADR 0017 gave each business standard SAML 2.0 and OpenID Connect sign-in,
email-domain routing approved by ExaCarib, and SCIM 2.0 provisioning with
directory groups mapped to teams, seats and (after a second approval) admin
rights. A business still had to work out, on its own, what its identity
provider wants: which URLs to paste where, which claims to send, which SCIM
settings to tick. Some providers do not speak SCIM at all (Google Workspace
for custom apps, Auth0, Active Directory, LDAP servers), and some speak it
in their own dialect. Sign-in is shared by Connect and CommAI, so this covers
both.

## Decision

**Templates are data.** `identity/directory/templates.py` holds one template
per provider: Microsoft Entra ID, Google Workspace, Okta, JumpCloud, OneLogin,
Ping Identity (PingOne), Auth0, on-premises Active Directory (AD FS plus LDAPS)
and generic LDAP. Each has the provider's metadata or discovery URL pattern,
the claim and attribute names for email, name and groups, the NameID format
and signing algorithm, the SCIM attribute mappings and known quirks, the
steps in plain British English, and the provider's own console label for
each value. `render` fills them for one business: our ACS URL, entity ID,
redirect URI, SP metadata, sign-on URL and SCIM address come from the
business's own gateway alias, made when it saves its first details. Inputs
(tenant ID, Okta domain...) are checked against patterns with examples.

**Reuse, not a second sign-in path.** Saving a template creates or updates
the business's ordinary SSO connection (draft until a test sign-in passes)
and its domains go through the existing approval. Connecting needs a
successful test sign-in, then switches the connection on, makes a SCIM token
(shown once) or schedules the pull sync. Disconnecting switches sign-in off
and revokes the token. `api/sso.py` and `identity/scim.py` are unchanged
except two lines in `api/scim.py` that pass PATCH operations and filters
through the quirk normaliser.

**SCIM quirks.** `quirks.py` brings real-world shapes to the one shape
`identity/scim.py` understands, for every provider: operation names in any
case (Entra's `Replace`), core-schema-URN-prefixed paths and keys (Ping),
single values wrapped in a list, a members value that is one object
(OneLogin), and filter operators in any case (Ping's `Eq`; RFC 7644 makes
them case-insensitive). `scim_fixtures.py` holds real-shaped request
sequences for Entra, Okta, JumpCloud, OneLogin and Ping. The tests run each
through the real SCIM endpoint; the connection test runs the business's
provider sequence as a dry run in a savepoint that is always rolled back.

**Connection test.** Fetches the SAML metadata (or uses the uploaded file) or
the OIDC discovery document and says plainly what is wrong: XML errors with
line and column, an HTML page instead of metadata, service-provider metadata
uploaded by mistake, no signing certificate, an expired or expiring one, a
missing endpoint, an issuer mismatch. Fetches go only over https to the
provider's own hosts (or, for AD FS, a public host name: IP literals,
private and loopback addresses are refused), with no redirects, a 10-second
timeout and a 500 KB cap. The test also reports the last test sign-in and,
for pull connectors, a real read of the directory.

**Pull connectors** (`sources.py`), read-only, one `Snapshot` shape:
- Microsoft Graph: ExaCarib's multi-tenant app, client credentials against
  the business's tenant after its Global Administrator opens the admin
  consent link. Application permissions `User.Read.All` and
  `GroupMember.Read.All`.
- Google Admin SDK Directory API: ExaCarib's service account with
  domain-wide delegation the business grants, acting as one of its admins,
  with the three `.readonly` directory scopes.
- LDAP and Active Directory with `ldap3`: LDAPS only, certificate checked
  (optionally against the business's internal CA), bind DN plus a password
  entered once and kept encrypted in the vault (ADR 0020). Plain LDAP needs
  `EXA_DIRECTORY_LDAP_ALLOW_PLAIN=lab` and a per-set-up switch; never set it
  outside the lab. AD reads `objectGUID` and the disabled bit of
  `userAccountControl`; other servers read `entryUUID`,
  `pwdAccountLockedTime` and `nsAccountLock`.

Only groups whose names start with the business's prefix (default
"ExaCarib") are read; for Graph and Google the people in scope are their
members. Real adapters refuse to run until their environment settings exist;
tests use simulated stand-ins (a fake HTTP transport, an ldap3 mock server).

**Sync.** `sync.apply` turns a snapshot into accounts and groups through
`identity/scim.py`, so ADR 0017's rules hold: member rights only, groups
become teams, admin rights wait for a second approval, an existing
hand-made account is adopted. Someone disabled or gone from the directory is
switched off at once: every session ended, every API key revoked, removed
from synced groups. A sync that would switch off more than half of the people
it made (at least five) is refused as a likely wrong filter. The sync is the
durable job `directory.sync`, which schedules its next run (15 minutes to a
day, default an hour); "Sync now" queues a one-off run.

**Presets.** Each template suggests "ExaCarib Agents", "ExaCarib Internal"
and "ExaCarib Admins". Nothing applies until the business admin accepts them;
an accepted admin preset only asks for admin rights, and a different business
admin (or ExaCarib) still approves it through the existing endpoint.

**Go-live.** Each provider is a go-live feature `directory-<provider>`
(ADR 0028) with criteria including "tested against a real tenant of this
provider". A business can prepare and test while it is off; Connect needs
it on (or in pilot for that business). Each set-up starts off for each
business until that business connects it.

## Consequences

- Nothing here has been run against a real tenant of any provider: the
  provider shapes, console labels and steps are written from each provider's
  published documentation and must be checked when the go-live criteria are.
- The pull connectors need Dudley to register a Microsoft multi-tenant app
  and a Google Cloud service account (both free); see
  `docs/commai/directory.md`.
- Claim names in the templates are guidance for the provider's console. The
  gateway import (ADR 0017) does not yet push per-provider claim mappers to
  Keycloak; groups reach ExaCarib by SCIM or pull, not by sign-in claims.
- An LDAP server inside a business's network must be reachable from the
  controller (VPN or a firewall rule). That is the business's set-up.
- SCIM tokens made by Connect count towards the five-token limit.
