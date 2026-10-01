import { useState, type FormEvent } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, useApi, type CustomerSettings } from "../api";
import { ErrorNote, Eyebrow, StatusPill, ago } from "../components";
import { useCustomer, who } from "../customer";
import { Card, useAction } from "../ui";
import { Classes, PRIORITY_LABEL, type Priority } from "./Classes";

// Traffic: what the network has seen leaving each site, the customer's own
// rules for what goes in which class, and the classes themselves (ADR 0007).
export default function Traffic() {
  return (
    <>
      <div className="page-head">
        <Eyebrow>Traffic</Eyebrow>
        <h1>Applications and priorities</h1>
        <p className="muted">
          Decide which traffic matters most. Rules put applications, addresses, VLANs and websites into classes; each class
          has a queue priority, an SLA and a preferred path. The network also watches what leaves each site and suggests
          rules for applications it recognises. It looks at addresses, ports and packet rates only, never at content.
        </p>
      </div>
      <nav className="tabs" aria-label="Traffic sections">
        <NavLink to="/traffic/applications">Seen on your network</NavLink>
        <NavLink to="/traffic/rules">Your rules</NavLink>
        <NavLink to="/traffic/classes">Classes</NavLink>
      </nav>
      <Routes>
        <Route index element={<Navigate to="applications" replace />} />
        <Route path="applications" element={<Detections />} />
        <Route path="rules" element={<Rules />} />
        <Route path="classes" element={<Classes />} />
      </Routes>
    </>
  );
}

// ---- Detected applications ----

interface Detection {
  id: number;
  site: string;
  key: string;
  app_id: string | null;
  label: string;
  proto: string;
  dport: number;
  current_class: string;
  suggested_class: string;
  profile: Priority;
  confidence: number;
  reason: string;
  status: "suggested" | "applied" | "dismissed";
  first_seen: string;
  last_seen: string;
}

