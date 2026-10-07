import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import { api, useApi } from "../../api";
import { useAuth } from "../../auth";
import { ErrorNote } from "../../components";
import { Card, PageHead, useAction } from "../../ui";
import { useCommaiBase } from "./lib";
import "./channels.css";

/* Countries (ADR 0023): what CommAI can do where. Shapes from
   controller/exaconnect_controller/commai/channels/countries.py (matrix_for). */

interface Fact {
  summary: string;
  verified: boolean;
}

interface SmsFact extends Fact {
  sender_id_types: string[];
  registration: Record<string, string>;
  quiet_hours: { from: string; until: string } | null;
  quiet_hours_note: string;
  rate_per_minute: number;
}

interface Country {
  code: string;
  name: string;
  region: string;
  dial_code: string;
  timezones: string[];
  available: boolean;
  research: { status: string; as_of: string };
  numbers: Fact;
  porting: Fact;
  sms: SmsFact;
  whatsapp: Fact;
  calling: Fact;
  emergency: Fact;
  restrictions: Fact;
}

interface Registry {
  key: string;
  status?: "off" | "pilot" | "on";
  met?: number;
  criteria?: number;
}

interface Registration {
  id: string;
  sender: string;
  country: string;
  kind: string;
  status: "pending" | "approved" | "rejected";
  reference: string;
}

const SENDER: Record<string, string> = {
  long_code: "Ordinary number",
  toll_free: "Toll-free number",
  short_code: "Short code",
  alphanumeric: "Name (sender ID)",
};

const AREAS: [keyof Country, string][] = [
  ["numbers", "Numbers"],
  ["porting", "Moving numbers (porting)"],
  ["whatsapp", "WhatsApp"],
  ["calling", "Calling"],
  ["emergency", "Emergency calls"],
  ["restrictions", "Rules to know"],
];

function Research({ f }: { f: Fact }) {
  return (
    <>
      {f.summary}{" "}
      {!f.verified && (
        <span className="tag" title="Not yet checked by someone who knows the local rules">
          Unverified research
        </span>
      )}
    </>
  );
}

