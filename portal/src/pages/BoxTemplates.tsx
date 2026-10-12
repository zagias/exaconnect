// Box templates (ADR 0052): one port set-up for many of an organisation's
// ExaCarib boxes. A box that follows a template gets its ports now and every
// time the template changes; its office port can take "auto", the first
// address of each location's own office network.
import { useState, type FormEvent } from "react";
import { api, useApi, type BoxPort } from "../api";
import { ErrorNote, ago } from "../components";
import { useCustomer } from "../customer";
import { PortsForm, rowsFrom, toNetwork, type Row } from "../boxports";
import { Card, PageHead, useAction } from "../ui";
import { useCanManage } from "./org/common";

interface Template {
  id: string;
  name: string;
  network: { wan: BoxPort[]; lan?: BoxPort | null; lan_dhcp?: boolean };
  updated_by: string;
  updated_at: string;
}
interface BoxRow {
  site_id: string;
  site: string;
  managed: "site" | "cloud" | "both";
  template_id: string | null;
  ports: string[];
  state: string | null;
}
interface Result {
  site_id: string;
  site?: string;
  ok: boolean;
  error?: string;
}

const PATH_NAMES: Record<string, string> = {
  "carrier-a": "Internet link 1",
  "carrier-b": "Internet link 2",
  sat: "Satellite",
};
const MODE_NAMES: Record<string, string> = {
  dhcp: "automatic",
  static: "fixed",
  keep: "as it is",
  pppoe: "PPPoE",
};
const STATE_NAMES: Record<string, string> = {
  sent: "Sent, waiting for the box",
  trying: "Box is trying it",
  kept: "Box is using it",
  undone: "Box put its earlier ports back",
  refused: "Box did not use it",
  replaced: "Changed again on the box",
};
const MANAGED_NAMES = {
  site: "On site",
  cloud: "Cloud",
  both: "On site and cloud",
};

function summary(n: Template["network"]): string {
  const wan = n.wan.map(
    (p) =>
      `${PATH_NAMES[p.path ?? ""] ?? p.path} on ${p.iface} (${MODE_NAMES[p.mode] ?? p.mode})`,
  );
  const lan = n.lan
    ? [
        `office network on ${n.lan.iface}${n.lan.address ? ` ${n.lan.address}` : ""}${n.lan_dhcp ? ", handing out addresses" : ""}`,
      ]
    : [];
  return [...wan, ...lan].join(", ");
}

