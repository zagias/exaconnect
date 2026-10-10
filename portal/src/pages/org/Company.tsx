import { useEffect, useState, type FormEvent } from "react";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { useCustomer } from "../../customer";
import { Card, PageHead, useAction } from "../../ui";
import { NoOrganisation } from "./Setup";
import { useCanManage, useOrgId, ZONES, type Company } from "./common";
import "./org.css";

const FIELDS: { key: keyof Company; label: string; hint?: string; max: number }[] = [
  { key: "name", label: "Organisation name", max: 120 },
  { key: "country", label: "Country (two letters)", hint: "TT", max: 2 },
  { key: "address", label: "Main address", hint: "1 Independence Square, Port of Spain", max: 300 },
  { key: "phone", label: "Main phone", hint: "+1 868 …", max: 40 },
  { key: "website", label: "Website", hint: "https://", max: 200 },
];

/** Company details (ADR 0043): the organisation's name and main contact details, in one place. */
export default function CompanyPage() {
  const cid = useOrgId();
  const manage = useCanManage();
  const { reload } = useCustomer();
  const co = useApi<Company>(cid ? `/orgs/${cid}/company` : null, 0);
  const [form, setForm] = useState<Partial<Company>>({});
  const [saved, setSaved] = useState(false);
  const act = useAction();
  useEffect(() => {
    if (co.data) setForm(co.data);
  }, [co.data]);
  if (!cid) return <NoOrganisation />;

  const save = (e: FormEvent) => {
    e.preventDefault();
    setSaved(false);
    act.run(async () => {
      const { name, country, timezone, address, phone, website } = form;
      await api(`/orgs/${cid}/company`, {
        method: "PATCH",
        body: JSON.stringify({ name, country, timezone, address, phone, website }),
      });
      co.reload();
      reload();
      setSaved(true);
    });
  };

  return (
    <>
      <PageHead title="Company details">
        Your organisation&apos;s name and main contact details. Branches and offices go under Locations.
      </PageHead>
      <ErrorNote error={co.error} />
      {co.data && (
        <Card title="Details">
          <form className="form" onSubmit={save}>
            {FIELDS.map((f) => (
              <label key={f.key} className={f.key === "address" ? "wide" : undefined}>
                {f.label}
                <input
                  value={String(form[f.key] ?? "")}
                  placeholder={f.hint}
                  maxLength={f.max}
                  required={f.key === "name"}
                  disabled={!manage}
                  onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}
                />
              </label>
            ))}
            <label>
              Time zone
              <input
                list="org-zones"
                value={form.timezone ?? ""}
                placeholder="America/Port_of_Spain"
                disabled={!manage}
                onChange={(e) => setForm({ ...form, timezone: e.target.value })}
              />
              <datalist id="org-zones">
                {ZONES.map((z) => (
                  <option key={z} value={z} />
                ))}
              </datalist>
            </label>
            {manage && (
              <div className="actions wide">
                <button className="button" disabled={act.busy}>
                  {act.busy ? "Saving…" : "Save details"}
                </button>
                {saved && <span className="ok-note">✓ Saved</span>}
              </div>
            )}
          </form>
          <ErrorNote error={act.error} />
          {!manage && <p className="small muted">Only your organisation&apos;s owners and admins can change these.</p>}
        </Card>
      )}
    </>
  );
}
