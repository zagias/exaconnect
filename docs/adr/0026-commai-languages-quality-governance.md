# ADR 0026: CommAI languages, AI quality, attachments, governance and team features

Status: accepted, 7 October 2026

## Context

Phase 3 of CommAI asks for the portal and chat widget in the region's languages,
a way to judge conversation quality against each business's own standards, the
AI reading what customers send (files, photos, voice notes), controls on every
change to the AI before it goes live, and the inbox features a team needs day to
day. Nothing paid and no real accounts may be used; every capability starts off.

## Decision

### Languages (`commai/i18n/`, portal `pages/commai/i18n.tsx`)

- JSON catalogues ship with the controller: `en-GB` is the source and holds every
  key; `es`, `fr`, `nl` and `ht` are drafts with the same keys and placeholders
  (a test enforces this). English fills any key a draft lacks.
- Each non-English language is a go-live capability (kind `language`) with three
  extra criteria: catalogue reviewed, formats checked, support in that language.
  It is offered to a business only when `on`, or `pilot` for that business.
- A draft shows "Machine-drafted" until an ExaCarib admin signs off. The sign-off
  is stored against a hash of the catalogue, so any later edit puts the label back.
- Each person picks a language (`commai_user_locale`); dates, numbers and money
  use `Intl` in that locale (Caribbean variants where they exist).
- The business picks which switched-on languages the AI may reply in; that list
  feeds the existing language gate in the AI runtime.
- The widget asks `/i18n/widget/{key}?lang=` with the browser's languages and
  stays English unless the business has that language switched on.

### AI quality (`commai/ai/quality.py`, `gaps.py`, `followups.py`, `judging.py`)

- Each business writes up to 20 criteria in plain words. A review is a durable
  job: it samples resolved conversations (half AI-handled, half human), asks the
  judge model for pass, fail or unclear per criterion with quotes, and **drops
  any quote that is not in the conversation** before storing it. A failed
  criterion flags the conversation for a person to review.
- The judge is a model task like any other (simulated by default), metered as
  `ai_quality`. It reads customer-visible messages only; private notes never go in.
- The knowledge-gap report groups open questions by shared terms. "Draft article"
  writes an unapproved knowledge source with `[Check: …]` placeholders; it cannot
  be approved while one remains, and the AI uses it only once approved.
- Promises in outgoing messages ("I'll call you back tomorrow") become follow-up
  reminders with a due time in the business's time zone; the owner is notified
  when it falls due.

### Attachments and voice notes (`commai/ai/attachments.py`)

- Files are type-sniffed from their bytes; the declared type must match.
  Executables, scripts, archives, HTML and SVG are refused. Size limits per kind.
  Nothing is ever executed; files stay in the database, outside any web root.
- Text is read directly; PDF text is extracted with a small Flate reader (capped);
  images go through a `Vision` interface and voice through a `Speech` interface.
  Both default to simulated readers. An OpenAI-compatible vision reader exists but
  is not live until the key and the capability are switched on.
- What was read is stored per file and shown to staff; the AI agent gets the text
  and escalates when nothing could be read.

### Governance (`commai/ai/governance.py`)

- Evaluation suites: each business has its own cases, and ExaCarib keeps a global
  suite. A case is a question, phrases the answer must contain, and phrases it must
  never contain (or "must escalate").
- A change to the model (`EXA_LLM_MODEL`) or to a business's AI instructions is a
  **candidate**. It runs the suites as a durable job; results are kept. It can go
  live only once every case passes and a person promotes it. Instruction edits in
  the AI profile become candidates instead of applying at once.
- Daily action limits per role (customer AI agent, copilot, platform assistant,
  workflow, person). Over the limit, actions are refused with 429 and audited.

### Team features (`commai/team.py`, `commai/csat.py`)

- `@mentions` in notes notify the person. Files on notes (5 MB, 5 per note) use
  the same checks as customer files and are reachable only with the notes scope.
- Saved inbox views, personal or shared with the team.
- Satisfaction surveys after resolution, off by default, sent on the
  conversation's own channel through the normal send path so channel rules (e.g.
  WhatsApp's 24-hour window) apply; held-back surveys are recorded. Results feed
  the "satisfaction" report.
- Direct and group staff chat. It emits no customer events, has no customer API
  scope, and is never in exports or AI context. Tests prove it.

## Consequences

- Drafts need fluent reviewers before any language is switched on.
- Survey links need `EXA_PUBLIC_URL` set to the public address.
- Real image and voice reading cost money and need Dudley's approval.
- A small in-process listener hook was added to `commai/events.py` so modules can
  react to events in the same transaction (mentions, follow-ups, surveys).
