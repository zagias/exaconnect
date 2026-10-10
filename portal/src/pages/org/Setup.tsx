import { Link } from "react-router-dom";
import { api, useApi } from "../../api";
import { AppMark } from "../../apps";
import { ErrorNote } from "../../components";
import { PageHead, useAction } from "../../ui";
import { useCanManage, useOrgId, type Setup } from "./common";
import "./org.css";

const ACTION: Record<string, [string, string]> = {
  company: ["Add company details", "Edit"],
  locations: ["Add locations", "Edit"],
  people: ["Invite people", "Manage people"],
  connect: ["Connect a location", "See locations"],
  jibsy: ["Start", "Open Jibsy set-up"],
};

/** The set-up checklist (ADR 0043): five steps that take a new organisation from nothing to working. */
export default function SetupPage() {
  const cid = useOrgId();
  const manage = useCanManage();
  const setup = useApi<Setup>(cid ? `/orgs/${cid}/setup` : null, 30_000);
  const act = useAction();
  if (!cid) return <NoOrganisation />;
  const d = setup.data;
  const finish = (done: boolean) =>
    act.run(async () => {
      await api(`/orgs/${cid}/setup`, { method: "POST", body: JSON.stringify({ done }) });
      setup.reload();
    });

  return (
    <>
      <PageHead eyebrow="Set up" title={d ? `Set up ${d.organisation}` : "Set up"}>
        {d?.complete
          ? "Set-up is finished. You can come back to any step from here."
          : "A few steps, in order. Leave and come back whenever you like; this list stays until it's done."}
      </PageHead>
      <ErrorNote error={setup.error ?? act.error} />
      {d && (
        <>
          <div
            className="setup-progress"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={d.total}
            aria-valuenow={d.done}
            aria-label={`${d.done} of ${d.total} steps done`}
          >
            <i style={{ width: `${(100 * d.done) / Math.max(d.total, 1)}%` }} />
          </div>
          <p className="small muted" style={{ margin: "6px 0 18px" }}>
            {d.done} of {d.total} steps done
          </p>
          <ol className="setup-steps">
            {d.steps.map((s, i) => (
              <li key={s.id} className={s.done ? "setup-step done" : "setup-step"}>
                <span className="setup-dot" aria-hidden="true">
                  {s.done ? "✓" : i + 1}
                </span>
                <div className="setup-text">
                  <h2>
                    {s.title}
                    {s.app && (
                      <span className="setup-app">
                        <AppMark app={s.app} size={18} /> {s.app === "connect" ? "Connect" : "Jibsy"}
                      </span>
                    )}
                    <span className="sr-only">{s.done ? " (done)" : " (to do)"}</span>
                  </h2>
                  <p>{s.detail}</p>
                  {s.id === "connect" && s.hub_ready === false && (
                    <p className="small muted">
                      ExaCarib is preparing your network hub. You can add locations and install boxes now; they
                      connect once the hub is ready.
                    </p>
                  )}
                </div>
                {(manage || s.done) && (
                  <Link className={s.done ? "button secondary small" : "button small"} to={s.to}>
                    {ACTION[s.id]?.[s.done ? 1 : 0] ?? "Open"}
                  </Link>
                )}
              </li>
            ))}
          </ol>
          {manage && (
            <p className="small muted" style={{ marginTop: 18 }}>
              {d.dismissed_at ? (
                <>
                  You marked set-up as finished.{" "}
                  <button className="link" disabled={act.busy} onClick={() => finish(false)}>
                    Show the reminder again
                  </button>
                </>
              ) : (
                !d.complete && (
                  <>
                    Done enough for now?{" "}
                    <button className="link" disabled={act.busy} onClick={() => finish(true)}>
                      Hide the set-up reminder
                    </button>
                  </>
                )
              )}
            </p>
          )}
        </>
      )}
    </>
  );
}

/** ExaCarib staff with no organisation chosen yet. */
export function NoOrganisation() {
  return (
    <>
      <PageHead eyebrow="Organisation" title="No organisation chosen" />
      <p className="muted">
        Choose an organisation to work on in the top bar, or{" "}
        <Link to="/ops/organisations">create a new one</Link>.
      </p>
    </>
  );
}
