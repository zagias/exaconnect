import { useApi } from "../api";
import { ErrorNote, StatusPill } from "../components";
import { Card } from "../ui";

interface Release {
  id: number;
  commit: string;
  previous: string | null;
  started_at: string;
  finished_at: string | null;
  status: "deploying" | "live" | "rolled_back" | "failed";
  kind: "release" | "rollback";
  backed_up: boolean;
  detail: string;
}

const WORD: Record<Release["status"], [string, "ok" | "warn" | "bad"]> = {
  deploying: ["Deploying", "warn"],
  live: ["Went live", "ok"],
  rolled_back: ["Rolled back", "warn"],
  failed: ["Failed", "bad"],
};

const when = (t: string) =>
  new Date(t).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

/** Releases on this server (ADR 0025): each one is health-checked and rolled back if it is not healthy. */
export function ReleasesAdmin() {
  const { data, error } = useApi<{ running: { version: string; commit: string }; releases: Release[] }>(
    "/releases",
    30_000,
  );
  return (
    <Card
      title="Releases"
      note={
        data && (
          <span className="muted small">
            Running <span className="mono">{data.running.commit}</span> (version {data.running.version})
          </span>
        )
      }
    >
      <ErrorNote error={error} />
      <p className="small muted">
        Each release takes a database backup, starts the new version and checks sign-in, the portal, the agent gateway
        and the agents. If any check fails, the previous version comes back on its own. On the server,{" "}
        <span className="mono">make rollback</span> goes back by hand.
      </p>
      <div className="table-wrap">
        <table className="paths dt compact small">
          <thead>
            <tr>
              <th scope="col">Started</th>
              <th scope="col">Commit</th>
              <th scope="col">Result</th>
              <th scope="col">Backup first</th>
              <th scope="col">Detail</th>
            </tr>
          </thead>
          <tbody>
            {(data?.releases ?? []).map((r) => (
              <tr key={r.id}>
                <td className="mono nowrap">{when(r.started_at)}</td>
                <td className="mono">
                  {r.commit.slice(0, 7)}
                  {r.kind === "rollback" && <span className="sub">Rollback from {r.previous?.slice(0, 7) ?? "?"}</span>}
                </td>
                <td>
                  <StatusPill health={WORD[r.status][1]}>{WORD[r.status][0]}</StatusPill>
                </td>
                <td>{r.backed_up ? "Yes" : "No"}</td>
                <td className="cell-wrap muted">{r.detail}</td>
              </tr>
            ))}
            {data?.releases.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  No releases recorded yet. The next deploy on this server records one.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
