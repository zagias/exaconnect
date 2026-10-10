import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, useApi } from "../../api";
import { APP_INFO, APP_ORDER, type AppId } from "../../apps";
import { ErrorNote, StatusPill } from "../../components";
import { useCustomer } from "../../customer";
import { Card, PageHead, useAction } from "../../ui";
import "../org/org.css";

interface Org {
  id: string;
  name: string;
  products: AppId[] | null;
  owners: string | null;
  people: number;
  invites: number;
  sites: number;
  locations: number;
  hub: boolean;
  example: boolean;
  setup: { done: number; total: number; complete: boolean };
}

interface Created {
  id: string;
  name: string;
  invite: { email: string; url: string; expires_at: string };
}

/** ExaCarib operations (ADR 0043): every organisation, and a new one in one step. */
export default function Organisations() {
  const orgs = useApi<Org[]>("/admin/organisations", 30_000);
  const { select, reload } = useCustomer();
  const navigate = useNavigate();
  const open = (o: { id: string }, to: string) => {
    select(o.id);
    reload();
    navigate(to);
  };
  return (
    <>
      <PageHead title="Organisations">
        Every customer organisation, how far each has got with set-up, and a new one in one step. Choose one to work
        on it as its owners see it.
      </PageHead>
      <NewOrganisation onCreated={() => {
        orgs.reload();
        reload();
      }} onOpen={(c) => open(c, "/org/setup")} />
      <Card title="All organisations">
        <ErrorNote error={orgs.error} />
        <div className="table-wrap">
          <table className="paths dt stack org-table">
            <thead>
              <tr>
                <th scope="col">Organisation</th>
                <th scope="col">Plans</th>
                <th scope="col">Set-up</th>
                <th scope="col">People</th>
                <th scope="col">Locations</th>
                <th scope="col" className="actions">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {(orgs.data ?? []).map((o) => (
                <tr key={o.id}>
                  <td>
                    <strong>{o.name}</strong> {o.example && <span className="tag">Example data</span>}
                    <span className="sub muted small">{o.owners ? `Owner: ${o.owners}` : "No owner yet"}</span>
                  </td>
                  <td data-label="Plans">{APP_ORDER.filter((p) => (o.products ?? []).includes(p)).map((p) => APP_INFO[p].name).join(", ") || "None"}</td>
                  <td data-label="Set-up">
                    <StatusPill health={o.setup.complete ? "ok" : "warn"}>
                      {o.setup.complete ? "Done" : `${o.setup.done} of ${o.setup.total}`}
                    </StatusPill>
                  </td>
                  <td data-label="People">
                    {o.people}
                    {o.invites > 0 && <span className="sub muted small">{o.invites} invited</span>}
                  </td>
                  <td data-label="Locations">
                    {o.locations}
                    <span className="sub muted small">
                      {o.sites} connected{(o.products ?? []).includes("connect") && !o.hub ? " · no hub yet" : ""}
                    </span>
                  </td>
                  <td className="actions">
                    <button className="button secondary small" onClick={() => open(o, "/org/setup")}>
                      Open
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}

function NewOrganisation({ onCreated, onOpen }: { onCreated: () => void; onOpen: (c: Created) => void }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [country, setCountry] = useState("TT");
  const [plans, setPlans] = useState<AppId[]>(["connect"]);
  const [created, setCreated] = useState<Created | null>(null);
  const act = useAction();
  const toggle = (p: AppId) => setPlans(plans.includes(p) ? plans.filter((x) => x !== p) : [...plans, p]);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const out = await api<Created>("/admin/organisations", {
        method: "POST",
        body: JSON.stringify({ name, owner_email: email, products: plans, country }),
      });
      setCreated(out);
      setName("");
      setEmail("");
      onCreated();
    });
  };
  const link = created ? (created.invite.url.startsWith("/") ? window.location.origin + created.invite.url : created.invite.url) : "";
  return (
    <Card title="New organisation">
      {created ? (
        <div className="install" style={{ marginBottom: 0 }}>
          <p style={{ marginTop: 0 }}>
            <strong>{created.name}</strong> is ready. Send this link to <strong>{created.invite.email}</strong>; they
            choose a password and become the owner. It works once and expires in 7 days. No email is sent for you.
          </p>
          <pre className="mono small install-command">{link}</pre>
          <p className="form-actions">
            <button className="button small" onClick={() => navigator.clipboard?.writeText(link).catch(() => undefined)}>
              Copy the link
            </button>
            <button className="button secondary small" onClick={() => onOpen(created)}>
              Open its set-up
            </button>
            <button className="button secondary small" onClick={() => setCreated(null)}>
              Add another
            </button>
          </p>
        </div>
      ) : (
        <form className="form" onSubmit={submit}>
          <label>
            Organisation name
            <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} placeholder="Harbour Bank" />
          </label>
          <label>
            Owner&apos;s work email
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required placeholder="it@harbourbank.tt" />
          </label>
          <label>
            Country (two letters)
            <input value={country} onChange={(e) => setCountry(e.target.value)} maxLength={2} />
          </label>
          <fieldset>
            <legend>Plans</legend>
            {(["connect", "commai"] as AppId[]).map((p) => (
              <label key={p} className="check">
                <input type="checkbox" checked={plans.includes(p)} onChange={() => toggle(p)} /> {APP_INFO[p].full}
              </label>
            ))}
          </fieldset>
          <div className="actions wide">
            <button className="button" disabled={act.busy || plans.length === 0}>
              {act.busy ? "Creating…" : "Create and invite the owner"}
            </button>
          </div>
        </form>
      )}
      <ErrorNote error={act.error} />
    </Card>
  );
}
