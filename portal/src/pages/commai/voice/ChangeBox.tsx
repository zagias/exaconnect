import { useEffect, useState } from "react";
import { api } from "../../../api";
import { ErrorNote, ExampleTag } from "../../../components";
import { useAction } from "../../../ui";
import type { ChangeResult, Op, PriceImpact } from "./types";

const AREA: Record<string, string> = {
  sites: "Site",
  users: "User",
  numbers: "Number",
  devices: "Device",
  ring_groups: "Ring group",
  queues: "Queue",
  hours: "Business hours",
  menus: "Menu",
  ai_rules: "AI rule",
};

export function money(currency: string, amount: string): string {
  return `${currency} ${amount}`;
}

/** What a change does to the bill, before anyone saves it. */
export function PriceLines({ price }: { price: PriceImpact }) {
  if (!price.changes_bill) return <p className="muted small">No change to your bill.</p>;
  return (
    <div className="voice-price" aria-label="Price impact">
      <div className="row-between">
        <strong>Price impact</strong>
        {price.example_prices && <ExampleTag />}
      </div>
      <ul className="plain-list">
        {price.lines.map((l) => (
          <li key={l.label} className="row-between">
            <span>{l.label}</span>
            <span className="mono">
              {money(price.currency, l.amount)}
              {l.recurring ? " a month" : " once"}
            </span>
          </li>
        ))}
      </ul>
      <p className="small">
        Monthly change <span className="mono">{money(price.currency, price.monthly_delta)}</span>, one-time{" "}
        <span className="mono">{money(price.currency, price.one_time)}</span> (rate card v{price.rate_card_version})
      </p>
    </div>
  );
}

export function DiffList({ diff }: { diff: ChangeResult["diff"] }) {
  if (!diff.length) return <p className="muted small">Nothing would change.</p>;
  return (
    <ul className="plain-list">
      {diff.map((d, i) => (
        <li key={i}>
          <span className={`pill small ${d.change === "removed" ? "warn" : "ok"}`}>
            {d.change === "added" ? "Added" : d.change === "removed" ? "Removed" : "Changed"}
          </span>{" "}
          {AREA[d.area] ?? d.area}: {d.item}
          {d.fields?.length ? <span className="muted small"> ({d.fields.join(", ")})</span> : null}
        </li>
      ))}
    </ul>
  );
}

/**
 * Check a change, show the exact difference and price, then save it now or
 * at a set time. Nothing is saved until Save is pressed.
 */
export function ChangeBox({
  base,
  ops,
  title,
  onDone,
  onCancel,
}: {
  base: string;
  ops: Op[];
  title: string;
  onDone: (out: ChangeResult) => void;
  onCancel: () => void;
}) {
  const [preview, setPreview] = useState<ChangeResult | null>(null);
  const [when, setWhen] = useState<"now" | "later">("now");
  const [at, setAt] = useState("");
  const check = useAction();
  const save = useAction();

  useEffect(() => {
    setPreview(null);
    check.run(async () => {
      setPreview(await api<ChangeResult>(`${base}/voice/preview`, { method: "POST", body: JSON.stringify({ ops }) }));
    });
  }, [base, JSON.stringify(ops)]);

  const submit = () =>
    save.run(async () => {
      if (!preview) return;
      const body: Record<string, unknown> = {
        ops,
        accepted_price: { monthly_delta: preview.price_impact.monthly_delta, one_time: preview.price_impact.one_time },
      };
      if (when === "later") {
        if (!at) throw new Error("Choose when to make the change.");
        body.run_at = new Date(at).toISOString();
      }
      onDone(await api<ChangeResult>(`${base}/voice/changes`, { method: "POST", body: JSON.stringify(body) }));
    });

  const blocked = !!preview && (!preview.ok || (preview.price_impact.changes_bill && !preview.can_spend));
  return (
    <section className="card voice-change" aria-live="polite" aria-labelledby="voice-change-title" style={{ marginBottom: 24 }}>
      <div className="card-head">
        <h2 id="voice-change-title">Check and save: {title}</h2>
      </div>
      <ErrorNote error={check.error} />
      {!preview && !check.error && <p className="muted">Checking…</p>}
      {preview && (
        <>
          {preview.errors.length > 0 && (
            <div role="alert">
              <p className="pill bad">This can't be saved yet</p>
              <ul className="plain-list">
                {preview.errors.map((e) => (
                  <li key={e.index}>
                    {ops.length > 1 ? `Change ${e.index + 1}: ` : ""}
                    {e.error}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <h3 className="small">What changes</h3>
          <DiffList diff={preview.diff} />
          <PriceLines price={preview.price_impact} />
          {preview.price_impact.changes_bill && !preview.can_spend && (
            <p className="pill warn">This changes your bill. Someone with spend permission must save it.</p>
          )}
          <fieldset className="voice-when">
            <legend>When</legend>
            <label className="check">
              <input type="radio" name="voice-when" checked={when === "now"} onChange={() => setWhen("now")} /> Now
            </label>
            <label className="check">
              <input type="radio" name="voice-when" checked={when === "later"} onChange={() => setWhen("later")} /> At a set time
            </label>
            {when === "later" && (
              <label>
                Date and time
                <input type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} required />
              </label>
            )}
          </fieldset>
        </>
      )}
      <ErrorNote error={save.error} />
      <div className="actions">
        <button className="button" onClick={submit} disabled={!preview || blocked || save.busy}>
          {when === "later" ? "Schedule change" : "Save change"}
        </button>{" "}
        <button className="button secondary" onClick={onCancel} disabled={save.busy}>
          Cancel
        </button>
      </div>
    </section>
  );
}
