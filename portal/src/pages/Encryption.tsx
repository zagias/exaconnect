import { Fragment } from "react";
import { encryptionPath, useApi, type EncryptionCircuit, type EncryptionReport, type PathEncryption } from "../api";
import { ErrorNote, Eyebrow } from "../components";
import { useCustomer } from "../customer";
import { Card } from "../ui";

// Encryption report (ADR 0012, docs/protection-contract.md §3): which traffic is
// encrypted, how, and since when. Read-only, for customers and admins alike.

const STATUS: Record<PathEncryption, { cls: string; word: string }> = {
  encrypted: { cls: "ok", word: "Encrypted" },
  // Idle is normal, not a warning: WireGuard only renews keys while traffic flows.
  idle: { cls: "shadow", word: "Idle" },
  down: { cls: "bad", word: "Down" },
};

const TUNNEL_WORD: Record<EncryptionCircuit["tunnel"], string> = { primary: "Primary", secondary: "Secondary" };

function Status({ status }: { status: string }) {
  const st = STATUS[status as PathEncryption] ?? { cls: "warn", word: status || "Unknown" };
  return <span className={`pill ${st.cls}`}>{st.word}</span>;
}

/** "45 s ago" from an age in seconds; null or -1 means unknown. */
function ageAgo(s: number | null | undefined, never = "Not yet"): string {
  if (s === null || s === undefined || s < 0) return never;
  const n = Math.round(s);
  if (n < 60) return `${n} s ago`;
  if (n < 3600) return `${Math.round(n / 60)} min ago`;
  if (n < 86400) return `${Math.round(n / 3600)} h ago`;
  return `${Math.round(n / 86400)} days ago`;
}

/** A long cipher suite, breaking only at its slashes. */
function Cipher({ value }: { value: string }) {
  if (!value) return <span className="muted">–</span>;
  const parts = value.split("/");
  return (
    <span className="cipher">
      {parts.map((p, i) => (
        <Fragment key={i}>
          {i > 0 && (
            <>
              /<wbr />
            </>
          )}
          {p}
        </Fragment>
      ))}
    </span>
  );
}

export default function Encryption() {
  const { current } = useCustomer();
  const report = useApi<EncryptionReport>(current ? encryptionPath(current.id) : null, 30_000);
  if (!current) return null;
  const data = report.data;
  return (
    <>
      <div className="page-head">
        <Eyebrow>Encryption</Eyebrow>
        <h1>Encryption</h1>
        <p className="muted">
          Which of {current.name}'s traffic is encrypted, how, and since when. Updated every 30 seconds.
        </p>
      </div>
      <ErrorNote error={report.error} />
      {data && (
        <>
          <Summary data={data} />
          <Paths data={data} />
          <Circuits data={data} />
          {data.layer2.length > 0 && <Layer2 data={data} />}
          <Card title="Control channel and internet">
            <p style={{ marginTop: 0 }}>
              <strong>Between your sites and ExaCarib's controller:</strong> {data.control.protocol}.
            </p>
            <p style={{ marginBottom: 0 }}>{data.internet}</p>
          </Card>
        </>
      )}
    </>
  );
}

function Summary({ data }: { data: EncryptionReport }) {
  const { encrypted, total } = data.summary;
  const down = data.paths.filter((p) => p.status === "down").length + data.circuits.filter((c) => c.status === "down").length;
  const idle = data.paths.filter((p) => p.status === "idle").length;
  const notes = data.circuits.filter((c) => c.notes.length > 0).length;
  return (
    <section className="card" style={{ marginBottom: 24 }}>
      <div className="stat-figure" style={{ marginTop: 0 }}>
        {encrypted} of {total}
      </div>
      <p style={{ margin: "0 0 8px" }}>
        {total === 0 ? "No paths or circuits yet." : `${encrypted} of ${total} paths and circuits encrypted.`}
      </p>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 16px" }}>
        {total > 0 && down === 0 && idle === 0 && <span className="pill ok">All encrypted</span>}
        {down > 0 && <span className="pill bad">{down} down</span>}
        {idle > 0 && <span className="pill shadow">{idle} idle</span>}
        {notes > 0 && <span className="pill warn">{notes === 1 ? "1 tunnel uses" : `${notes} tunnels use`} weak algorithms</span>}
      </div>
    </section>
  );
}

