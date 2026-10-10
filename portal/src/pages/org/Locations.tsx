import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, useApi } from "../../api";
import { ErrorNote, StatusPill, ago } from "../../components";
import { Card, PageHead, useAction } from "../../ui";
import { NoOrganisation } from "./Setup";
import { useCanManage, useOrgId, ZONES, type Install, type OrgLocation } from "./common";
import "./org.css";

interface Locations {
  locations: OrgLocation[];
  products: string[];
  hub_ready: boolean;
}

type Draft = Pick<
  OrgLocation,
  "name" | "address_line1" | "address_line2" | "city" | "island" | "country" | "postcode" | "timezone"
>;

const EMPTY: Draft = {
  name: "",
  address_line1: "",
  address_line2: "",
  city: "",
  island: "",
  country: "",
  postcode: "",
  timezone: "America/Port_of_Spain",
};

const EMERGENCY: Record<string, [string, "ok" | "warn" | "bad"]> = {
  registered: ["Emergency address registered", "ok"],
  pending: ["Emergency address being checked", "warn"],
  not_registered: ["Emergency address not registered yet", "warn"],
  rejected: ["Emergency address rejected: check it", "bad"],
};

/** Locations (ADR 0043): each branch or office entered once, used by Connect, phones and opening hours. */
export default function LocationsPage() {
  const cid = useOrgId();
  const manage = useCanManage();
  const data = useApi<Locations>(cid ? `/orgs/${cid}/locations` : null, 15_000);
  const [editing, setEditing] = useState<OrgLocation | "new" | null>(null);
  const [connecting, setConnecting] = useState<OrgLocation | null>(null);
  const [install, setInstall] = useState<(Install & { place?: string }) | null>(null);
  const act = useAction();
  if (!cid) return <NoOrganisation />;
  const d = data.data;
  const connect = d?.products.includes("connect") ?? false;
  const jibsy = d?.products.includes("commai") ?? false;

  const remove = (l: OrgLocation) => {
    if (!window.confirm(`Delete ${l.name}? Its opening hours in Jibsy go too.`)) return;
    act.run(async () => {
      await api(`/orgs/${cid}/locations/${l.id}`, { method: "DELETE" });
      data.reload();
    });
  };
  const newCode = (l: OrgLocation) =>
    act.run(async () => {
      const code = await api<Install>(`/orgs/${cid}/sites/${l.connect?.id}/install-code`, { method: "POST" });
      setInstall({ ...code, place: l.name });
      window.scrollTo({ top: 0, behavior: "smooth" });
    });

  return (
    <>
      <PageHead title="Locations">
        Each branch or office, entered once. Connect, phones and Jibsy&apos;s opening hours all use this list.
      </PageHead>
      <ErrorNote error={data.error ?? act.error} />

      {install && <InstallCode install={install} onClose={() => setInstall(null)} hubReady={d?.hub_ready ?? true} />}

      {manage && !editing && !connecting && (
        <p>
          <button className="button" onClick={() => setEditing("new")}>
            Add a location
          </button>
        </p>
      )}
      {editing && (
        <LocationForm
          cid={cid}
          loc={editing === "new" ? null : editing}
          onDone={() => {
            setEditing(null);
            data.reload();
          }}
        />
      )}
      {connecting && (
        <ConnectForm
          cid={cid}
          loc={connecting}
          onDone={(out) => {
            setConnecting(null);
            if (out) setInstall({ ...out, place: connecting.name });
            data.reload();
          }}
        />
      )}

      {d && d.locations.length === 0 && !editing && (
        <div className="empty-note">
          No locations yet. Add your head office first, then each branch.
        </div>
      )}
      <div className="loc-list">
        {d?.locations.map((l) => (
          <section key={l.id} className="card loc" aria-labelledby={`loc-${l.id}`}>
            <div className="loc-head">
              <div>
                <h2 id={`loc-${l.id}`}>{l.name}</h2>
                <p className="muted small">
                  {[l.address_line1, l.address_line2, l.city, l.island, l.country].filter(Boolean).join(", ") ||
                    "No address yet"}{" "}
                  · {l.timezone}
                </p>
              </div>
              {manage && (
                <div className="form-actions">
                  <button className="button secondary small" onClick={() => setEditing(l)} disabled={act.busy}>
                    Edit
                  </button>
                  {!l.connect && !l.phone && (
                    <button className="button secondary small" onClick={() => remove(l)} disabled={act.busy}>
                      Delete
                    </button>
                  )}
                </div>
              )}
            </div>
            <ul className="loc-uses">
              {connect && (
                <li>
                  <span className="loc-app">Connect</span>
                  {l.connect ? (
                    l.connect.node_id ? (
                      <span>
                        <StatusPill health={l.connect.online ? "ok" : "bad"}>
                          {l.connect.online ? "Box online" : "Box offline"}
                        </StatusPill>{" "}
                        <Link to={`/sites/${l.connect.id}`}>{l.connect.name}</Link>, {l.connect.links} link
                        {l.connect.links === 1 ? "" : "s"}, seen {ago(l.connect.last_seen)}
                      </span>
                    ) : (
                      <span>
                        <StatusPill health="warn">Waiting for the box to be installed</StatusPill>{" "}
                        {manage && (
                          <button className="link" onClick={() => newCode(l)} disabled={act.busy}>
                            Get a new install code
                          </button>
                        )}
                      </span>
                    )
                  ) : manage ? (
                    <span>
                      Not connected.{" "}
                      <button className="button small" onClick={() => setConnecting(l)} disabled={act.busy}>
                        Connect this location
                      </button>
                    </span>
                  ) : (
                    <span className="muted">Not connected</span>
                  )}
                </li>
              )}
              {jibsy && (
                <li>
                  <span className="loc-app">Phones</span>
                  {l.phone ? (
                    <span>
                      <StatusPill health={EMERGENCY[l.phone.emergency_status]?.[1] ?? "warn"}>
                        {EMERGENCY[l.phone.emergency_status]?.[0] ?? l.phone.emergency_status}
                      </StatusPill>{" "}
                      {l.phone.people} {l.phone.people === 1 ? "person" : "people"}.{" "}
                      <Link to="/commai/voice">Phone system</Link>
                    </span>
                  ) : (
                    <span className="muted">
                      {l.address_line1 ? "Not used for phones." : "Add a street address to use it for phones."}
                    </span>
                  )}
                </li>
              )}
              {jibsy && (
                <li>
                  <span className="loc-app">Opening hours</span>
                  {l.hours && l.hours.intervals > 0 ? (
                    <span>
                      Set{l.hours.is_primary ? " (main location)" : ""}.{" "}
                      <Link to="/commai/settings/organisation">Change hours</Link>
                    </span>
                  ) : (
                    <span>
                      Open around the clock.{" "}
                      <Link to="/commai/settings/organisation">Set opening hours</Link>
                    </span>
                  )}
                </li>
              )}
            </ul>
          </section>
        ))}
      </div>
    </>
  );
}

