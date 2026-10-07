import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, useApi, type NodeRow } from "../api";
import { useAuth } from "../auth";
import { ErrorNote, StatusPill, ago } from "../components";
import { useCustomer, who } from "../customer";
import { Card, PageHead, RowActions, Tabs, useAction } from "../ui";
import { BillingAdmin } from "./BillingAdmin";
import { Classes } from "./Classes";
import { IntegrationsAdmin } from "./IntegrationsAdmin";
import { PartnersAdmin } from "./PartnersAdmin";
import { ProtectionAdmin } from "./ProtectionAdmin";
import { ReleasesAdmin } from "./ReleasesAdmin";

interface OpenToken {
  id: string;
  site: string;
  customer_id: string;
  created_by: string;
  expires_at: string;
}

interface EnrolToken {
  token: string;
  expires_at: string;
  ca_fingerprint: string;
  agent_url: string;
  /** The agent gateway on the internet; empty while it is not published. */
  public_agent_url: string;
}

// Admin screens (CLAUDE.md §4.6, screen 6): agents, customers, sites and links,
// enrolment tokens, classes and SLA policies, partners, DDoS protection, users, settings, releases and the audit log.
export default function Admin() {
  return (
    <>
      <PageHead eyebrow="Admin" title="Administration">
        Agents, customers and sites, classes, partners, protection, integrations, users, releases and the audit log.
      </PageHead>
      <Tabs label="Admin sections">
        <NavLink to="/admin/agents">Agents</NavLink>
        <NavLink to="/admin/sites">Sites and links</NavLink>
        <NavLink to="/admin/classes">Classes and SLA</NavLink>
        <NavLink to="/admin/partners">Partners</NavLink>
        <NavLink to="/admin/billing">Billing</NavLink>
        <NavLink to="/admin/protection">Protection</NavLink>
        <NavLink to="/admin/integrations">Integrations</NavLink>
        <NavLink to="/admin/users">Users</NavLink>
        <NavLink to="/admin/settings">Settings</NavLink>
        <NavLink to="/admin/releases">Releases</NavLink>
        <NavLink to="/admin/audit">Audit log</NavLink>
      </Tabs>
      <Routes>
        <Route index element={<Navigate to="agents" replace />} />
        <Route path="agents" element={<Agents />} />
        <Route path="sites" element={<SitesAdmin />} />
        <Route path="classes" element={<Classes />} />
        <Route path="partners" element={<PartnersAdmin />} />
        <Route path="billing" element={<BillingAdmin />} />
        <Route path="protection" element={<ProtectionAdmin />} />
        <Route path="integrations" element={<IntegrationsAdmin />} />
        <Route path="users" element={<UsersAdmin />} />
        <Route path="settings" element={<SettingsAdmin />} />
        <Route path="releases" element={<ReleasesAdmin />} />
        <Route path="audit" element={<AuditLog />} />
      </Routes>
    </>
  );
}

