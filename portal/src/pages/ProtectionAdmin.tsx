import { useState, type FormEvent } from "react";
import {
  addBlockedSource,
  protectionPaths,
  removeBlockedSource,
  updateProtection,
  useApi,
  type BlockedSource,
  type ProtectionAdminState,
  type ProtectionSettings,
} from "../api";
import { ErrorNote } from "../components";
import { who } from "../customer";
import { Card, RowActions, useAction } from "../ui";
import { DROP_KINDS, protectionLimits } from "./Internet";

// Admin: DDoS protection on the PoP's shared public address (ADR 0012,
// docs/protection-contract.md §2). Per-source limits with automatic blocking,
// a SYN flood limit and ExaCarib's block list.

const count = (v: unknown) => {
  const n = Number(v ?? 0);
  return Number.isFinite(n) ? n.toLocaleString("en-GB") : "–";
};

/** "9 min 20 s", "2 h 5 min". */
export function timeLeft(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    const r = s % 60;
    return r ? `${m} min ${r} s` : `${m} min`;
  }
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  if (h >= 48) return `${Math.round(s / 86400)} days`;
  return m ? `${h} h ${m} min` : `${h} h`;
}

const when = (iso: string) =>
  new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

export function ProtectionAdmin() {
  const state = useApi<ProtectionAdminState>(protectionPaths.admin, 10_000);
  const data = state.data;
  return (
    <>
      <ErrorNote error={state.error} />
      {data && (
        <>
          <Settings key={JSON.stringify(data.settings)} settings={data.settings} reload={state.reload} />
          <Dropped data={data} />
          <Blocklist list={data.blocklist} reload={state.reload} />
        </>
      )}
    </>
  );
}

// ---- Limits ----

function Settings({ settings, reload }: { settings: ProtectionSettings; reload: () => void }) {
  const [f, setF] = useState({
    enabled: settings.enabled,
    new_per_source: String(settings.new_per_source),
    syn_per_s: String(settings.syn_per_s),
    block_minutes: String(settings.block_minutes),
  });
  const [saved, setSaved] = useState<string | null>(null);
  const act = useAction();
  const set = (k: "new_per_source" | "syn_per_s" | "block_minutes") => (e: { target: { value: string } }) =>
    setF({ ...f, [k]: e.target.value });
  const next: ProtectionSettings = {
    enabled: f.enabled,
    new_per_source: Number(f.new_per_source),
    syn_per_s: Number(f.syn_per_s),
    block_minutes: Number(f.block_minutes),
  };
  const changed = (Object.keys(next) as (keyof ProtectionSettings)[]).filter((k) => next[k] !== settings[k]);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (settings.enabled && !next.enabled) {
      const q = "Switch DDoS protection off? The PoP's public address is then open to floods for every customer on it.";
      if (!window.confirm(q)) return;
    }
    act.run(async () => {
      setSaved(null);
      await updateProtection(Object.fromEntries(changed.map((k) => [k, next[k]])));
      setSaved("Saved. The PoP applies it within 10 seconds.");
      reload();
    });
  };
  return (
    <Card title="DDoS protection" note={settings.enabled ? <span className="pill ok">On</span> : <span className="pill warn">Off</span>}>
      <p className="small muted" style={{ marginTop: 0 }}>
        Limits new inbound connections to the PoP's shared public address. Replies to customers' outbound traffic are never
        affected. Customers see these limits and the counts on their Internet screen, read-only.
      </p>
      <form className="form" onSubmit={submit}>
        <label className="check wide">
          <input type="checkbox" checked={f.enabled} onChange={(e) => setF({ ...f, enabled: e.target.checked })} /> Protection on
        </label>
        <label>
          New connections a second per source
          <input type="number" min={1} max={100000} step={1} required value={f.new_per_source} onChange={set("new_per_source")} />
        </label>
        <label>
          Block for (minutes)
          <input type="number" min={1} max={1440} step={1} required value={f.block_minutes} onChange={set("block_minutes")} />
        </label>
        <label>
          New TCP connections a second, all sources
          <input type="number" min={10} max={1000000} step={1} required value={f.syn_per_s} onChange={set("syn_per_s")} />
        </label>
        <div className="actions">
          <button className="button" disabled={act.busy || changed.length === 0}>
            Save
          </button>
        </div>
        {next.enabled && changed.length > 0 && Object.values(next).every((v) => v !== 0) && (
          <ul className="small muted wide" style={{ margin: 0, paddingLeft: 20 }}>
            {protectionLimits(next)
              .slice(0, 2)
              .map((line) => (
                <li key={line}>{line}</li>
              ))}
          </ul>
        )}
        {saved && (
          <p className="ok-note small wide" role="status" style={{ margin: 0 }}>
            {saved}
          </p>
        )}
        <div className="wide">
          <ErrorNote error={act.error} />
        </div>
      </form>
    </Card>
  );
}

// ---- Counts and automatically blocked sources ----

