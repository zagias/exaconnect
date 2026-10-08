import { useState } from "react";
import { api, useApi } from "../../../api";
import { ErrorNote } from "../../../components";
import "../../identity.css";
import { Card, Tabs, useAction } from "../../../ui";

interface SecuritySettings {
  require_two_step: boolean;
  session_hours: number | null;
  ip_allowlist: string[];
  sso_logout: boolean;
  thresholds: Record<string, number>;
  thresholds_effective: Record<string, number>;
  your_ip: string;
  without_two_step: number;
  people: { id: string; email: string; two_step: boolean; provisioned_by: string | null }[];
  sso_connections: { id: string; name: string; protocol: string; logout: "yes" | "no" | "if_published"; note: string }[];
}
interface Alert {
  id: string;
  kind: string;
  severity: "info" | "warning" | "critical";
  summary: string;
  action_taken: string;
  status: string;
  created_at: string;
}
interface Key {
  id: number;
  name: string;
  prefix: string;
  owner: string;
  last_used_at: string | null;
  locked: boolean;
  locked_until: string | null;
  locked_reason: string;
  addresses: number;
}
interface AuditRow {
  id: number;
  at: string;
  actor: string;
  action: string;
  target: string;
  detail: Record<string, unknown>;
}

const THRESHOLD_LABEL: Record<string, string> = {
  failed_signins: "Failed sign-ins in 15 minutes",
  send_spike_factor: "Messages sent in an hour, times the usual",
  send_spike_min: "…and at least this many",
  usage_jump_factor: "Use of a meter in a day, times the usual",
  usage_jump_min: "…and at least this much",
  key_new_addresses: "New addresses for one API key in 10 minutes before it is locked",
  key_lock_minutes: "Lock an API key for (minutes)",
};