function LocationForm({ cid, loc, onDone }: { cid: string; loc: OrgLocation | null; onDone: () => void }) {
  const [f, setF] = useState<Draft>(loc ?? EMPTY);
  const act = useAction();
  const set = (k: keyof Draft) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api(loc ? `/orgs/${cid}/locations/${loc.id}` : `/orgs/${cid}/locations`, {
        method: loc ? "PUT" : "POST",
        body: JSON.stringify(f),
      });
      onDone();
    });
  };
  return (
    <Card title={loc ? `Edit ${loc.name}` : "New location"}>
      <form className="form" onSubmit={submit}>
        <label>
          Name
          <input value={f.name} onChange={set("name")} required maxLength={120} placeholder="Head office" />
        </label>
        <label className="wide">
          Street address
          <input value={f.address_line1} onChange={set("address_line1")} maxLength={200} placeholder="1 Independence Square" />
        </label>
        <label>
          Address line 2
          <input value={f.address_line2} onChange={set("address_line2")} maxLength={200} />
        </label>
        <label>
          Town or city
          <input value={f.city} onChange={set("city")} maxLength={120} placeholder="Port of Spain" />
        </label>
        <label>
          Island
          <input value={f.island} onChange={set("island")} maxLength={120} placeholder="Trinidad" />
        </label>
        <label>
          Country (two letters)
          <input value={f.country} onChange={set("country")} maxLength={2} placeholder="TT" />
        </label>
        <label>
          Postcode
          <input value={f.postcode} onChange={set("postcode")} maxLength={20} />
        </label>
        <label>
          Time zone
          <input list="loc-zones" value={f.timezone} onChange={set("timezone")} maxLength={64} />
          <datalist id="loc-zones">
            {ZONES.map((z) => (
              <option key={z} value={z} />
            ))}
          </datalist>
        </label>
        <p className="small muted wide" style={{ margin: 0 }}>
          The street address is what emergency calls from this location give, so use the real one.
        </p>
        <div className="actions wide">
          <button className="button" disabled={act.busy}>
            {act.busy ? "Saving…" : loc ? "Save location" : "Add location"}
          </button>
          <button type="button" className="button secondary" onClick={onDone}>
            Cancel
          </button>
        </div>
      </form>
      <ErrorNote error={act.error} />
    </Card>
  );
}

interface LinkDraft {
  carrier: string;
  underlay_type: string;
  commit_mbps: string;
}

const TYPES: [string, string][] = [
  ["fibre", "Fibre"],
  ["broadband", "Broadband (cable or DSL)"],
  ["lte", "Mobile (4G/5G)"],
  ["leo", "Satellite, low orbit (e.g. Starlink)"],
  ["geo", "Satellite, geostationary"],
];

