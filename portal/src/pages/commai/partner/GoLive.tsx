import { useState } from "react";
import { api, useApi } from "../../../api";
import { useAuth } from "../../../auth";
import { ErrorNote } from "../../../components";
import { useCustomer } from "../../../customer";
import { Card, PageHead, useAction } from "../../../ui";
import { when } from "../lib";
import "./partner.css";

/* Shapes from controller/exaconnect_controller/commai/api/golive.py (ADR 0028) and regions.py (ADR 0031). */

interface Row {
  kind: string;
  key: string;
  name: string;
  status: "off" | "pilot" | "on";
  met: number;
  criteria: number;
  updated_by: string;
  updated_at: string;
}
interface Detail extends Omit<Row, "criteria"> {
  criteria: Criterion[];
  pilots: string[];
}
interface Criterion {
  criterion: string;
  text: string;
  met: boolean;
  evidence: string;
  checked_by: string;
  checked_at: string | null;
}
interface Region {
  dependencies: { dependency: string; label: string; provider: string | null; provider_region: string | null }[];
  missing: string[];
  location: string;
  real: boolean;
}

const KINDS: [string, string][] = [
  ["country", "Countries"],
  ["channel", "Channels"],
  ["language", "Languages"],
  ["carrier", "Carriers"],
  ["region", "Regions"],
  ["feature", "Features"],
];
const STATUS: Record<Row["status"], [string, string]> = { off: ["off", "Off"], pilot: ["warn", "Pilot"], on: ["ok", "On"] };

/** Admin > Go-live (ADR 0028): every country, channel, language, carrier and region, its written criteria with evidence, and its status. */
export default function GoLive() {
  const { user } = useAuth();
  const [kind, setKind] = useState("country");
  const [picked, setPicked] = useState<string | null>(null);
  const list = useApi<Row[]>(`/commai/golive?kind=${kind}`, 0);
  if (user?.role !== "admin") return <p className="muted">ExaCarib admins only.</p>;
  return (
    <>
      <PageHead title="Go-live">
        Each capability starts off. Record every criterion as met, with evidence, before switching it to a pilot or on.
      </PageHead>
      <div className="segmented" role="group" aria-label="Kind" style={{ marginBottom: 16, flexWrap: "wrap" }}>
        {KINDS.map(([k, l]) => (
          <button
            key={k}
            aria-pressed={kind === k}
            onClick={() => {
              setKind(k);
              setPicked(null);
            }}
          >
            {l}
          </button>
        ))}
      </div>
      <ErrorNote error={list.error} />
      <div className="golive-grid">
        <ul className="golive-list" aria-label="Capabilities">
          {list.data?.length === 0 && <li className="empty">Nothing of this kind is declared yet.</li>}
          {list.data?.map((r) => (
            <li key={r.key}>
              <button aria-current={picked === r.key} onClick={() => setPicked(r.key)}>
                <span>
                  {r.name}
                  <br />
                  <span className="small muted mono">
                    {r.met}/{r.criteria} met
                  </span>
                </span>
                <span className={`pill ${STATUS[r.status][0]}`}>{STATUS[r.status][1]}</span>
              </button>
            </li>
          ))}
        </ul>
        <div>{picked ? <Capability kind={kind} cKey={picked} onChange={list.reload} /> : <p className="muted">Choose one to see its criteria.</p>}</div>
      </div>
    </>
  );
}