/** A value shown once (a token or password), with a copy button. */
function Secret({ label, value, children }: { label: string; value: string; children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const copy = () =>
    navigator.clipboard
      ?.writeText(value)
      .then(() => setCopied(true))
      .catch(() => {});
  return (
    <div className="secret" role="status">
      <div className="small muted">{label}. It is shown once; copy it now.</div>
      <code>{value}</code>{" "}
      <button type="button" className="button secondary small" onClick={copy}>
        {copied ? "Copied" : "Copy"}
      </button>
      {children}
    </div>
  );
}

// ---- Agents ----

function Agents() {
  const { data, error, reload } = useApi<NodeRow[]>("/nodes", 10_000);
  const act = useAction();
  const revoke = (n: NodeRow) => {
    if (!window.confirm(`Revoke ${n.name}? Its certificate stops working at once and it keeps forwarding on its last state. A new enrolment token brings it back.`)) return;
    act.run(async () => {
      await api(`/nodes/${n.id}/revoke`, { method: "POST" });
      reload();
    });
  };
  return (
    <Card title="Agents" note={<span className="muted small">Each agent polls for its desired state every 10 seconds.</span>}>
      <ErrorNote error={error ?? act.error} />
      <div className="table-wrap">
        <table className="paths dt">
          <thead>
            <tr>
              <th scope="col">Node</th>
              <th scope="col">Role</th>
              <th scope="col">Last seen</th>
              <th scope="col">Config</th>
              <th scope="col">Agent</th>
              <th scope="col">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {(data ?? []).map((n) => (
              <tr key={n.id}>
                <td className="mono">{n.name}</td>
                <td>{n.role === "pop" ? "PoP" : "Site"}</td>
                <td className="nowrap">{ago(n.last_seen)}</td>
                <td>
                  {n.apply_ok === false ? (
                    <StatusPill health="bad">v{n.desired_version} failed</StatusPill>
                  ) : n.applied_version === n.desired_version ? (
                    <StatusPill health="ok">v{n.applied_version} applied</StatusPill>
                  ) : (
                    <StatusPill health="warn">
                      v{n.applied_version} of v{n.desired_version ?? "–"}
                    </StatusPill>
                  )}
                  {n.apply_error && <span className="sub">{n.apply_error}</span>}
                </td>
                <td className="mono muted">{n.agent_version || "–"}</td>
                <td className="actions">
                  {n.revoked ? (
                    <StatusPill health="bad">Revoked</StatusPill>
                  ) : (
                    <RowActions
                      label={n.name}
                      disabled={act.busy}
                      items={[{ label: "Revoke certificate", danger: true, onSelect: () => revoke(n) }]}
                    />
                  )}
                </td>
              </tr>
            ))}
            {data?.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">
                  No agents yet. Add a site in Sites and links, issue an enrolment token and install the agent.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// ---- Customers, sites, links and enrolment tokens ----

interface LinkRow {
  id: string;
  path: "carrier-a" | "carrier-b" | "sat";
  carrier: string;
  underlay_type: string;
  underlay_interface: string;
  underlay_ip: string | null;
  underlay_gateway: string | null;
  commit_mbps: string | number;
  cost_per_mbps: string | number;
  burst_price: string | number;
}

interface SiteRow {
  id: string;
  name: string;
  kind: "site" | "pop";
  location: string;
  timezone: string;
  asn: number;
  lan_prefixes: string[];
  overlay_host: number;
  latitude: number | null;
  longitude: number | null;
  cloud_interface: string | null;
  cloud_address: string | null;
  lan_interface: string | null;
  internet_interface: string | null;
  internet_gateway: string | null;
  node: string | null;
  last_seen: string | null;
  links: LinkRow[];
}

function SitesAdmin() {
  const { current, reload: reloadCustomers } = useCustomer();
  const inv = useApi<{ sites: SiteRow[] }>(current ? `/inventory?customer_id=${current.id}` : null, 0);
  const [editing, setEditing] = useState<SiteRow | "new" | null>(null);
  const [linkFor, setLinkFor] = useState<{ site: SiteRow; link: LinkRow | null } | null>(null);
  const [token, setToken] = useState<({ site: string } & EnrolToken) | null>(null);
  const tokens = useApi<OpenToken[]>("/enrolment-tokens", 0);
  const act = useAction();

  const issue = (s: SiteRow) =>
    act.run(async () => {
      const t = await api<EnrolToken>("/enrolment-tokens", {
        method: "POST",
        body: JSON.stringify({ site_id: s.id, ttl_hours: 24 }),
      });
      setToken({ site: s.name, ...t });
      tokens.reload();
    });

  // Deleting asks once; if the link has samples this billing month, it says so and asks again.
  const remove = (what: string, path: string) => {
    if (!window.confirm(`Delete the ${what}? Agents get the change at once. This can't be undone.`)) return;
    act.run(async () => {
      try {
        await api(path, { method: "DELETE" });
      } catch (e) {
        const msg = (e as Error).message;
        if (!/billing month/.test(msg) || !window.confirm(`${msg}\n\nDelete it anyway?`)) throw e;
        await api(`${path}?force=true`, { method: "DELETE" });
      }
      inv.reload();
    });
  };
  const rename = () => {
    if (!current) return;
    const name = window.prompt("New name for this customer", current.name)?.trim();
    if (!name || name === current.name) return;
    act.run(async () => {
      await api(`/customers/${current.id}`, { method: "PATCH", body: JSON.stringify({ name }) });
      reloadCustomers();
    });
  };
  const cancelToken = (t: OpenToken) =>
    act.run(async () => {
      await api(`/enrolment-tokens/${t.id}`, { method: "DELETE" });
      tokens.reload();
    });

  return (
    <>
      <NewCustomer onDone={reloadCustomers} />
      {current && (
        <Card
          title={`Sites for ${current.name}`}
          note={
            <>
              <button className="button secondary small" onClick={rename} disabled={act.busy}>
                Rename
              </button>{" "}
              <button className="button small" onClick={() => setEditing("new")}>
                Add a site
              </button>
            </>
          }
        >
          <ErrorNote error={inv.error ?? act.error} />
          {token && (
            <Secret label={`Enrolment token for ${token.site}, valid until ${new Date(token.expires_at).toLocaleString()}`} value={token.token}>
              {token.public_agent_url ? (
                <>
                  <p className="small muted">
                    On the site's Linux box (Debian or Ubuntu), with the site kit from the latest build, run as root:
                  </p>
                  <pre className="mono small">
                    {`EXA_ENROL_TOKEN=<the token above> bash install.sh \\\n  --controller ${token.public_agent_url} \\\n  --ca-fingerprint ${token.ca_fingerprint} \\\n  --name ${token.site}`}
                  </pre>
                  <p className="small muted" style={{ marginBottom: 0 }}>
                    The site needs outbound HTTPS to port 8443 only; nothing inbound. The agent generates its own
                    WireGuard keys and sends only the public key. Lab nodes use{" "}
                    <span className="mono">{token.agent_url}</span>.
                  </p>
                </>
              ) : (
                <p className="small muted" style={{ marginBottom: 0 }}>
                  Install with the controller URL <span className="mono">{token.agent_url}</span> and CA fingerprint{" "}
                  <span className="mono">{token.ca_fingerprint}</span>. The agent generates its own WireGuard keys and
                  sends only the public key.
                </p>
              )}
            </Secret>
          )}
          {editing && (
            <SiteForm
              customerId={current.id}
              site={editing === "new" ? null : editing}
              onDone={() => {
                setEditing(null);
                inv.reload();
              }}
            />
          )}
          {linkFor && (
            <LinkForm
              site={linkFor.site}
              link={linkFor.link}
              onDone={() => {
                setLinkFor(null);
                inv.reload();
              }}
            />
          )}
          <div className="table-wrap">
            <table className="paths dt stack">
              <thead>
                <tr>
                  <th scope="col">Site</th>
                  <th scope="col">Agent</th>
                  <th scope="col">Links</th>
                  <th scope="col" className="actions">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {(inv.data?.sites ?? []).map((s) => (
                  <tr key={s.id}>
                    <td>
                      <strong className="mono">{s.name}</strong> {s.kind === "pop" && <span className="tag">PoP</span>}
                      <span className="sub">
                        {s.location || "No location"} · AS{s.asn} · {s.lan_prefixes.join(", ") || "no LAN prefixes"}
                      </span>
                    </td>
                    <td data-label="Agent">
                      {s.node ? (
                        <>
                          <span className="mono">{s.node}</span>
                          <span className="sub">Seen {ago(s.last_seen)}</span>
                        </>
                      ) : (
                        <span className="pill off">Not enrolled</span>
                      )}
                    </td>
                    <td data-label="Links">
                      {s.links.map((l) => (
                        <div key={l.id} className="small">
                          <button className="link" onClick={() => setLinkFor({ site: s, link: l })}>
                            {l.path}
                          </button>{" "}
                          {l.carrier}, {l.underlay_type}, <span className="mono">{l.underlay_interface}</span>, commit{" "}
                          <span className="mono">{Number(l.commit_mbps)}</span> Mbps
                        </div>
                      ))}
                      {s.links.length === 0 && <span className="muted small">No links</span>}
                    </td>
                    <td className="actions">
                      <RowActions
                        label={s.name}
                        primary={
                          <button className="button secondary small" aria-label={`Edit ${s.name}`} onClick={() => setEditing(s)}>
                            Edit
                          </button>
                        }
                        items={[
                          { label: "Add a link", onSelect: () => setLinkFor({ site: s, link: null }) },
                          { label: "Issue enrolment token", disabled: act.busy, onSelect: () => issue(s) },
                          ...s.links.map((l) => ({
                            label: `Remove link ${l.path}`,
                            danger: true,
                            disabled: act.busy,
                            onSelect: () => remove(`link ${l.path} at ${s.name}`, `/sites/${s.id}/links/${l.id}`),
                          })),
                          {
                            label: "Delete site",
                            danger: true,
                            disabled: act.busy,
                            onSelect: () => remove(`site ${s.name}, its links and its agent`, `/sites/${s.id}`),
                          },
                        ]}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(tokens.data ?? []).filter((t) => t.customer_id === current.id).length > 0 && (
            <>
              <h3 style={{ margin: "20px 0 8px" }}>Open enrolment tokens</h3>
              <ul className="lines small">
                {(tokens.data ?? [])
                  .filter((t) => t.customer_id === current.id)
                  .map((t) => (
                    <li key={t.id}>
                      <span className="mono">{t.site}</span>, issued by {t.created_by}, valid until{" "}
                      {new Date(t.expires_at).toLocaleString("en-GB")}{" "}
                      <button className="link" disabled={act.busy} onClick={() => cancelToken(t)}>
                        Cancel
                      </button>
                    </li>
                  ))}
              </ul>
            </>
          )}
        </Card>
      )}
    </>
  );
}

function NewCustomer({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState("");
  const act = useAction();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api("/customers", { method: "POST", body: JSON.stringify({ name }) });
      setName("");
      onDone();
    });
  };
  return (
    <Card title="Customers" note={<span className="muted small">With more than one customer, choose which to work on in the top bar (in the menu on phones).</span>}>
      <form className="form" onSubmit={submit}>
        <label>
          New customer organisation
          <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} />
        </label>
        <div className="actions">
          <button className="button" disabled={act.busy}>
            Add customer
          </button>
        </div>
      </form>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function SiteForm({ customerId, site, onDone }: { customerId: string; site: SiteRow | null; onDone: () => void }) {
  const [f, setF] = useState({
    name: site?.name ?? "",
    kind: site?.kind ?? "site",
    location: site?.location ?? "",
    timezone: site?.timezone ?? "America/Port_of_Spain",
    asn: String(site?.asn ?? ""),
    lan_prefixes: site?.lan_prefixes.join(", ") ?? "",
    overlay_host: String(site?.overlay_host ?? ""),
    latitude: site?.latitude == null ? "" : String(site.latitude),
    longitude: site?.longitude == null ? "" : String(site.longitude),
    lan_interface: site?.lan_interface ?? "",
    cloud_interface: site?.cloud_interface ?? "",
    cloud_address: site?.cloud_address ?? "",
    internet_interface: site?.internet_interface ?? "",
    internet_gateway: site?.internet_gateway ?? "",
  });
  const act = useAction();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api("/sites", {
        method: "POST",
        body: JSON.stringify({
          customer_id: customerId,
          name: f.name,
          kind: f.kind,
          location: f.location,
          timezone: f.timezone,
          asn: Number(f.asn),
          lan_prefixes: f.lan_prefixes
            .split(",")
            .map((p) => p.trim())
            .filter(Boolean),
          overlay_host: Number(f.overlay_host),
          latitude: f.latitude.trim() === "" ? null : Number(f.latitude),
          longitude: f.longitude.trim() === "" ? null : Number(f.longitude),
          // Sent every time: a field left out would be cleared.
          lan_interface: f.lan_interface.trim() || null,
          cloud_interface: f.cloud_interface.trim() || null,
          cloud_address: f.cloud_address.trim() || null,
          internet_interface: f.internet_interface.trim() || null,
          internet_gateway: f.internet_gateway.trim() || null,
        }),
      });
      onDone();
    });
  };
  return (
    <form className="form card-inset" onSubmit={submit} style={{ marginBottom: 16 }}>
      <h3 className="wide">{site ? `Edit ${site.name}` : "New site"}</h3>
      <label>
        Name
        <input value={f.name} onChange={set("name")} required pattern="[a-z0-9][a-z0-9-]{0,30}" readOnly={!!site} title="Lower case letters, digits and dashes" />
      </label>
      <label>
        Kind
        <select value={f.kind} onChange={set("kind")}>
          <option value="site">Customer site</option>
          <option value="pop">PoP</option>
        </select>
      </label>
      <label>
        Location
        <input value={f.location} onChange={set("location")} maxLength={120} />
      </label>
      <label>
        Time zone
        <input value={f.timezone} onChange={set("timezone")} />
      </label>
      <label>
        BGP ASN
        <input value={f.asn} onChange={set("asn")} required inputMode="numeric" pattern="[0-9]+" />
      </label>
      <label>
        Overlay host number (1 to 254)
        <input value={f.overlay_host} onChange={set("overlay_host")} required inputMode="numeric" pattern="[0-9]+" />
      </label>
      <label>
        Latitude (north +)
        <input value={f.latitude} onChange={set("latitude")} inputMode="decimal" placeholder="17.97" />
      </label>
      <label>
        Longitude (east +)
        <input value={f.longitude} onChange={set("longitude")} inputMode="decimal" placeholder="-76.79" />
      </label>
      <p className="small muted wide" style={{ margin: 0 }}>
        Coordinates let the hurricane watch warn this site when a storm is forecast to pass close.
      </p>
      <label className="wide">
        LAN prefixes, comma separated
        <input value={f.lan_prefixes} onChange={set("lan_prefixes")} placeholder="192.168.10.0/24" />
      </label>
      {f.kind === "site" ? (
        <label>
          LAN interface (for layer 2 circuits)
          <input value={f.lan_interface} onChange={set("lan_interface")} placeholder="eth4" pattern="[a-zA-Z0-9._-]{1,15}" />
        </label>
      ) : (
        <>
          <label>
            Cloud interface
            <input value={f.cloud_interface} onChange={set("cloud_interface")} placeholder="eth5" pattern="[a-zA-Z0-9._-]{1,15}" />
          </label>
          <label>
            Public address for clouds and port forwards
            <input value={f.cloud_address} onChange={set("cloud_address")} placeholder="100.64.0.2/24" />
          </label>
          <label>
            Internet interface
            <input value={f.internet_interface} onChange={set("internet_interface")} placeholder="eth5" pattern="[a-zA-Z0-9._-]{1,15}" />
          </label>
          <label>
            Internet next hop
            <input value={f.internet_gateway} onChange={set("internet_gateway")} placeholder="100.64.0.1" />
          </label>
        </>
      )}
      <div className="actions wide">
        <button className="button" disabled={act.busy}>
          Save site
        </button>
        <button type="button" className="button secondary" onClick={onDone}>
          Cancel
        </button>
        <ErrorNote error={act.error} />
      </div>
    </form>
  );
}

function LinkForm({ site, link, onDone }: { site: SiteRow; link: LinkRow | null; onDone: () => void }) {
  const [f, setF] = useState({
    path: link?.path ?? "carrier-a",
    carrier: link?.carrier ?? "",
    underlay_type: link?.underlay_type ?? "fibre",
    underlay_interface: link?.underlay_interface ?? "",
    underlay_ip: link?.underlay_ip ?? "",
    underlay_gateway: link?.underlay_gateway ?? "",
    commit_mbps: String(link ? Number(link.commit_mbps) : ""),
    cost_per_mbps: String(link ? Number(link.cost_per_mbps) : ""),
    burst_price: String(link ? Number(link.burst_price) : ""),
  });
  const act = useAction();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api(`/sites/${site.id}/links`, {
        method: "POST",
        body: JSON.stringify({
          ...f,
          underlay_ip: f.underlay_ip || null,
          underlay_gateway: f.underlay_gateway || null,
          commit_mbps: Number(f.commit_mbps || 0),
          cost_per_mbps: Number(f.cost_per_mbps || 0),
          burst_price: Number(f.burst_price || 0),
        }),
      });
      onDone();
    });
  };
  return (
    <form className="form card-inset" onSubmit={submit} style={{ marginBottom: 16 }}>
      <h3 className="wide">
        {link ? "Edit" : "New"} link at {site.name}
      </h3>
      <label>
        Path
        <select value={f.path} onChange={set("path")} disabled={!!link}>
          <option value="carrier-a">carrier-a</option>
          <option value="carrier-b">carrier-b</option>
          <option value="sat">sat (satellite)</option>
        </select>
      </label>
      <label>
        Carrier
        <input value={f.carrier} onChange={set("carrier")} required maxLength={120} />
      </label>
      <label>
        Underlay
        <select value={f.underlay_type} onChange={set("underlay_type")}>
          {["fibre", "broadband", "lte", "leo", "geo"].map((t) => (
            <option key={t} value={t}>
              {t === "leo" ? "LEO satellite" : t === "geo" ? "GEO satellite" : t === "lte" ? "LTE" : t}
            </option>
          ))}
        </select>
      </label>
      <label>
        Interface
        <input value={f.underlay_interface} onChange={set("underlay_interface")} required pattern="[a-zA-Z0-9._-]{1,15}" />
      </label>
      <label>
        Underlay address (optional)
        <input value={f.underlay_ip} onChange={set("underlay_ip")} placeholder="10.11.1.2/24" />
      </label>
      <label>
        Carrier next hop (for internet straight out)
        <input value={f.underlay_gateway} onChange={set("underlay_gateway")} placeholder="10.11.1.1" />
      </label>
      <label>
        Commit, Mbps
        <input value={f.commit_mbps} onChange={set("commit_mbps")} inputMode="decimal" />
      </label>
      <label>
        Cost per Mbps
        <input value={f.cost_per_mbps} onChange={set("cost_per_mbps")} inputMode="decimal" />
      </label>
      <label>
        Burst price per Mbps
        <input value={f.burst_price} onChange={set("burst_price")} inputMode="decimal" />
      </label>
      <div className="actions wide">
        <button className="button" disabled={act.busy}>
          Save link
        </button>
        <button type="button" className="button secondary" onClick={onDone}>
          Cancel
        </button>
        <ErrorNote error={act.error} />
      </div>
    </form>
  );
}

