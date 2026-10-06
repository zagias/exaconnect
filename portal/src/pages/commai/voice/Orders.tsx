import { useState, type FormEvent } from "react";
import { api, useApi } from "../../../api";
import { ErrorNote } from "../../../components";
import { Card, useAction } from "../../../ui";
import { when } from "../lib";
import { PriceLines } from "./ChangeBox";
import type { Order, VoiceOverview } from "./types";

const STEP_LABEL: Record<string, string> = {
  tenant: "Phone system",
  numbers: "Numbers",
  devices: "Phones",
  confirm: "Test calls",
  billing: "Billing starts",
};
const STATUS: Record<Order["status"], [string, string]> = {
  draft: ["warn", "Waiting for approval"],
  approved: ["warn", "Approved"],
  provisioning: ["warn", "Setting up"],
  failed: ["bad", "Failed"],
  active: ["ok", "Active"],
  cancelled: ["off", "Cancelled"],
};

interface Row {
  name: string;
  extension: string;
  number: boolean;
  desk: string;
  softphone: boolean;
}

/** Voice orders: price approved by someone with spend permission, then set up step by step. */
export default function Orders({ base, v }: { base: string; v: VoiceOverview }) {
  const list = useApi<Order[]>(`${base}/voice/orders`, 10_000);
  const [open, setOpen] = useState<string | null>(null);
  return (
    <>
      <NewOrder base={base} v={v} onMade={(id) => (list.reload(), setOpen(id))} />
      <Card title="Orders">
        <ErrorNote error={list.error} />
        {list.data && list.data.length === 0 && <p className="muted">No orders yet.</p>}
        <ul className="plain-list">
          {(list.data ?? []).map((o) => (
            <li key={o.id}>
              <div className="row-between">
                <span>
                  <span className={`pill small ${STATUS[o.status][0]}`}>{STATUS[o.status][1]}</span> {o.users ?? 0} people,{" "}
                  <span className="mono">
                    {o.price.currency} {o.price.monthly_delta}
                  </span>{" "}
                  a month <span className="muted small">{when(o.created_at)}</span>
                  {o.status === "failed" && <span className="muted small"> at {STEP_LABEL[o.failed_step] ?? o.failed_step}</span>}
                </span>
                <button className="button secondary small" aria-expanded={open === o.id} onClick={() => setOpen(open === o.id ? null : o.id)}>
                  {open === o.id ? "Hide" : "Details"}
                </button>
              </div>
              {open === o.id && <OrderDetail base={base} id={o.id} v={v} onChange={list.reload} />}
            </li>
          ))}
        </ul>
      </Card>
    </>
  );
}

