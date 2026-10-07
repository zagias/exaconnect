# ADR 0023: Shared accounts, organisation roles and product plans

Date: 2026-10-07. Status: accepted.

## Context

Until now a customer account had one role (`customer`) and one organisation
(`users.customer_id`), and only ExaCarib admins made accounts. Real
organisations need several people with different rights, people who work for
more than one organisation (a group IT team, a managed-service partner), and
self-service invitations. Connect and CommAI are also sold as separate plans:
an organisation may hold either or both.

## Decision

**Memberships.** `org_memberships (customer_id, user_id, role, managed_by)`
with roles:

| Role | May |
| --- | --- |
| owner | everything an admin can, make owners and hand ownership on |
| admin | manage people and settings (customer settings, SSO, SCIM, CommAI settings, voice admin) |
| member | make changes (classes, traffic rules, Storm Mode, conversations) |
| viewer | read only |

`users.customer_id` stays as the person's primary organisation. A trigger on
`users` gives every customer account a membership in its primary
organisation, so every path that already sets it (ExaCarib admin, SCIM, SSO,
seed, tests) keeps working. The migration (`commai/sql/15_memberships.sql`) is
idempotent: existing hand-made accounts become admins (the rights they had),
the earliest in each organisation becomes its owner, and directory accounts
become admins or members by their scopes. Carrier and ExaCarib admin accounts
have no memberships and are unchanged.

An organisation always keeps at least one owner: the last owner cannot step
down, be removed or leave, and the owner rows are locked while that is
checked. Deleting a user who was the only owner promotes the longest-standing
hand-managed admin.

**Directory rules (SCIM and SSO).** A membership made by the directory
carries `managed_by` = `scim` or `sso`. The portal does not change, remove or
transfer ownership to such a membership, and the person cannot leave it by
hand; the directory owns it. `scim.refresh_rights` moves the membership's role
with the person's directory rights (admin or member). A directory-provisioned
account keeps its account-wide `access_scopes` (CommAI only, no network API)
in every organisation, so it never gains rights through a membership.
An enterprise SSO sign-in starts a session in the business whose provider
vouched, and that provider vouches only for its own members.

**Invitations.** Owners and admins (and ExaCarib admins) invite by email as
admin, member or viewer; ownership is handed on, not invited. The token is
256 bits from `secrets`; only its SHA-256 is stored, it expires in 7 days and
works once (the claim is a conditional `UPDATE`). A new invitation replaces an
earlier one for the same address. No email is sent yet: the link is shown
once to the inviter, and CommAI's simulated email sender records the notice
in `sim_channel_outbox` without the link. `GET /invites/{token}` and
`POST /invites/{token}/accept` work signed out: a newcomer chooses a password
(refused when their domain requires SSO, without using up the invitation);
someone with an account signs in first, and the invitation must match their
email.

**Current organisation.** `sessions.customer_id` is the organisation a
session acts for (NULL: the primary). `POST /auth/organisation` switches it
after checking membership; another session of the same person is
unaffected. An API key records the organisation it was made in
(`api_keys.customer_id`) and acts there only; it can't switch, and it is
revoked when its owner leaves or is removed from that organisation.

**Enforcement.** `deps.current_user` resolves the membership on every request
and sets `User.customer_id` to the current organisation, `User.org_role` and
`User.products`. Because every existing check (`check_customer`,
`customer_scope`, `access.check`, the CommAI queries) already reads
`User.customer_id`, they all follow the current organisation without
changing, and none is weakened. In addition:

- viewers get 403 on every non-GET request, in one place (`current_user`),
  except their own sign-in (`/auth/…`), accepting invitations and leaving;
- a customer account with no organisation left gets 403 on everything but
  its own sign-in;
- `require_org_manager` guards people, invitations and customer settings;
  `access.require_scope("commai:admin")` and voice admin also need an owner or
  admin, on top of the key's scopes;
- CommAI member lists, team members, assignment and voice permissions use
  memberships instead of `users.customer_id`; viewers can't be assigned
  conversations.

**Plans.** `customers.products text[]` (`connect`, `commai`), nullable,
defaulting to both; NULL also reads as both. The billing work reconciles this
column with subscriptions when it merges. `deps.require_product(p)` is a
router dependency on every Connect router and on the signed-in CommAI
routers; the CommAI live socket checks it itself. It answers 403 "Your
organisation doesn't have the … plan." ExaCarib admins bypass it, and carrier
accounts keep their Connect carrier view. Shared endpoints (sign-in,
organisations and people, `/customers/mine`, SSO and SCIM) need no plan.
ExaCarib admins set plans with `PUT /customers/{id}/products` (audited).

**API.** `GET /orgs/{id}/members`, `PATCH|DELETE /orgs/{id}/members/{user}`,
`POST /orgs/{id}/leave`, `POST /orgs/{id}/transfer-ownership`,
`GET|POST /orgs/{id}/invites`, `DELETE /orgs/{id}/invites/{invite}`,
`GET /invites/{token}`, `POST /invites/{token}/accept`,
`POST /auth/organisation`, `PUT /customers/{id}/products`. `/auth/me` returns
`org_role`, `organisation`, `products` and `memberships`, each with its
plans. Every write is audited (`org.*`, `customer.products`).

**Portal.** Account > People (members, roles, pending invitations, the
invite form with the one-time link), `/invite/:token` outside the sign-in
gate, an organisation switcher in the header for people in more than one,
and nav groups hidden for plans the organisation doesn't hold.

## Consequences

- Tenant isolation is tested by walking every organisation-scoped GET in the
  OpenAPI schema as another organisation's owner, and every list endpoint with
  the other organisation's id in the query.
- `users.customer_id` remains for SCIM (a directory sees the people whose
  primary organisation is its own) and as the default organisation. Leaving
  the primary organisation moves the primary to the next membership.
- An invitation email sender is a later change: replace the simulated notice
  with the real sender and include the link only in the sent message.
- SSO for an organisation other than the primary one: an enterprise SSO
  sign-in acts for the vouching business; a membership elsewhere is reached
  by switching.
