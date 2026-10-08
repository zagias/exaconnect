# Jibsy voice stage 5: operator note

See ADR 0033. The code is in `controller/exaconnect_controller/commai/voice/`:
- `countries.py`, `porting.py`, `emergency.py`, `carriers.py`, `fraud.py` and
  `livekit.py`;
- the provider interface in `provider.py`;
- the API in `commai/api/voice_global.py`;
- the tables in `commai/sql/64_voice_global.sql`;
- the portal in `portal/src/pages/commai/voice/Global.tsx`;
- the edge config in `deploy/kamailio/` and `deploy/livekit/`.

## Switching things on (ExaCarib admins, `/api/v1/commai/golive`)

| Capability | Key | What it allows |
| --- | --- | --- |
| feature | `voice-numbers-XX` | Searching and ordering numbers in country XX. If the countries module has declared the `country` XX, that must be on too. |
| feature | `voice-porting-XX` | Port orders for numbers in country XX. |
| carrier | `sim-carrier-1`, `sim-carrier-2` or a new carrier's key | That carrier carrying a business's calls. |

All start off. Record each criterion as met, with evidence, and then set the
capability to `pilot` (named businesses) or `on`.

## Endpoints

These are under `/api/v1/commai/customers/{id}/voice`, for the business's voice
admins unless noted:

| What | Endpoints |
| --- | --- |
| Countries and numbers | `GET /countries`, `GET /numbers/search?country=&area=&contains=`. Order through `POST /orders` with `numbers: {new, country, area, choose: [e164]}`. |
| Port orders | `GET/POST /port-orders`, `GET/PATCH /port-orders/{id}`, `POST /port-orders/{id}/submit` (spend permission and the price shown), `POST /port-orders/{id}/documents` (multipart `doc_type` + `file`), `GET/DELETE /port-orders/{id}/documents/{doc}`, `POST /port-orders/{id}/reschedule`, `POST /port-orders/{id}/cancel` |
| Emergency | `GET /emergency`, `GET /emergency/rules`, `POST /emergency/sites/{id}/validate`, `PUT /emergency/users/{voice_user_id}` (own address, or `null` for the site's) |
| Each person | `GET/POST /me/emergency-notice` (read and acknowledge) and `GET /notifications`, `POST /notifications/read` |
| Fraud | `GET/PUT /fraud` (allowing a high-risk prefix needs spend permission), `POST /fraud/restore` (spend permission), `GET /route-attempts?call_id=` |

ExaCarib admins use `/api/v1/commai/voice-admin`:
- `GET/POST /carriers` and `PUT /carriers/{key}/rates`;
- `POST /carriers/{key}/options` (OPTIONS now);
- `POST /carriers/{key}/import?period=` and `GET /carriers/{key}/reconcile?period=`;
- `GET/PUT /routing` and `DELETE /routing/{prefix}`;
- `GET /route?customer_id=&to=` (the route plan, with reasons);
- `GET /kamailio` (rendered files);
- `GET/PUT /high-risk` and `DELETE /high-risk/{origin}/{prefix}`.

The LiveKit agent worker uses `/api/v1/commai/voice/livekit/calls`,
`/calls/{customer}/{conversation}/turns` and `/end`, with
`Authorization: Bearer $EXA_LIVEKIT_AGENT_SECRET`.

## Events and jobs

Events:
- `voice.port_submitted`, `voice.port_documents_needed`, `voice.port_rejected`,
  `voice.port_scheduled`, `voice.port_completed`, `voice.port_rolled_back`,
  `voice.port_cancelled` and `voice.port_updated`;
- `voice.emergency_submitted`, `voice.emergency_validated` and
  `voice.emergency_rejected`;
- `voice.outbound_blocked` and `voice.outbound_enabled`;
- `voice.fraud_suspended` and `voice.fraud_restored`, with `voice.fraud_alert`
  from stage 4.

Jobs: `voice.port_poll`, `voice.port_cutover`, `voice.emergency_check` and
`voice.trunk_options`.

## Settings

| Name | Meaning |
| --- | --- |
| `EXA_SIP_PROVIDER` | `simulated` (the default) or `sip`. `sip` refuses until `EXA_SIP_PROVIDER_URL`, `EXA_SIP_PROVIDER_KEY` and `EXA_SIP_PROVIDER_ACCOUNT` are set. |
| `EXA_KAMAILIO_DIR` | Where the controller writes Kamailio's `dispatcher.list` and `address.list` (`/data/kamailio` in compose). |
| `EXA_KAMAILIO_PBX_NETS` | FreeSWITCH's addresses (CIDR, comma-separated) in `address.list` group 2. `make voice-up` writes it for `EXA_VOICE_EDGE=kamailio`. |
| `EXA_PBX_SECRET` | Shared by the controller and FreeSWITCH: the dial plan signs its check before each outside call with it (`GET /api/v1/commai/internal/voice/authorise`, `voice/pbx.py`). Unset: every outside call is refused. |
| `EXA_PBX_CONTROLLER_URL` | Where FreeSWITCH reaches the controller (default `http://controller:8000`), rendered into the dial plans. |
| `EXA_VOICE_EDGE` | `provider` (default) or `kamailio`: which gateway `make voice-up` installs for outside calls (`deploy/kamailio/README.md`). |
| `EXA_LIVEKIT_URL`, `EXA_LIVEKIT_API_KEY`, `EXA_LIVEKIT_API_SECRET`, `EXA_LIVEKIT_AGENT_SECRET` | LiveKit for AI phone calls. Nothing runs until they are set. |

## Testing

```
cd controller
EXA_TEST_DATABASE_URL=postgresql://exa@127.0.0.1:5432/exatest_voice \
  python -m pytest -q tests/test_commai_voice_global.py
```

The simulated provider answers after `provider.SIM_DELAYS` seconds (120 s for
ports, 30 s for addresses; the tests set 0). `provider.FAULTS` takes these
fault keys:
- `port_reject`, `port_activate`, `emergency_reject`;
- `options:<carrier>`, `call:<carrier>`;
- the stage 4 keys.

An account number ending `0000` is rejected by the simulated losing carrier. A
port without a letter of authorisation and a recent bill comes back as
"documents needed".
