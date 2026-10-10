import { useState, type FormEvent } from "react";
import { api, useApi } from "../../../api";
import { ErrorNote } from "../../../components";
import { Card, useAction } from "../../../ui";
import type { VoiceOverview } from "./types";

/* The organisation's own SIP trunks (ADR 0044): trunks, a primary and
   secondary per location, the organisation default, numbers on a trunk and a
   route test. */

export interface Trunk {
  id: string;
  key: string;
  name: string;
  adapter: "sip" | "simulated";
  host: string;
  port: number;
  transport: string;
  prefix: string;
  max_channels: number;
  allow_ips: string[];
  enabled: boolean;
  health: string;
  last_options_at: string | null;
  sim_fail: string;
}
interface TrunkLocation {
  id: string;
  name: string;
  primary_trunk_id: string | null;
  secondary_trunk_id: string | null;
  has_phone_site: boolean;
  people: number;
  numbers: number;
}
interface TrunkNumber {
  id: string;
  e164: string;
  source: string;
  status: string;
  trunk: string | null;
  location_id: string | null;
  location: string | null;
}
interface TrunksView {
  trunks: Trunk[];
  settings: { default_trunk_id: string | null; exacarib_fallback: boolean };
  locations: TrunkLocation[];
  numbers: TrunkNumber[];
}
interface RouteStep {
  key: string;
  name: string;
  role: string;
  health: string;
  usable: boolean;
  reason: string;
}
interface RouteView {
  own_trunks: boolean;
  to: string;
  caller: string;
  location: string | null;
  steps: RouteStep[];
  route: string[];
  note: string;
}

const send = (path: string, body?: unknown, method = "POST") =>
  api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });

const HEALTH: Record<string, string> = { up: "ok", degraded: "warn", down: "bad", unknown: "off" };

function health(t: Trunk) {
  if (!t.enabled) return <span className="pill small off">Off</span>;
  return <span className={`pill small ${HEALTH[t.health] ?? "off"}`}>{t.health === "unknown" ? "Not checked yet" : t.health === "up" ? "Up" : t.health === "down" ? "Down" : "Degraded"}</span>;
}

const BLANK = { name: "", adapter: "sip", host: "", port: "5060", transport: "udp", prefix: "", allow_ips: "" };