export function BoxTemplates() {
  const { current } = useCustomer();
  const canManage = useCanManage();
  const q = current ? `?customer_id=${current.id}` : "";
  const data = useApi<{ templates: Template[]; boxes: BoxRow[] }>(
    current ? `/box-templates${q}` : null,
    15_000,
  );
  const [editing, setEditing] = useState<Template | "new" | null>(null);
  const [rows, setRows] = useState<Row[] | null>(null);
  const [dhcp, setDhcp] = useState(false);
  const [name, setName] = useState("");
  const [results, setResults] = useState<Result[] | null>(null);
  const [pick, setPick] = useState<Record<string, string[]>>({});
  const act = useAction();
  if (!current) return null;
  const templates = data.data?.templates ?? [];
  const boxes = data.data?.boxes ?? [];
  // Every port name any of the organisation's boxes has, so a template can use any of them.
  const names = Array.from(new Set(boxes.flatMap((b) => b.ports))).sort();
  if (names.length === 0) names.push("eth0", "eth1", "eth2", "eth3");

  const open = (t: Template | "new") => {
    const n = t === "new" ? { wan: [], lan: null, lan_dhcp: false } : t.network;
    const all = Array.from(
      new Set([
        ...names,
        ...n.wan.map((p) => p.iface),
        ...(n.lan ? [n.lan.iface] : []),
      ]),
    );
    setRows(rowsFrom(all, n.wan, n.lan));
    setDhcp(!!n.lan_dhcp);
    setName(t === "new" ? "" : t.name);
    setEditing(t);
    setResults(null);
    act.setError(null);
  };
  const save = (e: FormEvent) => {
    e.preventDefault();
    if (!rows || !editing) return;
    act.run(async () => {
      const body = JSON.stringify({ name, network: toNetwork(rows, dhcp) });
      if (editing === "new") {
        await api(`/box-templates${q}`, { method: "POST", body });
      } else {
        const r = await api<{ results: Result[] }>(
          `/box-templates/${editing.id}`,
          { method: "PUT", body },
        );
        setResults(r.results);
      }
      setEditing(null);
      setRows(null);
      data.reload();
    });
  };
  const follow = (t: Template) =>
    act.run(async () => {
      const r = await api<{ results: Result[] }>(
        `/box-templates/${t.id}/sites`,
        {
          method: "POST",
          body: JSON.stringify({ site_ids: pick[t.id] ?? [] }),
        },
      );
      setResults(r.results);
      setPick((p) => ({ ...p, [t.id]: [] }));
      data.reload();
    });
  const unfollow = (t: Template, b: BoxRow) =>
    act.run(async () => {
      await api(`/box-templates/${t.id}/sites/${b.site_id}`, {
        method: "DELETE",
      });
      data.reload();
    });
  const remove = (t: Template) => {
    if (
      !window.confirm(
        `Remove the template ${t.name}? Its boxes keep the ports they have.`,
      )
    )
      return;
    act.run(async () => {
      await api(`/box-templates/${t.id}`, { method: "DELETE" });
      data.reload();
    });
  };

  return (
    <>
      <PageHead title="Box templates">
        One port set-up for many ExaCarib boxes. Boxes that follow a template
        get its ports now and whenever it changes, if they are managed from the
        cloud or both.
      </PageHead>
      <ErrorNote error={data.error} />
      {results && (
        <Card title="Result">
          <ul className="small" role="status">
            {results.map((r) => (
              <li key={r.site_id}>
                {r.site ?? r.site_id}: {r.ok ? "sent to the box" : r.error}
              </li>
            ))}
          </ul>
        </Card>
      )}
      {editing && rows && (
        <Card
          title={editing === "new" ? "New template" : `Change ${editing.name}`}
        >
          <PortsForm
            rows={rows}
            setRows={setRows}
            dhcp={dhcp}
            setDhcp={setDhcp}
            busy={act.busy}
            error={act.error}
            submit={
              editing === "new"
                ? "Save the template"
                : "Save and send to its boxes"
            }
            onSubmit={save}
            onCancel={() => setEditing(null)}
            intro={
              'For the office port, the address "auto" gives each box the first address of its own location\'s office network. PPPoE sign-ins differ per location, so set those on each box.'
            }
            noPppoe
            autoLan
          >
            <label className="wide">
              Template name
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                maxLength={80}
              />
            </label>
          </PortsForm>
        </Card>
      )}
      <Card
        title={`Templates for ${current.name}`}
        note={
          canManage && !editing ? (
            <button
              className="button secondary small"
              onClick={() => open("new")}
            >
              New template
            </button>
          ) : null
        }
      >
        {templates.length === 0 && (
          <p className="muted small empty">No templates yet.</p>
        )}
        {templates.map((t) => {
          const following = boxes.filter((b) => b.template_id === t.id);
          const others = boxes.filter((b) => b.template_id !== t.id);
          return (
            <section
              key={t.id}
              className="card-inset"
              aria-label={`Template ${t.name}`}
              style={{ marginBottom: 16 }}
            >
              <div className="card-head">
                <h3>{t.name}</h3>
                {canManage && (
                  <div className="actions">
                    <button
                      className="button secondary small"
                      onClick={() => open(t)}
                      disabled={act.busy}
                    >
                      Change
                    </button>
                    <button
                      className="button secondary small"
                      onClick={() => remove(t)}
                      disabled={act.busy}
                    >
                      Remove
                    </button>
                  </div>
                )}
              </div>
              <p className="small">{summary(t.network)}</p>
              <p className="small muted">
                Changed by {t.updated_by}, {ago(t.updated_at)}.
              </p>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Location</th>
                      <th>Managed from</th>
                      <th>Ports</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {following.length === 0 && (
                      <tr>
                        <td colSpan={4} className="muted small">
                          No box follows this template yet.
                        </td>
                      </tr>
                    )}
                    {following.map((b) => (
                      <tr key={b.site_id} data-site={b.site}>
                        <td>{b.site}</td>
                        <td>{MANAGED_NAMES[b.managed]}</td>
                        <td>
                          {b.state ? (STATE_NAMES[b.state] ?? b.state) : "–"}
                        </td>
                        <td>
                          {canManage && (
                            <button
                              className="button secondary small"
                              onClick={() => unfollow(t, b)}
                              disabled={act.busy}
                            >
                              Stop following
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {canManage && others.length > 0 && (
                <div
                  className="form-actions"
                  style={{
                    marginTop: 8,
                    alignItems: "center",
                    flexWrap: "wrap",
                  }}
                >
                  <span className="small">Make these boxes follow it:</span>
                  {others.map((b) => (
                    <label key={b.site_id} className="check small">
                      <input
                        type="checkbox"
                        aria-label={`${b.site} follows ${t.name}`}
                        checked={(pick[t.id] ?? []).includes(b.site_id)}
                        disabled={b.managed === "site"}
                        onChange={(e) =>
                          setPick((p) => ({
                            ...p,
                            [t.id]: e.target.checked
                              ? [...(p[t.id] ?? []), b.site_id]
                              : (p[t.id] ?? []).filter((x) => x !== b.site_id),
                          }))
                        }
                      />{" "}
                      {b.site}
                      {b.managed === "site" ? " (managed on site)" : ""}
                    </label>
                  ))}
                  <button
                    className="button small"
                    onClick={() => follow(t)}
                    disabled={act.busy || !(pick[t.id] ?? []).length}
                  >
                    Send to these boxes
                  </button>
                </div>
              )}
            </section>
          );
        })}
        {!editing && act.error && <p className="pill bad">{act.error}</p>}
        {boxes.length === 0 && (
          <p className="muted small">
            This organisation has no ExaCarib boxes in Connect yet.
          </p>
        )}
      </Card>
    </>
  );
}
