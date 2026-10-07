# Languages, quality, governance and team: operator note

Decision record: ADR 0032.

## Switching a language on

1. Find a fluent reviewer. They read `controller/exaconnect_controller/commai/i18n/<locale>.json`
   (or Portal > Languages) and send corrections; edit the file, keep every `{placeholder}`.
2. An ExaCarib admin signs it off: Portal > Languages > Sign off, or
   `POST /api/v1/commai/i18n/catalogues/<locale>/review`. Editing the file later
   brings the "Machine-drafted" label back until signed again.
3. Record the go-live criteria (tested, security review, operations, catalogue
   reviewed, formats, support) at `/api/v1/commai/golive/language/<locale>`, then
   set `pilot` for named businesses or `on`.

## Quality reviews

- Portal > Quality: write criteria, run a review, work through flags, draft
  articles from knowledge gaps, and keep promises (follow-ups).
- The judge is simulated until a real model is set; reviews are metered as `ai_quality`.

## Attachments

- Text and PDF text are read locally. Images and voice notes use simulated readers
  until a real reader is set up (`EXA_COMMAI_VISION=on` for images) and the spend is approved.
- Refused types: executables, scripts, archives, HTML, SVG, and any file whose
  bytes do not match its declared type.

## Changing the AI's model or instructions

- Set `EXA_LLM_MODEL` to the new model: it becomes a candidate, runs every suite,
  and stays on the old model until an admin promotes it (Portal > AI governance).
- Instruction edits in a business's AI profile become candidates in the same way.
- Daily action limits per role are set on the same screen.

## Surveys

- Off by default per business (Portal > Quality > Satisfaction). Set
  `EXA_PUBLIC_URL` so links point at the public address.
