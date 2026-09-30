import { useControllerStatus } from "../api";
import { Eyebrow, ExampleTag, StatTile, StatusPill } from "../components";
import { exampleSites } from "../example";

// M0 shows example data only; M3 replaces it with live telemetry from the controller.
export default function Overview() {
  const controller = useControllerStatus();
  const atRisk = exampleSites.flatMap((s) => s.paths.filter((p) => p.health !== "ok").map((p) => `${p.name} at ${s.name}`));
  const headline = atRisk.length === 0 ? "All sites within SLA" : atRisk.length === 1 ? "1 path needs attention" : `${atRisk.length} paths need attention`;

  return (
    <>
      <div className="page-head">
        <Eyebrow>Overview</Eyebrow>
        <h1>{headline}</h1>
        <p className="muted">
          {atRisk.length > 0 && `${atRisk.join(", ")}. `}
          {controller.ok ? `Controller online, version ${controller.version}.` : "We can't reach the controller yet."}
        </p>
      </div>
      <div className="grid">
        <StatTile label="Voice SLA met, 24 hours" figure="99.98%" change="▲ 0.03 on last week" example />
        <StatTile label="Business SLA met, 24 hours" figure="99.91%" change="▼ 0.02 on last week" example />
        <StatTile label="Decisions, 24 hours" figure="4" change="All with reasons in the log" example />
        {exampleSites.map((s) => (
          <section className="card span-6" key={s.name}>
            <div className="card-head">
              <div>
                <Eyebrow>Path health</Eyebrow>
                <h2>{s.name}</h2>
                <span className="muted">{s.location}</span>
              </div>
              <ExampleTag />
            </div>
            <table className="paths">
              <thead>
                <tr>
                  <th scope="col">Path</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="num">Latency</th>
                  <th scope="col" className="num">Loss</th>
                </tr>
              </thead>
              <tbody>
                {s.paths.map((p) => (
                  <tr key={p.name}>
                    <td>{p.name}</td>
                    <td><StatusPill health={p.health} /></td>
                    <td className="num mono">{p.latencyMs.toFixed(1)} ms</td>
                    <td className="num mono">{p.lossPct.toFixed(2)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        ))}
      </div>
    </>
  );
}
