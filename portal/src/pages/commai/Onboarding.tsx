import { useState, type FormEvent } from "react";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { Card, PageHead, useAction } from "../../ui";
import { useCommaiBase } from "./lib";
import "./automation.css";

/* Shapes from controller/exaconnect_controller/commai/api/automation.py (ADR 0020). */

interface Draft {
  id: string;
  kind: "profile" | "team" | "knowledge" | "routing" | "workflow";
  title: string;
  content: Record<string, unknown>;
  source: string;
  status: "draft" | "approved" | "rejected";
  reviewed_by: string;
}
interface Batch {
  batch: string | null;
  drafts: Draft[];
}

const KIND_LABEL: Record<Draft["kind"], string> = {
  profile: "Business profile",
  team: "Teams",
  knowledge: "Knowledge",
  routing: "Routing rules",
  workflow: "Starter workflows",
};
const TYPES = ["Appointments (clinic, salon)", "Online shop", "Professional services", "Property", "Hospitality", "Other"];
const CHANNELS: [string, string][] = [
  ["web", "Website chat"],
  ["whatsapp", "WhatsApp"],
  ["sms", "SMS"],
  ["email", "Email"],
  ["voice", "Calls"],
];

export default function Onboarding() {
  const base = useCommaiBase();
  const batch = useApi<Batch>(base && `${base}/onboarding`, 0);
  const [dropped, setDropped] = useState<string[]>([]);
  if (!base) return <p className="muted">Choose an organisation first.</p>;
  return (
    <>
      <PageHead eyebrow="CommAI" title="Set up">
        Tell CommAI about your business. It drafts a profile, teams, knowledge, routing and starter workflows; you review and approve each one before
        anything goes live.
      </PageHead>
      <SetupForm base={base} onDone={(d) => {
        setDropped(d);
        batch.reload();
      }} />
      {dropped.length > 0 && (
        <div className="auto-banner bad" role="status">
          <strong>Left out of the drafts:</strong> these lines in your website text read like instructions to an AI, so CommAI ignored them.
          <ul className="auto-evidence">
            {dropped.map((l) => (
              <li key={l}>{l}</li>
            ))}
          </ul>
        </div>
      )}
      <ErrorNote error={batch.error} />
      {batch.data?.drafts.length ? <Drafts base={base} drafts={batch.data.drafts} reload={batch.reload} /> : null}
    </>
  );
}