// ---- Users ----

const ROLE_WORD: Record<string, string> = { admin: "Admin", customer: "Customer", carrier: "Carrier (read-only)" };

interface UserRow {
  id: string;
  email: string;
  role: "admin" | "customer" | "carrier";
  customer: string | null;
  carrier: string | null;
  created_at: string;
  last_login: string | null;
}

function UsersAdmin() {
  const { user } = useAuth();
  const { customers } = useCustomer();
  const users = useApi<UserRow[]>("/users", 0);
  const carriers = useApi<{ id: string; name: string }[]>("/carriers", 0);
  const [f, setF] = useState({ email: "", role: "customer", customer_id: "", carrier_id: "" });
  const [shown, setShown] = useState<{ email: string; password: string } | null>(null);
  const act = useAction();

  useEffect(() => {
    if (!f.customer_id && customers[0]) setF((x) => ({ ...x, customer_id: customers[0].id }));
    if (!f.carrier_id && carriers.data?.[0]) setF((x) => ({ ...x, carrier_id: carriers.data![0].id }));
  }, [customers, carriers.data, f.customer_id, f.carrier_id]);

  const create = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const out = await api<{ email: string; password: string }>("/users", { method: "POST", body: JSON.stringify(f) });
      setShown(out);
      setF({ ...f, email: "" });
      users.reload();
    });
  };
  const reset = (u: UserRow) => {
    if (!window.confirm(`Reset the password for ${u.email}? Their sessions are signed out.`)) return;
    act.run(async () => {
      setShown(await api<{ email: string; password: string }>(`/users/${u.id}/reset-password`, { method: "POST" }));
    });
  };
  const remove = (u: UserRow) => {
    if (!window.confirm(`Delete the account ${u.email}?`)) return;
    act.run(async () => {
      await api(`/users/${u.id}`, { method: "DELETE" });
      users.reload();
    });
  };

  return (
    <Card title="Users">
      <p className="muted small" style={{ marginTop: 0 }}>
        Admins see everything. Customer users see their own organisation. Carrier users see only their own links, read-only.
      </p>
      <form className="form" onSubmit={create}>
        <label>
          Email
          <input type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} required />
        </label>
        <label>
          Role
          <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>
            <option value="customer">Customer</option>
            <option value="carrier">Carrier (read only)</option>
            <option value="admin">Admin</option>
          </select>
        </label>
        {f.role === "customer" && (
          <label>
            Customer
            <select value={f.customer_id} onChange={(e) => setF({ ...f, customer_id: e.target.value })}>
              {customers.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {f.role === "carrier" && (
          <label>
            Carrier
            <select value={f.carrier_id} onChange={(e) => setF({ ...f, carrier_id: e.target.value })}>
              {(carriers.data ?? []).map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <div className="actions">
          <button className="button" disabled={act.busy}>
            Add user
          </button>
        </div>
      </form>
      <ErrorNote error={users.error ?? act.error} />
      {shown && <Secret label={`One-time password for ${shown.email}. Ask them to change it after signing in`} value={shown.password} />}
      <div className="table-wrap" style={{ marginTop: 24 }}>
        <table className="paths dt stack">
          <thead>
            <tr>
              <th scope="col">Email</th>
              <th scope="col">Role</th>
              <th scope="col">For</th>
              <th scope="col">Last sign-in</th>
              <th scope="col" className="actions">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {(users.data ?? []).map((u) => (
              <tr key={u.id}>
                <td className="cell-wrap">
                  {u.email}
                  {u.email === user?.email && <span className="sub">You</span>}
                </td>
                <td data-label="Role">{ROLE_WORD[u.role] ?? u.role}</td>
                <td data-label="For">{u.customer ?? u.carrier ?? "All"}</td>
                <td data-label="Last sign-in" className="nowrap">
                  {u.last_login ? ago(u.last_login) : "Never"}
                </td>
                <td className="actions">
                  <RowActions
                    label={u.email}
                    disabled={act.busy}
                    items={[
                      { label: "Reset password", onSelect: () => reset(u) },
                      ...(u.email !== user?.email ? [{ label: "Delete user", danger: true, onSelect: () => remove(u) }] : []),
                    ]}
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

// ---- Customer settings ----

function SettingsAdmin() {
  const { current, reload } = useCustomer();
  const act = useAction();
  if (!current) return null;
  const patch = (body: object, question: string) => {
    if (!window.confirm(question)) return;
    act.run(async () => {
      await api(`/customers/${current.id}/settings`, { method: "PATCH", body: JSON.stringify(body) });
      reload();
    });
  };
  return (
    <Card title={`Settings for ${current.name}`}>
      <ErrorNote error={act.error} />
      <div className="form">
        <div className="wide">
          <h3>Shadow mode {current.shadow_mode ? <StatusPill health="warn">On</StatusPill> : <StatusPill health="ok">Off</StatusPill>}</h3>
          <p className="muted small">
            In shadow mode the routing engine logs every decision it would make, marked “Shadow”, but traffic stays on the
            default paths. BFD failover on the agents still works. Use it to build trust before letting the engine act.
          </p>
          <button
            className="button secondary"
            disabled={act.busy}
            onClick={() =>
              patch(
                { shadow_mode: !current.shadow_mode },
                current.shadow_mode
                  ? "Switch shadow mode off? The engine starts moving traffic."
                  : "Switch shadow mode on? Every class returns to its default path and the engine only logs.",
              )
            }
          >
            {current.shadow_mode ? "Switch shadow mode off" : "Switch shadow mode on"}
          </button>
        </div>
        <div className="wide">
          <h3>Bulk traffic on satellite in Storm Mode</h3>
          <p className="muted small">
            By default bulk traffic pauses rather than use satellite when both terrestrial paths fail. Currently:{" "}
            <strong>{current.storm_allow_bulk_sat ? "allowed" : "paused"}</strong>.
          </p>
          <button
            className="button secondary"
            disabled={act.busy}
            onClick={() =>
              patch(
                { storm_allow_bulk_sat: !current.storm_allow_bulk_sat },
                current.storm_allow_bulk_sat ? "Keep bulk off satellite?" : "Allow bulk traffic on satellite in Storm Mode? Satellite capacity costs more.",
              )
            }
          >
            {current.storm_allow_bulk_sat ? "Keep bulk off satellite" : "Allow bulk on satellite"}
          </button>
        </div>
        <div className="wide">
          <h3>Storm Mode</h3>
          <p className="muted small">Storm Mode is set per site, from the switch at the top of the page or on each site's page.</p>
          <ul className="small">
            {current.sites.map((s) => (
              <li key={s.id}>
                {s.name}:{" "}
                {s.storm_mode
                  ? `on since ${new Date(s.storm_since ?? "").toLocaleString()}, switched on by ${who(s.storm_by)}`
                  : "off"}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </Card>
  );
}

// ---- Audit log ----

interface AuditRow {
  at: string;
  actor: string;
  action: string;
  target: string | null;
  detail: Record<string, unknown> | null;
}

function AuditLog() {
  const { data, error } = useApi<AuditRow[]>("/audit?limit=300", 30_000);
  return (
    <Card title="Audit log" note={<span className="muted small">Every write, sign-in and enrolment, newest first.</span>}>
      <ErrorNote error={error} />
      <div className="table-wrap">
        <table className="paths dt compact small">
          <thead>
            <tr>
              <th scope="col">When</th>
              <th scope="col">Who</th>
              <th scope="col">Action</th>
              <th scope="col">Target</th>
              <th scope="col">Detail</th>
            </tr>
          </thead>
          <tbody>
            {(data ?? []).map((a, i) => (
              <tr key={i}>
                <td className="mono nowrap">{new Date(a.at).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" })}</td>
                <td>{who(a.actor)}</td>
                <td className="mono">{a.action}</td>
                <td className="cell-wrap">{a.target}</td>
                <td className="mono muted cell-wrap">{a.detail ? JSON.stringify(a.detail).slice(0, 160) : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
