# ADR 0019: CommAI AI runtime: three roles, knowledge, safe actions and browser calls

Date: 2026-10-06. Status: accepted.

## Context

The CommAI plan ("Three AI roles", "How AI acts safely", phases 1 and 2,
voice stage 1) asks for AI that answers a business's customers, helps its
staff and its admins, and never acts on its own say-so. The foundation
(ADR 0016) already had the inbox's one-handler rule, private notes in their
own table, `actions.propose` with per-role tool lists, durable jobs and
usage limits. Connect's Ask (ADR 0006, 0015) already called an
OpenAI-compatible model on DeepInfra. No AI key is set in tests or the lab,
DeepInfra speech has no agreed budget, and pgvector is not installed.

## Decision

- **One runtime, three role profiles** (`commai/ai/runtime.py`):
  `customer_agent`, `copilot`, `platform_assistant`. Each has its own system
  prompt, the kinds of tools it may ever hold (the copilot and platform
  assistant only read), token and history limits, and a usage meter. The tools
  a role may use for a business are the rows in `commai_role_tools`, edited on
  the AI agents screen from the connectors catalogue. The prompt lists them,
  but `actions.propose` is what refuses anything not listed: a test makes the
  model ask for an unlisted booking and checks the service rejects it.
- **The model interface** (`commai/ai/model.py`): `complete(ModelInput) ->
  ModelOutput`, a JSON contract of answer, language, the answer in the
  business language, escalate and reason, intent, cited knowledge chunks, tool
  calls and facts. `OpenAICompatibleModel` reuses the Ask settings
  (`EXA_LLM_API_KEY`, `EXA_LLM_BASE_URL`, `EXA_LLM_MODEL`) and asks for a JSON
  object. Without a key, `SimulatedModel` answers: deterministic, free, and
  honest. It answers only from retrieved knowledge (the best-matching
  sentences), recognises booking requests and collects time, name and contact
  before proposing one, and escalates whenever it isn't sure. It cannot
  translate and says so.
- **Knowledge with sources** (`commai/ai/knowledge.py`): sources are chunked;
  only approved sources are searched; editing a source withdraws approval.
  Retrieval is Postgres full-text search (a generated, weighted `tsvector`
  with a GIN index), behind a `Retriever` interface so **pgvector can replace
  it later** without touching the agent. A chunk is relevant when it covers at
  least half the question's terms (each sentence of a long message is tried
  on its own). Two relevant chunks from different sources that state figures
  and share none are treated as contradictory. Missing or contradictory
  knowledge escalates and records a knowledge gap, grouped by question.
  `add_source(conn, customer_id, *, title, body, source_url, approved,
  created_by)` is the entry point for onboarding.
- **The customer agent** is the job `ai.respond` (`commai/ai/agent.py`), so
  `inbox.ai_available()` is true. It reads customer-facing messages through
  `inbox.messages` (never `commai_notes`), approved knowledge, and the
  contact's record, previous conversations and memory only when the
  conversation's identity is verified. Deterministic business rules run
  before the model: escalation words, a maximum number of AI replies,
  languages the business allows, the agent switched off, hard usage limits.
  Every answer is stored in `ai_runs` with the sources it used, which the
  copilot panel shows staff.
- **Acting safely**: tool calls go through `actions.propose` with role
  `customer_agent` and an `on_success` reply. The AI's own words are checked
  for claims like "confirmed" and replaced when a booking is in play; the
  customer hears "confirmed" only from `actions` after the calendar reports
  success. A failing calendar hands the conversation to a person with a
  holding reply (from `actions`).
- **One voice at a time**: the run happens in a savepoint and replies through
  `inbox.send(author_kind="ai")`, which refuses after a takeover; the savepoint
  is then rolled back, so a run interrupted by a takeover writes nothing (no
  reply, no usage, no run record). The run is claimed (its `ai_runs` row) only
  after the model call, because that row references the conversation and
  would otherwise block a person's takeover while the model thinks.
- **Failure**: any error from the model or a dependency hands the
  conversation to a person with a holding reply and a handover packet
  (summary, what was collected, what was tried, why), records an `ai.failed`
  event, and finishes the job, so it is neither lost nor retried into
  silence.
- **Copilot**: summarise, draft a grounded reply with sources, translate,
  flag missing details, suggest next steps. It reads what the person may read:
  notes only when they (or their key) may read notes. Drafts that quote a
  note are withheld. Nothing is sent by the copilot.
- **Memory**: short facts per contact, written and read only for verified
  identities; staff see and delete them.
- **Languages**: a small stop-word detector picks the customer's language.
  With a model, the reply is in their language and the business-language
  version is kept in `messages.original_body` / `original_language` so staff
  see both. Without one, the reply opens with a fixed sentence in their
  language saying it answers in the business language and a person can help.
- **Voice stage 1**: browser calls. Speech recognition and synthesis run in
  the browser (Web Speech API, free); the server carries text. A call is a
  conversation on channel `voice` with an `ai_calls` record; each caller turn
  is stored and answered in its own transaction. Minutes are metered as
  `ai_voice_minute`. A server-side `DeepInfraSpeech` provider is written
  against DeepInfra's OpenAI-compatible audio endpoints but is **not live**:
  it needs Dudley's agreement to the spend, then `EXA_COMMAI_SERVER_SPEECH=on`
  and `EXA_SPEECH_API_KEY`. It has not been called.

## Consequences

- Tests and the lab run the whole flow with no key and no spend. The
  simulated model is deliberately narrow: it hands over more often than a
  real model would, and its answers are sentences copied from approved
  sources.
- The real model path is written and its JSON parsing is tested, but it has
  not been run against DeepInfra in this build (no key in the session).
- Bookings are interpreted in UTC, matching the simulated calendar's hours.
  The Google Calendar connector should interpret times in the business's
  timezone.
- Website visitors can't start a browser call yet: the call endpoints are for
  signed-in staff ("Try a browser call"). A public endpoint keyed by the
  website chat key is the next step and belongs with the widget.
- Quality review against the business's criteria is not built (optional in
  this phase); `ai_runs` holds what it would need.
