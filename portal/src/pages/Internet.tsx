import { useState, type FormEvent } from "react";
import {
  createFirewallRule,
  createPortForward,
  deleteFirewallRule,
  deletePortForward,
  internetPaths,
  num,
  orderFirewallRules,
  setBreakout,
  updateFirewallRule,
  updatePortForward,
  useApi,
  type BreakoutMode,
  type FirewallProtocol,
  type FirewallRule,
  type FirewallRuleIn,
  type InternetSite,
  type InternetState,
  type PortForward,
  type PortForwardIn,
  type ProtectionSummary,
} from "../api";
import { ErrorNote, ago } from "../components";
import { useCustomer } from "../customer";
import { Card, PageHead, RowActions, useAction } from "../ui";

// Internet breakout, NAT gateway and firewall (ADR 0010, docs/internet-contract.md).
// Each site sends its internet traffic through the PoP, straight out of its own
// carrier links, or not at all. The firewall applies where traffic leaves.

const MODES: { mode: BreakoutMode; label: string }[] = [
  { mode: "pop", label: "Through ExaCarib's PoP" },
  { mode: "local", label: "Straight out at the site" },
  { mode: "off", label: "Off" },
];

const ACTION: Record<string, { cls: string; word: string }> = {
  allow: { cls: "ok", word: "Allow" },
  deny: { cls: "bad", word: "Deny" },
};

const PROTOCOLS: { value: FirewallProtocol; label: string }[] = [
  { value: "any", label: "Any" },
  { value: "tcp", label: "TCP" },
  { value: "udp", label: "UDP" },
  { value: "icmp", label: "ICMP (ping)" },
];

// "443" or "8000-8100,8443", as in traffic rules.
const PORTS_PATTERN = " *[0-9]+(-[0-9]+)?( *, *[0-9]+(-[0-9]+)?)* *";
const PORTS_HINT = "A port, a range or a list, such as 443 or 8000-8100,8443.";

const split = (s: string) =>
  s
    .split(/[\s,]+/)
    .map((x) => x.trim())
    .filter(Boolean);

const count = (v: unknown) => {
  const n = num(v);
  return n === null ? "–" : n.toLocaleString("en-GB");
};

/** 1,000-based units, as carriers count traffic. */
const bytes = (v: unknown) => {
  const n = num(v);
  if (n === null) return "–";
  const units = ["B", "kB", "MB", "GB", "TB"];
  let x = n;
  let i = 0;
  while (x >= 1000 && i < units.length - 1) {
    x /= 1000;
    i++;
  }
  return `${x.toLocaleString("en-GB", { maximumFractionDigits: i === 0 ? 0 : 1 })} ${units[i]}`;
};

const any = (list: string[] | null | undefined, word = "Any") => ((list ?? []).length ? (list ?? []).join(", ") : word);

export default function Internet() {
  const { current } = useCustomer();
  const state = useApi<InternetState>(current ? internetPaths.state(current.id) : null, 10_000);
  if (!current) return null;
  const data = state.data;
  return (
    <>
      <PageHead eyebrow="Internet access" title="Internet access">
        How each site reaches the internet, what may leave, and what outside can reach in. Sites pick up changes within 10
        seconds.
      </PageHead>
      <ErrorNote error={state.error} />
      {data && (
        <>
          <Breakout customerId={current.id} data={data} reload={state.reload} />
          <Firewall customerId={current.id} data={data} reload={state.reload} />
          <Forwards customerId={current.id} data={data} reload={state.reload} />
          {data.protection && <Protection p={data.protection} publicAddress={data.public_address} />}
        </>
      )}
    </>
  );
}

interface SectionProps {
  customerId: string;
  data: InternetState;
  reload: () => void;
}

// ---- Breakout per site ----

function Breakout({ customerId, data, reload }: SectionProps) {
  return (
    <Card title="How each site reaches the internet">
      <p className="small muted" style={{ marginTop: 0 }}>
        Through the PoP, the PoP's firewall and NAT apply and port forwards work. Straight out uses the site's own carrier
        links, with failover between them.
      </p>
      {data.sites.length === 0 ? (
        <p className="muted">No sites yet.</p>
      ) : (
        <div style={{ display: "grid", gap: 12 }}>
          {data.sites.map((s) => (
            <SiteBreakout key={s.id} customerId={customerId} site={s} forwards={data.forwards} reload={reload} />
          ))}
        </div>
      )}
    </Card>
  );
}

