# Jibsy AI agents: operator note

The customer AI agent, the employee copilot, knowledge with sources, customer
memory, languages and browser calls. Decision record:
`docs/adr/0019-commai-ai-runtime.md`.

## What runs where

- `controller/exaconnect_controller/commai/ai/`: `model.py` (model interface,
  DeepInfra/OpenAI-compatible and simulated), `runtime.py` (role profiles,
  the business's AI profile, tools, usage), `knowledge.py`, `agent.py` (job
  `ai.respond`), `copilot.py`, `memory.py`, `language.py`, `voice.py`.
- `controller/exaconnect_controller/commai/api/ai.py`: the API.
- `controller/exaconnect_controller/commai/sql/30_ai.sql`: `knowledge_sources`,
  `knowledge_chunks`, `knowledge_gaps`, `ai_runs`, `contact_memory`, `ai_calls`.
- Portal: Jibsy → AI agents (`portal/src/pages/commai/AiAgents.tsx`) and the
  copilot panel beside a conversation (`CopilotPanel.tsx`).
- The business's AI profile lives in `commai_settings.config["ai"]`. Whether
  new conversations go to the AI is the inbox mode (`ai_first`).

## Endpoints

Signed in, under `/api/v1/commai/customers/{customer_id}`:

| Path | What |
| --- | --- |
| `GET /ai/status` | Model in use (simulated or live), agent on/off, mode, speech |
| `GET/PUT /ai/profile` | Name, tone, greeting, languages, memory, holding reply, escalation rules |
| `GET /ai/roles`, `PUT /ai/roles/{role}/tools` | Tools each role may use (from the connectors catalogue) |
| `GET/POST /ai/knowledge`, `GET/PATCH/DELETE /ai/knowledge/{id}` | Knowledge sources |
| `POST /ai/knowledge/{id}/approve` | `{"approved": true\|false}` |
| `GET /ai/knowledge/search?q=` | What the AI would find |
| `GET /ai/gaps`, `POST /ai/gaps/{id}/resolve` | Questions it couldn't answer |
| `GET /ai/conversations/{id}/runs` | Each AI answer with its sources and proposals |
| `POST /ai/conversations/{id}/copilot/{task}` | `summary`, `draft`, `translate`, `missing`, `next_steps` |
| `GET/POST/DELETE /ai/contacts/{id}/memory`, `DELETE .../memory/{fact_id}` | Customer memory |
| `POST /ai/calls`, `POST /ai/calls/{id}/turns`, `GET /ai/calls/{id}`, `POST /ai/calls/{id}/end` | Browser calls |
| `GET /ai/speech` | Speech status |

Reads need `commai:read`; profile, tools and knowledge need `commai:admin` and
a seat other than internal; memory and calls need `commai:write`. Every write
is in the audit log.

## Environment

- `EXA_LLM_API_KEY` (and optionally `EXA_LLM_BASE_URL`, `EXA_LLM_MODEL`): the
  same AI service as Connect's Ask. Empty means the simulated model.
- `EXA_COMMAI_SERVER_SPEECH`, `EXA_SPEECH_API_KEY`, `EXA_SPEECH_BASE_URL`,
  `EXA_SPEECH_STT_MODEL`, `EXA_SPEECH_TTS_MODEL`: server-side speech.
  **Not live**: it waits for Dudley's agreement to the spend. Browser calls
  work without it.

Usage meters: `ai_reply` (one per answered customer message), `ai_tokens`,
`copilot`, `ai_voice_minute`. A hard limit in `usage_limits` hands
conversations to a person.

## How to test

```
cd controller
EXA_TEST_DATABASE_URL=postgresql://exa@127.0.0.1:5432/exatest_ai python -m pytest -q tests/test_commai_ai.py
```

By hand: set the inbox mode to "AI first", add and approve a source on the
Knowledge tab, then write to the business through website chat or use "Try a
browser call". To rehearse bookings, connect the Example calendar in
Integrations, switch on "Book an appointment", and tick it for the customer AI
agent on "Tools it may use".