function Dropped({ data }: { data: ProtectionAdminState }) {
  const auto = [...data.auto_blocked].sort((a, b) => b.expires_s - a.expires_s);
  return (
    <Card title="Dropped and blocked now">
      <div className="grid">
        <div className="span-6">
          <div className="table-wrap">
            <table className="paths dt compact small">
              <thead>
                <tr>
                  <th scope="col">Dropped</th>
                  <th scope="col" className="num">Packets</th>
                </tr>
              </thead>
              <tbody>
                {DROP_KINDS.map((k) => (
                  <tr key={k.key}>
                    <td>{k.label}</td>
                    <td className="num">{count(data.dropped?.[k.key])}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <div className="span-6">
          <h3 style={{ marginTop: 0, marginBottom: 8 }}>
            Blocked automatically: <span className="mono">{auto.length}</span>
          </h3>
          {auto.length === 0 ? (
            <p className="small muted">No source is over the limit right now.</p>
          ) : (
            <div className="table-wrap">
              <table className="paths dt compact small">
                <thead>
                  <tr>
                    <th scope="col">Address</th>
                    <th scope="col" className="num">Time left</th>
                  </tr>
                </thead>
                <tbody>
                  {auto.map((a) => (
                    <tr key={a.address}>
                      <td className="mono">{a.address}</td>
                      <td className="num">{timeLeft(a.expires_s)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {auto.length >= 100 && <p className="small muted">The PoP reports up to 100 addresses; there may be more.</p>}
        </div>
      </div>
    </Card>
  );
}

// ---- Block list ----

function Blocklist({ list, reload }: { list: BlockedSource[]; reload: () => void }) {
  const [adding, setAdding] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const act = useAction();
  const remove = (b: BlockedSource) => {
    if (!window.confirm(`Remove ${b.prefix} from the block list? It can reach the public address again within 10 seconds.`)) return;
    act.run(async () => {
      setNote(null);
      await removeBlockedSource(b.id);
      setNote(`${b.prefix} removed from the block list.`);
      reload();
    });
  };
  return (
    <Card
      title="Block list"
      note={
        <button className="button small" aria-pressed={adding} onClick={() => setAdding(!adding)}>
          Block an address
        </button>
      }
    >
      <p className="callout warn small">
        <strong>Applies to every customer.</strong> The PoP's public address is shared, so an address on this list can't reach
        any customer's port forwards or services on it.
      </p>
      <ErrorNote error={act.error} />
      {note && (
        <p className="ok-note small" role="status">
          {note}
        </p>
      )}
      {adding && (
        <BlockForm
          onDone={(msg) => {
            setAdding(false);
            setNote(msg);
            reload();
          }}
          onCancel={() => setAdding(false)}
        />
      )}
      {list.length === 0 ? (
        <p className="muted">Nothing on the block list.</p>
      ) : (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Address or range</th>
                <th scope="col">Reason</th>
                <th scope="col">Added</th>
                <th scope="col">Until</th>
                <th scope="col" className="actions">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {list.map((b) => (
                <tr key={b.id}>
                  <td className="mono">{b.prefix}</td>
                  <td data-label="Reason" className="small cell-wrap">
                    {b.reason || "–"}
                  </td>
                  <td data-label="Added" className="small">
                    {when(b.created_at)}
                    <span className="sub">by {who(b.created_by)}</span>
                  </td>
                  <td data-label="Until" className="small">
                    {b.expires_at ? (
                      <>
                        {when(b.expires_at)}
                        <span className="sub">{timeLeft((new Date(b.expires_at).getTime() - Date.now()) / 1000)} left</span>
                      </>
                    ) : (
                      "Until removed"
                    )}
                  </td>
                  <td className="actions">
                    <RowActions
                      label={b.prefix}
                      disabled={act.busy}
                      items={[{ label: "Remove from block list", danger: true, onSelect: () => remove(b) }]}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function BlockForm({ onDone, onCancel }: { onDone: (msg: string) => void; onCancel: () => void }) {
  const [f, setF] = useState({ prefix: "", reason: "", hours: "24", forever: false });
  const act = useAction();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const prefix = f.prefix.trim();
    const hours = f.forever ? null : Number(f.hours);
    act.run(async () => {
      await addBlockedSource({ prefix, reason: f.reason.trim() || undefined, hours });
      onDone(`${prefix} blocked ${hours === null ? "until removed" : `for ${hours === 1 ? "1 hour" : `${hours} hours`}`}. The PoP applies it within 10 seconds.`);
    });
  };
  return (
    <form className="form card-inset" onSubmit={submit} style={{ marginBottom: 16 }}>
      <h3 className="wide">Block an address</h3>
      <label>
        Address or range
        <input value={f.prefix} onChange={(e) => setF({ ...f, prefix: e.target.value })} required placeholder="203.0.113.66 or 198.51.100.0/24" />
      </label>
      <label>
        Reason
        <input value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} maxLength={200} placeholder="Optional" />
      </label>
      <label>
        For (hours)
        <input
          type="number"
          min={1}
          step={1}
          required={!f.forever}
          disabled={f.forever}
          value={f.forever ? "" : f.hours}
          onChange={(e) => setF({ ...f, hours: e.target.value })}
          placeholder="Until removed"
        />
      </label>
      <label className="check">
        <input type="checkbox" checked={f.forever} onChange={(e) => setF({ ...f, forever: e.target.checked })} /> Until removed
      </label>
      <div className="actions wide">
        <button className="button" disabled={act.busy}>
          Block
        </button>
        <button type="button" className="button secondary" onClick={onCancel}>
          Cancel
        </button>
      </div>
      <div className="wide">
        <ErrorNote error={act.error} />
      </div>
    </form>
  );
}
