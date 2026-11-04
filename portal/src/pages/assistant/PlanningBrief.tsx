import { useState } from "react";

export interface PlanningBrief {
  format: "xakore-planning-brief";
  version: 1;
  offer: "Connect" | "Jibsy" | "Network operations" | "Connected devices" | "Discovery";
  facts: { label: string; value: string }[];
  gaps: string[];
}

const offers = ["Connect", "Jibsy", "Network operations", "Connected devices", "Discovery"];
const secret = /-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})\b|\b(?:password|passcode|api[_ -]?key|access[_ -]?token|private[_ -]?key|secret)\s*(?:is\b|[:=])\s*\S+|\b(?:\d[ -]?){13,19}\b/i;

function exactKeys(value: object, keys: string[]) {
  return Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}
function boundedText(value: unknown, max: number): value is string {
  return typeof value === "string" && value.trim().length > 0 && value.length <= max && !value.includes("\0");
}

/** A local preview only. A website brief never becomes trusted account evidence. */
export function parsePlanningBrief(text: string): PlanningBrief {
  if (new TextEncoder().encode(text).length > 18_000) throw new Error("This brief is too large. Shorten the notes on the website and copy it again.");
  let value: unknown;
  try { value = JSON.parse(text); } catch { throw new Error("Paste the brief from Copy brief and open Hub on the XaKore planning page."); }
  if (!value || typeof value !== "object" || Array.isArray(value) || !exactKeys(value, ["format", "version", "offer", "facts", "gaps"])) throw new Error("This is not a supported XaKore planning brief.");
  const v = value as Record<string, unknown>;
  if (v.format !== "xakore-planning-brief" || v.version !== 1 || typeof v.offer !== "string" || !offers.includes(v.offer)
    || !Array.isArray(v.facts) || v.facts.length > 24 || !Array.isArray(v.gaps) || v.gaps.length > 6 || v.facts.length + v.gaps.length < 1
    || v.facts.some((f) => !f || typeof f !== "object" || Array.isArray(f) || !exactKeys(f, ["label", "value"]) || !boundedText(f.label, 80) || !boundedText(f.value, 500))
    || v.gaps.some((g) => !boundedText(g, 200))) throw new Error("The brief has invalid or overlong notes. Review it on the website, then copy it again.");
  const brief = { format: v.format, version: v.version, offer: v.offer, facts: v.facts.map((f) => ({ label: f.label.trim(), value: f.value.trim() })), gaps: v.gaps.map((g) => (g as string).trim()) } as PlanningBrief;
  if ([...brief.facts.map((f) => f.label + ": " + f.value), ...brief.gaps].some((v) => secret.test(v))) throw new Error("Remove passwords, keys, tokens and account or card numbers before importing. Describe the requirement instead.");
  return brief;
}

export function PlanningBriefImport({ organisation, initialOpen, onUse }: { organisation: string; initialOpen: boolean; onUse: (brief: PlanningBrief) => void }) {
  const [open, setOpen] = useState(initialOpen);
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<PlanningBrief | null>(null);
  const [error, setError] = useState("");
  return <section className="assistant-brief-import" aria-label="Website planning brief">
    {!open ? <button type="button" className="linklike" onClick={() => setOpen(true)}>Bring a website brief</button> : <>
      <h3>Continue from your website brief</h3>
      <p className="small muted">Paste the notes you copied on the XaKore website. Review them for {organisation} before using them.</p>
      {!preview ? <form onSubmit={(event) => { event.preventDefault(); try { setPreview(parsePlanningBrief(text)); setError(""); } catch (e) { setError((e as Error).message); } }}>
        <label htmlFor="website-planning-brief">Paste your planning brief</label>
        <textarea id="website-planning-brief" rows={4} value={text} maxLength={18000} onChange={(e) => { setText(e.target.value); setError(""); }} />
        {error && <p className="small" role="alert">{error}</p>}
        <div className="assistant-actions"><button className="button secondary" disabled={!text.trim()}>Review notes</button><button type="button" className="linklike" onClick={() => { setText(""); setError(""); setOpen(false); }}>Cancel</button></div>
      </form> : <>
        <p><strong>{preview.facts.length} {preview.facts.length === 1 ? "note" : "notes"}</strong> · {preview.gaps.length} open {preview.gaps.length === 1 ? "question" : "questions"} · {preview.offer}</p>
        <details><summary>Review the imported notes</summary><dl>{preview.facts.map((f, i) => <div key={i}><dt>{f.label}</dt><dd>{f.value}</dd></div>)}</dl>{preview.gaps.length > 0 && <ul>{preview.gaps.map((g, i) => <li key={i}>{g}</li>)}</ul>}</details>
        <p className="small muted">These are planning notes, not verified account facts. They reach your AI provider only when you send a question. Nothing is configured or purchased.</p>
        <div className="assistant-actions"><button type="button" className="button secondary" onClick={() => { onUse(preview); setPreview(null); setText(""); setOpen(false); }}>Use these notes for {organisation}</button><button type="button" className="linklike" onClick={() => setPreview(null)}>Edit pasted brief</button><button type="button" className="linklike" onClick={() => { setPreview(null); setText(""); setError(""); setOpen(false); }}>Discard</button></div>
      </>}
    </>}
  </section>;
}