function Paths({ data }: { data: EncryptionReport }) {
  return (
    <Card title="Paths from your sites to the PoP">
      <p className="small muted" style={{ marginTop: 0 }}>
        Each site has a WireGuard tunnel over every carrier link. <strong>Idle</strong> means no traffic has needed the tunnel
        for over 3 minutes while the link is up: WireGuard only renews keys while traffic flows, so the next packet starts a
        fresh handshake. It is not a fault.
      </p>
      {data.paths.length === 0 ? (
        <p className="muted">No paths yet.</p>
      ) : (
        <div className="table-wrap">
          <table className="paths">
            <thead>
              <tr>
                <th scope="col">Site</th>
                <th scope="col">Carrier</th>
                <th scope="col">Protocol</th>
                <th scope="col">Cipher</th>
                <th scope="col">Last handshake</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {data.paths.map((p) => (
                <tr key={`${p.site}-${p.path}`}>
                  <td>{p.site}</td>
                  <td>{p.label || p.path}</td>
                  <td className="small">{p.protocol}</td>
                  <td className="small" style={{ minWidth: 180 }}>
                    {p.cipher || "–"}
                  </td>
                  <td className="mono small">{ageAgo(p.handshake_age_s)}</td>
                  <td>
                    <Status status={p.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function Circuits({ data }: { data: EncryptionReport }) {
  return (
    <Card title="Cloud circuit tunnels">
      <p className="small muted" style={{ marginTop: 0 }}>
        IPsec tunnels from the PoP to your cloud gateways, with the algorithms your cloud agreed to. A resilient circuit has a
        primary and a secondary tunnel.
      </p>
      {data.circuits.length === 0 ? (
        <p className="muted">No cloud circuits.</p>
      ) : (
        <div className="table-wrap">
          <table className="paths">
            <thead>
              <tr>
                <th scope="col">Circuit</th>
                <th scope="col">Tunnel</th>
                <th scope="col">Protocol</th>
                <th scope="col">Key exchange (IKE)</th>
                <th scope="col">Traffic (ESP)</th>
                <th scope="col">Established</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {data.circuits.map((c) => (
                <tr key={`${c.id}-${c.tunnel}`}>
                  <td>
                    {c.name}
                    {c.notes.length > 0 && (
                      <ul className="note-list">
                        {c.notes.map((n) => (
                          <li key={n}>
                            <span className="pill warn small" style={{ whiteSpace: "normal" }}>
                              {n}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </td>
                  <td className="small">{TUNNEL_WORD[c.tunnel] ?? c.tunnel}</td>
                  <td className="small">{c.protocol}</td>
                  <td style={{ minWidth: 160 }}>
                    <Cipher value={c.ike_cipher} />
                  </td>
                  <td style={{ minWidth: 140 }}>
                    <Cipher value={c.esp_cipher} />
                  </td>
                  <td className="mono small">{c.status === "down" ? "–" : ageAgo(c.established_s, "Unknown")}</td>
                  <td>
                    <Status status={c.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data.circuits.some((c) => c.notes.length > 0) && (
        <p className="small muted" style={{ marginBottom: 0 }}>
          A warning means the cloud agreed to an older algorithm (SHA-1, MD5, DES or 3DES, or a key exchange weaker than
          2048-bit). Choose stronger settings in your cloud's VPN options; the tunnel picks them up when it next reconnects.
        </p>
      )}
    </Card>
  );
}

function Layer2({ data }: { data: EncryptionReport }) {
  return (
    <Card title="Layer 2 circuits between sites">
      <div className="table-wrap">
        <table className="paths">
          <thead>
            <tr>
              <th scope="col">Circuit</th>
              <th scope="col">Protocol</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {data.layer2.map((l) => (
              <tr key={l.id}>
                <td>{l.name}</td>
                <td className="small">{l.protocol}</td>
                <td>
                  <Status status={l.status} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