function exitText(s: InternetSite) {
  if (s.mode === "off") return "No internet from this site";
  return s.via_label || "Not reported yet";
}

function SiteBreakout({
  customerId,
  site,
  forwards,
  reload,
}: {
  customerId: string;
  site: InternetSite;
  forwards: PortForward[];
  reload: () => void;
}) {
  const act = useAction();
  const [saved, setSaved] = useState<string | null>(null);
  const choose = (mode: BreakoutMode) => {
    if (mode === site.mode) return;
    const fwd = forwards.filter((f) => f.to_site_id === site.id).length;
    const fwdNote = fwd ? ` Its ${fwd === 1 ? "port forward stops" : `${fwd} port forwards stop`} working until it goes through the PoP again.` : "";
    if (mode === "off" && !window.confirm(`Switch internet off for ${site.name}? Its LAN loses internet access within 10 seconds; your own sites and clouds stay reachable.${fwdNote}`))
      return;
    if (mode === "local" && fwd && !window.confirm(`Send ${site.name}'s internet traffic straight out at the site?${fwdNote}`)) return;
    act.run(async () => {
      setSaved(null);
      await setBreakout(customerId, site.id, mode);
      setSaved("Saved. The site picks it up within 10 seconds.");
      reload();
    });
  };
  return (
    <div className="card-inset">
      <fieldset style={{ border: "none", padding: 0, margin: 0, minWidth: 0 }} disabled={act.busy}>
        <legend style={{ padding: 0 }}>
          <strong>{site.name}</strong>
        </legend>
        <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 16px", margin: "6px 0" }}>
          {MODES.map((m) => (
            <label key={m.mode} className="check" style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
              <input type="radio" name={`breakout-${site.id}`} value={m.mode} checked={site.mode === m.mode} onChange={() => choose(m.mode)} />
              {m.label}
            </label>
          ))}
        </div>
      </fieldset>
      <div className="small">
        <span className="muted">Leaves now through: </span>
        {exitText(site)}
      </div>
      {site.mode === "local" && site.uplinks.length > 0 && (
        <div className="small muted">Fails over between {site.uplinks.join(", then ")}, in that order.</div>
      )}
      {saved && (
        <p className="ok-note small" role="status" style={{ margin: "6px 0 0" }}>
          {saved}
        </p>
      )}
      <ErrorNote error={act.error} />
    </div>
  );
}

// ---- Firewall rules ----

