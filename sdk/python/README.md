# ExaConnect Python SDK

Automate ExaConnect, ExaCarib's connectivity platform, from Python: circuits
to clouds and between sites, internet breakout and firewall rules, traffic
rules, plain-English orders, routing decisions, metering and Storm Mode.

```bash
pip install ./sdk/python            # from the ExaConnect repository
export EXACONNECT_URL=https://connect.exacarib.com
export EXACONNECT_API_KEY=exa_...   # Account > API keys in the portal
```

```python
import os
from exaconnect import ExaConnect, ExaConnectError

exa = ExaConnect()                   # URL and key from the environment
cid = exa.me()["customer_id"]
sites = exa.sites.by_name(cid)

aws = exa.circuits.create(
    cid, name="AWS prod", kind="cloud", provider="aws", region="us-east-1",
    a_site_id=sites["site-a"]["id"], peer_address="52.1.2.3",
    secondary_peer_address="52.1.2.4",          # a resilient pair
    psk=os.environ["AWS_VPN_PSK"], cloud_prefixes=["10.100.0.0/16"], bandwidth_mbps=50,
)
exa.circuits.update(cid, aws["id"], bandwidth_mbps=100)   # billed by the hour from now

order = exa.orders.draft(cid, "Send Port of Spain's internet straight out")
print(order["summary"], order["problems"])
exa.orders.confirm(cid, order["id"])                       # nothing changes until this

for d in exa.decisions.list(limit=5):
    print(d["time"], d["reason"])
```

- A key acts as the person who made it, with their access. Keep it in the
  environment or a secrets manager, never in code. The client never prints it.
- Errors raise `ExaConnectError` with the API's reason in `detail`.
- Methods return the API's JSON. The API is described in the repository's
  `docs/*-contract.md` files and at `/docs` on the controller.

`examples/inventory.py` prints a customer's sites, circuits and encryption.

## Integrations

```python
pd = exa.integrations.create(
    "pagerduty", "NOC on-call",
    secrets={"routing_key": os.environ["PD_ROUTING_KEY"]},
    event_types=["path.*", "sla.breach", "node.offline"], min_severity="warning",
)
exa.integrations.test(pd["id"])            # simulated until the controller is set live
print(exa.integrations.metrics())           # Prometheus text for your organisation

# In your webhook receiver: check the Standard Webhooks signature on the raw body.
from exaconnect import verify_event
ok = verify_event(os.environ["CONNECT_WEBHOOK_SECRET"], request.headers, request.body)
```

`exa.hooks`, `exa.notices`, `exa.onramps` and `exa.links` cover REST hooks,
carrier notices, cloud on-ramps and single links. See `docs/integrations.md`.