function SetupForm({ base, onDone }: { base: string; onDone: (dropped: string[]) => void }) {
  const [f, setF] = useState({ business_name: "", business_type: "", hours: "", locations: "", teams: "", website_text: "", website_url: "" });
  const [channels, setChannels] = useState<string[]>(["web"]);
  const act = useAction();
  const set = (k: keyof typeof f, v: string) => setF((x) => ({ ...x, [k]: v }));
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const out = await api<{ dropped_lines: string[] }>(`${base}/onboarding`, {
        method: "POST",
        body: JSON.stringify({
          ...f,
          locations: f.locations.split("\n").map((x) => x.trim()).filter(Boolean),
          teams: f.teams.split(",").map((x) => x.trim()).filter(Boolean),
          channels,
        }),
      });
      onDone(out.dropped_lines);
    });
  };
  return (
    <Card title="About your business">
      <form className="form" onSubmit={submit}>
        <label>
          Business name
          <input value={f.business_name} onChange={(e) => set("business_name", e.target.value)} maxLength={120} />
        </label>
        <label>
          Type of business
          <select value={f.business_type} onChange={(e) => set("business_type", e.target.value)}>
            <option value="">Choose…</option>
            {TYPES.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </label>
        <label>
          Opening hours
          <input value={f.hours} onChange={(e) => set("hours", e.target.value)} placeholder="Mon–Fri 8:00–17:00" maxLength={500} />
        </label>
        <label>
          Teams (comma separated)
          <input value={f.teams} onChange={(e) => set("teams", e.target.value)} placeholder="Sales, Support" />
        </label>
        <label className="wide">
          Locations (one per line)
          <textarea className="auto-text" style={{ minHeight: 60 }} value={f.locations} onChange={(e) => set("locations", e.target.value)} />
        </label>
        <fieldset className="wide">
          <legend>Channels</legend>
          {CHANNELS.map(([v, l]) => (
            <label key={v} className="check">
              <input
                type="checkbox"
                checked={channels.includes(v)}
                onChange={() => setChannels((c) => (c.includes(v) ? c.filter((x) => x !== v) : [...c, v]))}
              />
              {l}
            </label>
          ))}
        </fieldset>
        <label className="wide">
          Website address (for reference)
          <input value={f.website_url} onChange={(e) => set("website_url", e.target.value)} placeholder="https://" maxLength={300} />
        </label>
        <label className="wide">
          Website text (paste it here)
          <textarea className="auto-text" style={{ minHeight: 180 }} value={f.website_text} onChange={(e) => set("website_text", e.target.value)} maxLength={60000} />
        </label>
        <p className="small muted wide" style={{ margin: 0 }}>
          Website text becomes draft knowledge for you to read. It never changes a setting, and never paste passwords or keys here.
        </p>
        <div className="actions">
          <button className="button" disabled={act.busy}>
            {act.busy ? "Drafting…" : "Draft my set-up"}
          </button>
        </div>
      </form>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function Drafts({ base, drafts, reload }: { base: string; drafts: Draft[]; reload: () => void }) {
  const act = useAction();
  const [editing, setEditing] = useState<Record<string, string>>({});
  const decide = (d: Draft, verb: "approve" | "reject") =>
    act.run(async () => {
      if (editing[d.id] !== undefined) {
        const key = d.kind === "knowledge" ? "body" : "summary";
        await api(`${base}/onboarding/${d.id}`, { method: "PATCH", body: JSON.stringify({ content: { [key]: editing[d.id] } }) });
      }
      await api(`${base}/onboarding/${d.id}/${verb}`, { method: "POST" });
      setEditing((e) => {
        const { [d.id]: _, ...rest } = e;
        return rest;
      });
      reload();
    });
  const kinds = Object.keys(KIND_LABEL) as Draft["kind"][];
  const pending = drafts.filter((d) => d.status === "draft").length;
  return (
    <>
      <div className="auto-banner" role="status">
        {pending ? `${pending} draft(s) waiting for your review. Approve teams before their routing rules.` : "Everything has been reviewed."}
        {drafts[0]?.source === "model" ? " Drafted with the AI." : " Drafted by CommAI's rules."}
      </div>
      <ErrorNote error={act.error} />
      {kinds.map((k) => {
        const list = drafts.filter((d) => d.kind === k);
        if (!list.length) return null;
        return (
          <Card key={k} title={KIND_LABEL[k]}>
            <div className="auto-grid">
              {list.map((d) => {
                const editable = d.kind === "knowledge" || d.kind === "profile";
                const key = d.kind === "knowledge" ? "body" : "summary";
                return (
                  <section key={d.id} className="auto-draft" aria-label={d.title}>
                    <div className="auto-row" style={{ justifyContent: "space-between" }}>
                      <strong>{d.title}</strong>
                      <span className={`pill ${d.status === "approved" ? "ok" : d.status === "rejected" ? "shadow" : "warn"}`}>
                        {d.status === "draft" ? "To review" : d.status === "approved" ? "Approved" : "Rejected"}
                      </span>
                    </div>
                    {editable && d.status === "draft" ? (
                      <label>
                        <span className="sr-only">Edit {d.title}</span>
                        <textarea
                          className="auto-text"
                          value={editing[d.id] ?? String(d.content[key] ?? "")}
                          onChange={(e) => setEditing((x) => ({ ...x, [d.id]: e.target.value }))}
                        />
                      </label>
                    ) : (
                      <pre>{summary(d)}</pre>
                    )}
                    {d.status === "draft" && (
                      <div className="auto-row">
                        <button className="button small" disabled={act.busy} onClick={() => decide(d, "approve")}>
                          Approve
                        </button>
                        <button className="button small danger-text" disabled={act.busy} onClick={() => decide(d, "reject")}>
                          Reject
                        </button>
                      </div>
                    )}
                    {d.kind === "workflow" && d.status === "approved" && <span className="small muted">Added to Workflows as a draft to publish.</span>}
                  </section>
                );
              })}
            </div>
          </Card>
        );
      })}
    </>
  );
}

function summary(d: Draft): string {
  const c = d.content as Record<string, unknown>;
  if (d.kind === "routing") return `Messages mentioning ${(c.keywords as string[]).join(", ")} go to ${c.team}.`;
  if (d.kind === "team") return `A team called ${c.name}.`;
  if (d.kind === "workflow") return String(c.description ?? c.name ?? "");
  if (d.kind === "profile")
    return [c.summary, c.hours && `Hours: ${c.hours}`, (c.locations as string[])?.length ? `Locations: ${(c.locations as string[]).join("; ")}` : ""]
      .filter(Boolean)
      .join("\n");
  return String(c.body ?? "");
}
