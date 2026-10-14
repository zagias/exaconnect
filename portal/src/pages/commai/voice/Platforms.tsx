import { useState, type FormEvent } from "react";
import { api, useApi } from "../../../api";
import { ErrorNote } from "../../../components";
import { Card, useAction } from "../../../ui";
import type { VoiceOverview } from "./types";

/* Teams, Zoom Phone, Webex Calling, Google Voice and contact-centre platforms
   joined to Jibsy (ADR 0056): one link per platform, the people (or queues) on it
   with their Jibsy number and extension, the set-up guide for the platform's own
   admin, and test calls both ways. */

interface Platform {
  key: string;
  name: string;
  method: string;
  kind: "people" | "destinations";
  account_label: string;
  account_hint: string;
  needs: string;
  built_in_addresses: boolean;
}
interface PlatformUser {
  id: string;
  name: string;
  account: string;
  number_id: string;
  e164: string;
  extension: string | null;
  enabled: boolean;
  number_rings_here: boolean;
}
interface Link {
  id: string;
  key: string;
  platform: string;
  name: string;
  method: string;
  kind: "people" | "destinations";
  account_label: string;
  mode: "live" | "simulated";
  enabled: boolean;
  health: string;
  fqdn: string;
  port: number;
  hosts: string[];
  allow_ips: string[];
  built_in_addresses: boolean;
  max_channels: number;
  sim_fail: string;
  dns_txt: string;
  dns_done: boolean;
  missing: string[];
  users: PlatformUser[];
  guide: { needs: string; sip_address: string; port: number; steps: string[]; script: string };
}
interface View {
  platforms: Platform[];
  links: Link[];
  numbers: { id: string; e164: string; target_type: string }[];
  sip_domain: string;
}
interface TestResult {
  ok: boolean;
  steps: string[];
  reason: string;
}

const send = (path: string, body?: unknown, method = "POST") =>
  api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });

const HEALTH: Record<string, [string, string]> = {
  up: ["ok", "Up"],
  degraded: ["warn", "Degraded"],
  down: ["bad", "Down"],
  unknown: ["off", "Not checked yet"],
};

export default function Platforms({ base, v }: { base: string; v: VoiceOverview }) {
  const view = useApi<View>(`${base}/voice/platforms`, 30_000);
  const act = useAction();
  const d = view.data;
  if (!d) return <ErrorNote error={view.error} />;
  const admin = v.is_admin;
  const joined = new Set(d.links.map((l) => l.platform));
  const join = (p: Platform) =>
    act.run(async () => {
      await send(`${base}/voice/platforms`, { platform: p.key });
      view.reload();
    });

  return (
    <>
      <Card
        title="Calling platforms"
        note={
          <span className="muted small">
            People keep calling in the app they already use. Jibsy gives them their number, carries their outside calls, and lets them reach your extensions.
          </span>
        }
      >
        <ErrorNote error={act.error} />
        <ul className="platform-list" aria-label="Calling platforms">
          {d.platforms.map((p) => (
            <li key={p.key}>
              <div>
                <strong>{p.name}</strong> <span className="muted small">{p.method}</span>
                <div className="muted small">{p.needs}</div>
              </div>
              {joined.has(p.key) ? (
                <span className="pill small ok">Joined</span>
              ) : (
                admin && (
                  <button className="button secondary small" disabled={act.busy} onClick={() => join(p)}>
                    Join {p.name}
                  </button>
                )
              )}
            </li>
          ))}
        </ul>
        <p className="small muted">A new link starts simulated: Jibsy answers calls to its people itself, so you can try everything before the platform account is ready.</p>
      </Card>
      {d.links.map((l) => (
        <LinkCard key={l.id} base={base} l={l} d={d} admin={admin} reload={view.reload} />
      ))}
    </>
  );
}

