import { useState, type FormEvent } from "react";
import { api, useApi } from "../../../api";
import { ErrorNote, ExampleTag } from "../../../components";
import { Card, useAction } from "../../../ui";
import { when } from "../lib";
import { money } from "./ChangeBox";
import type { CallRow, FraudLimits, Invoice, RateCard, Reconcile, Spend, SpendGroup, VoiceOverview } from "./types";

const KIND_LABEL: Record<string, string> = {
  call: "Calls",
  ai_minutes: "AI agent minutes",
  monthly_user: "Users",
  monthly_number: "Numbers",
  one_time: "One-time fees",
};

function thisMonth(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

/** Rate card, usage and spend, bundles, fraud limits, calls, invoices and (ExaCarib only) the supplier side. */
export default function Billing({ base, v }: { base: string; v: VoiceOverview }) {
  const [period, setPeriod] = useState(thisMonth());
  return (
    <>
      <div className="voice-period">
        <label>
          Month
          <input type="month" value={period} onChange={(e) => setPeriod(e.target.value || thisMonth())} />
        </label>
      </div>
      <SpendCard base={base} period={period} />
      <RateCardCard base={base} v={v} />
      <CallsCard base={base} v={v} />
      <FraudCard base={base} />
      <InvoicesCard base={base} v={v} period={period} />
      {v.is_exacarib && <SupplierCard base={base} period={period} />}
    </>
  );
}

function Groups({ title, rows, currency }: { title: string; rows: SpendGroup[]; currency: string }) {
  return (
    <div className="voice-group">
      <h3 className="voice-sub">{title}</h3>
      {rows.length === 0 ? (
        <p className="muted small">No charges.</p>
      ) : (
        <table className="paths dt">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Call minutes</th>
              <th scope="col">Spend</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id ?? r.name}>
                <td>{r.name}</td>
                <td className="mono">{r.minutes}</td>
                <td className="mono">{money(currency, r.amount)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function SpendCard({ base, period }: { base: string; period: string }) {
  const s = useApi<Spend>(`${base}/voice/spend?period=${period}`, 15_000);
  const d = s.data;
  return (
    <Card title="Usage and spend" note={d?.example_prices && <ExampleTag />}>
      <ErrorNote error={s.error} />
      {d && (
        <>
          <div className="voice-figures">
            <div>
              <div className="eyebrow">This month</div>
              <div className="stat-figure mono">{money(d.currency, d.total)}</div>
            </div>
            <div>
              <div className="eyebrow">Call minutes</div>
              <div className="stat-figure mono">{d.minutes}</div>
            </div>
            <div>
              <div className="eyebrow">Calls today</div>
              <div className="stat-figure mono">{money(d.currency, d.spend_today)}</div>
            </div>
          </div>
          {d.example_prices && <p className="muted small">Prices come from the example rate card until ExaCarib sets yours.</p>}
          <ul className="plain-list">
            {d.by_kind.map((k) => (
              <li key={k.kind} className="row-between">
                <span>{KIND_LABEL[k.kind] ?? k.kind}</span>
                <span className="mono">{money(d.currency, k.amount)}</span>
              </li>
            ))}
          </ul>
          {d.bundles.length > 0 && (
            <>
              <h3 className="voice-sub">Bundles (shared across your company)</h3>
              <ul className="plain-list">
                {d.bundles.map((b) => {
                  const pct = Math.min(100, (Number(b.used) / Number(b.minutes)) * 100);
                  return (
                    <li key={b.id}>
                      <div className="row-between">
                        <span>
                          {b.name} <span className="muted small">{b.prefixes.length ? `calls to ${b.prefixes.join(", ")}` : "all calls"}</span>
                        </span>
                        <span className="mono">
                          {b.used} of {b.minutes} min {pct >= 100 ? <span className="pill small bad">Used up</span> : pct >= b.alert_pct ? <span className="pill small warn">Running low</span> : null}
                        </span>
                      </div>
                      <meter className="voice-meter" min={0} max={100} low={b.alert_pct} high={99} optimum={0} value={pct} aria-label={`${b.name} used`} />
                    </li>
                  );
                })}
              </ul>
            </>
          )}
          <div className="voice-groups">
            <Groups title="By site" rows={d.by_site} currency={d.currency} />
            <Groups title="By team" rows={d.by_team} currency={d.currency} />
            <Groups title="By person" rows={d.by_user} currency={d.currency} />
          </div>
        </>
      )}
    </Card>
  );
}

function RateCardCard({ base, v }: { base: string; v: VoiceOverview }) {
  const cards = useApi<RateCard[]>(`${base}/voice/rate-cards`, 0);
  const [editing, setEditing] = useState(false);
  const cur = cards.data?.[0];
  return (
    <Card title="Rate card" note={cur?.example && <ExampleTag />}>
      <ErrorNote error={cards.error} />
      {cur && (
        <>
          <p className="small">
            {cur.label || "Rate card"} — version {cur.version}, from {new Date(cur.effective_from).toLocaleDateString("en-GB")}. Each call is priced
            with the version in force when it ended, and keeps that version on its charge.
          </p>
          <div className="voice-groups">
            <table className="paths dt">
              <tbody>
                <tr>
                  <th scope="row">Per person, a month</th>
                  <td className="mono">{money(cur.currency, cur.monthly_user)}</td>
                </tr>
                <tr>
                  <th scope="row">Per number, a month</th>
                  <td className="mono">{money(cur.currency, cur.monthly_number)}</td>
                </tr>
                <tr>
                  <th scope="row">AI agent, a minute</th>
                  <td className="mono">{money(cur.currency, cur.ai_minute)}</td>
                </tr>
                {Object.entries(cur.one_time).map(([k, a]) => (
                  <tr key={k}>
                    <th scope="row">{k.replace("_", " ")} (once)</th>
                    <td className="mono">{money(cur.currency, a)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <table className="paths dt">
              <thead>
                <tr>
                  <th scope="col">Calls to</th>
                  <th scope="col">Prefix</th>
                  <th scope="col">A minute</th>
                </tr>
              </thead>
              <tbody>
                {cur.destinations.map((d) => (
                  <tr key={d.prefix}>
                    <td>{d.name}</td>
                    <td className="mono">{d.prefix ? `+${d.prefix}` : "other"}</td>
                    <td className="mono">{money(cur.currency, d.per_minute)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {v.is_exacarib && cur && (
        <>
          {!editing ? (
            <button className="button secondary" onClick={() => setEditing(true)}>
              New version
            </button>
          ) : (
            <CardForm base={base} cur={cur} onDone={() => (setEditing(false), cards.reload())} />
          )}
          <BundleForm base={base} />
        </>
      )}
    </Card>
  );
}

function CardForm({ base, cur, onDone }: { base: string; cur: RateCard; onDone: () => void }) {
  const [f, setF] = useState({
    label: cur.example ? "" : cur.label,
    monthly_user: cur.monthly_user,
    monthly_number: cur.monthly_number,
    ai_minute: cur.ai_minute,
    one_time: Object.entries(cur.one_time)
      .map(([k, a]) => `${k},${a}`)
      .join("\n"),
    destinations: cur.destinations.map((d) => `${d.prefix},${d.name},${d.per_minute}`).join("\n"),
  });
  const act = useAction();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const one_time = Object.fromEntries(
        f.one_time
          .split("\n")
          .filter((l) => l.trim())
          .map((l) => l.split(",").map((x) => x.trim())),
      );
      const destinations = f.destinations
        .split("\n")
        .filter((l) => l.trim())
        .map((l) => {
          const [prefix, name, per_minute] = l.split(",").map((x) => x.trim());
          return { prefix, name, per_minute };
        });
      await api(`${base}/voice/rate-cards`, {
        method: "POST",
        body: JSON.stringify({ label: f.label, currency: cur.currency, monthly_user: f.monthly_user, monthly_number: f.monthly_number, ai_minute: f.ai_minute, one_time, destinations }),
      });
      onDone();
    });
  };
  return (
    <form className="form" onSubmit={submit}>
      <label className="wide">
        Label
        <input value={f.label} onChange={(e) => setF({ ...f, label: e.target.value })} maxLength={120} />
      </label>
      <label>
        Per person a month
        <input value={f.monthly_user} onChange={(e) => setF({ ...f, monthly_user: e.target.value })} inputMode="decimal" required />
      </label>
      <label>
        Per number a month
        <input value={f.monthly_number} onChange={(e) => setF({ ...f, monthly_number: e.target.value })} inputMode="decimal" required />
      </label>
      <label>
        AI agent a minute
        <input value={f.ai_minute} onChange={(e) => setF({ ...f, ai_minute: e.target.value })} inputMode="decimal" required />
      </label>
      <label className="wide">
        One-time fees (key,amount per line)
        <textarea className="mono" rows={4} value={f.one_time} onChange={(e) => setF({ ...f, one_time: e.target.value })} />
      </label>
      <label className="wide">
        Destinations (prefix,name,rate per line; empty prefix for everything else)
        <textarea className="mono" rows={7} value={f.destinations} onChange={(e) => setF({ ...f, destinations: e.target.value })} />
      </label>
      <div className="actions wide">
        <button className="button" disabled={act.busy}>
          Save as a new version
        </button>{" "}
        <button type="button" className="button secondary" onClick={onDone}>
          Cancel
        </button>
      </div>
      <ErrorNote error={act.error} />
    </form>
  );
}

function BundleForm({ base }: { base: string }) {
  const [f, setF] = useState({ name: "", minutes: "", prefixes: "", alert_pct: 80 });
  const act = useAction();
  const [done, setDone] = useState(false);
  return (
    <form
      className="form"
      onSubmit={(e) => {
        e.preventDefault();
        act.run(async () => {
          await api(`${base}/voice/bundles`, {
            method: "POST",
            body: JSON.stringify({ name: f.name, minutes: f.minutes, prefixes: f.prefixes.split(/[\s,]+/).filter(Boolean), alert_pct: f.alert_pct }),
          });
          setDone(true);
        });
      }}
    >
      <h3 className="voice-sub wide">Bundle of included minutes</h3>
      <label>
        Name
        <input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required maxLength={80} />
      </label>
      <label>
        Minutes a month
        <input value={f.minutes} onChange={(e) => setF({ ...f, minutes: e.target.value })} inputMode="decimal" required />
      </label>
      <label>
        Covers prefixes (blank: all)
        <input value={f.prefixes} onChange={(e) => setF({ ...f, prefixes: e.target.value })} placeholder="1868" />
      </label>
      <label>
        Alert at (% used)
        <input type="number" min={1} max={99} value={f.alert_pct} onChange={(e) => setF({ ...f, alert_pct: Number(e.target.value) })} />
      </label>
      <div className="actions wide">
        <button className="button secondary" disabled={act.busy}>
          Save bundle
        </button>
        {done && <span className="pill ok small">Saved</span>}
      </div>
      <ErrorNote error={act.error} />
    </form>
  );
}

function FraudCard({ base }: { base: string }) {
  const lim = useApi<FraudLimits>(`${base}/voice/fraud-limits`, 0);
  const [draft, setDraft] = useState<FraudLimits | null>(null);
  const act = useAction();
  const d = draft ?? lim.data;
  if (!d) return <ErrorNote error={lim.error} />;
  return (
    <Card title="Fraud limits">
      <p className="muted small">These stop calls. Emergency calls are never stopped. An unusual number of calls in an hour raises an alert.</p>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          act.run(async () => {
            await api(`${base}/voice/fraud-limits`, { method: "PUT", body: JSON.stringify({ ...d, daily_cap: d.daily_cap || null }) });
            setDraft(null);
            lim.reload();
          });
        }}
      >
        <label>
          Daily spend cap on calls (blank: none)
          <input value={d.daily_cap ?? ""} onChange={(e) => setDraft({ ...d, daily_cap: e.target.value })} inputMode="decimal" />
        </label>
        <label>
          Alert above this many calls an hour
          <input type="number" min={1} value={d.calls_per_hour_alert} onChange={(e) => setDraft({ ...d, calls_per_hour_alert: Number(e.target.value) })} />
        </label>
        <label className="wide">
          Blocked prefixes (premium and high-risk)
          <input value={d.blocked_prefixes.join(", ")} onChange={(e) => setDraft({ ...d, blocked_prefixes: e.target.value.split(/[\s,]+/).filter(Boolean) })} />
        </label>
        <label className="check wide">
          <input type="checkbox" checked={d.international} onChange={(e) => setDraft({ ...d, international: e.target.checked })} /> Allow calls outside
          North America and the Caribbean (+1)
        </label>
        <div className="actions wide">
          <button className="button" disabled={!draft || act.busy}>
            Save limits
          </button>
        </div>
      </form>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function CallsCard({ base, v }: { base: string; v: VoiceOverview }) {
  const calls = useApi<CallRow[]>(`${base}/voice/calls`, 15_000);
  const [f, setF] = useState({ extension: v.users?.[0]?.extension ?? "", to: "", seconds: 60, ai_seconds: 0 });
  const act = useAction();
  const [last, setLast] = useState<{ allowed: boolean; reason: string } | null>(null);
  return (
    <Card title="Calls" note={!v.provider.live && <span className="tag">Simulated calls</span>}>
      <p className="muted small">
        Every call is priced when it ends. There is no SIP provider yet, so you can place a simulated call to see rating, bundles and fraud limits at
        work.
      </p>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          act.run(async () => {
            const r = await api<{ allowed: boolean; reason: string }>(`${base}/voice/calls/simulate`, { method: "POST", body: JSON.stringify(f) });
            setLast(r);
            calls.reload();
          });
        }}
      >
        <label>
          From extension
          <select value={f.extension} onChange={(e) => setF({ ...f, extension: e.target.value })}>
            {(v.users ?? []).map((u) => (
              <option key={u.id} value={u.extension}>
                {u.extension} {u.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          To
          <input value={f.to} onChange={(e) => setF({ ...f, to: e.target.value })} required placeholder="+1 868 555 0199" />
        </label>
        <label>
          Seconds
          <input type="number" min={1} max={14400} value={f.seconds} onChange={(e) => setF({ ...f, seconds: Number(e.target.value) })} />
        </label>
        <label>
          AI agent seconds
          <input type="number" min={0} max={14400} value={f.ai_seconds} onChange={(e) => setF({ ...f, ai_seconds: Number(e.target.value) })} />
        </label>
        <div className="actions wide">
          <button className="button secondary" disabled={act.busy || !(v.users ?? []).length}>
            Place simulated call
          </button>
          {last && <span className={`pill small ${last.allowed ? "ok" : "bad"}`}>{last.allowed ? "Connected and priced" : `Stopped: ${last.reason}`}</span>}
        </div>
      </form>
      <ErrorNote error={calls.error ?? act.error} />
      <div className="table-wrap">
        <table className="paths dt stack">
          <thead>
            <tr>
              <th scope="col">When</th>
              <th scope="col">Who</th>
              <th scope="col">To</th>
              <th scope="col">Length</th>
              <th scope="col">Price</th>
            </tr>
          </thead>
          <tbody>
            {(calls.data ?? []).slice(0, 50).map((c) => (
              <tr key={c.call_id}>
                <td>{when(c.ended_at)}</td>
                <td data-label="Who">{c.user_name ? `${c.extension} ${c.user_name}` : c.from_number}</td>
                <td data-label="To" className="mono">
                  {c.to_number}
                  {c.status === "blocked" && <span className="pill small bad">Blocked</span>}
                </td>
                <td data-label="Length" className="mono">
                  {c.seconds} s
                </td>
                <td data-label="Price" className="mono">
                  {c.amount ?? "—"}
                  {c.rate_card_version && <span className="sub">rate card v{c.rate_card_version}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function InvoicesCard({ base, v, period }: { base: string; v: VoiceOverview; period: string }) {
  const list = useApi<Invoice[]>(`${base}/voice/invoices`, 30_000);
  const [open, setOpen] = useState<string | null>(null);
  const act = useAction();
  return (
    <Card title="Invoices and credit notes">
      <p className="muted small">
        Issued invoices never change; corrections are credit notes. Every line traces back to its call or fee. Voice charges join your single
        ExaCarib bill when billing is connected (not built yet).
      </p>
      {v.is_exacarib && (
        <button
          className="button secondary"
          disabled={act.busy}
          onClick={() =>
            act.run(async () => {
              const inv = await api<Invoice>(`${base}/voice/invoices/draft`, { method: "POST", body: JSON.stringify({ period }) });
              list.reload();
              setOpen(inv.id);
            })
          }
        >
          Build draft for {period}
        </button>
      )}
      <ErrorNote error={list.error ?? act.error} />
      <ul className="plain-list">
        {(list.data ?? []).map((i) => (
          <li key={i.id}>
            <div className="row-between">
              <span>
                <span className={`pill small ${i.status === "issued" ? "ok" : "warn"}`}>{i.status === "issued" ? "Issued" : "Draft"}</span>{" "}
                {i.kind === "credit_note" ? "Credit note" : "Invoice"} {i.number ?? ""} <span className="muted small">{i.period_start.slice(0, 7)}</span>
                {i.credits_number && <span className="muted small"> for {i.credits_number}</span>}
              </span>
              <span>
                <span className="mono">{money(i.currency, i.total)}</span>{" "}
                <button className="button secondary small" aria-expanded={open === i.id} onClick={() => setOpen(open === i.id ? null : i.id)}>
                  {open === i.id ? "Hide" : "Lines"}
                </button>
              </span>
            </div>
            {open === i.id && <InvoiceDetail base={base} id={i.id} v={v} onChange={list.reload} />}
          </li>
        ))}
      </ul>
    </Card>
  );
}

function InvoiceDetail({ base, id, v, onChange }: { base: string; id: string; v: VoiceOverview; onChange: () => void }) {
  const inv = useApi<Invoice>(`${base}/voice/invoices/${id}`, 0);
  const act = useAction();
  const [credit, setCredit] = useState<Record<number, string>>({});
  const [reason, setReason] = useState("");
  const d = inv.data;
  if (!d) return <ErrorNote error={inv.error} />;
  const canCredit = v.is_exacarib && d.kind === "invoice" && d.status === "issued";
  return (
    <div className="voice-panel">
      <div className="table-wrap">
        <table className="paths dt stack">
          <thead>
            <tr>
              {canCredit && <th scope="col">Credit</th>}
              <th scope="col">Line</th>
              <th scope="col">Traces to</th>
              <th scope="col">Amount</th>
            </tr>
          </thead>
          <tbody>
            {(d.lines ?? []).map((l) => (
              <tr key={l.id}>
                {canCredit && (
                  <td>
                    <input
                      aria-label={`Credit amount for ${l.description}`}
                      className="voice-credit"
                      inputMode="decimal"
                      placeholder="0.00"
                      value={credit[l.id] ?? ""}
                      onChange={(e) => setCredit({ ...credit, [l.id]: e.target.value })}
                    />
                  </td>
                )}
                <td data-label="Line" className="cell-wrap">
                  {l.description}
                </td>
                <td data-label="Traces to" className="mono small">
                  {l.call_id ? `call ${l.call_id}, ${l.seconds} s, rate card v${l.rate_card_version}` : l.kind ?? ""}
                </td>
                <td data-label="Amount" className="mono">
                  {l.amount}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {v.is_exacarib && d.status === "draft" && (
        <button
          className="button"
          disabled={act.busy}
          onClick={() =>
            act.run(async () => {
              if (!window.confirm("Issue this invoice? It can't be changed afterwards; corrections become credit notes.")) return;
              await api(`${base}/voice/invoices/${id}/issue`, { method: "POST" });
              inv.reload();
              onChange();
            })
          }
        >
          Issue invoice
        </button>
      )}
      {canCredit && (
        <form
          className="form"
          onSubmit={(e) => {
            e.preventDefault();
            const lines = Object.entries(credit)
              .filter(([, a]) => a.trim())
              .map(([line_id, amount]) => ({ line_id: Number(line_id), amount }));
            act.run(async () => {
              await api(`${base}/voice/invoices/${id}/credit`, { method: "POST", body: JSON.stringify({ lines, reason }) });
              setCredit({});
              setReason("");
              onChange();
            });
          }}
        >
          <label className="wide">
            Reason for the credit
            <input value={reason} onChange={(e) => setReason(e.target.value)} required minLength={3} maxLength={300} />
          </label>
          <div className="actions wide">
            <button className="button secondary" disabled={act.busy}>
              Issue credit note
            </button>
          </div>
        </form>
      )}
      <ErrorNote error={act.error} />
    </div>
  );
}

function SupplierCard({ base, period }: { base: string; period: string }) {
  const rec = useApi<Reconcile>(`${base}/voice/supplier/reconcile?period=${period}`, 0);
  const [csv, setCsv] = useState("");
  const act = useAction();
  const [result, setResult] = useState<{ added: number; duplicates: number; rejected: { row: number; error: string }[] } | null>(null);
  const r = rec.data;
  return (
    <Card title="Supplier side (ExaCarib only)">
      <p className="muted small">What the SIP provider charges ExaCarib for this company's calls, against what we bill, and the margin.</p>
      <ErrorNote error={rec.error} />
      {r && (
        <>
          <div className="voice-figures">
            <div>
              <div className="eyebrow">Billed for calls</div>
              <div className="stat-figure mono">{r.billed_calls}</div>
            </div>
            <div>
              <div className="eyebrow">Supplier cost</div>
              <div className="stat-figure mono">{r.supplier_cost}</div>
            </div>
            <div>
              <div className="eyebrow">Margin</div>
              <div className="stat-figure mono">
                {r.call_margin}
                {r.call_margin_pct && <span className="small"> ({r.call_margin_pct}%)</span>}
              </div>
            </div>
          </div>
          <p className="small">
            {r.calls} calls; other fees {r.other_fees}. {r.unmatched_supplier_rows} supplier rows match no call ({r.unmatched_supplier_cost}).
            {r.simulated_cost_estimate !== null && ` Simulated provider estimate: ${r.simulated_cost_estimate}.`}
          </p>
          {r.issues.length > 0 && (
            <ul className="plain-list">
              {r.issues.slice(0, 20).map((i) => (
                <li key={i.call_id}>
                  <span className="pill small warn">Check</span> <span className="mono">{i.call_id}</span>: {i.issue}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          act.run(async () => {
            setResult(await api(`${base}/voice/supplier/import`, { method: "POST", body: JSON.stringify({ csv }) }));
            setCsv("");
            rec.reload();
          });
        }}
      >
        <label className="wide">
          Supplier CSV (call_ref, started_at, destination, seconds, cost)
          <textarea className="mono" rows={4} value={csv} onChange={(e) => setCsv(e.target.value)} />
        </label>
        <div className="actions wide">
          <button className="button secondary" disabled={!csv || act.busy}>
            Import
          </button>
        </div>
      </form>
      {result && (
        <p className="small" role="status">
          Added {result.added}, already imported {result.duplicates}, rejected {result.rejected.length}
          {result.rejected.length ? ` (rows ${result.rejected.map((x) => x.row).join(", ")})` : ""}.
        </p>
      )}
      <ErrorNote error={act.error} />
    </Card>
  );
}
