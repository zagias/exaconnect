import { Link } from "react-router-dom";
import { api, useApi, type OrgApps, type OrgMembers } from "../api";
import { APP_INFO, AppMark, myApps } from "../apps";
import { useAuth } from "../auth";
import { ErrorNote, Stamp } from "../components";
import { useCustomer } from "../customer";
import { Card, PageHead, useAction } from "../ui";
import "./account-apps.css";

const STATUS = { active: "On your plan", requested: "Requested", off: "Not on your plan" } as const;

interface AppRequest {
  customer_id: string;
  organisation: string;
  product: "connect" | "commai";
  requested_by: string;
  created_at: string;
}

/** Account > Apps and plans (ADR 0041): which apps the organisation holds, who
 * may open each, and asking ExaCarib to add one. Each app is its own plan. */
export default function AppsAndPlans() {
  const { user } = useAuth();
  const { current } = useCustomer();
  const admin = user?.role === "admin";
  const cid = (admin ? current?.id : user?.customer_id) ?? null;
  const apps = useApi<OrgApps>(cid ? `/orgs/${cid}/apps` : null, 60_000);
  const members = useApi<OrgMembers>(cid ? `/orgs/${cid}/members` : null, 60_000);
  const requests = useApi<AppRequest[]>(admin ? "/admin/app-requests" : null, 60_000);
  const act = useAction();
  const mine = myApps(user);
  const manage = apps.data?.can_manage ?? false;
  const people = members.data?.members ?? [];

  const ask = (id: string) =>
    act.run(async () => {
      await api(`/orgs/${cid}/apps/${id}/request`, { method: "POST" });
      apps.reload();
    });

  return (
    <>
      <PageHead eyebrow="Organisation" title="Apps and plans">
        Each app is its own plan with its own monthly invoice. {manage ? "Choose who may open each app on " : "Owners and admins choose who may open each app on "}
        <Link to="/account/people">People</Link>.
      </PageHead>
      <ErrorNote error={apps.error} />
      {!cid && <p className="muted">Pick an organisation first.</p>}
      <div className="plan-grid">
        {(apps.data?.apps ?? []).map((a) => {
          const info = APP_INFO[a.id];
          const count = people.filter((p) => p.apps.includes(a.id)).length;
          return (
            <section key={a.id} className="card plan-card" aria-labelledby={`plan-${a.id}`}>
              <div className="plan-head">
                <AppMark app={a.id} size={40} />
                <div>
                  <h2 id={`plan-${a.id}`}>{info.full}</h2>
                  <p className="muted small">{info.blurb}</p>
                </div>
                <span className={`plan-status plan-${a.status}`}>{STATUS[a.status]}</span>
              </div>
              {a.status === "active" && (
                <p className="small" style={{ margin: 0 }}>
                  {count === 1 ? "1 person" : `${count} people`} in {members.data?.organisation.name ?? "the organisation"} can open it.
                </p>
              )}
              {a.status === "requested" && (
                <p className="small muted" style={{ margin: 0 }}>
                  Asked for by {a.requested_by}
                  {a.requested_at && (
                    <>
                      , <Stamp iso={a.requested_at} />
                    </>
                  )}
                  . ExaCarib will be in touch.
                </p>
              )}
              <div className="actions">
                {a.status === "active" && mine.includes(a.id) && !admin && (
                  <Link className="button secondary" to={info.home}>
                    Open {info.name}
                  </Link>
                )}
                {a.status === "off" && manage && !admin && (
                  <button type="button" className="button" disabled={act.busy} onClick={() => ask(a.id)}>
                    Ask ExaCarib to add {info.name}
                  </button>
                )}
                {a.status === "off" && !manage && <span className="muted small">An owner or admin can ask ExaCarib to add it.</span>}
              </div>
            </section>
          );
        })}
      </div>
      <ErrorNote error={act.error} />
      {admin && (
        <Card title="Waiting to be added" note={<span className="muted small">Set an organisation's plans in Billing, Plans.</span>}>
          <ErrorNote error={requests.error} />
          {requests.data?.length === 0 && <p className="muted">No organisation is waiting for an app.</p>}
          {(requests.data?.length ?? 0) > 0 && (
            <div className="table-wrap">
              <table className="paths dt stack">
                <thead>
                  <tr>
                    <th scope="col">Organisation</th>
                    <th scope="col">App</th>
                    <th scope="col">Asked by</th>
                    <th scope="col">When</th>
                  </tr>
                </thead>
                <tbody>
                  {requests.data?.map((r) => (
                    <tr key={`${r.customer_id}-${r.product}`}>
                      <td>{r.organisation}</td>
                      <td data-label="App">{APP_INFO[r.product].name}</td>
                      <td data-label="Asked by">{r.requested_by}</td>
                      <td data-label="When">
                        <Stamp iso={r.created_at} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </>
  );
}
