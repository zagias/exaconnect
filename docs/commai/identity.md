# Identity and backups (operator note)

Design: [ADR 0017](../adr/0017-identity-and-backups.md). Provider templates and directory connectors: [directory.md](directory.md) (ADR 0036).

## What it does

- Portal sessions in a secure HttpOnly cookie (`exa_session`); Bearer tokens
  and API keys still work for scripts. Cookie writes need
  `X-Requested-With: exa-portal`.
- Two-step sign-in: authenticator app (TOTP), passkeys, recovery codes.
  Your account page, "Two-step sign-in".
- Google and Microsoft sign-in through Keycloak (existing accounts only).
- Enterprise SSO (SAML 2.0, OpenID Connect) per business, with email-domain
  routing and "require SSO". CommAI Settings, "Sign-in" tab
  (`portal/src/pages/commai/settings/SignIn.tsx`).
- SCIM 2.0 provisioning with directory groups mapped to teams, seats and
  (after a second approval) business-admin rights.
- Encrypted nightly backups with a restore test.

## Endpoints (under /api/v1)

| Endpoint | Who |
| --- | --- |
| `POST /auth/login`, `POST /auth/login/mfa`, `POST /auth/login/passkey/options`, `POST /auth/login/passkey` | anyone |
| `GET /auth/providers`, `POST /auth/sso/discover`, `GET /auth/oidc/start?idp=&next=`, `GET /auth/oidc/callback` | anyone |
| `GET /auth/two-step`, `POST /auth/two-step/start`, `/confirm`, `/disable`, `/recovery-codes` | signed in |
| `POST /auth/passkeys/options`, `POST /auth/passkeys`, `DELETE /auth/passkeys/{id}` | signed in |
| `GET/POST /customers/{id}/sso-connections`, `PATCH/DELETE .../{cid}`, `POST .../{cid}/test`, `/enable`, `/disable` | business admin |
| `GET/POST /customers/{id}/scim-tokens`, `DELETE .../{tid}` | business admin |
| `GET /customers/{id}/directory-groups`, `PUT .../{gid}`, `POST .../{gid}/approve-admin` | business admin |
| `GET /sso-domains?state=pending`, `POST /sso-domains/{id}/approve` or `/reject` | ExaCarib admin |
| `/scim/v2/ServiceProviderConfig`, `/ResourceTypes`, `/Schemas`, `/Users`, `/Groups` | SCIM token |

"Business admin" means a full customer account of that business (or an
ExaCarib admin). Directory-provisioned people are not, until an approved
admin mapping.

## Environment

| Variable | Purpose |
| --- | --- |
| `EXA_COOKIE_SECURE` | `1` (default) marks the cookie Secure |
| `EXA_PUBLIC_URL` | portal address, used for the OIDC redirect and passkeys |
| `EXA_OIDC_ISSUER`, `EXA_OIDC_CLIENT_ID`, `EXA_OIDC_CLIENT_SECRET` | the gateway; SSO is off while empty |
| `EXA_OIDC_IDPS` | sign-in buttons, default `google` (see the ADR on Microsoft) |
| `EXA_KEYCLOAK_ADMIN_CLIENT_ID`, `EXA_KEYCLOAK_ADMIN_CLIENT_SECRET` | live enterprise SSO set-up; simulated while empty |
| `KEYCLOAK_DB_PASSWORD`, `KEYCLOAK_ADMIN_PASSWORD`, `EXA_SSO_HOSTNAME` | the Keycloak container |
| `GOOGLE_CLIENT_ID/SECRET`, `MICROSOFT_CLIENT_ID/SECRET` | OAuth apps Dudley creates (free) |
| `EXA_BACKUP_PASSPHRASE` | encrypts backups (16+ characters); keep a copy offline |
| `EXA_BACKUP_DATABASE_URL` or `EXA_BACKUP_COMPOSE_FILE` | where the database is |
| `EXA_BACKUP_DIR`, `EXA_BACKUP_KEEP_DAYS`, `EXA_BACKUP_RCLONE_REMOTE` | where, how long, off-site |

## Turning on the gateway (when the OAuth apps exist)

1. Create a Google OAuth client (web) and a Microsoft Entra app registration.
   Redirect URI for both: `<EXA_SSO_HOSTNAME>/realms/exacarib/broker/google/endpoint`
   (and `.../microsoft/endpoint`).
2. Put the values in `.env`, plus random values for the Keycloak passwords and
   the two client secrets. `EXA_OIDC_ISSUER=<EXA_SSO_HOSTNAME>/realms/exacarib`.
3. `docker compose -f deploy/docker-compose.yml --profile sso up -d`, then
   restart the controller. Publish Keycloak through the public proxy; the
   controller must be able to reach the issuer URL too.

## Backups

```
sudo install -d -m 700 /etc/exaconnect
sudo install -m 600 deploy/backup/backup.env.example /etc/exaconnect/backup.env   # fill it in
sudo cp deploy/systemd/exaconnect-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now exaconnect-backup.timer
```

By hand: `deploy/backup/backup.sh`, `deploy/backup/restore-test.sh [file]`,
`deploy/backup/restore.sh <file> <empty database>`.

## Tests

```
cd controller
EXA_TEST_DATABASE_URL=postgresql://exa@127.0.0.1:5432/exatest_identity python -m pytest -q tests/test_identity_*.py
```

`test_identity_backup.py` runs the real scripts against the test database
(skipped without `pg_dump`). OIDC uses a fake issuer; passkeys a software
authenticator.
