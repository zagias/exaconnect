// Box software (ADR 0054): update an organisation's ExaCarib boxes from the
// portal, or take them back to an earlier version. Each box installs the version
// into its spare software slot, restarts into it and keeps it only if it comes
// up healthy; otherwise it goes back to the old software by itself.
import { useState } from "react";
import { api, useApi } from "../api";
import { ErrorNote, ago } from "../components";
import { useCustomer } from "../customer";
import { Card, PageHead, useAction } from "../ui";
import { useCanManage } from "./org/common";

interface BoxRow {
  site_id: string;
  site: string;
  managed: "site" | "cloud" | "both";
  ab: boolean;
  version: string;
  slot: string;
  trial: boolean;
  installing: boolean;
  asked: { version: string; at: string; by: string } | null;
  state: string | null;
  detail: string | null;
}
interface Result {
  site_id: string;
  site?: string;
  ok: boolean;
  error?: string;
}

const STATE_NAMES: Record<string, string> = {
  sent: "Sent, waiting for the box",
  installing: "Installing",
  done: "Updated",
  current: "Already on this version",
  rolled_back: "Went back to its earlier software",
  failed: "Did not install",
  refused: "Box did not take it",
};
const MANAGED_NAMES = {
  site: "On site",
  cloud: "Cloud",
  both: "On site and cloud",
};

export function BoxSoftware() {
  const { current } = useCustomer();
  const canManage = useCanManage();
  const q = current ? `?customer_id=${current.id}` : "";
  const data = useApi<{ boxes: BoxRow[] }>(
    current ? `/box-software${q}` : null,
    10_000,
  );
  const versions = useApi<{ versions: string[]; latest: string | null }>(
    current ? "/box-software/versions" : null,
  );
  const [version, setVersion] = useState("");
  const [pick, setPick] = useState<string[]>([]);
  const [results, setResults] = useState<Result[] | null>(null);
  const act = useAction();
  if (!current) return null;
  const boxes = data.data?.boxes ?? [];
  const list = versions.data?.versions ?? [];
  const chosen = version || versions.data?.latest || "";

  const send = () =>
    act.run(async () => {
      const r = await api<{ results: Result[] }>(`/box-software${q}`, {
        method: "POST",
        body: JSON.stringify({ site_ids: pick, version: chosen }),
      });
      setResults(r.results);
      setPick([]);
      data.reload();
    });

  return (
    <>
      <PageHead title="Box software">
        Update ExaCarib boxes from here, or take them back to an earlier
        version. Each box restarts into the new software and keeps it only if it
        comes up healthy; otherwise it goes back to its earlier software by
        itself.
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
      <Card title={`Boxes of ${current.name}`}>
        {boxes.length === 0 ? (
          <p className="muted small">
            This organisation has no ExaCarib boxes in Connect yet.
          </p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  {canManage && <th />}
                  <th>Location</th>
                  <th>Managed from</th>
                  <th>Software</th>
                  <th>Last update from here</th>
                </tr>
              </thead>
              <tbody>
                {boxes.map((b) => (
                  <tr
                    key={b.site_id}
                    data-site={b.site}
                    data-ab={String(b.ab)}
                    data-version={b.version}
                  >
                    {canManage && (
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`Update ${b.site}`}
                          checked={pick.includes(b.site_id)}
                          disabled={b.managed === "site"}
                          onChange={(e) =>
                            setPick((p) =>
                              e.target.checked
                                ? [...p, b.site_id]
                                : p.filter((x) => x !== b.site_id),
                            )
                          }
                        />
                      </td>
                    )}
                    <td>{b.site}</td>
                    <td>{MANAGED_NAMES[b.managed]}</td>
                    <td>
                      {b.ab ? (
                        <>
                          <span className="mono">{b.version || "–"}</span>
                          {b.slot && (
                            <span className="muted">
                              {" "}
                              slot {b.slot.toUpperCase()}
                            </span>
                          )}
                          {b.installing && " (installing)"}
                          {b.trial && " (trying it)"}
                        </>
                      ) : (
                        <span className="muted">
                          Not the box image; updates with apt
                        </span>
                      )}
                    </td>
                    <td>
                      {b.asked ? (
                        <span title={b.detail ?? undefined}>
                          <span className="mono">{b.asked.version}</span>:{" "}
                          {STATE_NAMES[b.state ?? ""] ?? b.state}
                          {b.detail && b.state !== "done" && (
                            <span className="muted"> ({b.detail})</span>
                          )}
                          <br />
                          <span className="muted small">
                            {b.asked.by}, {ago(b.asked.at)}
                          </span>
                        </span>
                      ) : (
                        "–"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {canManage && boxes.length > 0 && (
          <div
            className="form-actions"
            style={{ marginTop: 12, alignItems: "center", flexWrap: "wrap" }}
          >
            <label className="small">
              Version{" "}
              <select
                value={chosen}
                onChange={(e) => setVersion(e.target.value)}
                aria-label="Version"
              >
                {list.length === 0 && <option value="">No versions yet</option>}
                {list.map((v, i) => (
                  <option key={v} value={v}>
                    {v}
                    {i === 0 ? " (newest)" : ""}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="button small"
              onClick={send}
              disabled={act.busy || !pick.length || !chosen}
            >
              Send to the ticked boxes
            </button>
            <span className="muted small">
              Boxes managed on site are updated from their own console.
            </span>
          </div>
        )}
        <ErrorNote error={versions.error} />
        {act.error && <p className="pill bad">{act.error}</p>}
      </Card>
    </>
  );
}