export default function Trunks({ base, v }: { base: string; v: VoiceOverview }) {
  const view = useApi<TrunksView>(`${base}/voice/trunks`, 30_000);
  const act = useAction();
  const [form, setForm] = useState<typeof BLANK & { id?: string }>(BLANK);
  const [open, setOpen] = useState(false);
  const d = view.data;
  if (!d) return <ErrorNote error={view.error} />;
  const admin = v.is_admin;
  const name = (id: string | null) => d.trunks.find((t) => t.id === id)?.name ?? "";

  const save = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const body = {
        name: form.name,
        adapter: form.adapter,
        host: form.host,
        port: Number(form.port) || undefined,
        transport: form.transport,
        prefix: form.prefix,
        allow_ips: form.allow_ips.split(/[\s,]+/).filter(Boolean),
      };
      if (form.id) await send(`${base}/voice/trunks/${form.id}`, body, "PATCH");
      else await send(`${base}/voice/trunks`, body);
      setForm(BLANK);
      setOpen(false);
      view.reload();
    });
  };
  const edit = (t: Trunk) => {
    setForm({ id: t.id, name: t.name, adapter: t.adapter, host: t.adapter === "sip" ? t.host : "", port: String(t.port), transport: t.transport, prefix: t.prefix, allow_ips: t.allow_ips.join(", ") });
    setOpen(true);
  };
  const patch = (t: Trunk, body: Record<string, unknown>) =>
    act.run(async () => {
      await send(`${base}/voice/trunks/${t.id}`, body, "PATCH");
      view.reload();
    });
  const remove = (t: Trunk) =>
    act.run(async () => {
      if (!window.confirm(`Remove the trunk ${t.name}?`)) return;
      await send(`${base}/voice/trunks/${t.id}`, undefined, "DELETE");
      view.reload();
    });
  const setLoc = (l: TrunkLocation, primary: string | null, secondary: string | null) =>
    act.run(async () => {
      await send(`${base}/voice/locations/${l.id}/trunks`, { primary_trunk_id: primary, secondary_trunk_id: primary ? secondary : null }, "PUT");
      view.reload();
    });
  const setSettings = (body: Record<string, unknown>) =>
    act.run(async () => {
      await send(`${base}/voice/trunk-settings`, body, "PUT");
      view.reload();
    });

  return (
    <>
      <Card
        title="Your SIP trunks"
        note={<span className="muted small">Bring your own carrier: calls from each location try its primary trunk, then its secondary, then your default, then ExaCarib's carriers.</span>}
      >
        <ErrorNote error={act.error} />
        {d.trunks.length === 0 ? (
          <p className="muted">No trunks of your own yet. Your calls go out through ExaCarib's carriers.</p>
        ) : (
          <table className="paths dt" aria-label="SIP trunks">
            <thead>
              <tr>
                <th scope="col">Trunk</th>
                <th scope="col">Health</th>
                <th scope="col">Carrier addresses</th>
                <th scope="col" />
              </tr>
            </thead>
            <tbody>
              {d.trunks.map((t) => (
                <tr key={t.id}>
                  <td>
                    {t.name} {t.adapter === "simulated" && <span className="tag">Simulated</span>}
                    <div className="muted small mono">{t.adapter === "sip" ? `${t.host}:${t.port} ${t.transport}` : "test trunk, no carrier"}</div>
                  </td>
                  <td>{health(t)}</td>
                  <td className="small mono">{t.allow_ips.join(", ") || <span className="muted">Its host only</span>}</td>
                  <td>
                    {admin && (
                      <div className="row-actions">
                        <button className="button secondary small" disabled={act.busy} onClick={() => edit(t)}>
                          Change
                        </button>
                        <button className="button secondary small" disabled={act.busy} onClick={() => patch(t, { enabled: !t.enabled })}>
                          {t.enabled ? "Switch off" : "Switch on"}
                        </button>
                        {t.adapter === "simulated" && (
                          <label className="small">
                            Test as{" "}
                            <select aria-label={`Test ${t.name} as`} value={t.sim_fail} disabled={act.busy} onChange={(e) => patch(t, { sim_fail: e.target.value })}>
                              <option value="">Working</option>
                              <option value="refuse">Refusing calls</option>
                              <option value="down">Down</option>
                            </select>
                          </label>
                        )}
                        <button className="button secondary small" disabled={act.busy} onClick={() => remove(t)}>
                          Remove
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {admin && !open && (
          <button className="button" onClick={() => (setForm(BLANK), setOpen(true))}>
            Add a trunk
          </button>
        )}
        {admin && open && (
          <form className="voice-when" onSubmit={save} aria-label={form.id ? "Change trunk" : "Add a trunk"}>
            <label>
              Name
              <input required value={form.name} maxLength={120} placeholder="e.g. TSTT SIP" onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </label>
            <label>
              Kind
              <select value={form.adapter} onChange={(e) => setForm({ ...form, adapter: e.target.value })}>
                <option value="sip">SIP trunk</option>
                <option value="simulated">Simulated (for testing)</option>
              </select>
            </label>
            {form.adapter === "sip" && (
              <>
                <label>
                  Carrier host or IP
                  <input required value={form.host} placeholder="sip.carrier.example" onChange={(e) => setForm({ ...form, host: e.target.value })} />
                </label>
                <label>
                  Port
                  <input inputMode="numeric" value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} />
                </label>
                <label>
                  Transport
                  <select value={form.transport} onChange={(e) => setForm({ ...form, transport: e.target.value })}>
                    <option value="udp">UDP</option>
                    <option value="tcp">TCP</option>
                    <option value="tls">TLS</option>
                  </select>
                </label>
                <label>
                  Carrier signalling addresses
                  <input value={form.allow_ips} placeholder="203.0.113.10, 203.0.113.0/28" onChange={(e) => setForm({ ...form, allow_ips: e.target.value })} />
                </label>
                <label>
                  Dial prefix
                  <input value={form.prefix} placeholder="None" onChange={(e) => setForm({ ...form, prefix: e.target.value })} />
                </label>
              </>
            )}
            <button className="button" disabled={act.busy}>
              {form.id ? "Save trunk" : "Add trunk"}
            </button>
            <button type="button" className="button secondary" onClick={() => setOpen(false)}>
              Cancel
            </button>
            <p className="small muted">Your carrier lets calls in by IP address: give them ExaCarib's address for your account. Trunks that need a SIP registration aren't supported yet.</p>
          </form>
        )}
      </Card>

      {d.trunks.length > 0 && (
        <Card title="Trunks by location" note={<span className="muted small">Each location's calls go out on its primary trunk; if that is down or refuses the call, the secondary takes it.</span>}>
          <table className="paths dt" aria-label="Trunks by location">
            <thead>
              <tr>
                <th scope="col">Location</th>
                <th scope="col">Primary</th>
                <th scope="col">Secondary</th>
              </tr>
            </thead>
            <tbody>
              {d.locations.map((l) => (
                <tr key={l.id}>
                  <td>
                    {l.name}
                    <div className="muted small">
                      {l.people} {l.people === 1 ? "person" : "people"}, {l.numbers} {l.numbers === 1 ? "number" : "numbers"}
                    </div>
                  </td>
                  <td>
                    <select aria-label={`Primary trunk for ${l.name}`} disabled={!admin || act.busy} value={l.primary_trunk_id ?? ""} onChange={(e) => setLoc(l, e.target.value || null, l.secondary_trunk_id)}>
                      <option value="">Organisation default</option>
                      {d.trunks.map((t) => (
                        <option key={t.id} value={t.id}>
                          {t.name}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <select aria-label={`Secondary trunk for ${l.name}`} disabled={!admin || act.busy || !l.primary_trunk_id} value={l.secondary_trunk_id ?? ""} onChange={(e) => setLoc(l, l.primary_trunk_id, e.target.value || null)}>
                      <option value="">None</option>
                      {d.trunks
                        .filter((t) => t.id !== l.primary_trunk_id)
                        .map((t) => (
                          <option key={t.id} value={t.id}>
                            {t.name}
                          </option>
                        ))}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {d.locations.length === 0 && <p className="muted">Add your locations under Organisation &gt; Locations first.</p>}
          <div className="voice-when">
            <label>
              Organisation default
              <select aria-label="Organisation default trunk" disabled={!admin || act.busy} value={d.settings.default_trunk_id ?? ""} onChange={(e) => setSettings({ default_trunk_id: e.target.value || null })}>
                <option value="">None</option>
                {d.trunks.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label className="small" style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 12 }}>
            <input type="checkbox" disabled={!admin || act.busy} checked={d.settings.exacarib_fallback} onChange={(e) => setSettings({ exacarib_fallback: e.target.checked })} />
            Use ExaCarib's carriers when none of your trunks can take a call
          </label>
          {d.settings.default_trunk_id && <p className="small muted">Locations without their own trunks use {name(d.settings.default_trunk_id)}.</p>}
        </Card>
      )}

      {d.trunks.length > 0 && <TrunkNumbers base={base} d={d} admin={admin} reload={view.reload} />}
      <RouteTest base={base} />
    </>
  );
}

function TrunkNumbers({ base, d, admin, reload }: { base: string; d: TrunksView; admin: boolean; reload: () => void }) {
  const act = useAction();
  const [n, setN] = useState({ e164: "", trunk: d.trunks[0]?.id ?? "", location: d.locations[0]?.id ?? "" });
  const add = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await send(`${base}/voice/trunks/${n.trunk}/numbers`, { e164: n.e164, location_id: n.location });
      setN({ ...n, e164: "" });
      reload();
    });
  };
  const move = (num: TrunkNumber, location: string) =>
    act.run(async () => {
      await send(`${base}/voice/numbers/${num.id}/location`, { location_id: location }, "PUT");
      reload();
    });
  return (
    <Card title="Numbers and locations" note={<span className="muted small">Each number belongs to a location: its emergency address, and where its calls come in.</span>}>
      <ErrorNote error={act.error} />
      <table className="paths dt" aria-label="Numbers and locations">
        <thead>
          <tr>
            <th scope="col">Number</th>
            <th scope="col">Arrives on</th>
            <th scope="col">Location</th>
          </tr>
        </thead>
        <tbody>
          {d.numbers.map((num) => (
            <tr key={num.id}>
              <td className="mono">{num.e164}</td>
              <td>{num.source === "trunk" ? num.trunk : "ExaCarib"}</td>
              <td>
                <select aria-label={`Location of ${num.e164}`} disabled={!admin || act.busy} value={num.location_id ?? ""} onChange={(e) => move(num, e.target.value)}>
                  {!num.location_id && <option value="">Not set</option>}
                  {d.locations
                    .filter((l) => l.has_phone_site)
                    .map((l) => (
                      <option key={l.id} value={l.id}>
                        {l.name}
                      </option>
                    ))}
                </select>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {admin && (
        <form className="voice-when" onSubmit={add} aria-label="Add a number on your trunk">
          <label>
            A number your carrier gives you
            <input required value={n.e164} placeholder="+1 868 555 0100" onChange={(e) => setN({ ...n, e164: e.target.value })} />
          </label>
          <label>
            Arrives on
            <select value={n.trunk} onChange={(e) => setN({ ...n, trunk: e.target.value })}>
              {d.trunks.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Location
            <select value={n.location} onChange={(e) => setN({ ...n, location: e.target.value })}>
              {d.locations
                .filter((l) => l.has_phone_site)
                .map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.name}
                  </option>
                ))}
            </select>
          </label>
          <button className="button" disabled={act.busy || !n.trunk || !n.location}>
            Add number
          </button>
          <p className="small muted">Then choose who answers it under Phone system &gt; People and numbers.</p>
        </form>
      )}
    </Card>
  );
}

function RouteTest({ base }: { base: string }) {
  const [q, setQ] = useState({ extension: "", to: "" });
  const [out, setOut] = useState<RouteView | null>(null);
  const act = useAction();
  const run = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      setOut(await api<RouteView>(`${base}/voice/route?extension=${encodeURIComponent(q.extension)}&to=${encodeURIComponent(q.to)}`));
    });
  };
  return (
    <Card title="Test a route" note={<span className="muted small">Which trunks a call would try, in order, without placing it.</span>}>
      <form className="voice-when" onSubmit={run} aria-label="Test a route">
        <label>
          From extension
          <input required inputMode="numeric" value={q.extension} placeholder="201" onChange={(e) => setQ({ ...q, extension: e.target.value })} />
        </label>
        <label>
          To
          <input required value={q.to} placeholder="+1 868 555 0100" onChange={(e) => setQ({ ...q, to: e.target.value })} />
        </label>
        <button className="button" disabled={act.busy}>
          Show route
        </button>
      </form>
      <ErrorNote error={act.error} />
      {out && (
        <div aria-label="Route" role="status">
          <p className="small">
            {out.caller} {out.location ? `at ${out.location}` : "(no location)"} calling {out.to}
          </p>
          {out.steps.length > 0 && (
            <ol className="small">
              {out.steps.map((s) => (
                <li key={s.key}>
                  <strong>{s.name}</strong> <span className="muted">{s.role}</span>{" "}
                  {s.usable ? <span className="pill small ok">Will try</span> : <span className="pill small off">Skipped: {s.reason}</span>}
                </li>
              ))}
            </ol>
          )}
          {out.note && <p className="small muted">{out.note}</p>}
        </div>
      )}
    </Card>
  );
}