function Capability({ kind, cKey, onChange }: { kind: string; cKey: string; onChange: () => void }) {
  const url = `/commai/golive/${kind}/${encodeURIComponent(cKey)}`;
  const d = useApi<Detail>(url, 0);
  const region = useApi<Region>(kind === "region" ? `/commai/regions/${encodeURIComponent(cKey)}` : null, 0);
  const { customers } = useCustomer();
  const [evidence, setEvidence] = useState<Record<string, string>>({});
  const [pilots, setPilots] = useState<string[] | null>(null);
  const act = useAction();
  const reload = () => {
    d.reload();
    region.reload();
    onChange();
  };
  if (!d.data) return <ErrorNote error={d.error} />;
  const c = d.data;
  const allMet = c.criteria.every((x) => x.met);
  const chosen = pilots ?? c.pilots;
  const setStatus = (status: string) =>
    act.run(async () => {
      await api(`${url}/status`, { method: "PUT", body: JSON.stringify({ status, pilots: status === "pilot" ? chosen : status === "off" ? null : undefined }) });
      reload();
    });
  return (
    <>
      <Card title={c.name} note={<span className={`pill ${STATUS[c.status][0]}`}>{STATUS[c.status][1]}</span>}>
        <p className="small muted">
          {c.kind} · <span className="mono">{c.key}</span>
          {c.updated_by && ` · last changed by ${c.updated_by.replace(/^user:/, "")} ${when(c.updated_at)}`}
        </p>
        {c.criteria.map((x) => (
          <div key={x.criterion} className="golive-criterion">
            <div>
              <span className={`pill ${x.met ? "ok" : "off"}`}>{x.met ? "Met" : "Not met"}</span> {x.text}
            </div>
            {x.evidence && (
              <p className="small">
                Evidence: {x.evidence} <span className="muted">({x.checked_by.replace(/^user:/, "")}, {when(x.checked_at)})</span>
              </p>
            )}
            <div className="form-row">
              <label>
                Evidence
                <input value={evidence[x.criterion] ?? ""} onChange={(e) => setEvidence({ ...evidence, [x.criterion]: e.target.value })} maxLength={2000} placeholder="Test run, document or review" />
              </label>
              <button
                className="button secondary"
                disabled={act.busy || !(evidence[x.criterion] ?? "").trim()}
                onClick={() =>
                  act.run(async () => {
                    await api(`${url}/criteria/${x.criterion}`, { method: "PUT", body: JSON.stringify({ met: true, evidence: evidence[x.criterion] }) });
                    setEvidence({ ...evidence, [x.criterion]: "" });
                    reload();
                  })
                }
              >
                Record as met
              </button>
              {x.met && (
                <button
                  className="button danger-text"
                  disabled={act.busy}
                  onClick={() =>
                    act.run(async () => {
                      await api(`${url}/criteria/${x.criterion}`, { method: "PUT", body: JSON.stringify({ met: false, evidence: "" }) });
                      reload();
                    })
                  }
                >
                  Mark not met
                </button>
              )}
            </div>
          </div>
        ))}
      </Card>
      {kind === "region" && region.data && <RegionDeps cKey={cKey} region={region.data} reload={reload} />}
      <Card title="Status">
        {!allMet && <p className="small">Every criterion must be met before a pilot or switching on. Marking one not met switches it off.</p>}
        <fieldset className="partner-scopes">
          <legend>Pilot customers</legend>
          {customers.map((cu) => (
            <label key={cu.id} className="check">
              <input type="checkbox" checked={chosen.includes(cu.id)} onChange={(e) => setPilots(e.target.checked ? [...chosen, cu.id] : chosen.filter((x) => x !== cu.id))} />
              {cu.name}
            </label>
          ))}
        </fieldset>
        <div className="form-row">
          <button className="button secondary" disabled={act.busy || c.status === "off"} onClick={() => setStatus("off")}>
            Switch off
          </button>
          <button className="button secondary" disabled={act.busy || !allMet || chosen.length === 0} onClick={() => setStatus("pilot")}>
            Pilot for {chosen.length} customer{chosen.length === 1 ? "" : "s"}
          </button>
          <button className="button" disabled={act.busy || !allMet} onClick={() => setStatus("on")}>
            Switch on for everyone
          </button>
        </div>
        <ErrorNote error={act.error} />
      </Card>
    </>
  );
}

function RegionDeps({ cKey, region, reload }: { cKey: string; region: Region; reload: () => void }) {
  const [form, setForm] = useState<Record<string, { provider: string; provider_region: string }>>({});
  const act = useAction();
  return (
    <Card title="Dependencies in this region">
      <p className="small muted">
        {region.location}. {region.real ? "This is the server Connect runs on today." : "Declared only: no cloud resources exist for it."} A dependency's criterion is met only once its provider in this region is recorded.
      </p>
      {region.dependencies.map((dep) => {
        const f = form[dep.dependency] ?? { provider: "", provider_region: "" };
        return (
          <div key={dep.dependency} className="golive-criterion">
            <div>
              <span className={`pill ${dep.provider ? "ok" : "off"}`}>{dep.provider ? "Recorded" : "Missing"}</span> {dep.label}
            </div>
            {dep.provider && (
              <p className="small">
                {dep.provider} · {dep.provider_region}
              </p>
            )}
            <div className="form-row">
              <label>
                Provider
                <input value={f.provider} onChange={(e) => setForm({ ...form, [dep.dependency]: { ...f, provider: e.target.value } })} maxLength={120} />
              </label>
              <label>
                Its region
                <input value={f.provider_region} onChange={(e) => setForm({ ...form, [dep.dependency]: { ...f, provider_region: e.target.value } })} maxLength={120} />
              </label>
              <button
                className="button secondary"
                disabled={act.busy || !f.provider.trim() || !f.provider_region.trim()}
                onClick={() =>
                  act.run(async () => {
                    await api(`/commai/regions/${cKey}/dependencies/${dep.dependency}`, { method: "PUT", body: JSON.stringify(f) });
                    reload();
                  })
                }
              >
                Record
              </button>
              {dep.provider && (
                <button
                  className="button danger-text"
                  disabled={act.busy}
                  onClick={() =>
                    act.run(async () => {
                      await api(`/commai/regions/${cKey}/dependencies/${dep.dependency}`, { method: "DELETE" });
                      reload();
                    })
                  }
                >
                  Remove
                </button>
              )}
            </div>
          </div>
        );
      })}
      <ErrorNote error={act.error} />
    </Card>
  );
}
