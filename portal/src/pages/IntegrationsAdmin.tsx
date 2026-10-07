import { integrationPaths, useApi, type IntegrationsStatus } from "../api";
import { ErrorNote, ago } from "../components";
import { Card } from "../ui";

function Configured({ on }: { on: boolean }) {
  return on ? <span className="tag">Configured</span> : <span className="muted">Not configured</span>;
}

/** Admin > Integrations (ADR 0026): what is configured, never the values. */
export function IntegrationsAdmin() {
  const { data, error } = useApi<IntegrationsStatus>(integrationPaths.adminStatus, 30_000);
  return (
    <>
      <ErrorNote error={error} />
      {data && (
        <>
          <Card title="Platform">
            <p className="muted small" style={{ marginTop: 0 }}>
              Live sending is {data.live ? "on" : "off"}: {data.live ? "integrations with credentials send for real." : "every integration is simulated and records what it would send."}{" "}
              Secure storage for secrets is {data.secure_storage ? "set up" : "not set up, so no integration can hold a secret"}.
              Values are never shown here; they are read from the controller's environment.
            </p>
            <div className="table-wrap">
              <table className="paths dt stack">
                <thead>
                  <tr>
                    <th scope="col">Setting</th>
                    <th scope="col">Purpose</th>
                    <th scope="col">State</th>
                  </tr>
                </thead>
                <tbody>
                  {data.platform.map((p) => (
                    <tr key={p.name}>
                      <td className="mono">{p.name}</td>
                      <td data-label="Purpose" className="cell-wrap small">
                        {p.purpose}
                      </td>
                      <td data-label="State">
                        <Configured on={p.configured} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card title="Connectors in use">
            <div className="table-wrap">
              <table className="paths dt stack">
                <thead>
                  <tr>
                    <th scope="col">Connector</th>
                    <th scope="col">In use</th>
                    <th scope="col">Live</th>
                    <th scope="col">Last delivery</th>
                    <th scope="col">To go live</th>
                  </tr>
                </thead>
                <tbody>
                  {data.providers.map((p) => (
                    <tr key={p.key}>
                      <td>
                        <strong>{p.name}</strong>
                      </td>
                      <td data-label="In use" className="mono">
                        {p.in_use} ({p.enabled} on)
                      </td>
                      <td data-label="Live" className="mono">
                        {p.live}
                      </td>
                      <td data-label="Last delivery" className="small">
                        {p.last_delivery ? ago(p.last_delivery) : <span className="muted">Never</span>}
                      </td>
                      <td data-label="To go live" className="cell-wrap small muted">
                        {p.live_needs}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card title="Cloud on-ramp adapters">
            <div className="table-wrap">
              <table className="paths dt stack">
                <thead>
                  <tr>
                    <th scope="col">Adapter</th>
                    <th scope="col">Credentials</th>
                    <th scope="col">Mode</th>
                    <th scope="col">Environment</th>
                  </tr>
                </thead>
                <tbody>
                  {data.onramp_adapters.map((a) => (
                    <tr key={a.key}>
                      <td className="cell-wrap">
                        <strong>{a.name}</strong>
                        <span className="sub">{a.live_needs}</span>
                      </td>
                      <td data-label="Credentials">
                        <Configured on={a.configured} />
                      </td>
                      <td data-label="Mode">{a.mode === "live" ? <span className="tag">Live</span> : "Simulated"}</td>
                      <td data-label="Environment" className="mono small cell-wrap">
                        {a.env.join(", ")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card title="Carrier feeds">
            <p className="muted small" style={{ marginTop: 0 }}>
              Carriers post faults and maintenance on their own links (portal, REST, TMF621 or TMF688).
            </p>
            <div className="table-wrap">
              <table className="paths dt stack">
                <thead>
                  <tr>
                    <th scope="col">Carrier</th>
                    <th scope="col">Accounts</th>
                    <th scope="col">Notices</th>
                    <th scope="col">Open</th>
                    <th scope="col">Last notice</th>
                    <th scope="col">Outbound hooks</th>
                  </tr>
                </thead>
                <tbody>
                  {data.carrier_feeds.map((c) => (
                    <tr key={c.id}>
                      <td>
                        <strong>{c.name}</strong>
                      </td>
                      <td data-label="Accounts" className="mono">
                        {c.accounts}
                      </td>
                      <td data-label="Notices" className="mono">
                        {c.notices}
                      </td>
                      <td data-label="Open" className="mono">
                        {c.open}
                      </td>
                      <td data-label="Last notice" className="small">
                        {c.last_notice ? ago(c.last_notice) : <span className="muted">Never</span>}
                      </td>
                      <td data-label="Outbound hooks" className="mono">
                        {c.outbound}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </>
  );
}
