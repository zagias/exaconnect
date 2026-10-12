// ExaCarib boxes by serial number, for zero-touch installation (ADR 0050): every box
// that has called home or joined, and the customer site each one is assigned to. A box
// assigned here joins Connect by itself the next time it calls home.
import { useState, type FormEvent } from "react";
import { api, useApi } from "../api";
import { ErrorNote, StatusPill, ago } from "../components";
import { Card, RowActions, useAction } from "../ui";

export interface BoxRow {
  serial: string;
  key_id: string | null;
  key_conflict: boolean;
  note: string;
  mac: string | null;
  model: string | null;
  version: string | null;
  last_ip: string | null;
  last_seen: string | null;
  joined_at: string | null;
  organisation: string | null;
  site_id: string | null;
  site: string | null;
  location: string | null;
  node_id: string | null;
  node_seen: string | null;
  agent_version: string | null;
  state: "waiting" | "assigned" | "online" | "offline" | "conflict";
}

interface SiteChoice {
  id: string;
  name: string;
  organisation: string;
  location: string | null;
  enrolled: boolean;
}

const STATES: Record<BoxRow["state"], [string, "ok" | "warn" | "bad"]> = {
  waiting: ["Waiting to be assigned", "warn"],
  assigned: ["Assigned, joining when it calls home", "warn"],
  online: ["Online", "ok"],
  offline: ["Offline", "bad"],
  conflict: ["Another box used this serial", "bad"],
};

function siteLabel(s: { organisation: string | null; location?: string | null; name?: string | null; site?: string | null }) {
  const place = s.location || s.name || s.site || "";
  return `${s.organisation ?? ""} · ${place}`;
}

function SitePicker({
  sites,
  value,
  onChange,
  label,
  disabled,
}: {
  sites: SiteChoice[];
  value: string;
  onChange: (v: string) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <select aria-label={label} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
      <option value="">Not assigned</option>
      {sites.map((s) => (
        <option key={s.id} value={s.id}>
          {siteLabel(s)}
          {s.enrolled ? " (has a box)" : ""}
        </option>
      ))}
    </select>
  );
}

export function BoxesAdmin() {
  const { data, error, reload } = useApi<BoxRow[]>("/admin/boxes", 10_000);
  const sites = useApi<SiteChoice[]>("/admin/boxes/sites", 0);
  const act = useAction();
  const [serial, setSerial] = useState("");
  const [note, setNote] = useState("");
  const [site, setSite] = useState("");

  const add = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api("/admin/boxes", { method: "POST", body: JSON.stringify({ serial, note, site_id: site || null }) });
      setSerial("");
      setNote("");
      setSite("");
      reload();
    });
  };
  const assign = (b: BoxRow, siteId: string) =>
    act.run(async () => {
      await api(`/admin/boxes/${encodeURIComponent(b.serial)}`, { method: "PATCH", body: JSON.stringify({ site_id: siteId }) });
      reload();
    });
  const release = (b: BoxRow) => {
    if (!window.confirm(`Release ${b.serial}? The next box that calls home with this serial number is trusted.`)) return;
    act.run(async () => {
      await api(`/admin/boxes/${encodeURIComponent(b.serial)}/release`, { method: "POST" });
      reload();
    });
  };
  const remove = (b: BoxRow) => {
    if (!window.confirm(`Remove ${b.serial} from the list? If it calls home again it comes back as waiting.`)) return;
    act.run(async () => {
      await api(`/admin/boxes/${encodeURIComponent(b.serial)}`, { method: "DELETE" });
      reload();
    });
  };
  const choices = sites.data ?? [];

  return (
    <>
      <Card title="Add a box" note={<span className="muted small">From its label or the delivery note, before it is plugged in.</span>}>
        <form className="form-row" onSubmit={add} style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "end" }}>
          <label>
            Serial number
            <input value={serial} onChange={(e) => setSerial(e.target.value)} required maxLength={80} className="mono" />
          </label>
          <label>
            Note
            <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={200} placeholder="Optional" />
          </label>
          <label>
            Location
            <SitePicker sites={choices} value={site} onChange={setSite} label="Location for the new box" />
          </label>
          <button className="button" disabled={act.busy}>
            Add box
          </button>
        </form>
      </Card>
      <Card
        title="Boxes"
        note={
          <span className="muted small">
            A box plugged into any internet line calls home. Assign it to a location and it joins Connect by itself, with nobody on site.
          </span>
        }
      >
        <ErrorNote error={error ?? act.error ?? sites.error} />
        <div className="table-wrap">
          <table className="paths dt">
            <thead>
              <tr>
                <th scope="col">Serial number</th>
                <th scope="col">Status</th>
                <th scope="col">Organisation and location</th>
                <th scope="col">Last called home</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {(data ?? []).map((b) => {
                const [word, health] = STATES[b.state];
                const joined = b.state === "online" || b.state === "offline";
                return (
                  <tr key={b.serial} data-serial={b.serial}>
                    <td>
                      <span className="mono">{b.serial}</span>
                      <span className="sub">
                        {[b.model, b.mac, b.key_id ? `key ${b.key_id}` : null, b.note].filter(Boolean).join(" · ") || "Not called home yet"}
                      </span>
                    </td>
                    <td>
                      <StatusPill health={health}>{word}</StatusPill>
                      {joined && b.joined_at && <span className="sub">Joined {ago(b.joined_at)}</span>}
                    </td>
                    <td>
                      {joined ? (
                        <>
                          {siteLabel(b)}
                          <span className="sub mono">{b.site}</span>
                        </>
                      ) : (
                        <SitePicker
                          sites={choices}
                          value={b.site_id ?? ""}
                          onChange={(v) => assign(b, v)}
                          label={`Location for ${b.serial}`}
                          disabled={act.busy}
                        />
                      )}
                    </td>
                    <td className="nowrap">
                      {b.last_seen ? ago(b.last_seen) : "–"}
                      {b.last_ip && <span className="sub mono">{b.last_ip}</span>}
                    </td>
                    <td className="actions">
                      <RowActions
                        label={b.serial}
                        disabled={act.busy}
                        items={[
                          ...(b.key_id || b.key_conflict ? [{ label: "Release serial number", onSelect: () => release(b) }] : []),
                          { label: "Remove from the list", danger: true, onSelect: () => remove(b) },
                        ]}
                      />
                    </td>
                  </tr>
                );
              })}
              {data?.length === 0 && (
                <tr>
                  <td colSpan={5} className="muted">
                    No boxes yet. Add one by its serial number, or plug one into an internet line and it appears here.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}
