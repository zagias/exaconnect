# 0044 An organisation's own SIP trunks, primary and secondary per location

Date: 2026-10-10
Status: accepted

## Context

Dudley asked whether Jibsy can take each organisation's own SIP trunks, with a
primary and a secondary trunk per location. Until now every outside call went
out through ExaCarib's carriers (ADR 0033), chosen by price or quality per
destination; an organisation could not bring the trunks it already pays a
carrier for, and a call from Port of Spain and one from San Fernando were routed
the same way.

## Decision

1. **A trunk is a carrier row owned by the organisation.** `voice_carriers`
   gains `customer_id`; ExaCarib's carriers have none. Trunk keys are
   `org-<customer>-<random>`. The organisation's trunks need no go-live switch
   (their contract is the organisation's) and have no ExaCarib rate sheet. They
   join Kamailio's dispatcher and allow-lists and the OPTIONS health checks like
   any carrier, so failover works the same way on real calls.
2. **Routing by the caller's location.** The caller's phone site gives their
   location (Organisation > Locations, ADR 0043). Calls try, in order: the
   location's primary trunk, its secondary, the organisation default, then
   ExaCarib's carriers (in their usual price or quality order) unless the
   organisation switches that fallback off. A trunk that is switched off or down
   (no answer to OPTIONS) is skipped; one that refuses the call passes it on
   (Kamailio's failure route on real calls, `carriers.connect` on simulated
   ones). An organisation with no trunks routes exactly as before, and when none
   of its trunks is usable and ExaCarib has no carrier switched on, the standard
   provider takes the call as before.
3. **Billing.** A call carried on the organisation's own trunk has no ExaCarib
   per-minute charge (its carrier bills it). Numbers that arrive on its trunk
   (`source = 'trunk'`) are active at once and carry no monthly number charge.
4. **Numbers belong to a location** through their phone site, which also gives
   the emergency address. Numbers can be moved between locations.
5. **Stable dispatcher set ids.** Each carrier and trunk keeps its Kamailio set
   id for good (`voice_carriers.set_id`); before, set ids were positions in the
   list of enabled carriers, so adding or switching off one renumbered the rest.
   Existing carriers keep the numbers they had.
6. **Kamailio reloads by itself.** `deploy/kamailio/start.sh` runs Kamailio with
   a watcher that runs `dispatcher.reload` and `permissions.addressReload` when
   the controller renders new lists, so trunks added in the portal take effect
   without a restart.
7. **Screens and API.** Phone > SIP trunks: trunks (with a simulated kind that
   can be set to refuse calls or be down, to try failover before a real carrier
   exists), trunks by location, the default and the fallback switch, numbers and
   locations, and a route test. Phone > Billing's test call shows which trunks
   were tried. API under `/commai/customers/{id}/voice/`: `trunks`,
   `trunk-settings`, `locations/{id}/trunks`, `trunks/{id}/numbers`,
   `numbers/{id}/location`, `route`. Voice admins change them; every change is
   audited.

## Limits

- Trunks authenticate by IP address (the carrier's signalling addresses on the
  allow-list). Trunks that need a SIP registration or digest password are not
  supported yet.
- Emergency calls still use ExaCarib's carriers (or the standard provider), never
  an organisation's trunk.
- On real calls, the call record does not yet say which trunk carried it, so a
  real call on the organisation's trunk is still rated by the minute; simulated
  calls are exact. Fixing this needs the carrier from Kamailio in the call record.
- Any trunk may deliver a call to any of the organisation's numbers, as on the
  public network.

## Testing

`controller/tests/test_commai_voice_trunks.py` covers the order, failover on
refusal and on a trunk that is down, the fallbacks, billing, the PBX answer,
stable set ids and isolation between organisations. On live, every deploy runs
`lab/ci/checks/voice-trunks.sh`: a test organisation's owner sets up three
simulated trunks in the portal and places test calls while trunks refuse or go
down, and the check confirms Kamailio reloaded its lists.
