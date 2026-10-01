# ADR 0006: AI features beyond SLA routing

Date: 2026-10-01. Status: accepted.

## Context

Dudley asked for four AI features on top of the SLA routing engine: a storm
warning that suggests Storm Mode, a bill-shock forecast, carrier anomaly
flags and an "Ask your network" chat. The brief's principle for AI routing
applies to all of them: start simple and explainable, and show the reason.

## Decision

- **Hurricane watch** (`controller/exaconnect_controller/ai/storms.py`). The
  controller reads the US National Hurricane Center's public active-storms
  feed (`CurrentStorms.json`, no key) every 15 minutes. For each storm and
  each site with coordinates, it projects the storm's current motion forward
  72 hours, an hour at a time, and takes the closest approach. Within 250 km
  (350 km for a hurricane) it raises a critical insight that suggests Storm
  Mode for that site; within 600 km, a warning. It never switches Storm Mode
  on by itself. Dead reckoning is not NHC's forecast track, so every warning
  says so and links the NHC advisory. Using NHC's forecast cone (GIS
  shapefiles) is a later improvement.
- **Bill-shock forecast** (`ai/billshock.py`). Per link, every 5 minutes: a
  hard floor (if more samples are already above a rate than the month can
  discard, the bill can't come in below it) and a forecast (the observed 95th
  percentile, scaled by the last 7 days' trend, clipped to ×0.8 to ×1.5).
  Critical when the excess is locked in, warning when the forecast is above
  commit, and a note once half the month's discard allowance is used.
- **Carrier anomalies** (`ai/anomaly.py`). Every 2 minutes, per site and
  path: the last 15 minutes against the same hour of day over 14 days (or
  the last 24 hours while there is under 3 days of history), using the median
  and the median absolute deviation so past faults don't skew the baseline. A
  flag needs 4 robust standard deviations and a material change (latency
  10 ms and 20%, jitter 5 ms and 50%, loss 0.5 points). Carrier users see
  anomalies on their own links.
- **Ask your network** (`ai/ask.py`). The controller builds a compact snapshot
  of one customer's data (sites, path health, steering, 7 days of decisions,
  a day of events, open insights, month-to-date metering) and sends it with
  the question to an OpenAI-compatible API. DeepInfra with
  `deepseek-ai/DeepSeek-V4-Flash` is the default (`EXA_LLM_BASE_URL`,
  `EXA_LLM_MODEL`). The key is read from `EXA_LLM_API_KEY` and the feature
  is off without it. The model is told to answer only from the snapshot. Each
  question is audited, with 30 questions per user per hour. Carrier users
  can't use it.

The first three run in the controller with no external AI service. They
write `insights` (one open row per key, resolved when the condition clears),
which the portal, the carrier view and the chat all read.

## Consequences

Customer data leaves the controller only for "Ask your network", only for the
asking customer, and only when a key is configured. The hurricane watch
needs outbound HTTPS to nhc.noaa.gov. For demos, admins can raise a labelled
example hurricane near Jamaica from the Insights screen.