/** Countries: the capability matrix for the business, and SMS sender registrations. */
export default function Countries() {
  const base = useCommaiBase();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const list = useApi<Country[]>(base ? `${base}/countries` : null, 120_000);
  const registry = useApi<Registry[]>(admin ? "/commai/golive?kind=country" : null, 120_000);
  const [open, setOpen] = useState<string | null>(null);
  if (!base) return <p className="muted">Choose a business first.</p>;
  const reg = new Map((registry.data ?? []).map((r) => [r.key, r]));
  const groups = ["Caribbean", "Atlantic", "Reference"];
  return (
    <>
      <PageHead title="Countries">
        What CommAI can do in each country. A country is switched on only after its written checks pass.
      </PageHead>
      <p className="callout small">
        <strong>Unverified research.</strong> The details below come from desk research and have not yet been checked by someone who knows each country's rules. Treat them as a guide, not as
        fact.
      </p>
      <ErrorNote error={list.error} />
      {admin && (
        <p className="muted small">
          ExaCarib admins switch countries on, and record each check, on the <Link to="/commai/golive">Go-live</Link> screen.
        </p>
      )}
      {groups.map((g) => {
        const rows = (list.data ?? []).filter((c) => c.region === g);
        if (rows.length === 0) return null;
        return (
          <Card key={g} title={g === "Reference" ? "Reference markets" : g}>
            <div className="table-wrap">
              <table className="paths dt stack">
                <thead>
                  <tr>
                    <th scope="col">Country</th>
                    <th scope="col">For you</th>
                    <th scope="col">SMS senders</th>
                    <th scope="col">Registration</th>
                    <th scope="col">Quiet hours</th>
                    {admin && <th scope="col">Registry</th>}
                    <th scope="col">
                      <span className="sr-only">Details</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((c) => {
                    const r = reg.get(c.code);
                    return (
                      <Fragment key={c.code}>
                        <tr>
                          <td>
                            <strong>{c.name}</strong> <span className="muted mono small">{c.dial_code}</span>
                          </td>
                          <td data-label="For you">
                            <span className={`pill ${c.available ? "ok" : "warn"}`}>{c.available ? "Available" : "Not yet"}</span>
                          </td>
                          <td data-label="SMS senders">{c.sms.sender_id_types.map((t) => SENDER[t] ?? t).join(", ")}</td>
                          <td data-label="Registration">{Object.values(c.sms.registration).join("; ") || "None found"}</td>
                          <td data-label="Quiet hours">{c.sms.quiet_hours ? `Send ${c.sms.quiet_hours.from} to ${c.sms.quiet_hours.until}` : "None found"}</td>
                          {admin && (
                            <td data-label="Registry" className="mono">
                              {r ? `${r.status ?? "off"} · ${r.met ?? 0}/${r.criteria ?? 0} checks` : "–"}
                            </td>
                          )}
                          <td>
                            <button type="button" className="button secondary small" aria-expanded={open === c.code} onClick={() => setOpen(open === c.code ? null : c.code)}>
                              {open === c.code ? "Hide" : "Details"}
                            </button>
                          </td>
                        </tr>
                        {open === c.code && (
                          <tr>
                            <td colSpan={admin ? 7 : 6}>
                              <dl className="ch-dl">
                                <dt>SMS</dt>
                                <dd>
                                  <Research f={c.sms} /> {c.sms.quiet_hours_note} Up to {c.sms.rate_per_minute} a minute.
                                </dd>
                                {AREAS.map(([k, label]) => (
                                  <div key={k} style={{ display: "contents" }}>
                                    <dt>{label}</dt>
                                    <dd>
                                      <Research f={c[k] as Fact} />
                                    </dd>
                                  </div>
                                ))}
                                <dt>Time zones</dt>
                                <dd className="mono small">{c.timezones.join(", ")}</dd>
                                <dt>Research as of</dt>
                                <dd>{c.research.as_of}</dd>
                              </dl>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>
        );
      })}
      <Senders base={base} />
    </>
  );
}

function Senders({ base }: { base: string }) {
  const regs = useApi<Registration[]>(`${base}/sms-senders`, 60_000);
  const add = useAction();
  const [sender, setSender] = useState("");
  const [country, setCountry] = useState("US");
  return (
    <Card title="SMS sender registrations">
      <p className="muted">
        Some countries only deliver SMS from registered senders (in the United States, 10DLC registration for ordinary numbers and verification for toll-free numbers). Ask here; ExaCarib
        files it with the carrier and records the outcome.
      </p>
      <ErrorNote error={regs.error} />
      {regs.data && regs.data.length > 0 && (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Sender</th>
                <th scope="col">Country</th>
                <th scope="col">Status</th>
                <th scope="col">Reference</th>
              </tr>
            </thead>
            <tbody>
              {regs.data.map((r) => (
                <tr key={r.id}>
                  <td className="mono">{r.sender}</td>
                  <td data-label="Country">{r.country}</td>
                  <td data-label="Status">
                    <span className={`pill ${r.status === "approved" ? "ok" : r.status === "rejected" ? "bad" : "warn"}`}>{r.status === "approved" ? "Approved" : r.status === "rejected" ? "Rejected" : "Waiting"}</span>
                  </td>
                  <td data-label="Reference" className="mono">
                    {r.reference || "–"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          add.run(async () => {
            await api(`${base}/sms-senders`, { method: "POST", body: JSON.stringify({ sender, country }) });
            setSender("");
            regs.reload();
          });
        }}
      >
        <label>
          Sending number
          <input value={sender} onChange={(e) => setSender(e.target.value)} placeholder="+1 212 555 0100" required />
        </label>
        <label>
          Country
          <select value={country} onChange={(e) => setCountry(e.target.value)}>
            <option value="US">United States</option>
            <option value="PR">Puerto Rico</option>
            <option value="CA">Canada</option>
          </select>
        </label>
        <div className="actions">
          <button className="button secondary" disabled={add.busy}>
            Ask for registration
          </button>
        </div>
      </form>
      <ErrorNote error={add.error} />
    </Card>
  );
}