function ConnectForm({ cid, loc, onDone }: { cid: string; loc: OrgLocation; onDone: (i: Install | null) => void }) {
  const [links, setLinks] = useState<LinkDraft[]>([{ carrier: "", underlay_type: "fibre", commit_mbps: "" }]);
  const [lan, setLan] = useState("");
  const act = useAction();
  const setLink = (i: number, k: keyof LinkDraft, v: string) =>
    setLinks(links.map((l, j) => (j === i ? { ...l, [k]: v } : l)));
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const out = await api<{ install: Install }>(`/orgs/${cid}/locations/${loc.id}/connect`, {
        method: "POST",
        body: JSON.stringify({
          links: links.map((l) => ({ carrier: l.carrier, underlay_type: l.underlay_type, commit_mbps: Number(l.commit_mbps) || 0 })),
          lan_prefixes: lan.split(",").map((p) => p.trim()).filter(Boolean),
        }),
      });
      onDone(out.install);
    });
  };
  return (
    <Card title={`Connect ${loc.name}`}>
      <p className="small muted" style={{ marginTop: 0 }}>
        List the internet links at this location: up to two from carriers on the ground and one satellite. Connect
        watches every link and moves your applications between them before a slow or failing one hurts them.
      </p>
      <form className="form" onSubmit={submit}>
        {links.map((l, i) => (
          <fieldset key={i} className="wide connect-link">
            <legend>Link {i + 1}</legend>
            <label>
              Carrier
              <input value={l.carrier} onChange={(e) => setLink(i, "carrier", e.target.value)} required maxLength={120} placeholder="Digicel" />
            </label>
            <label>
              Kind of link
              <select value={l.underlay_type} onChange={(e) => setLink(i, "underlay_type", e.target.value)}>
                {TYPES.map(([v, t]) => (
                  <option key={v} value={v}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Speed you pay for (Mbps)
              <input inputMode="decimal" value={l.commit_mbps} onChange={(e) => setLink(i, "commit_mbps", e.target.value)} placeholder="100" />
            </label>
            {links.length > 1 && (
              <button type="button" className="link" onClick={() => setLinks(links.filter((_, j) => j !== i))}>
                Remove
              </button>
            )}
          </fieldset>
        ))}
        {links.length < 3 && (
          <div className="wide">
            <button
              type="button"
              className="button secondary small"
              onClick={() => setLinks([...links, { carrier: "", underlay_type: links.length === 2 ? "leo" : "broadband", commit_mbps: "" }])}
            >
              Add another link
            </button>
          </div>
        )}
        <label className="wide">
          Office network (optional)
          <input value={lan} onChange={(e) => setLan(e.target.value)} placeholder="192.168.10.0/24" />
          <span className="small muted" style={{ fontWeight: 400 }}>
            The address range of the computers at this location, if other locations need to reach them. Ask your IT
            team, or leave it blank.
          </span>
        </label>
        <div className="actions wide">
          <button className="button" disabled={act.busy}>
            {act.busy ? "Connecting…" : "Connect and get the install code"}
          </button>
          <button type="button" className="button secondary" onClick={() => onDone(null)}>
            Cancel
          </button>
        </div>
      </form>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function InstallCode({
  install,
  onClose,
  hubReady,
}: {
  install: Install & { place?: string };
  onClose: () => void; hubReady: boolean }) {
  const [copied, setCopied] = useState(false);
  const url = install.public_agent_url || install.agent_url;
  const command = `EXA_ENROL_TOKEN=${install.token} bash install.sh \\\n  --controller ${url} \\\n  --ca-fingerprint ${install.ca_fingerprint} \\\n  --name ${install.site}`;
  const copy = () =>
    navigator.clipboard
      ?.writeText(command)
      .then(() => setCopied(true))
      .catch(() => setCopied(false));
  return (
    <section className="card install" aria-labelledby="install-title">
      <div className="card-head">
        <h2 id="install-title">Install the ExaCarib box at {install.place ?? install.site}</h2>
        <button className="button secondary small" onClick={onClose}>
          Done
        </button>
      </div>
      <ol className="install-steps">
        <li>Plug the box into each internet link and into the office network.</li>
        <li>
          On the box (Debian or Ubuntu Linux), with the site kit, run this as root. The code works once and expires{" "}
          {new Date(install.expires_at).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}.
        </li>
      </ol>
      <pre className="mono small install-command">{command}</pre>
      <p className="form-actions">
        <button className="button small" onClick={copy}>
          {copied ? "✓ Copied" : "Copy the command"}
        </button>
      </p>
      <p className="small muted" style={{ marginBottom: 0 }}>
        The box only needs to reach out on port 8443; nothing comes in. It makes its own keys and sends only the public
        one. This location turns green here once the box checks in.
        {!hubReady && " ExaCarib is still preparing your network hub, so traffic flows once that is ready."}
      </p>
    </section>
  );
}