function LinkCard({ base, l, d, admin, reload }: { base: string; l: Link; d: View; admin: boolean; reload: () => void }) {
  const act = useAction();
  const [addr, setAddr] = useState({ hosts: l.hosts.join(", "), ips: l.allow_ips.join(", "), channels: String(l.max_channels) });
  const [txt, setTxt] = useState(l.dns_txt);
  const patch = (body: Record<string, unknown>) =>
    act.run(async () => {
      await send(`${base}/voice/platforms/${l.id}`, body, "PATCH");
      reload();
    });
  const remove = () =>
    act.run(async () => {
      if (!window.confirm(`Remove ${l.name}? Its people's numbers stop ringing them there.`)) return;
      await send(`${base}/voice/platforms/${l.id}`, undefined, "DELETE");
      reload();
    });
  const saveAddr = (e: FormEvent) => {
    e.preventDefault();
    const list = (s: string) => s.split(/[\s,]+/).filter(Boolean);
    patch({ hosts: list(addr.hosts), allow_ips: list(addr.ips), max_channels: Number(addr.channels) || undefined });
  };
  const [cls, label] = l.enabled ? (HEALTH[l.health] ?? HEALTH.unknown) : ["off", "Off"];

  return (
    <Card title={`${l.name}`} note={<span className="muted small">{l.method}</span>}>
      <section aria-label={`${l.name} link`}>
        <ErrorNote error={act.error} />
        <p>
          <span className={`tag ${l.mode === "live" ? "" : "muted"}`}>{l.mode === "live" ? "Live" : "Simulated"}</span>{" "}
          <span className={`pill small ${cls}`}>{label}</span>
        </p>
        <dl className="platform-kv">
          <dt>Jibsy's SIP address for you</dt>
          <dd className="mono">
            {l.fqdn}:{l.port} (TLS)
          </dd>
          <dt>{l.name}'s SIP addresses</dt>
          <dd className="mono small">{l.hosts.join(", ") || <span className="muted">Not entered yet</span>}</dd>
          <dt>Signalling addresses let in</dt>
          <dd className="mono small">{l.allow_ips.join(", ") || <span className="muted">Not entered yet</span>}</dd>
        </dl>
        {l.mode === "live" && l.missing.length > 0 && (
          <p className="small platform-warn" role="note">
            Calls can't flow until Jibsy has: {l.missing.join("; ")}.
          </p>
        )}
        {admin && (
          <div className="row-actions">
            <label className="small">
              Mode{" "}
              <select aria-label={`${l.name} mode`} value={l.mode} disabled={act.busy} onChange={(e) => patch({ mode: e.target.value })}>
                <option value="simulated">Simulated (Jibsy answers)</option>
                <option value="live">Live (calls go to {l.name})</option>
              </select>
            </label>
            {l.mode === "simulated" && (
              <label className="small">
                Test as{" "}
                <select aria-label={`Test ${l.name} as`} value={l.sim_fail} disabled={act.busy} onChange={(e) => patch({ sim_fail: e.target.value })}>
                  <option value="">Working</option>
                  <option value="refuse">Refusing calls</option>
                  <option value="down">Down</option>
                </select>
              </label>
            )}
            <button className="button secondary small" disabled={act.busy} onClick={() => patch({ enabled: !l.enabled })}>
              {l.enabled ? "Switch off" : "Switch on"}
            </button>
            <button className="button secondary small" disabled={act.busy} onClick={remove}>
              Remove
            </button>
          </div>
        )}
        {admin && !l.built_in_addresses && (
          <form className="voice-when" onSubmit={saveAddr} aria-label={`${l.name} addresses`}>
            <label>
              {l.name}'s SIP addresses
              <input value={addr.hosts} placeholder="sip1.platform.example, sip2.platform.example" onChange={(e) => setAddr({ ...addr, hosts: e.target.value })} />
            </label>
            <label>
              Signalling IP addresses
              <input value={addr.ips} placeholder="198.51.100.10, 198.51.100.0/28" onChange={(e) => setAddr({ ...addr, ips: e.target.value })} />
            </label>
            <label>
              Calls at once
              <input inputMode="numeric" value={addr.channels} onChange={(e) => setAddr({ ...addr, channels: e.target.value })} />
            </label>
            <button className="button secondary" disabled={act.busy}>
              Save addresses
            </button>
          </form>
        )}
        {admin && l.platform === "teams" && (
          <form
            className="voice-when"
            aria-label="Microsoft 365 domain check"
            onSubmit={(e) => {
              e.preventDefault();
              patch({ dns_txt: txt });
            }}
          >
            <label>
              TXT value from Microsoft 365
              <input value={txt} placeholder="ms=ms12345678" onChange={(e) => setTxt(e.target.value)} />
            </label>
            <button className="button secondary" disabled={act.busy}>
              Send to ExaCarib
            </button>
            <p className="small muted">
              {l.dns_done ? "ExaCarib has published it: press Verify in Microsoft 365." : l.dns_txt ? "ExaCarib will publish it and let you know." : "Microsoft shows this when you add the domain."}
            </p>
          </form>
        )}
      </section>
      <People base={base} l={l} d={d} admin={admin} reload={reload} />
      <Guide l={l} />
      {l.users.length > 0 && admin && <TestCall base={base} l={l} />}
    </Card>
  );
}

