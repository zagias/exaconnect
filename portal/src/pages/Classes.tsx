import { useState, type FormEvent } from "react";
import { api, useApi } from "../api";
import { ErrorNote } from "../components";
import { useCustomer } from "../customer";
import { Card, RowActions, useAction } from "../ui";

// ---- Application classes and SLA policies ----

interface ClassRow {
  customer_id: string;
  name: string;
  description: string;
  dscp: number[];
  ports: string;
  subnets: string[];
  ordinal: number;
  max_latency_ms: string | number | null;
  max_jitter_ms: string | number | null;
  max_loss_pct: string | number | null;
  allow_satellite: boolean;
  priority: Priority;
  preferred_path: string | null;
  builtin: boolean;
}

export type Priority = "realtime" | "interactive" | "normal" | "bulk";

export const PRIORITY_LABEL: Record<Priority, string> = {
  realtime: "Real-time, first in the queue",
  interactive: "Interactive",
  normal: "Normal",
  bulk: "Bulk, last in the queue",
};

export interface PathOption {
  name: string;
  label: string;
  carriers: string[];
}

/** Classes say how traffic is treated: queue priority, SLA and preferred path. */
export function Classes() {
  const { current } = useCustomer();
  const list = useApi<ClassRow[]>(current ? `/classes?customer_id=${current.id}` : null, 0);
  const paths = useApi<PathOption[]>(current ? `/customers/${current.id}/paths` : null, 0);
  const [editing, setEditing] = useState<ClassRow | "new" | null>(null);
  const act = useAction();
  if (!current) return null;
  const remove = (c: ClassRow) => {
    if (!window.confirm(`Delete the ${c.name} class? Its traffic goes back to normal priority and routing.`)) return;
    act.run(async () => {
      await api(`/customers/${current.id}/classes/${c.name}`, { method: "DELETE" });
      list.reload();
    });
  };
  const val = (v: string | number | null, unit: string) => (v == null ? "–" : `${Number(v)} ${unit}`);
  return (
    <Card
      title={`Classes for ${current.name}`}
      note={
        <button className="button small" onClick={() => setEditing("new")}>
          Add a class
        </button>
      }
    >
      <p className="muted small" style={{ marginTop: 0 }}>
        A class sets its traffic's place in the queue, the SLA the routing engine keeps it within, and the path it prefers.
        Its own DSCP, ports and subnets are matched after your traffic rules.
      </p>
      <ErrorNote error={list.error ?? act.error} />
      {editing && (
        <ClassForm
          customerId={current.id}
          cls={editing === "new" ? null : editing}
          paths={paths.data ?? []}
          onDone={() => {
            setEditing(null);
            list.reload();
          }}
        />
      )}
      <div className="table-wrap">
        <table className="paths dt stack">
          <thead>
            <tr>
              <th scope="col">Class</th>
              <th scope="col">Priority</th>
              <th scope="col">Prefers</th>
              <th scope="col">Match</th>
              <th scope="col" className="num">Latency</th>
              <th scope="col" className="num">Jitter</th>
              <th scope="col" className="num">Loss</th>
              <th scope="col">Satellite</th>
              <th scope="col" className="actions">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {(list.data ?? []).map((c) => (
              <tr key={c.name}>
                <td>
                  <strong className="mono">{c.name}</strong>
                  {c.description && <span className="sub">{c.description}</span>}
                </td>
                <td data-label="Priority" className="small">
                  {PRIORITY_LABEL[c.priority]}
                </td>
                <td data-label="Prefers" className="small">
                  {pathLabel(paths.data ?? [], c.preferred_path)}
                </td>
                <td data-label="Match" className="small cell-wrap">
                  {c.dscp.length > 0 && <div>DSCP {c.dscp.join(", ")}</div>}
                  {c.ports && <div className="mono">{c.ports}</div>}
                  {c.subnets.length > 0 && <div className="mono">{c.subnets.join(", ")}</div>}
                  {c.dscp.length === 0 && !c.ports && c.subnets.length === 0 && <span className="muted">Rules only</span>}
                </td>
                <td data-label="Latency" className="num">
                  {val(c.max_latency_ms, "ms")}
                </td>
                <td data-label="Jitter" className="num">
                  {val(c.max_jitter_ms, "ms")}
                </td>
                <td data-label="Loss" className="num">
                  {val(c.max_loss_pct, "%")}
                </td>
                <td data-label="Satellite" className="small">
                  {c.allow_satellite ? "In Storm Mode" : "Never"}
                </td>
                <td className="actions">
                  <RowActions
                    label={`the ${c.name} class`}
                    disabled={act.busy}
                    primary={
                      <button className="button secondary small" aria-label={`Edit ${c.name}`} onClick={() => setEditing(c)}>
                        Edit
                      </button>
                    }
                    items={c.builtin ? [] : [{ label: "Delete class", danger: true, onSelect: () => remove(c) }]}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function pathLabel(paths: PathOption[], name: string | null): string {
  if (!name) return "Best path";
  return paths.find((p) => p.name === name)?.label ?? name;
}

function ClassForm({
  customerId,
  cls,
  paths,
  onDone,
}: {
  customerId: string;
  cls: ClassRow | null;
  paths: PathOption[];
  onDone: () => void;
}) {
  const num = (v: string | number | null | undefined) => (v == null ? "" : String(Number(v)));
  const [f, setF] = useState({
    name: cls?.name ?? "",
    description: cls?.description ?? "",
    dscp: cls?.dscp.join(", ") ?? "",
    ports: cls?.ports ?? "",
    subnets: cls?.subnets.join(", ") ?? "",
    ordinal: String(cls?.ordinal ?? 10),
    latency: num(cls?.max_latency_ms),
    jitter: num(cls?.max_jitter_ms),
    loss: num(cls?.max_loss_pct),
    allow_satellite: cls?.allow_satellite ?? true,
    priority: cls?.priority ?? ("normal" as Priority),
    preferred_path: cls?.preferred_path ?? "",
  });
  const act = useAction();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const list = (s: string) =>
    s
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);
  const opt = (s: string) => (s.trim() === "" ? null : Number(s));
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api(`/customers/${customerId}/classes/${encodeURIComponent(f.name)}`, {
        method: "PUT",
        body: JSON.stringify({
          description: f.description,
          dscp: list(f.dscp).map(Number),
          ports: f.ports.trim(),
          subnets: list(f.subnets),
          ordinal: Number(f.ordinal),
          priority: f.priority,
          preferred_path: f.preferred_path || null,
          sla: { max_latency_ms: opt(f.latency), max_jitter_ms: opt(f.jitter), max_loss_pct: opt(f.loss), allow_satellite: f.allow_satellite },
        }),
      });
      onDone();
    });
  };
  return (
    <form className="form card-inset" onSubmit={submit} style={{ marginBottom: 16 }}>
      <h3 className="wide">{cls ? `Edit ${cls.name}` : "New class"}</h3>
      <label>
        Name
        <input value={f.name} onChange={set("name")} required pattern="[a-z0-9][a-z0-9-]{0,19}" readOnly={!!cls} title="Lower case letters, digits and dashes" />
      </label>
      <label>
        Description
        <input value={f.description} onChange={set("description")} maxLength={200} />
      </label>
      <label>
        Order
        <input value={f.ordinal} onChange={set("ordinal")} inputMode="numeric" pattern="[0-9]+" />
      </label>
      <label>
        Priority
        <select value={f.priority} onChange={(e) => setF({ ...f, priority: e.target.value as Priority })}>
          {(Object.keys(PRIORITY_LABEL) as Priority[]).map((p) => (
            <option key={p} value={p}>
              {PRIORITY_LABEL[p]}
            </option>
          ))}
        </select>
      </label>
      <label>
        Preferred path
        <select value={f.preferred_path} onChange={set("preferred_path")}>
          <option value="">Best path (the engine decides)</option>
          {paths.map((p) => (
            <option key={p.name} value={p.name}>
              {p.label} ({p.carriers.join(", ")})
            </option>
          ))}
        </select>
      </label>
      <label>
        DSCP values
        <input value={f.dscp} onChange={set("dscp")} placeholder="46, 34" />
      </label>
      <label>
        Ports
        <input value={f.ports} onChange={set("ports")} placeholder="udp:5060, udp:10000-20000" />
      </label>
      <label>
        Subnets
        <input value={f.subnets} onChange={set("subnets")} placeholder="52.112.0.0/14" />
      </label>
      <label>
        Latency limit, ms
        <input value={f.latency} onChange={set("latency")} inputMode="decimal" placeholder="Best effort" />
      </label>
      <label>
        Jitter limit, ms
        <input value={f.jitter} onChange={set("jitter")} inputMode="decimal" placeholder="Best effort" />
      </label>
      <label>
        Loss limit, %
        <input value={f.loss} onChange={set("loss")} inputMode="decimal" placeholder="Best effort" />
      </label>
      <label className="check wide">
        <input type="checkbox" checked={f.allow_satellite} onChange={(e) => setF({ ...f, allow_satellite: e.target.checked })} />
        May use satellite in Storm Mode when both terrestrial paths fail
      </label>
      <div className="actions wide">
        <button className="button" disabled={act.busy}>
          Save class
        </button>
        <button type="button" className="button secondary" onClick={onDone}>
          Cancel
        </button>
        <ErrorNote error={act.error} />
      </div>
    </form>
  );
}