function stamp(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

/** Two-step for everyone, session lifetime, IP allow-list, SSO sign-out, alerts, API keys and the audit view (ADR 0030). */
export default function Security({ base }: { base: string }) {
  const s = useApi<SecuritySettings>(`${base}/security`, 0);
  const alerts = useApi<Alert[]>(`${base}/security/alerts?status=all`, 60_000);
  const keys = useApi<Key[]>(`${base}/security/keys`, 60_000);
  const act = useAction();
  const [draft, setDraft] = useState<{
    require_two_step: boolean;
    session_hours: string;
    ip_allowlist: string;
    sso_logout: boolean;
    thresholds: Record<string, number>;
  } | null>(null);
  if (!s.data) return <ErrorNote error={s.error} />;
  const d = s.data;
  const f = draft ?? {
    require_two_step: d.require_two_step,
    session_hours: d.session_hours ? String(d.session_hours) : "",
    ip_allowlist: d.ip_allowlist.join("\n"),
    sso_logout: d.sso_logout,
    thresholds: d.thresholds,
  };
  const set = (patch: Partial<typeof f>) => setDraft({ ...f, ...patch });
  const save = () =>
    act.run(async () => {
      await api(`${base}/security`, {
        method: "PUT",
        body: JSON.stringify({
          require_two_step: f.require_two_step,
          session_hours: f.session_hours ? Number(f.session_hours) : null,
          ip_allowlist: f.ip_allowlist.split(/[\s,]+/).filter(Boolean),
          sso_logout: f.sso_logout,
          thresholds: f.thresholds,
        }),
      });
      setDraft(null);
      s.reload();
    });
  const post = (path: string, reload: () => void) =>
    act.run(async () => {
      await api(`${base}${path}`, { method: "POST" });
      reload();
    });

  return (
    <>
      <ErrorNote error={act.error} />
      <Card title="Sign-in rules">
        <div className="form">
          <fieldset className="wide">
            <legend>Two-step sign-in</legend>
            <label className="check">
              <input type="checkbox" checked={f.require_two_step} onChange={(e) => set({ require_two_step: e.target.checked })} />
              Require two-step sign-in for everyone
            </label>
            <p className="muted small">
              People without it can sign in only to set it up. Single sign-on users are exempt: your own provider asks for
              their second step. {d.without_two_step} of {d.people.length} people have not set it up yet.
            </p>
          </fieldset>
          <label>
            Sign people out after (hours)
            <input
              type="number"
              min={1}
              max={720}
              placeholder="Server default"
              value={f.session_hours}
              onChange={(e) => set({ session_hours: e.target.value })}
            />
          </label>
          <label className="wide">
            Allowed addresses for the portal and API keys (one range per line, empty for any)
            <textarea rows={4} value={f.ip_allowlist} placeholder="203.0.113.0/24" onChange={(e) => set({ ip_allowlist: e.target.value })} />
          </label>
          <p className="muted small wide">
            Your address now is <span className="mono">{d.your_ip || "unknown"}</span>. A list that leaves it out is
            refused, so you can&apos;t lock yourself out. ExaCarib staff are not limited by it.
          </p>
          <fieldset className="wide">
            <legend>Signing out</legend>
            <label className="check">
              <input type="checkbox" checked={f.sso_logout} onChange={(e) => set({ sso_logout: e.target.checked })} />
              Signing out of ExaCarib also signs out of your company&apos;s identity provider
            </label>
            {d.sso_connections.map((c) => (
              <p key={c.id} className="muted small">
                <span className={`identity-status ${c.logout === "yes" ? "on" : c.logout === "no" ? "off" : "wait"}`}>{c.name}</span> {c.note}
              </p>
            ))}
            {d.sso_connections.length === 0 && <p className="muted small">No single sign-on connection, so this applies to nobody yet.</p>}
          </fieldset>
          <fieldset className="wide">
            <legend>Unusual-use alerts</legend>
            {Object.entries(d.thresholds_effective).map(([k, v]) => (
              <label key={k}>
                {THRESHOLD_LABEL[k] ?? k}
                <input
                  type="number"
                  min={1}
                  value={f.thresholds[k] ?? v}
                  onChange={(e) => set({ thresholds: { ...f.thresholds, [k]: Number(e.target.value) } })}
                />
              </label>
            ))}
          </fieldset>
          <div className="actions wide">
            <button className="button" disabled={act.busy || !draft} onClick={save}>
              Save
            </button>
          </div>
        </div>
      </Card>
      <Card title="Alerts" note={<button className="button secondary" disabled={act.busy} onClick={() => post("/security/watch", alerts.reload)}>Check now</button>}>
        <p className="muted small">Alerts go to you here and by webhook (security.alert), and to ExaCarib.</p>
        <ErrorNote error={alerts.error} />
        {(alerts.data ?? []).length === 0 && <p className="muted">No alerts.</p>}
        <ul>
          {(alerts.data ?? []).map((a) => (
            <li key={a.id}>
              <span className={`identity-status ${a.severity === "critical" ? "bad" : a.severity === "warning" ? "wait" : "off"}`}>
                {a.severity === "critical" ? "✕ Critical" : a.severity === "warning" ? "! Warning" : "Info"}
              </span>{" "}
              {a.summary} <span className="muted small">{stamp(a.created_at)}</span>
              {a.action_taken && <span className="small"> · {a.action_taken}</span>}{" "}
              {a.status === "open" ? (
                <button className="link" disabled={act.busy} onClick={() => post(`/security/alerts/${a.id}/acknowledge`, alerts.reload)}>
                  Acknowledge
                </button>
              ) : (
                <span className="muted small">acknowledged</span>
              )}
            </li>
          ))}
        </ul>
      </Card>
      <Card title="API keys">
        <ErrorNote error={keys.error} />
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Key</th>
                <th scope="col">Owner</th>
                <th scope="col">Addresses used</th>
                <th scope="col">State</th>
              </tr>
            </thead>
            <tbody>
              {(keys.data ?? []).map((k) => (
                <tr key={k.id}>
                  <td>
                    {k.name} <span className="mono small">{k.prefix}…</span>
                  </td>
                  <td data-label="Owner">{k.owner}</td>
                  <td data-label="Addresses" className="mono">
                    {k.addresses}
                  </td>
                  <td data-label="State">
                    {k.locked ? (
                      <>
                        <span className="identity-status bad">✕ Locked until {stamp(k.locked_until)}</span>{" "}
                        <span className="small">{k.locked_reason}</span>{" "}
                        <button className="link" disabled={act.busy} onClick={() => post(`/security/keys/${k.id}/unlock`, keys.reload)}>
                          Unlock
                        </button>
                      </>
                    ) : (
                      <span className="identity-status on">✓ Working</span>
                    )}
                  </td>
                </tr>
              ))}
              {(keys.data ?? []).length === 0 && (
                <tr>
                  <td colSpan={4} className="muted">
                    No API keys.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
      <AuditLog base={base} />
    </>
  );
}

function AuditLog({ base }: { base: string }) {
  const [kind, setKind] = useState<"signin" | "failed" | "settings">("signin");
  const rows = useApi<AuditRow[]>(`${base}/security/audit?kind=${kind}`, 60_000);
  const names = { signin: "Sign-ins", failed: "Failed sign-ins", settings: "Setting changes" } as const;
  return (
    <Card title="Audit">
      <Tabs label="Audit views">
        {(Object.keys(names) as (keyof typeof names)[]).map((k) => (
          <a
            key={k}
            href={`#${k}`}
            className={kind === k ? "active" : undefined}
            aria-current={kind === k ? "page" : undefined}
            onClick={(e) => {
              e.preventDefault();
              setKind(k);
            }}
          >
            {names[k]}
          </a>
        ))}
      </Tabs>
      <ErrorNote error={rows.error} />
      <div className="table-wrap">
        <table className="paths dt stack">
          <thead>
            <tr>
              <th scope="col">When</th>
              <th scope="col">Who</th>
              <th scope="col">What</th>
              <th scope="col">Details</th>
            </tr>
          </thead>
          <tbody>
            {(rows.data ?? []).map((r) => (
              <tr key={r.id}>
                <td className="mono small">{stamp(r.at)}</td>
                <td data-label="Who">{r.actor.replace(/^user:/, "")}</td>
                <td data-label="What">
                  {r.action} {r.target && <span className="muted small">{r.target}</span>}
                </td>
                <td data-label="Details" className="small mono">
                  {[r.detail.ip, r.detail.reason, r.detail.via].filter(Boolean).join(" · ")}
                </td>
              </tr>
            ))}
            {(rows.data ?? []).length === 0 && (
              <tr>
                <td colSpan={4} className="muted">
                  Nothing recorded.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