function People({ base, l, d, admin, reload }: { base: string; l: Link; d: View; admin: boolean; reload: () => void }) {
  const act = useAction();
  const blank = { name: "", account: "", number_id: d.numbers[0]?.id ?? "", extension: "" };
  const [f, setF] = useState(blank);
  const [open, setOpen] = useState(false);
  const who = l.kind === "people" ? "people" : "queues and call flows";
  const add = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await send(`${base}/voice/platforms/${l.id}/users`, { ...f, extension: f.extension || null });
      setF(blank);
      setOpen(false);
      reload();
    });
  };
  const change = (u: PlatformUser, body: Record<string, unknown>) =>
    act.run(async () => {
      await send(`${base}/voice/platform-users/${u.id}`, body, "PATCH");
      reload();
    });
  const remove = (u: PlatformUser) =>
    act.run(async () => {
      if (!window.confirm(`Remove ${u.name} from ${l.name}? ${u.e164} stops ringing them there.`)) return;
      await send(`${base}/voice/platform-users/${u.id}`, undefined, "DELETE");
      reload();
    });
  return (
    <section aria-label={`${l.name} ${who}`}>
      <h3 className="voice-sub">{l.kind === "people" ? "People" : "Queues and call flows"}</h3>
      <ErrorNote error={act.error} />
      {l.users.length === 0 ? (
        <p className="muted">No {who} yet. Each gets one of your Jibsy numbers{l.kind === "people" ? ", and an extension if you like" : ""}.</p>
      ) : (
        <table className="paths dt" aria-label={`${l.name} ${who} table`}>
          <thead>
            <tr>
              <th scope="col">{l.kind === "people" ? "Person" : "Queue or flow"}</th>
              <th scope="col">Number</th>
              <th scope="col">Extension</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {l.users.map((u) => (
              <tr key={u.id}>
                <td>
                  {u.name} {!u.enabled && <span className="tag">Off</span>}
                  <div className="muted small">{u.account}</div>
                </td>
                <td className="mono">
                  {u.e164}
                  {!u.number_rings_here && <div className="small platform-warn">This number now rings someone else in Phone system.</div>}
                </td>
                <td className="mono">{u.extension ?? <span className="muted">None</span>}</td>
                <td>
                  {admin && (
                    <div className="row-actions">
                      <button className="button secondary small" disabled={act.busy} onClick={() => change(u, { enabled: !u.enabled })}>
                        {u.enabled ? "Switch off" : "Switch on"}
                      </button>
                      <button className="button secondary small" disabled={act.busy} onClick={() => remove(u)}>
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
        <button className="button" disabled={d.numbers.length === 0} onClick={() => (setF(blank), setOpen(true))}>
          {l.kind === "people" ? "Add a person" : "Add a queue or flow"}
        </button>
      )}
      {admin && d.numbers.length === 0 && <p className="small muted">You need a free number first: add one under Numbers, or on one of your SIP trunks.</p>}
      {admin && open && (
        <form className="voice-when" onSubmit={add} aria-label={`Add to ${l.name}`}>
          <label>
            Name
            <input required value={f.name} maxLength={120} onChange={(e) => setF({ ...f, name: e.target.value })} />
          </label>
          <label>
            {l.account_label}
            <input required value={f.account} maxLength={200} onChange={(e) => setF({ ...f, account: e.target.value })} />
          </label>
          <label>
            Jibsy number
            <select required value={f.number_id} onChange={(e) => setF({ ...f, number_id: e.target.value })}>
              {d.numbers.map((n) => (
                <option key={n.id} value={n.id}>
                  {n.e164}
                  {n.target_type !== "none" ? " (rings someone now)" : ""}
                </option>
              ))}
            </select>
          </label>
          {l.kind === "people" && (
            <label>
              Extension
              <input inputMode="numeric" value={f.extension} placeholder="Optional" maxLength={6} onChange={(e) => setF({ ...f, extension: e.target.value })} />
            </label>
          )}
          <button className="button" disabled={act.busy}>
            Add
          </button>
          <button type="button" className="button secondary" onClick={() => setOpen(false)}>
            Cancel
          </button>
        </form>
      )}
    </section>
  );
}

function Guide({ l }: { l: Link }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(l.guide.script);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <details className="platform-guide">
      <summary>Set up {l.name}</summary>
      <p className="small">You need: {l.guide.needs}</p>
      <ol aria-label={`${l.name} set-up steps`}>
        {l.guide.steps.map((s) => (
          <li key={s}>{s}</li>
        ))}
      </ol>
      {l.guide.script && (
        <>
          <pre className="mono small" aria-label={`${l.name} PowerShell`}>
            {l.guide.script}
          </pre>
          <button type="button" className="button secondary small" onClick={copy}>
            {copied ? "Copied" : "Copy PowerShell"}
          </button>
        </>
      )}
    </details>
  );
}

function TestCall({ base, l }: { base: string; l: Link }) {
  const act = useAction();
  const [t, setT] = useState({ user_id: l.users[0].id, direction: "to", to: "" });
  const [res, setRes] = useState<TestResult | null>(null);
  const run = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      setRes((await send(`${base}/voice/platforms/${l.id}/test`, t)) as TestResult);
    });
  };
  return (
    <section aria-label={`${l.name} test call`}>
      <h3 className="voice-sub">Test call</h3>
      <form className="voice-when" onSubmit={run}>
        <label>
          {l.kind === "people" ? "Person" : "Queue or flow"}
          <select aria-label="Test with" value={t.user_id} onChange={(e) => setT({ ...t, user_id: e.target.value })}>
            {l.users.map((u) => (
              <option key={u.id} value={u.id}>
                {u.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Call
          <select aria-label="Test direction" value={t.direction} onChange={(e) => setT({ ...t, direction: e.target.value })}>
            <option value="to">From Jibsy to them</option>
            <option value="from">From them in {l.name}</option>
          </select>
        </label>
        {t.direction === "from" && (
          <label>
            To
            <input required aria-label="Test call to" value={t.to} placeholder="201 or +1 868 555 0100" onChange={(e) => setT({ ...t, to: e.target.value })} />
          </label>
        )}
        <button className="button secondary" disabled={act.busy}>
          Test the call
        </button>
      </form>
      <ErrorNote error={act.error} />
      {res && (
        <div aria-label="Test call result" role="status">
          <p>
            <span className={`pill small ${res.ok ? "ok" : "bad"}`}>{res.ok ? "Call goes through" : "Call fails"}</span>
          </p>
          <ol className="small">
            {res.steps.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}