function Detections() {
  const { current, reload: reloadCustomer } = useCustomer();
  const [closed, setClosed] = useState(false);
  const q = closed ? "?include_closed=true" : "";
  const list = useApi<Detection[]>(current ? `/customers/${current.id}/applications${q}` : null, 30_000);
  const act = useAction();
  if (!current) return null;
  const post = (path: string, body?: unknown) =>
    act.run(async () => {
      await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
      list.reload();
    });
  const auto = (on: boolean) =>
    act.run(async () => {
      await api<CustomerSettings>(`/customers/${current.id}/settings`, {
        method: "PATCH",
        body: JSON.stringify({ auto_prioritise: on }),
      });
      reloadCustomer();
    });
  const items = list.data ?? [];
  return (
    <Card
      title="Seen on your network"
      note={
        <div className="form-actions">
          <label className="small">
            <input type="checkbox" checked={closed} onChange={(e) => setClosed(e.target.checked)} /> Include applied and
            dismissed
          </label>
          <button className="button secondary small" disabled={act.busy} onClick={() => post(`/customers/${current.id}/applications/detect`)}>
            Check now
          </button>
        </div>
      }
    >
      <div className="card-inset" style={{ marginBottom: 16 }}>
        <label className="check">
          <input type="checkbox" checked={!!current.auto_prioritise} disabled={act.busy} onChange={(e) => auto(e.target.checked)} />{" "}
          <strong>Prioritise known applications automatically.</strong>
        </label>
        <p className="small muted" style={{ margin: "4px 0 0" }}>
          Applications the network recognises with confidence, such as Teams, Zoom or Citrix, get a rule for their site
          without asking. Anything it is less sure of still waits for you here. Every rule it creates shows in Your rules
          and the audit log.
        </p>
      </div>
      <ErrorNote error={list.error ?? act.error} />
      {items.length === 0 ? (
        <p className="muted">
          Nothing to suggest. Sites report what they send every minute, and the network checks every five minutes.
        </p>
      ) : (
        <ul className="insights">
          {items.map((d) => (
            <li key={d.id} className={d.status === "suggested" ? "warning" : "info"}>
              <div className="insight-head">
                <StatusPill health={d.status === "applied" ? "ok" : d.confidence >= 0.8 ? "warn" : "ok"}>
                  {d.status === "applied" ? "Applied" : d.status === "dismissed" ? "Dismissed" : d.confidence >= 0.8 ? "Recognised" : "Likely"}
                </StatusPill>
                <span className="eyebrow" style={{ margin: 0 }}>
                  {d.site}
                </span>
                <span className="muted small">
                  first seen {ago(d.first_seen)}, last {ago(d.last_seen)}
                </span>
              </div>
              <strong>
                {d.label}: {d.current_class || "unclassified"} → {d.suggested_class}
              </strong>
              <p>{d.reason}</p>
              {d.status === "suggested" && (
                <div className="form-actions">
                  <button className="button small" disabled={act.busy} onClick={() => post(`/applications/${d.id}/apply`)}>
                    Put it in {d.suggested_class}
                  </button>
                  <button className="button secondary small" disabled={act.busy} onClick={() => post(`/applications/${d.id}/dismiss`)}>
                    Dismiss
                  </button>
                  <span className="small muted">Confidence {Math.round(d.confidence * 100)}%</span>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// ---- Traffic rules ----

interface Rule {
  id: number;
  name: string;
  class_name: string;
  site_ids: string[];
  apps: string[];
  ports: string;
  dst_subnets: string[];
  src_subnets: string[];
  vlans: number[];
  domains: string[];
  dscp: number[];
  enabled: boolean;
  ordinal: number;
  source: "customer" | "admin" | "detected";
  created_by: string;
  updated_at: string;
}

interface CatalogueApp {
  id: string;
  name: string;
  category: string;
  priority: Priority;
}

interface ClassLite {
  name: string;
  priority: Priority;
}

function Rules() {
  const { current } = useCustomer();
  const list = useApi<Rule[]>(current ? `/customers/${current.id}/rules` : null, 0);
  const catalogue = useApi<CatalogueApp[]>("/applications/catalogue", 0);
  const classes = useApi<ClassLite[]>(current ? `/classes?customer_id=${current.id}` : null, 0);
  const [editing, setEditing] = useState<Rule | "new" | null>(null);
  const act = useAction();
  if (!current) return null;
  const appName = (id: string) => catalogue.data?.find((a) => a.id === id)?.name ?? id;
  const siteName = (id: string) => current.sites.find((s) => s.id === id)?.name ?? "another site";
  const remove = (r: Rule) => {
    if (!window.confirm(`Delete the rule "${r.name}"? Its traffic goes back to the class defaults.`)) return;
    act.run(async () => {
      await api(`/customers/${current.id}/rules/${r.id}`, { method: "DELETE" });
      list.reload();
    });
  };
  const toggle = (r: Rule) =>
    act.run(async () => {
      await api(`/customers/${current.id}/rules/${r.id}`, { method: "PUT", body: JSON.stringify({ ...r, enabled: !r.enabled }) });
      list.reload();
    });
  return (
    <Card
      title="Your rules"
      note={
        <button className="button small" onClick={() => setEditing("new")}>
          Add a rule
        </button>
      }
    >
      <p className="muted small">
        Rules are checked in order, before the classes' own DSCP and port matches. Within a rule, everything you fill in must
        match; a website or a destination address both count as the destination. Sites get changes within 10 seconds.
      </p>
      <ErrorNote error={list.error ?? act.error} />
      {editing && (
        <RuleForm
          customerId={current.id}
          rule={editing === "new" ? null : editing}
          catalogue={catalogue.data ?? []}
          classes={classes.data ?? []}
          sites={current.sites}
          onDone={() => {
            setEditing(null);
            list.reload();
          }}
        />
      )}
      {(list.data ?? []).length === 0 ? (
        <p className="muted">No rules yet. Add one, or apply a suggestion from Seen on your network.</p>
      ) : (
        <div className="table-wrap">
          <table className="paths">
            <thead>
              <tr>
                <th scope="col">Rule</th>
                <th scope="col">Traffic</th>
                <th scope="col">Class</th>
                <th scope="col">Sites</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(list.data ?? []).map((r) => (
                <tr key={r.id} className={r.enabled ? undefined : "muted"}>
                  <td>
                    <strong>{r.name}</strong>
                    <div className="small muted">
                      {r.enabled ? "On" : "Off"} · {r.source === "detected" ? "from a suggestion" : "added"} by {who(r.created_by)}
                    </div>
                  </td>
                  <td className="small">
                    {r.apps.length > 0 && <div>{r.apps.map(appName).join(", ")}</div>}
                    {r.domains.length > 0 && <div className="mono">{r.domains.join(", ")}</div>}
                    {r.dst_subnets.length > 0 && <div className="mono">to {r.dst_subnets.join(", ")}</div>}
                    {r.src_subnets.length > 0 && <div className="mono">from {r.src_subnets.join(", ")}</div>}
                    {r.ports && <div className="mono">{r.ports}</div>}
                    {r.vlans.length > 0 && <div>VLAN {r.vlans.join(", ")}</div>}
                    {r.dscp.length > 0 && <div>DSCP {r.dscp.join(", ")}</div>}
                  </td>
                  <td className="mono">{r.class_name}</td>
                  <td className="small">{r.site_ids.length ? r.site_ids.map(siteName).join(", ") : "All sites"}</td>
                  <td>
                    <button className="button secondary small" onClick={() => setEditing(r)}>
                      Edit
                    </button>{" "}
                    <button className="button secondary small" disabled={act.busy} onClick={() => toggle(r)}>
                      {r.enabled ? "Turn off" : "Turn on"}
                    </button>{" "}
                    <button className="button secondary small" disabled={act.busy} onClick={() => remove(r)}>
                      Delete
                    </button>
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

const split = (s: string) =>
  s
    .split(/[\s,]+/)
    .map((x) => x.trim())
    .filter(Boolean);

function RuleForm({
  customerId,
  rule,
  catalogue,
  classes,
  sites,
  onDone,
}: {
  customerId: string;
  rule: Rule | null;
  catalogue: CatalogueApp[];
  classes: ClassLite[];
  sites: { id: string; name: string }[];
  onDone: () => void;
}) {
  const [f, setF] = useState({
    name: rule?.name ?? "",
    class_name: rule?.class_name ?? classes[0]?.name ?? "voice",
    apps: rule?.apps ?? [],
    domains: rule?.domains.join(", ") ?? "",
    dst: rule?.dst_subnets.join(", ") ?? "",
    src: rule?.src_subnets.join(", ") ?? "",
    ports: rule?.ports ?? "",
    vlans: rule?.vlans.join(", ") ?? "",
    dscp: rule?.dscp.join(", ") ?? "",
    site_ids: rule?.site_ids ?? [],
    ordinal: String(rule?.ordinal ?? 100),
  });
  const act = useAction();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const flip = (k: "apps" | "site_ids", v: string) =>
    setF({ ...f, [k]: f[k].includes(v) ? f[k].filter((x) => x !== v) : [...f[k], v] });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const body = {
        name: f.name,
        class_name: f.class_name,
        apps: f.apps,
        domains: split(f.domains),
        dst_subnets: split(f.dst),
        src_subnets: split(f.src),
        ports: f.ports.trim(),
        vlans: split(f.vlans).map(Number),
        dscp: split(f.dscp).map(Number),
        site_ids: f.site_ids,
        ordinal: Number(f.ordinal),
        enabled: rule?.enabled ?? true,
      };
      await api(rule ? `/customers/${customerId}/rules/${rule.id}` : `/customers/${customerId}/rules`, {
        method: rule ? "PUT" : "POST",
        body: JSON.stringify(body),
      });
      onDone();
    });
  };
  const categories = [...new Set(catalogue.map((a) => a.category))];
  const branchSites = sites;
  return (
    <form className="form card-inset" onSubmit={submit} style={{ marginBottom: 16 }}>
      <h3 className="wide">{rule ? `Edit ${rule.name}` : "New rule"}</h3>
      <label>
        Name
        <input value={f.name} onChange={set("name")} required maxLength={80} placeholder="Head office ERP" />
      </label>
      <label>
        Put it in class
        <select value={f.class_name} onChange={set("class_name")}>
          {classes.map((c) => (
            <option key={c.name} value={c.name}>
              {c.name} ({PRIORITY_LABEL[c.priority].split(",")[0].toLowerCase()})
            </option>
          ))}
        </select>
      </label>
      <label>
        Order
        <input value={f.ordinal} onChange={set("ordinal")} inputMode="numeric" pattern="[0-9]+" title="Lower is checked first" />
      </label>
      <fieldset className="wide">
        <legend>Applications</legend>
        {categories.map((cat) => (
          <div key={cat} className="small" style={{ marginBottom: 6 }}>
            <span className="muted">{cat}: </span>
            {catalogue
              .filter((a) => a.category === cat)
              .map((a) => (
                <label key={a.id} className="check" style={{ marginRight: 12 }}>
                  <input type="checkbox" checked={f.apps.includes(a.id)} onChange={() => flip("apps", a.id)} /> {a.name}
                </label>
              ))}
          </div>
        ))}
      </fieldset>
      <label className="wide">
        Websites
        <input value={f.domains} onChange={set("domains")} placeholder="erp.example.com, portal.bank.example" />
      </label>
      <label>
        To addresses
        <input value={f.dst} onChange={set("dst")} placeholder="10.50.0.0/16" />
      </label>
      <label>
        From addresses
        <input value={f.src} onChange={set("src")} placeholder="192.168.10.0/24" />
      </label>
      <label>
        Ports
        <input value={f.ports} onChange={set("ports")} placeholder="tcp:443, udp:5060" />
      </label>
      <label>
        VLANs
        <input value={f.vlans} onChange={set("vlans")} placeholder="20, 30" />
      </label>
      <label>
        DSCP already set
        <input value={f.dscp} onChange={set("dscp")} placeholder="46" />
      </label>
      <fieldset className="wide">
        <legend>Sites</legend>
        <span className="small muted">None ticked means every site. </span>
        {branchSites.map((s) => (
          <label key={s.id} className="check small" style={{ marginRight: 12 }}>
            <input type="checkbox" checked={f.site_ids.includes(s.id)} onChange={() => flip("site_ids", s.id)} /> {s.name}
          </label>
        ))}
      </fieldset>
      <div className="actions wide">
        <button className="button" disabled={act.busy}>
          Save rule
        </button>
        <button type="button" className="button secondary" onClick={onDone}>
          Cancel
        </button>
        <ErrorNote error={act.error} />
      </div>
    </form>
  );
}