function Firewall({ customerId, data, reload }: SectionProps) {
  const [editing, setEditing] = useState<FirewallRule | "new" | null>(null);
  const act = useAction();
  const rules = [...data.rules].sort((a, b) => a.position - b.position);
  const move = (i: number, by: -1 | 1) => {
    const ids = rules.map((r) => r.id);
    [ids[i], ids[i + by]] = [ids[i + by], ids[i]];
    act.run(async () => {
      await orderFirewallRules(customerId, ids);
      reload();
    });
  };
  const toggle = (r: FirewallRule) =>
    act.run(async () => {
      await updateFirewallRule(customerId, r.id, { enabled: !r.enabled });
      reload();
    });
  const remove = (r: FirewallRule) => {
    if (!window.confirm(`Delete rule ${rules.indexOf(r) + 1}${r.description ? ` (${r.description})` : ""}?`)) return;
    act.run(async () => {
      await deleteFirewallRule(customerId, r.id);
      if (editing !== "new" && editing?.id === r.id) setEditing(null);
      reload();
    });
  };
  return (
    <Card
      title="Firewall rules"
      note={
        <button className="button small" aria-pressed={editing === "new"} onClick={() => setEditing(editing === "new" ? null : "new")}>
          Add a rule
        </button>
      }
    >
      <p className="small muted" style={{ marginTop: 0 }}>
        Checked in order where traffic leaves for the internet; the first match wins. Anything no rule matches is allowed.
      </p>
      <ErrorNote error={act.error} />
      {editing && (
        <RuleForm
          key={editing === "new" ? "new" : editing.id}
          customerId={customerId}
          rule={editing === "new" ? null : editing}
          sites={data.sites}
          count={rules.length}
          onDone={() => {
            setEditing(null);
            reload();
          }}
          onCancel={() => setEditing(null)}
        />
      )}
      {rules.length === 0 ? (
        <div className="empty">
          <p>No rules yet, so all outbound internet traffic is allowed.</p>
          {editing !== "new" && (
            <button className="button small" onClick={() => setEditing("new")}>
              Add a rule
            </button>
          )}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col" className="num">#</th>
                <th scope="col">Action</th>
                <th scope="col">Site</th>
                <th scope="col">From</th>
                <th scope="col">To</th>
                <th scope="col">Protocol</th>
                <th scope="col">Description</th>
                <th scope="col" className="num">Packets</th>
                <th scope="col" className="actions">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rules.map((r, i) => {
                const a = ACTION[r.action] ?? ACTION.allow;
                const proto = PROTOCOLS.find((p) => p.value === r.protocol)?.label ?? r.protocol;
                return (
                  <tr key={r.id} className={r.enabled ? undefined : "row-off"}>
                    <td className="num">
                      <span className="stack-only">Rule </span>
                      {i + 1}
                    </td>
                    <td data-label="Action">
                      <span className={`pill ${a.cls}`}>{a.word}</span>
                      {!r.enabled && <span className="sub">Off, not checked</span>}
                    </td>
                    <td data-label="Site">{r.site ?? "All sites"}</td>
                    <td data-label="From" className="mono cell-wrap">
                      {any(r.src)}
                    </td>
                    <td data-label="To" className="mono cell-wrap">
                      {any(r.dst)}
                    </td>
                    <td data-label="Protocol">
                      {proto}
                      {r.ports && <span className="mono"> {r.ports}</span>}
                    </td>
                    <td data-label="Description" className="cell-wrap">
                      {r.description || "–"}
                    </td>
                    <td data-label="Packets" className="num">
                      {count(r.packets)}
                      <span className="sub">{bytes(r.bytes)}</span>
                    </td>
                    <td className="actions">
                      <RowActions
                        label={`rule ${i + 1}`}
                        disabled={act.busy}
                        primary={
                          <button className="button secondary small" aria-label={`Edit rule ${i + 1}`} onClick={() => setEditing(r)}>
                            Edit
                          </button>
                        }
                        items={[
                          { label: "Move up", disabled: i === 0, onSelect: () => move(i, -1) },
                          { label: "Move down", disabled: i === rules.length - 1, onSelect: () => move(i, 1) },
                          { label: r.enabled ? "Turn off" : "Turn on", onSelect: () => toggle(r) },
                          { label: "Delete rule", danger: true, onSelect: () => remove(r) },
                        ]}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function RuleForm({
  customerId,
  rule,
  sites,
  count: total,
  onDone,
  onCancel,
}: {
  customerId: string;
  rule: FirewallRule | null;
  sites: InternetSite[];
  count: number;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [f, setF] = useState({
    action: rule?.action ?? "deny",
    site_id: rule?.site_id ?? "",
    src: (rule?.src ?? []).join(", "),
    dst: (rule?.dst ?? []).join(", "),
    protocol: (rule?.protocol ?? "any") as FirewallProtocol,
    ports: rule?.ports ?? "",
    description: rule?.description ?? "",
    enabled: rule?.enabled ?? true,
    position: "",
  });
  const act = useAction();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const hasPorts = f.protocol === "tcp" || f.protocol === "udp";
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const body: FirewallRuleIn = {
        action: f.action,
        site_id: f.site_id || null,
        src: split(f.src),
        dst: split(f.dst),
        protocol: f.protocol,
        ports: hasPorts ? f.ports.replace(/\s+/g, "") : "",
        description: f.description.trim(),
        enabled: f.enabled,
      };
      if (rule) await updateFirewallRule(customerId, rule.id, body);
      else await createFirewallRule(customerId, f.position ? { ...body, position: Number(f.position) } : body);
      onDone();
    });
  };
  return (
    <form className="form card-inset" onSubmit={submit} style={{ marginBottom: 16 }}>
      <h3 className="wide">{rule ? "Edit rule" : "New rule"}</h3>
      <label>
        Action
        <select value={f.action} onChange={set("action")}>
          <option value="deny">Deny</option>
          <option value="allow">Allow</option>
        </select>
      </label>
      <label>
        Site
        <select value={f.site_id} onChange={set("site_id")}>
          <option value="">All sites</option>
          {sites.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        From addresses
        <input value={f.src} onChange={set("src")} placeholder="Any, or 192.168.10.0/24" />
      </label>
      <label>
        To addresses
        <input value={f.dst} onChange={set("dst")} placeholder="Any, or 198.51.100.0/24" />
      </label>
      <label>
        Protocol
        <select value={f.protocol} onChange={set("protocol")}>
          {PROTOCOLS.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        Ports
        <input
          value={hasPorts ? f.ports : ""}
          onChange={set("ports")}
          disabled={!hasPorts}
          pattern={PORTS_PATTERN}
          title={PORTS_HINT}
          placeholder={hasPorts ? "Any, or 443, 8000-8100" : "TCP or UDP only"}
        />
      </label>
      <label className="wide">
        Description
        <input value={f.description} onChange={set("description")} maxLength={200} placeholder="Block file sharing sites" />
      </label>
      {!rule && (
        <label>
          Position
          <input
            type="number"
            min={1}
            max={total + 1}
            value={f.position}
            onChange={set("position")}
            placeholder={`At the end (${total + 1})`}
          />
        </label>
      )}
      <label className="check">
        <input type="checkbox" checked={f.enabled} onChange={(e) => setF({ ...f, enabled: e.target.checked })} /> On
      </label>
      <p className="small muted wide" style={{ margin: 0 }}>
        Leave addresses blank for any. Separate several with commas.
      </p>
      <div className="actions wide">
        <button className="button" disabled={act.busy}>
          Save rule
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

// ---- DDoS protection on the PoP's public address (read-only for customers) ----

/** "50 new connections a second", "1 minute", in plain words. */
export const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString("en-GB")} ${n === 1 ? one : many}`;

/** The protection limits as sentences; shared with the admin Protection tab. */
export function protectionLimits(p: { new_per_source: number; syn_per_s: number; block_minutes: number }): string[] {
  return [
    `A source sending more than ${plural(p.new_per_source, "new connection")} a second is blocked for ${plural(p.block_minutes, "minute")}.`,
    `New TCP connections above ${p.syn_per_s.toLocaleString("en-GB")} a second, from all sources together, are dropped (SYN flood protection).`,
    "Addresses on ExaCarib's block list are always dropped.",
  ];
}

export const DROP_KINDS: { key: "blocked" | "auto" | "flood" | "syn"; label: string }[] = [
  { key: "blocked", label: "On the block list" },
  { key: "auto", label: "From automatically blocked sources" },
  { key: "flood", label: "Flood: over the per-source limit" },
  { key: "syn", label: "SYN flood: over the overall limit" },
];

function Protection({ p, publicAddress }: { p: ProtectionSummary; publicAddress: string | null }) {
  return (
    <Card
      title="Protection"
      note={p.enabled ? <span className="pill ok">On</span> : <span className="pill warn">Off</span>}
    >
      <p className="small muted" style={{ marginTop: 0 }}>
        Protects the PoP's public address <span className="mono">{publicAddress ?? "–"}</span> from floods of new inbound
        connections. Replies to your own outbound traffic and your sites' traffic out are never affected.
      </p>
      {p.enabled ? (
        <ul className="small" style={{ margin: "0 0 12px", paddingLeft: 20 }}>
          {protectionLimits(p).map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      ) : (
        <p className="small">Protection is off, so no inbound limits apply right now.</p>
      )}
      <div className="table-wrap">
        <table className="paths dt compact small" style={{ maxWidth: 520 }}>
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
                <td className="num">{count(p.dropped?.[k.key] ?? 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="small" style={{ margin: "12px 0 0" }}>
        Sources blocked right now: <span className="mono">{count(p.auto_blocked ?? 0)}</span>
        {p.updated_at && <span className="muted"> · Counts from {ago(p.updated_at)}</span>}
      </p>
      <p className="small muted" style={{ margin: "4px 0 0" }}>
        ExaCarib manages these settings. The public address is shared, so they are the same for every customer on it. Contact
        ExaCarib if a service you rely on is being blocked.
      </p>
    </Card>
  );
}

// ---- Port forwards ----

function inactive(site: InternetSite | undefined): string | null {
  if (!site || site.mode === "pop") return null;
  return site.mode === "off" ? "Inactive: site's internet is off" : "Inactive: site goes straight out";
}

function Forwards({ customerId, data, reload }: SectionProps) {
  const [editing, setEditing] = useState<PortForward | "new" | null>(null);
  const act = useAction();
  const siteOf = (id: string) => data.sites.find((s) => s.id === id);
  const toggle = (p: PortForward) =>
    act.run(async () => {
      await updatePortForward(customerId, p.id, { enabled: !p.enabled });
      reload();
    });
  const remove = (p: PortForward) => {
    if (!window.confirm(`Delete the forward on ${p.protocol.toUpperCase()} port ${p.port}? Connections to it are refused within 10 seconds.`))
      return;
    act.run(async () => {
      await deletePortForward(customerId, p.id);
      if (editing !== "new" && editing?.id === p.id) setEditing(null);
      reload();
    });
  };
  const pub = data.public_address ?? "–";
  return (
    <Card
      title="Port forwards"
      note={
        <button className="button small" aria-pressed={editing === "new"} onClick={() => setEditing(editing === "new" ? null : "new")}>
          Add a forward
        </button>
      }
    >
      <p className="small muted" style={{ marginTop: 0 }}>
        Lets the internet reach a service on a site, through the PoP's shared public address{" "}
        <span className="mono">{pub}</span>. Each public port can be used once, and only while the site goes through the PoP.
      </p>
      <p className="small" style={{ margin: "0 0 12px" }}>
        Unsolicited inbound connections blocked: <span className="mono">{count(data.inbound_dropped ?? 0)}</span>
      </p>
      <ErrorNote error={act.error} />
      {editing && (
        <ForwardForm
          key={editing === "new" ? "new" : editing.id}
          customerId={customerId}
          fwd={editing === "new" ? null : editing}
          sites={data.sites}
          onDone={() => {
            setEditing(null);
            reload();
          }}
          onCancel={() => setEditing(null)}
        />
      )}
      {data.forwards.length === 0 ? (
        <div className="empty">
          <p>No port forwards. Nothing on the internet can start a connection to your sites.</p>
          {editing !== "new" && (
            <button className="button small" onClick={() => setEditing("new")}>
              Add a forward
            </button>
          )}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Public address</th>
                <th scope="col">Protocol</th>
                <th scope="col">To</th>
                <th scope="col">Allowed from</th>
                <th scope="col" className="num">Packets</th>
                <th scope="col" className="actions">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {data.forwards.map((p) => {
                const site = siteOf(p.to_site_id);
                const off = inactive(site);
                const name = `${p.protocol.toUpperCase()} port ${p.port}`;
                return (
                  <tr key={p.id} className={p.enabled ? undefined : "row-off"}>
                    <td>
                      <span className="mono">
                        {pub}:{p.port}
                      </span>
                      {p.description && <span className="sub">{p.description}</span>}
                      {!p.enabled && (
                        <div>
                          <span className="pill off">Off</span>
                        </div>
                      )}
                      {off && (
                        <div>
                          <span className="pill warn">{off}</span>
                        </div>
                      )}
                    </td>
                    <td data-label="Protocol" className="mono">
                      {p.protocol.toUpperCase()}
                    </td>
                    <td data-label="To">
                      {p.to_site ?? site?.name ?? "–"}
                      <span className="sub mono">
                        {p.to_address}:{p.to_port ?? p.port}
                      </span>
                    </td>
                    <td data-label="Allowed from" className="mono cell-wrap">
                      {any(p.allow_from, "Anyone")}
                    </td>
                    <td data-label="Packets" className="num">
                      {count(p.packets)}
                      <span className="sub">{bytes(p.bytes)}</span>
                    </td>
                    <td className="actions">
                      <RowActions
                        label={name}
                        disabled={act.busy}
                        primary={
                          <button className="button secondary small" aria-label={`Edit forward on ${name}`} onClick={() => setEditing(p)}>
                            Edit
                          </button>
                        }
                        items={[
                          { label: p.enabled ? "Turn off" : "Turn on", onSelect: () => toggle(p) },
                          { label: "Delete forward", danger: true, onSelect: () => remove(p) },
                        ]}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function ForwardForm({
  customerId,
  fwd,
  sites,
  onDone,
  onCancel,
}: {
  customerId: string;
  fwd: PortForward | null;
  sites: InternetSite[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const [f, setF] = useState({
    description: fwd?.description ?? "",
    protocol: fwd?.protocol ?? "tcp",
    port: fwd ? String(fwd.port) : "",
    to_site_id: fwd?.to_site_id ?? sites.find((s) => s.mode === "pop")?.id ?? sites[0]?.id ?? "",
    to_address: fwd?.to_address ?? "",
    to_port: fwd?.to_port != null ? String(fwd.to_port) : "",
    allow_from: (fwd?.allow_from ?? []).join(", "),
    enabled: fwd?.enabled ?? true,
  });
  const act = useAction();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const site = sites.find((s) => s.id === f.to_site_id);
  const off = inactive(site);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const body: PortForwardIn = {
        description: f.description.trim(),
        protocol: f.protocol,
        port: Number(f.port),
        to_site_id: f.to_site_id,
        to_address: f.to_address.trim(),
        to_port: f.to_port ? Number(f.to_port) : null,
        allow_from: split(f.allow_from),
        enabled: f.enabled,
      };
      if (fwd) await updatePortForward(customerId, fwd.id, body);
      else await createPortForward(customerId, body);
      onDone();
    });
  };
  return (
    <form className="form card-inset" onSubmit={submit} style={{ marginBottom: 16 }}>
      <h3 className="wide">{fwd ? "Edit forward" : "New port forward"}</h3>
      <label>
        Protocol
        <select value={f.protocol} onChange={set("protocol")}>
          <option value="tcp">TCP</option>
          <option value="udp">UDP</option>
        </select>
      </label>
      <label>
        Public port
        <input type="number" min={1} max={65535} required value={f.port} onChange={set("port")} placeholder="8080" />
      </label>
      <label>
        To site
        <select value={f.to_site_id} onChange={set("to_site_id")} required>
          {sites.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        To address
        <input value={f.to_address} onChange={set("to_address")} required inputMode="decimal" placeholder="192.168.10.10" />
      </label>
      <label>
        To port
        <input type="number" min={1} max={65535} value={f.to_port} onChange={set("to_port")} placeholder={f.port ? `${f.port}, the same` : "Same as public"} />
      </label>
      <label>
        Allowed from
        <input value={f.allow_from} onChange={set("allow_from")} placeholder="Anyone, or 203.0.113.0/24" />
      </label>
      <label className="wide">
        Description
        <input value={f.description} onChange={set("description")} maxLength={200} placeholder="CCTV recorder" />
      </label>
      <label className="check">
        <input type="checkbox" checked={f.enabled} onChange={(e) => setF({ ...f, enabled: e.target.checked })} /> On
      </label>
      <p className="small muted wide" style={{ margin: 0 }}>
        The address must be inside the site's LAN. Leave "Allowed from" blank to let anyone connect.
      </p>
      {off && (
        <p className="wide" style={{ margin: 0 }}>
          <span className="pill warn">{off}</span>{" "}
          <span className="small muted">You can save it now; it starts working when the site goes through the PoP.</span>
        </p>
      )}
      <div className="actions wide">
        <button className="button" disabled={act.busy || !f.to_site_id}>
          Save forward
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