function OrderDetail({ base, id, v, onChange }: { base: string; id: string; v: VoiceOverview; onChange: () => void }) {
  const o = useApi<Order>(`${base}/voice/orders/${id}`, 5_000);
  const act = useAction();
  const [portNote, setPortNote] = useState("");
  const d = o.data;
  if (!d) return <ErrorNote error={o.error} />;
  const post = (path: string, body?: unknown) =>
    act.run(async () => {
      await api(`${base}/voice/${path}`, { method: "POST", body: body ? JSON.stringify(body) : undefined });
      o.reload();
      onChange();
    });
  return (
    <div className="voice-panel">
      <PriceLines price={d.price} />
      {d.status === "draft" && (
        <div className="actions">
          {!v.can_spend && <p className="pill warn">Someone with spend permission must approve this order.</p>}
          <button className="button" disabled={!v.can_spend || act.busy} onClick={() => post(`orders/${id}/approve`, { accepted_price: { monthly_delta: d.price.monthly_delta, one_time: d.price.one_time } })}>
            Approve {d.price.currency} {d.price.monthly_delta} a month and {d.price.one_time} once
          </button>{" "}
          <button className="button secondary" disabled={act.busy} onClick={() => post(`orders/${id}/cancel`)}>
            Cancel order
          </button>
        </div>
      )}
      {d.steps && d.status !== "draft" && (
        <ol className="voice-steps" aria-label="Set-up steps">
          {d.steps.map((s) => (
            <li key={s.step} className={`voice-step ${s.status}`}>
              <span className={`pill small ${s.status === "done" ? "ok" : s.status === "failed" ? "bad" : "off"}`}>
                {s.status === "done" ? "Done" : s.status === "failed" ? "Failed" : "Waiting"}
              </span>{" "}
              {STEP_LABEL[s.step]}
              {s.attempts > 1 && <span className="muted small"> ({s.attempts} tries)</span>}
              {s.error && <div className="small voice-error">{s.error}</div>}
            </li>
          ))}
        </ol>
      )}
      {d.status === "failed" && (
        <p>
          Nothing is duplicated when you retry: each step picks up where it stopped.{" "}
          <button className="button small" disabled={act.busy} onClick={() => post(`orders/${id}/retry`)}>
            Retry
          </button>
        </p>
      )}
      {d.status === "active" && <p className="pill ok">Active: every test call worked, and billing started {when(d.activated_at)}.</p>}
      {(d.test_calls ?? []).length > 0 && (
        <>
          <h3 className="voice-sub">Test calls</h3>
          <ul className="plain-list">
            {d.test_calls!.map((t, i) => (
              <li key={i}>
                <span className={`pill small ${t.ok ? "ok" : "bad"}`}>{t.ok ? "Worked" : "Failed"}</span> <span className="mono">{t.e164}</span>{" "}
                <span className="muted small">{t.detail}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {(d.ports ?? []).length > 0 && (
        <>
          <h3 className="voice-sub">Number ports</h3>
          <ul className="plain-list">
            {d.ports!.map((p) => (
              <li key={p.id} className="row-between">
                <span>
                  <span className="mono">{p.e164}</span> from {p.losing_carrier || "current provider"}:{" "}
                  <span className={`pill small ${p.status === "completed" ? "ok" : p.status === "rejected" ? "bad" : "warn"}`}>{p.status}</span>
                  {p.switch_date && <span className="muted small"> switch-over {p.switch_date}</span>}
                  {p.note && <span className="muted small"> ({p.note})</span>}
                </span>
                {v.is_exacarib && !["completed", "cancelled"].includes(p.status) && (
                  <span className="voice-port-actions">
                    <input aria-label="Note" placeholder="Note" value={portNote} onChange={(e) => setPortNote(e.target.value)} maxLength={300} />
                    {["accepted", "scheduled", "completed", "rejected"].map((s) => (
                      <button key={s} className="button secondary small" disabled={act.busy} onClick={() => post(`ports/${p.id}`, { status: s, note: portNote })}>
                        {s[0].toUpperCase() + s.slice(1)}
                      </button>
                    ))}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
      <ErrorNote error={act.error} />
    </div>
  );
}

function NewOrder({ base, v, onMade }: { base: string; v: VoiceOverview; onMade: (id: string) => void }) {
  const blank: Row = { name: "", extension: "", number: true, desk: "", softphone: false };
  const [site, setSite] = useState("");
  const [rows, setRows] = useState<Row[]>([{ ...blank }]);
  const [extra, setExtra] = useState(0);
  const [ports, setPorts] = useState("");
  const [switchDate, setSwitchDate] = useState("");
  const [losing, setLosing] = useState("");
  const [mainGroup, setMainGroup] = useState(true);
  const [ai, setAi] = useState(false);
  const act = useAction();
  const sites = v.sites ?? [];
  const setRow = (i: number, patch: Partial<Row>) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const items = {
      site: site || sites[0]?.id,
      users: rows
        .filter((r) => r.name.trim())
        .map((r) => ({
          name: r.name.trim(),
          extension: r.extension || undefined,
          number: r.number,
          desk_phone: r.desk ? { mac: r.desk } : undefined,
          softphone: r.softphone || undefined,
        })),
      numbers: {
        new: extra,
        ported: ports
          .split(/[\n,]+/)
          .map((x) => x.trim())
          .filter(Boolean)
          .map((e164) => ({ e164, losing_carrier: losing, switch_date: switchDate || undefined })),
      },
      features: { main_ring_group: mainGroup, ai_after_hours: ai },
    };
    act.run(async () => {
      const o = await api<Order>(`${base}/voice/orders`, { method: "POST", body: JSON.stringify({ items }) });
      setRows([{ ...blank }]);
      setPorts("");
      onMade(o.id);
    });
  };

  return (
    <Card title="New order">
      <p className="muted small">
        Choose people, numbers and phones. You see the price before anything is set up; someone with spend permission approves it. The order is
        active only when test calls to every new number work.
      </p>
      {sites.length === 0 && <p className="pill warn">Add a site under Phone system first.</p>}
      <form className="form" onSubmit={submit}>
        <label>
          Site
          <select value={site} onChange={(e) => setSite(e.target.value)}>
            {sites.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <fieldset className="wide">
          <legend>People</legend>
          {rows.map((r, i) => (
            <div key={i} className="voice-order-row">
              <label>
                Name
                <input value={r.name} onChange={(e) => setRow(i, { name: e.target.value })} maxLength={120} />
              </label>
              <label>
                Extension
                <input value={r.extension} onChange={(e) => setRow(i, { extension: e.target.value })} inputMode="numeric" pattern="\d{2,6}" placeholder="next free" />
              </label>
              <label>
                Desk phone MAC
                <input value={r.desk} onChange={(e) => setRow(i, { desk: e.target.value })} placeholder="none" />
              </label>
              <label className="check">
                <input type="checkbox" checked={r.number} onChange={(e) => setRow(i, { number: e.target.checked })} /> Own number
              </label>
              <label className="check">
                <input type="checkbox" checked={r.softphone} onChange={(e) => setRow(i, { softphone: e.target.checked })} /> Softphone
              </label>
            </div>
          ))}
          <button type="button" className="button secondary small" onClick={() => setRows([...rows, { ...blank }])}>
            Add a person
          </button>
        </fieldset>
        <label>
          Extra new numbers (main line)
          <input type="number" min={0} max={100} value={extra} onChange={(e) => setExtra(Number(e.target.value))} />
        </label>
        <label className="wide">
          Numbers to bring over (one per line)
          <textarea rows={2} value={ports} onChange={(e) => setPorts(e.target.value)} placeholder="+1 868 …" />
        </label>
        {ports.trim() && (
          <>
            <label>
              Current provider
              <input value={losing} onChange={(e) => setLosing(e.target.value)} maxLength={120} />
            </label>
            <label>
              Switch-over date
              <input type="date" value={switchDate} onChange={(e) => setSwitchDate(e.target.value)} />
            </label>
          </>
        )}
        <fieldset className="wide">
          <legend>Features</legend>
          <label className="check">
            <input type="checkbox" checked={mainGroup} onChange={(e) => setMainGroup(e.target.checked)} /> A main line ring group with everyone in the
            order
          </label>
          <label className="check">
            <input type="checkbox" checked={ai} onChange={(e) => setAi(e.target.checked)} /> The AI agent answers after hours (falls back to voicemail)
          </label>
        </fieldset>
        <div className="actions wide">
          <button className="button" disabled={act.busy || sites.length === 0}>
            See the price
          </button>
        </div>
      </form>
      <ErrorNote error={act.error} />
    </Card>
  );
}
