# Directory templates and connectors (operator note)

Design: [ADR 0030](../adr/0030-directory-templates-and-connectors.md). Builds on
[identity](identity.md) (ADR 0017). Covers sign-in for Connect and CommAI.

Portal: CommAI Settings, "Sign-in" tab, card "Connect your directory"
(`portal/src/pages/commai/settings/Directory.tsx`). Pick a provider; the guided
set-up shows (1) your details, (2) the values to paste, with copy buttons,
(3) the steps, (4) the connection test, (5) group mappings and the preview,
(6) Connect.

## Providers

| Provider | Sign-in | People and groups | Go-live key |
| --- | --- | --- | --- |
| Microsoft Entra ID | SAML or OIDC | SCIM, or Microsoft Graph pull | `directory-entra` |
| Google Workspace | SAML | Directory API pull | `directory-google` |
| Okta | SAML or OIDC | SCIM with Group Push | `directory-okta` |
| JumpCloud | SAML or OIDC | SCIM | `directory-jumpcloud` |
| OneLogin | SAML or OIDC | SCIM (SCIM Provisioner connector) | `directory-onelogin` |
| Ping Identity (PingOne) | SAML or OIDC | SCIM outbound | `directory-ping` |
| Auth0 | OIDC or SAML add-on | none (accounts at first sign-in) | `directory-auth0` |
| Active Directory (on-premises) | SAML through AD FS | LDAPS sync | `directory-active-directory` |
| Generic LDAP | none of its own | LDAPS sync | `directory-ldap` |

Every provider starts off. An ExaCarib admin switches one on (or to pilot for
named businesses) in the go-live registry once its criteria are met, including
"tested against a real tenant of this provider".

### Steps per provider (short form; the portal shows the full text with this business's values)

- **Entra ID.** Enterprise applications > New > Create your own (non-gallery).
  SAML: Identifier = our entity ID, Reply URL = our ACS URL, Sign on URL; NameID
  user.mail (email format); group claim for assigned groups, emitted as names.
  OIDC: App registration with our redirect URI and a client secret (goes to the
  gateway only). SCIM: Provisioning > Automatic, Tenant URL = our SCIM address
  plus `?aadOptscim062020`, Secret Token = the token shown once at Connect.
  Graph pull instead: a Global Administrator opens the admin consent link.
- **Google Workspace.** Apps > Web and mobile apps > Add custom SAML app;
  upload Google's IdP metadata here; ACS URL, Entity ID, Start URL; Signed
  response; NameID EMAIL from Primary email; map email, firstName, lastName,
  groups. People: Security > API controls > Domain-wide delegation, add our
  client ID with the three read-only scopes; give an admin email to read as.
- **Okta.** SAML 2.0 app: Single sign-on URL and Audience URI; EmailAddress
  NameID; attribute and group statements. SCIM: enable SCIM provisioning, base
  URL, unique identifier `userName`, HTTP Header auth with the token; Create,
  Update, Deactivate; Push Groups.
- **JumpCloud.** Custom application with SAML: SP Entity ID, ACS URL, Login
  URL; NameID email; memberOf group attribute; export metadata and upload it.
  Identity Management: SCIM 2.0, Base URL, Token Key, test email, Activate.
- **OneLogin.** "SCIM Provisioner with SAML (SCIM v2 Enterprise)": Audience,
  Recipient, ACS URL Validator (regex given), ACS URL, SHA-256; SCIM Base URL
  and Bearer Token; keep name fields in the SCIM JSON Template (OneLogin
  updates with PUT); Rules set groups; deleted users: Suspend.
- **Ping Identity.** SAML application, ACS URL and Entity ID, RSA_SHA256;
  saml_subject Email. SCIM Outbound connection: base URL, OAuth 2 Bearer Token,
  filter `userName eq "%s"`.
- **Auth0.** Regular Web Application: Allowed Callback URLs, Application Login
  URI, client ID and secret; a post-login Action adds the
  `https://exacarib.com/groups` claim. SAML2 add-on: set rsa-sha256 and sha256.
- **Active Directory.** AD FS relying party trust from our SP metadata (or by
  hand); claim rules for email, names and groups; SHA-256. Sync: a read-only
  service account, LDAPS on 636 with a CA-signed certificate (paste an internal
  CA if needed), reachable from ExaCarib (VPN or firewall rule).
- **Generic LDAP.** Read-only bind account, LDAPS, base DN, optional filters.

## What the connection test checks

Metadata or discovery fetch (https, provider's own hosts, no redirects, 500 KB,
10 s) and parse; signing certificate present and in date; the last test sign-in;
for SCIM, the provider's real-shaped requests as a dry run (rolled back) and
whether the provider has called yet; for pull, a real read of the directory.

## Endpoints (under /api/v1; business admin unless noted)

| Endpoint | Purpose |
| --- | --- |
| `GET /directory/templates` | every template (anyone signed in) |
| `GET /customers/{id}/directory` | providers with status and availability |
| `GET /customers/{id}/directory/{provider}` | the guided set-up for this business |
| `PUT /customers/{id}/directory/{provider}` | save details (draft SSO connection; LDAP password to the vault) |
| `POST .../{provider}/test` | connection test |
| `POST .../{provider}/connect`, `/disconnect`, `DELETE .../{provider}` | switch on, off, remove |
| `POST .../{provider}/sync` | one pull sync now (202, queued) |
| `PUT .../{provider}/presets`, `POST .../presets/apply`, `GET .../preview[?live=1]` | group mappings |

## What Dudley must register (free; nothing is created until he does)

1. **Microsoft app for Graph pull.** In ExaCarib's own Entra tenant: App
   registrations > New registration, "ExaCarib Connect directory", accounts in
   any organisational directory (multi-tenant). API permissions > Microsoft
   Graph > Application: `User.Read.All`, `GroupMember.Read.All`. Make a client
   secret. Set `EXA_MS_GRAPH_CLIENT_ID` and `EXA_MS_GRAPH_CLIENT_SECRET` in the
   controller's `.env`. Microsoft may ask for publisher verification before
   other tenants can consent.
2. **Google service account for Directory API pull.** In a Google Cloud
   project: enable the Admin SDK API, create a service account (no roles
   needed), create a JSON key. Put the JSON (or its file path) in
   `EXA_GOOGLE_DIRECTORY_CREDENTIALS`. Each business authorises the service
   account's numeric client ID for the three read-only scopes.
3. **`EXA_SECRETS_KEY`** must be set (already needed by ADR 0020) for LDAP
   bind passwords.
4. A real tenant of each provider (most offer free developer tenants: Entra,
   Okta, Auth0, JumpCloud, OneLogin, PingOne trial) to meet the go-live
   criteria. Google Workspace and Active Directory need a paid or lab tenant;
   ask before spending.

Never set `EXA_DIRECTORY_LDAP_ALLOW_PLAIN` outside the lab.

## Tests

```
cd controller
EXA_TEST_DATABASE_URL=postgresql://exa@127.0.0.1:5432/exatest_dir python -m pytest -q tests/test_identity_directory.py
```

Graph and Google use a fake HTTP transport; LDAP uses an ldap3 mock server.
