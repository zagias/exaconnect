# CommAI inbox load test

Date: 2026-10-07. Machine: a shared cloud container (4 vCPU, 15 GB), not the
production server. The load generator, the controller and PostgreSQL all ran
on the same 4 cores, so absolute numbers are a floor, not a capacity figure.

## What it does

`controller/tests/load/inbox_load.py` (not collected by pytest) starts the
real controller with uvicorn on a local port, one process, its CommAI job
worker running in-process as in production (`EXA_ROUTING_INTERVAL_S` > 0),
against an emptied test database. It seeds one business with M agents, a
team, a routing rule, a website chat key and a simulated SMS number, then
for a fixed time:

- N customers: half on website chat (widget API, each from its own address
  via `X-Forwarded-For`, polling for replies), half on SMS (signed webhooks
  from the simulated provider, all from one provider address, as Twilio's
  are). Each sends a numbered message about every 2 s (jittered). A refused
  webhook is retried up to 3 times after `Retry-After`, as Meta and
  360dialog do; one still refused counts as lost.
- M agents: each lists the inbox about once a second, reads the
  conversations it owns (a fixed share, so two agents never answer the same
  one) and answers each unanswered customer message with a numbered reply
  (`Idempotency-Key`, `take_over`).

Afterwards it waits for the send queue to drain and checks from the
database that every customer message is stored exactly once, in that
customer's one conversation, and every reply is stored once, marked sent,
handed to the SMS provider exactly once and in the order written.

```
python3 controller/tests/load/inbox_load.py --db postgresql://exa:ci-only@127.0.0.1:5432/exatest_t \
    --customers 50 --agents 10 --duration 60 --sms-rate 6000
```

`--sms-rate` raises Trinidad and Tobago's SMS pace (default 60 a minute,
`PUT /api/v1/commai/countries/TT/sms-rules`) so the platform, not the
country rule, is measured. At the default pace the first run refused 881
replies with "The SMS rate for Trinidad and Tobago (60 a minute) is
reached", which is that rule working as designed.

## Results (60 s each)

| Customers / agents | Requests/s | Inbound stored | Replies sent | Errors | Correctness |
| --- | --- | --- | --- | --- | --- |
| 50 / 10 | 101 | 1,453 | 1,404 | 0 | all exactly once, in order |
| 200 / 20 | 141 to 146 | 5,005 to 5,189 | 973 to 1,012 | 0 to 2 (see below) | all exactly once, in order |
| 400 / 40 | 154 | 6,651 | 418 | 0 | all exactly once, in order |

Latency in ms (p50 / p95 / p99):

| Endpoint | 50 / 10 | 200 / 20 | 400 / 40 |
| --- | --- | --- | --- |
| widget: send message | 23 / 68 / 118 | 225 / 1,002 / 1,277 | 1,146 / 3,204 / 3,692 |
| widget: poll messages | 13 / 37 / 47 | 209 / 615 / 829 | 1,157 / 1,519 / 2,620 |
| sms: provider webhook | 25 / 77 / 162 | 237 / 1,065 / 1,363 | 1,178 / 3,112 / 3,572 |
| agent: list conversations | 18 / 62 / 116 | 365 / 1,246 / 1,824 | 1,820 / 3,542 / 4,079 |
| agent: read messages | 15 / 51 / 68 | 343 / 754 / 1,386 | 1,889 / 3,274 / 3,590 |
| agent: send reply | 32 / 99 / 126 | 421 / 879 / 1,969 | 2,059 / 3,861 / 4,142 |

The send queue drained within 0.5 s of the end at 50 customers, 9.5 s at 200
and 4 s at 400 (fewer replies were written).

## What degrades, and where

- **About 100 requests a second is comfortable; around 150 is the ceiling**
  for one controller process on this machine. Past it, latency grows (p50
  above 1 s at 400 customers) and agents' work slows (418 replies in a minute
  at 400 customers against 1,404 at 50), but nothing is lost, duplicated or
  sent out of order. The controller runs as one uvicorn process with a
  10-connection database pool; more processes (`--workers`, or more
  containers behind nginx) are the first lever, since the rate limits and
  job queue already live in PostgreSQL.
- **Provider webhooks were rate-limited per sending address (fixed).** At
  50 customers, 25 of 653 SMS webhooks got 429, because every webhook from
  the provider's one address shared a 600-a-minute budget (and so would every
  business on that provider). The retries saved them here, but Twilio does
  not retry a refused message webhook. Webhooks are now counted per webhook
  address (`EXA_WEBHOOK_RATE_PER_MIN`, 6,000 a minute); see the commit
  "Rate limits: provider webhooks get a budget per webhook address".
- **Two HTTP 500s on the SMS webhook** in the first 200-customer run (both
  succeeded on the provider's retry, nothing lost); not reproduced in two
  further runs with the server log captured, so the cause is not known.
- **Website visitors behind one address.** Unsigned widget calls are still
  limited per client address (600 a minute). Caribbean mobile networks put
  many phones behind one address (carrier-grade NAT), so a busy site could
  see 429s on the widget from one carrier's address. Keying the widget's
  budget by its session token would avoid it; not changed here.
