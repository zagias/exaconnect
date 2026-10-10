# 0043 Organisation set-up: one place for company, locations and people

Date: 2026-10-10
Status: accepted

## Context

Dudley, trying the portal as a new administrator, could not see how to set up
his company, add several sites or manage users. Walking the first-run journey
showed why:

- A new owner landed on an empty Overview with nothing saying what to do first.
- Owners could not add a Connect site at all. Only ExaCarib staff could, and the
  form asked for an ASN and an overlay host number.
- People were managed in five places with two different role sets.
- "Site" meant three things: a Connect site, a phone site (emergency address)
  and a Jibsy location (opening hours), each typed in separately.
- Company-wide settings (sign-in, company name) were spread across app menus.
- ExaCarib staff tools (agents, releases, partners) sat in the customer menus.

## Decision

1. **Organisation area.** The app switcher gains "Organisation", holding the
   set-up checklist, Company details, Locations, People and access, Sign-in and
   security, Apps and plans, Billing, and the person's own profile.
2. **Set-up checklist** (`/org/setup`, `GET /orgs/{id}/setup`): company
   details, locations, people, then one step per app on the plan (connect each
   location; set up Jibsy). Owners and admins see a "Continue set-up" band until
   it is done or they hide it. Invited owners and admins land on it.
3. **One locations list** (`org_locations`). Each branch is entered once. Saving
   it keeps the Connect site (time zone, place), the phone site (emergency
   address, when there is a street address and phones are enabled) and the Jibsy
   location (opening hours) in step. Existing records are joined to a location
   of the same name the first time the list is read, Connect sites first.
4. **Owners connect a location themselves**: carrier, kind of link and speed for
   up to two terrestrial links and one satellite link. The controller picks the
   ASN, overlay host and path names and returns a 72-hour install code.
5. **Operations area** for ExaCarib staff: organisations, sites and links,
   accounts, billing, agents, partners, releases, go-live, support and audit.
   "New organisation" creates the organisation with its plans and invites its
   owner in one step; the first admin to accept an organisation with no owner
   becomes its owner.

## Consequences

- Phone sites and Jibsy locations can still be made on their own pages; they
  join the shared list by name. The Jibsy "new location" form now points to
  Organisation › Locations.
- The checklist reads its state from existing records, so organisations made
  before this change show their real progress.
- A location with a Connect site or phone site cannot be deleted from the list;
  the site has to go first.
- Customers still cannot choose ASNs or interface names. Staff can change them
  under Operations › Sites and links.

## Testing on the live portal

`lab/ci/checks/org-setup.sh` runs `deploy/uat/org-setup.mjs` against the live
portal after every deploy: a staff test account (`setup-test-staff@exacarib.local`,
given a fresh random password each run that is never stored or printed) creates
an organisation named "Set-up check …", and its owner sets it up end to end. The
next run removes the earlier test organisation and its `@setup-check.example`
people first, so at most one test organisation is on the live list at a time.
