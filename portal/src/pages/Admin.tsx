import { useApi, type NodeRow } from "../api";
import { ErrorNote, Eyebrow, StatusPill, ago } from "../components";

// M2 view: enrolled agents and the config version each has applied. Inventory
// editing and enrolment tokens are in the API today (/api/v1/docs) and come to
// this screen in M7.
export default function Admin() {
  const { data, error } = useApi<NodeRow[]>("/nodes", 10_000);
  return (
    <>
      <div className="page-head">
        <Eyebrow>Admin</Eyebrow>
        <h1>Agents</h1>
        <p className="muted">Each agent polls for its desired state every 10 seconds and reports the version it applied.</p>
        <ErrorNote error={error} />
      </div>
      <section className="card">
        <table className="paths">
          <thead>
            <tr>
              <th scope="col">Node</th>
              <th scope="col">Role</th>
              <th scope="col">Last seen</th>
              <th scope="col">Config</th>
              <th scope="col">Agent</th>
            </tr>
          </thead>
          <tbody>
            {(data ?? []).map((n) => (
              <tr key={n.id}>
                <td className="mono">{n.name}</td>
                <td>{n.role === "pop" ? "PoP" : "Site"}</td>
                <td>{ago(n.last_seen)}</td>
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
                  {n.apply_error && <div className="muted small">{n.apply_error}</div>}
                </td>
                <td className="mono muted">{n.agent_version || "–"}</td>
              </tr>
            ))}
            {data?.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  No agents yet. Run make demo-seed on the lab host.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </>
  );
}
