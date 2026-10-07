import { useState, type FormEvent } from "react";
import { postNotice, updateNotice, useApi, type LinkRow, type Notice, type NoticeIn } from "../api";
import { useAuth } from "../auth";
import { ErrorNote, when } from "../components";
import { Card, PageHead, RowActions, useAction } from "../ui";

const STATUS_WORD: Record<Notice["status"], string> = {
  scheduled: "Scheduled",
  open: "Open",
  in_progress: "In progress",
  resolved: "Resolved",
  cancelled: "Cancelled",
  closed: "Closed",
};

const CLOSED = new Set<Notice["status"]>(["resolved", "cancelled", "closed"]);

function Window({ n }: { n: Notice }) {
  if (n.kind !== "maintenance" || !n.starts_at || !n.ends_at) return <span className="muted">Now</span>;
  return (
    <span className="mono">
      {when(n.starts_at)} to {when(n.ends_at)}
    </span>
  );
}

/**
 * Carrier notices (ADR 0026). Carriers post faults and planned maintenance on
 * their own links; organisations see the notices that touch their links. A
 * maintenance window moves traffic off the links a minute before it starts.
 */
export default function Notices() {
  const { user } = useAuth();
  const carrier = user?.role === "carrier";
  const admin = user?.role === "admin";
  const list = useApi<Notice[]>(carrier || admin ? "/carrier/notices" : "/notices", 15_000);
  const links = useApi<LinkRow[]>("/links", 0);
  const act = useAction();
  const linkName = (id: string) => {
    const l = links.data?.find((x) => x.id === id);
    return l ? `${l.site} ${l.path}` : id.slice(0, 8);
  };

  const setStatus = (n: Notice, status: Notice["status"], question: string) => {
    if (!window.confirm(question)) return;
    act.run(async () => {
      await updateNotice(n.id, { status });
      list.reload();
    });
  };

  const rows = list.data ?? [];
  return (
    <>
      <PageHead eyebrow={carrier ? "Carrier" : "Monitor"} title="Carrier notices">
        {carrier
          ? "Tell ExaCarib about faults and planned maintenance on your links. Affected sites see them, and traffic moves off a link before its maintenance window."
          : "Faults and planned maintenance that carriers have posted about your links. Traffic moves off a link a minute before its window starts."}
      </PageHead>
      <Card title={carrier ? "Your notices" : "Notices"}>
        <ErrorNote error={list.error} />
        {list.data && rows.length === 0 && (
          <div className="empty">
            <p>{carrier ? "You have posted no notices." : "No carrier notices about your links."}</p>
          </div>
        )}
        {rows.length > 0 && (
          <div className="table-wrap">
            <table className="paths dt stack">
              <thead>
                <tr>
                  <th scope="col">Notice</th>
                  {!carrier && <th scope="col">Carrier</th>}
                  <th scope="col">Links</th>
                  <th scope="col">Window</th>
                  <th scope="col">Status</th>
                  {carrier && (
                    <th scope="col" className="actions">
                      <span className="sr-only">Actions</span>
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {rows.map((n) => (
                  <tr key={n.id}>
                    <td className="cell-wrap">
                      <strong>{n.title}</strong>
                      <span className="sub">
                        {n.kind === "maintenance" ? "Planned maintenance" : "Fault"}
                        {n.severity === "critical" ? ", critical" : ""}
                        {n.external_id ? `, ref ${n.external_id}` : ""}
                      </span>
                    </td>
                    {!carrier && <td data-label="Carrier">{n.carrier}</td>}
                    <td data-label="Links" className="cell-wrap small">
                      {n.link_ids.map(linkName).join(", ")}
                    </td>
                    <td data-label="Window" className="cell-wrap small">
                      <Window n={n} />
                      {n.kind === "maintenance" && !n.move_traffic && <span className="sub">Traffic stays</span>}
                    </td>
                    <td data-label="Status">{STATUS_WORD[n.status]}</td>
                    {carrier && (
                      <td className="actions">
                        {!CLOSED.has(n.status) && (
                          <RowActions
                            label={n.title}
                            disabled={act.busy}
                            items={[
                              ...(n.kind === "fault"
                                ? [{ label: "Mark resolved", onSelect: () => setStatus(n, "resolved", `Mark "${n.title}" resolved?`) }]
                                : []),
                              {
                                label: n.kind === "maintenance" ? "Cancel maintenance" : "Withdraw notice",
                                danger: true,
                                onSelect: () => setStatus(n, "cancelled", `Cancel "${n.title}"? Traffic moved for it comes back after the hold time.`),
                              },
                            ]}
                          />
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <ErrorNote error={act.error} />
      </Card>
      {carrier && <PostNotice links={links.data ?? []} onPosted={list.reload} />}
    </>
  );
}

function PostNotice({ links, onPosted }: { links: LinkRow[]; onPosted: () => void }) {
  const [kind, setKind] = useState<NoticeIn["kind"]>("maintenance");
  const [title, setTitle] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const [starts, setStarts] = useState("");
  const [ends, setEnds] = useState("");
  const [ref, setRef] = useState("");
  const post = useAction();

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!chosen.length) {
      post.setError("Choose at least one of your links.");
      return;
    }
    post.run(async () => {
      const body: NoticeIn = { kind, title: title.trim(), link_ids: chosen, external_id: ref.trim() || undefined };
      if (kind === "maintenance") {
        body.starts_at = new Date(starts).toISOString();
        body.ends_at = new Date(ends).toISOString();
      }
      await postNotice(body);
      setTitle("");
      setChosen([]);
      setRef("");
      onPosted();
    });
  };

  return (
    <Card title="Post a notice">
      <form className="form" onSubmit={submit}>
        <label>
          Kind
          <select value={kind} onChange={(e) => setKind(e.target.value as NoticeIn["kind"])}>
            <option value="maintenance">Planned maintenance</option>
            <option value="fault">Fault</option>
          </select>
        </label>
        <label>
          Title
          <input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={200} placeholder="Core router upgrade" />
        </label>
        <label>
          Your reference (optional)
          <input value={ref} onChange={(e) => setRef(e.target.value)} maxLength={120} placeholder="CHG-1001" />
        </label>
        {kind === "maintenance" && (
          <>
            <label>
              Starts
              <input type="datetime-local" value={starts} onChange={(e) => setStarts(e.target.value)} required />
            </label>
            <label>
              Ends
              <input type="datetime-local" value={ends} onChange={(e) => setEnds(e.target.value)} required />
            </label>
          </>
        )}
        <fieldset className="wide" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="small" style={{ fontWeight: 600 }}>
            Links affected
          </legend>
          {links.length === 0 && <p className="muted small">You have no links with ExaCarib yet.</p>}
          {links.map((l) => (
            <label key={l.id} className="check">
              <input
                type="checkbox"
                checked={chosen.includes(l.id)}
                onChange={(e) => setChosen(e.target.checked ? [...chosen, l.id] : chosen.filter((x) => x !== l.id))}
              />
              {l.site}, {l.path}
            </label>
          ))}
        </fieldset>
        <div className="actions wide">
          <button className="button" disabled={post.busy}>
            {post.busy ? "Posting…" : "Post notice"}
          </button>
        </div>
      </form>
      <p className="muted small">
        Your systems can post the same notices as TM Forum TMF621 trouble tickets or TMF688 events. See the API documentation.
      </p>
      <ErrorNote error={post.error} />
    </Card>
  );
}
