import { Link } from "react-router-dom";
import { useState, type FormEvent, type ReactNode } from "react";
import { api, download, useApi } from "../../../api";
import { ErrorNote } from "../../../components";
import { Card, RowActions, useAction } from "../../../ui";
import { when } from "../lib";
import { ChangeBox, DiffList, PriceLines } from "./ChangeBox";
import { QrCode } from "./QrCode";
import type { ChangeResult, Op, PriceImpact, Version, VoiceOverview } from "./types";

type Propose = (ops: Op[], title: string) => void;

const EMERGENCY_LABEL: Record<string, [string, string]> = {
  not_registered: ["warn", "Not registered"],
  pending: ["warn", "Registration pending"],
  registered: ["ok", "Registered"],
  rejected: ["bad", "Rejected"],
};
const TARGET_LABEL: Record<string, string> = {
  none: "Not routed",
  user: "Person",
  ring_group: "Ring group",
  queue: "Queue",
  menu: "Menu",
  ai: "AI agent",
};
const DAYS: [string, string][] = [
  ["mon", "Mon"],
  ["tue", "Tue"],
  ["wed", "Wed"],
  ["thu", "Thu"],
  ["fri", "Fri"],
  ["sat", "Sat"],
  ["sun", "Sun"],
];

/** Holds the change being checked, shared by every section of the phone system. */
export function usePending() {
  const [pending, setPending] = useState<{ ops: Op[]; title: string } | null>(null);
  const [saved, setSaved] = useState<ChangeResult | null>(null);
  const propose: Propose = (ops, title) => {
    setSaved(null);
    setPending({ ops, title });
    window.scrollTo({ top: 0, behavior: "smooth" });
  };
  return { pending, setPending, saved, setSaved, propose };
}

export function PendingChange({ base, p, reload }: { base: string; p: ReturnType<typeof usePending>; reload: () => void }) {
  return (
    <>
      {p.pending && (
        <ChangeBox
          base={base}
          ops={p.pending.ops}
          title={p.pending.title}
          onCancel={() => p.setPending(null)}
          onDone={(out) => {
            p.setPending(null);
            p.setSaved(out);
            reload();
          }}
        />
      )}
      {p.saved && <Saved out={p.saved} onClose={() => p.setSaved(null)} />}
    </>
  );
}

function Saved({ out, onClose }: { out: ChangeResult & { change?: { run_at: string } }; onClose: () => void }) {
  const links = out.results.filter((r) => r.setup_url || r.join_link);
  return (
    <section className="card" role="status" style={{ marginBottom: 24 }}>
      <div className="row-between">
        <p className="pill ok">{out.change ? `Scheduled for ${new Date(out.change.run_at).toLocaleString("en-GB")}` : `Saved as version ${out.version}`}</p>
        <button className="button secondary small" onClick={onClose}>
          Close
        </button>
      </div>
      {links.length > 0 && (
        <>
          <p className="small">Set-up links, shown once. Keep them private: each one lets a phone sign in.</p>
          <ul className="plain-list">
            {links.map((l, i) => (
              <li key={i}>
                {l.setup_url ? "Desk phone provisioning URL" : "Softphone sign-in link (the QR code text)"}:{" "}
                <code className="voice-secret">{String(l.setup_url ?? l.join_link)}</code>
                {!l.setup_url && <QrCode text={String(l.join_link)} />}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label>
      {label}
      {children}
    </label>
  );
}

// ---- people, numbers and devices --------------------------------------------------------

export function PeopleAndNumbers({ base, v, propose }: { base: string; v: VoiceOverview; propose: Propose }) {
  return (
    <>
      <Sites v={v} propose={propose} />
      <Users v={v} propose={propose} />
      <Numbers v={v} propose={propose} />
      <Devices base={base} v={v} propose={propose} />
    </>
  );
}

function Sites({ v, propose }: { v: VoiceOverview; propose: Propose }) {
  const blank = { name: "", address_line1: "", address_line2: "", city: "", island: "", country: "TT", postcode: "" };
  const [f, setF] = useState(blank);
  const [editing, setEditing] = useState<string | null>(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (editing) propose([{ op: "update_site", site: editing, ...f }], `update site ${f.name}`);
    else propose([{ op: "add_site", ...f }], `add site ${f.name}`);
    setF(blank);
    setEditing(null);
  };
  return (
    <Card title="Sites and emergency addresses">
      <p className="muted small">
        Emergency calls give the site's address. Each person's address follows their site, and moves with them. Registering
        addresses with the provider, island by island, starts with real numbers (phase 3).
      </p>
      <p className="small">
        Each location in <Link to="/org/locations">Organisation › Locations</Link> with a street address has its phone
        site here already; add and edit addresses there so every app uses the same one.
      </p>
      <div className="table-wrap">
        <table className="paths dt stack">
          <thead>
            <tr>
              <th scope="col">Site</th>
              <th scope="col">Address</th>
              <th scope="col">Emergency address</th>
              <th scope="col" className="actions">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {(v.sites ?? []).map((s) => {
              const [cls, word] = EMERGENCY_LABEL[s.emergency_status];
              return (
                <tr key={s.id}>
                  <td>
                    <strong>{s.name}</strong>
                  </td>
                  <td data-label="Address" className="cell-wrap">
                    {[s.address_line1, s.address_line2, s.city, s.island, s.country].filter(Boolean).join(", ")}
                  </td>
                  <td data-label="Emergency address">
                    <span className={`pill small ${cls}`}>{word}</span>
                  </td>
                  <td className="actions">
                    <RowActions
                      label={`site ${s.name}`}
                      items={[
                        {
                          label: "Edit address",
                          onSelect: () => {
                            setEditing(s.id);
                            setF({ name: s.name, address_line1: s.address_line1, address_line2: s.address_line2, city: s.city, island: s.island, country: s.country, postcode: s.postcode });
                          },
                        },
                      ]}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <h3 className="voice-sub">{editing ? "Edit site" : "Add a site"}</h3>
      <form className="form" onSubmit={submit}>
        <Field label="Name">
          <input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required maxLength={120} />
        </Field>
        <Field label="Street address">
          <input value={f.address_line1} onChange={(e) => setF({ ...f, address_line1: e.target.value })} required maxLength={200} />
        </Field>
        <Field label="Address line 2">
          <input value={f.address_line2} onChange={(e) => setF({ ...f, address_line2: e.target.value })} maxLength={200} />
        </Field>
        <Field label="Town or city">
          <input value={f.city} onChange={(e) => setF({ ...f, city: e.target.value })} maxLength={200} />
        </Field>
        <Field label="Island">
          <input value={f.island} onChange={(e) => setF({ ...f, island: e.target.value })} maxLength={200} />
        </Field>
        <Field label="Country code">
          <input value={f.country} onChange={(e) => setF({ ...f, country: e.target.value.toUpperCase() })} maxLength={2} pattern="[A-Z]{2}" />
        </Field>
        <div className="actions wide">
          <button className="button">Check {editing ? "change" : "new site"}</button>
          {editing && (
            <button type="button" className="button secondary" onClick={() => (setEditing(null), setF(blank))}>
              Cancel
            </button>
          )}
        </div>
      </form>
    </Card>
  );
}

function Users({ v, propose }: { v: VoiceOverview; propose: Propose }) {
  const sites = v.sites ?? [];
  const teams = v.teams ?? [];
  const blank = { name: "", extension: "", site: sites[0]?.id ?? "", team: "", email: "", mobile: "", portal_email: "" };
  const [f, setF] = useState(blank);
  const [moving, setMoving] = useState<{ id: string; site: string; team: string; extension: string } | null>(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const op: Op = { op: "add_user", name: f.name, site: f.site || sites[0]?.id };
    for (const k of ["extension", "team", "email", "mobile", "portal_email"] as const) if (f[k]) op[k] = f[k];
    propose([op], `add ${f.name}`);
    setF({ ...blank, site: f.site });
  };
  const move = (e: FormEvent) => {
    e.preventDefault();
    if (!moving) return;
    const u = (v.users ?? []).find((x) => x.id === moving.id);
    const op: Op = { op: "move_user", user: moving.id, site: moving.site, team: moving.team };
    if (moving.extension && moving.extension !== u?.extension) op.extension = moving.extension;
    propose([op], `move ${u?.name ?? ""}`);
    setMoving(null);
  };
  return (
    <Card title="Users and extensions">
      {sites.length === 0 && <p className="muted">Add a site first: every person needs one for emergency calls.</p>}
      <div className="table-wrap">
        <table className="paths dt stack">
          <thead>
            <tr>
              <th scope="col">Ext</th>
              <th scope="col">Name</th>
              <th scope="col">Site</th>
              <th scope="col">Team</th>
              <th scope="col">Portal account</th>
              <th scope="col">Status</th>
              <th scope="col" className="actions">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {(v.users ?? []).map((u) => (
              <tr key={u.id}>
                <td className="mono">{u.extension}</td>
                <td data-label="Name">
                  <strong>{u.name}</strong>
                  {u.mobile && <span className="sub mono">{u.mobile}</span>}
                </td>
                <td data-label="Site">{u.site ?? "—"}</td>
                <td data-label="Team">{u.team ?? "—"}</td>
                <td data-label="Portal account" className="cell-wrap">
                  {u.portal_email ?? <span className="muted">None</span>}
                </td>
                <td data-label="Status">
                  {u.dnd ? <span className="pill small warn">Do not disturb</span> : u.forward_to ? <span className="pill small warn">Forwarded</span> : <span className="pill small ok">Ringing</span>}
                </td>
                <td className="actions">
                  <RowActions
                    label={`${u.name}, extension ${u.extension}`}
                    items={[
                      { label: "Move", onSelect: () => setMoving({ id: u.id, site: u.site_id ?? "", team: u.team_id ?? "", extension: u.extension }) },
                      { label: "Remove", danger: true, onSelect: () => propose([{ op: "remove_user", user: u.id }], `remove ${u.name}`) },
                    ]}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {moving && (
        <form className="form voice-inline" onSubmit={move} aria-label="Move a person">
          <h3 className="voice-sub wide">Move {(v.users ?? []).find((x) => x.id === moving.id)?.name}</h3>
          <Field label="Site">
            <select value={moving.site} onChange={(e) => setMoving({ ...moving, site: e.target.value })}>
              {sites.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Team">
            <select value={moving.team} onChange={(e) => setMoving({ ...moving, team: e.target.value })}>
              <option value="">No team</option>
              {teams.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Extension">
            <input value={moving.extension} onChange={(e) => setMoving({ ...moving, extension: e.target.value })} inputMode="numeric" pattern="\d{2,6}" />
          </Field>
          <p className="muted small wide">The emergency address changes to the new site's address, for the person and their numbers.</p>
          <div className="actions wide">
            <button className="button">Check move</button>{" "}
            <button type="button" className="button secondary" onClick={() => setMoving(null)}>
              Cancel
            </button>
          </div>
        </form>
      )}
      <h3 className="voice-sub">Add a person</h3>
      <form className="form" onSubmit={submit}>
        <Field label="Name">
          <input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required maxLength={120} />
        </Field>
        <Field label="Extension (blank: next free)">
          <input value={f.extension} onChange={(e) => setF({ ...f, extension: e.target.value })} inputMode="numeric" pattern="\d{2,6}" />
        </Field>
        <Field label="Site">
          <select value={f.site} onChange={(e) => setF({ ...f, site: e.target.value })} required>
            {sites.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Team">
          <select value={f.team} onChange={(e) => setF({ ...f, team: e.target.value })}>
            <option value="">No team</option>
            {teams.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Email (for voicemail)">
          <input type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} />
        </Field>
        <Field label="Mobile">
          <input type="tel" value={f.mobile} onChange={(e) => setF({ ...f, mobile: e.target.value })} placeholder="+1 868 …" />
        </Field>
        <Field label="Portal account email (for self-service)">
          <input type="email" value={f.portal_email} onChange={(e) => setF({ ...f, portal_email: e.target.value })} />
        </Field>
        <div className="actions wide">
          <button className="button" disabled={sites.length === 0}>
            Check new person
          </button>
        </div>
      </form>
    </Card>
  );
}

function targets(v: VoiceOverview, type: string): { id: string; label: string }[] {
  if (type === "user") return (v.users ?? []).map((u) => ({ id: u.id, label: `${u.extension} ${u.name}` }));
  if (type === "ring_group") return (v.ring_groups ?? []).map((g) => ({ id: g.id, label: `${g.extension} ${g.name}` }));
  if (type === "queue") return (v.queues ?? []).map((q) => ({ id: q.id, label: `${q.extension} ${q.name}` }));
  if (type === "menu") return (v.menus ?? []).map((m) => ({ id: m.id, label: `${m.extension} ${m.name}` }));
  return [];
}

function targetName(v: VoiceOverview, type: string, id: string | null | undefined): string {
  if (type === "ai") return "AI agent";
  const t = targets(v, type).find((x) => x.id === id);
  return t ? `${TARGET_LABEL[type]} ${t.label}` : TARGET_LABEL[type] ?? type;
}

function TargetPicker({ v, value, onChange, label }: { v: VoiceOverview; value: { type: string; id: string }; onChange: (t: { type: string; id: string }) => void; label: string }) {
  const list = targets(v, value.type);
  return (
    <>
      <Field label={`${label}: goes to`}>
        <select value={value.type} onChange={(e) => onChange({ type: e.target.value, id: targets(v, e.target.value)[0]?.id ?? "" })}>
          {Object.entries(TARGET_LABEL).map(([k, l]) => (
            <option key={k} value={k}>
              {l}
            </option>
          ))}
        </select>
      </Field>
      {list.length > 0 && (
        <Field label={`${label}: which`}>
          <select value={value.id} onChange={(e) => onChange({ ...value, id: e.target.value })}>
            {list.map((t) => (
              <option key={t.id} value={t.id}>
                {t.label}
              </option>
            ))}
          </select>
        </Field>
      )}
    </>
  );
}

function Numbers({ v, propose }: { v: VoiceOverview; propose: Propose }) {
  const [t, setT] = useState({ type: "none", id: "" });
  const [routing, setRouting] = useState<{ id: string; type: string; target: string } | null>(null);
  return (
    <Card
      title="Numbers"
      note={!v.provider.live && <span className="tag">Simulated provider: test numbers +1 868 555 01xx</span>}
    >
      <div className="table-wrap">
        <table className="paths dt stack">
          <thead>
            <tr>
              <th scope="col">Number</th>
              <th scope="col">Status</th>
              <th scope="col">Rings</th>
              <th scope="col">Emergency address</th>
              <th scope="col" className="actions">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {(v.numbers ?? []).map((n) => (
              <tr key={n.id}>
                <td className="mono">
                  {n.e164}
                  {n.source === "ported" && <span className="sub">Ported</span>}
                </td>
                <td data-label="Status">
                  <span className={`pill small ${n.status === "active" ? "ok" : "warn"}`}>{n.status === "active" ? "Active" : n.status === "porting" ? "Porting" : "Pending"}</span>
                </td>
                <td data-label="Rings">{targetName(v, n.target_type, n.target_id)}</td>
                <td data-label="Emergency address" className="cell-wrap">
                  {n.emergency_address?.address_line1 ? `${n.emergency_address.address_line1}, ${n.emergency_address.city}` : <span className="muted">Not set</span>}
                </td>
                <td className="actions">
                  <RowActions
                    label={`number ${n.e164}`}
                    items={[
                      { label: "Change where it rings", onSelect: () => setRouting({ id: n.id, type: n.target_type, target: n.target_id ?? "" }) },
                      { label: "Remove number", danger: true, onSelect: () => propose([{ op: "remove_number", number: n.id }], `remove ${n.e164}`) },
                    ]}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {routing && (
        <form
          className="form voice-inline"
          onSubmit={(e) => {
            e.preventDefault();
            propose([{ op: "assign_number", number: routing.id, target_type: routing.type, target: routing.target }], "route a number");
            setRouting(null);
          }}
        >
          <TargetPicker v={v} label="Number" value={{ type: routing.type, id: routing.target }} onChange={(x) => setRouting({ ...routing, type: x.type, target: x.id })} />
          <div className="actions wide">
            <button className="button">Check</button>{" "}
            <button type="button" className="button secondary" onClick={() => setRouting(null)}>
              Cancel
            </button>
          </div>
        </form>
      )}
      <h3 className="voice-sub">Add a number</h3>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          propose([{ op: "add_number", target_type: t.type, target: t.id }], "add a number");
        }}
      >
        <TargetPicker v={v} label="New number" value={t} onChange={setT} />
        <div className="actions wide">
          <button className="button">Check new number</button>
        </div>
      </form>
      <p className="muted small">To bring numbers from your current provider, place an order with a port (Orders).</p>
    </Card>
  );
}

function Devices({ base, v, propose }: { base: string; v: VoiceOverview; propose: Propose }) {
  const [f, setF] = useState({ user: "", kind: "desk", mac: "", model: "" });
  const [link, setLink] = useState<{ device: string; url: string; kind: string } | null>(null);
  const act = useAction();
  const issue = (id: string) =>
    act.run(async () => {
      const r = await api<{ kind: string; setup_url?: string; join_link?: string }>(`${base}/voice/devices/${id}/link`, { method: "POST" });
      setLink({ device: id, url: r.setup_url ?? r.join_link ?? "", kind: r.kind });
    });
  return (
    <Card title="Desk phones and softphones">
      <p className="muted small">Desk phones set themselves up from their MAC address. Softphones join with a sign-in link, which is also the text for a QR code.</p>
      <ErrorNote error={act.error} />
      {link && (
        <p className="voice-link" role="status">
          {link.kind === "desk" ? "Provisioning URL for the phone" : "Sign-in link (QR code text)"}, shown once: <code className="voice-secret">{link.url}</code>{" "}
          <button className="button secondary small" onClick={() => setLink(null)}>
            Hide
          </button>
          {link.kind !== "desk" && <QrCode text={link.url} />}
        </p>
      )}
      <div className="table-wrap">
        <table className="paths dt stack">
          <thead>
            <tr>
              <th scope="col">Device</th>
              <th scope="col">Person</th>
              <th scope="col">Status</th>
              <th scope="col" className="actions">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {(v.devices ?? []).map((d) => (
              <tr key={d.id}>
                <td>
                  {d.kind === "desk" ? "Desk phone" : "Softphone"}
                  {d.mac && <span className="sub mono">{d.mac}</span>}
                </td>
                <td data-label="Person">{d.extension ? `${d.extension} ${d.user_name}` : "—"}</td>
                <td data-label="Status">
                  <span className={`pill small ${d.status === "provisioned" ? "ok" : "warn"}`}>{d.status === "provisioned" ? "Set up" : "Waiting"}</span>
                </td>
                <td className="actions">
                  <RowActions
                    label={`device ${d.mac ?? d.id}`}
                    disabled={act.busy}
                    items={[
                      { label: d.kind === "desk" ? "New provisioning URL" : "New sign-in link", onSelect: () => issue(d.id) },
                      { label: "Remove", danger: true, onSelect: () => propose([{ op: "remove_device", device: d.id }], "remove a device") },
                    ]}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h3 className="voice-sub">Add a device</h3>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          const op: Op = { op: "add_device", user: f.user || (v.users ?? [])[0]?.id, kind: f.kind };
          if (f.kind === "desk") Object.assign(op, { mac: f.mac, model: f.model });
          propose([op], f.kind === "desk" ? "add a desk phone" : "add a softphone");
        }}
      >
        <Field label="Person">
          <select value={f.user} onChange={(e) => setF({ ...f, user: e.target.value })}>
            {(v.users ?? []).map((u) => (
              <option key={u.id} value={u.id}>
                {u.extension} {u.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Kind">
          <select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}>
            <option value="desk">Desk phone</option>
            <option value="softphone">Softphone</option>
          </select>
        </Field>
        {f.kind === "desk" && (
          <>
            <Field label="MAC address">
              <input value={f.mac} onChange={(e) => setF({ ...f, mac: e.target.value })} required placeholder="00:15:65:aa:bb:cc" />
            </Field>
            <Field label="Model">
              <input value={f.model} onChange={(e) => setF({ ...f, model: e.target.value })} maxLength={60} />
            </Field>
          </>
        )}
        <div className="actions wide">
          <button className="button" disabled={!(v.users ?? []).length}>
            Check new device
          </button>
        </div>
      </form>
    </Card>
  );
}

// ---- call routing -------------------------------------------------------------------------

export function Routing({ v, propose }: { v: VoiceOverview; propose: Propose }) {
  return (
    <>
      <GroupCard kind="ring_group" v={v} propose={propose} />
      <GroupCard kind="queue" v={v} propose={propose} />
      <HoursCard v={v} propose={propose} />
      <MenusCard v={v} propose={propose} />
      <AiRulesCard v={v} propose={propose} />
    </>
  );
}

function Members({ v, value, onChange }: { v: VoiceOverview; value: string[]; onChange: (m: string[]) => void }) {
  return (
    <fieldset className="wide">
      <legend>Who rings</legend>
      <div className="voice-checks">
        {(v.users ?? []).map((u) => (
          <label key={u.id} className="check">
            <input type="checkbox" checked={value.includes(u.id)} onChange={(e) => onChange(e.target.checked ? [...value, u.id] : value.filter((x) => x !== u.id))} />
            {u.extension} {u.name}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

/** Plain words for how a group or queue offers a call; the stored value keeps the phone system's name. */
const OFFER_WORD: Record<string, string> = {
  simultaneous: "All at once",
  sequential: "One after another",
  "longest-idle-agent": "Whoever has waited longest",
  "ring-all": "Everyone at once",
  "round-robin": "Take turns",
};

function GroupCard({ kind, v, propose }: { kind: "ring_group" | "queue"; v: VoiceOverview; propose: Propose }) {
  const rows = kind === "ring_group" ? v.ring_groups ?? [] : v.queues ?? [];
  const label = kind === "ring_group" ? "Ring groups" : "Queues";
  const blank = { id: "", name: "", extension: "", strategy: kind === "ring_group" ? "simultaneous" : "longest-idle-agent", members: [] as string[], n: kind === "ring_group" ? 20 : 300 };
  const [f, setF] = useState(blank);
  const names = (ids: string[]) =>
    ids
      .map((id) => (v.users ?? []).find((u) => u.id === id))
      .filter(Boolean)
      .map((u) => u!.extension)
      .join(", ");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const op: Op = { op: kind === "ring_group" ? "save_ring_group" : "save_queue", name: f.name, extension: f.extension, strategy: f.strategy, members: f.members };
    if (f.id) op.id = f.id;
    if (kind === "ring_group") op.ring_seconds = f.n;
    else op.max_wait_s = f.n;
    propose([op], `${f.id ? "change" : "add"} ${f.name}`);
    setF(blank);
  };
  return (
    <Card title={label}>
      <div className="table-wrap">
        <table className="paths dt stack">
          <thead>
            <tr>
              <th scope="col">Ext</th>
              <th scope="col">Name</th>
              <th scope="col">How</th>
              <th scope="col">Members</th>
              <th scope="col" className="actions">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((g) => (
              <tr key={g.id}>
                <td className="mono">{g.extension}</td>
                <td data-label="Name">{g.name}</td>
                <td data-label="How">{OFFER_WORD[g.strategy] ?? g.strategy}</td>
                <td data-label="Members" className="mono cell-wrap">
                  {names(g.members) || "—"}
                </td>
                <td className="actions">
                  <RowActions
                    label={`${g.name}`}
                    items={[
                      { label: "Edit", onSelect: () => setF({ id: g.id, name: g.name, extension: g.extension, strategy: g.strategy, members: g.members, n: "ring_seconds" in g ? g.ring_seconds : g.max_wait_s }) },
                      { label: "Delete", danger: true, onSelect: () => propose([{ op: kind === "ring_group" ? "delete_ring_group" : "delete_queue", id: g.id }], `delete ${g.name}`) },
                    ]}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h3 className="voice-sub">{f.id ? `Edit ${f.name}` : `Add a ${kind === "ring_group" ? "ring group" : "queue"}`}</h3>
      <form className="form" onSubmit={submit}>
        <Field label="Name">
          <input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required maxLength={80} />
        </Field>
        <Field label="Extension">
          <input value={f.extension} onChange={(e) => setF({ ...f, extension: e.target.value })} required inputMode="numeric" pattern="\d{2,6}" />
        </Field>
        <Field label="How calls are offered">
          <select value={f.strategy} onChange={(e) => setF({ ...f, strategy: e.target.value })}>
            {(kind === "ring_group" ? ["simultaneous", "sequential"] : ["longest-idle-agent", "ring-all", "round-robin"]).map((s) => (
              <option key={s} value={s}>
                {OFFER_WORD[s] ?? s}
              </option>
            ))}
          </select>
        </Field>
        <Field label={kind === "ring_group" ? "Ring for (seconds)" : "Longest wait (seconds)"}>
          <input type="number" value={f.n} onChange={(e) => setF({ ...f, n: Number(e.target.value) })} min={kind === "ring_group" ? 5 : 30} max={kind === "ring_group" ? 120 : 3600} />
        </Field>
        <Members v={v} value={f.members} onChange={(m) => setF({ ...f, members: m })} />
        <div className="actions wide">
          <button className="button">Check</button>
          {f.id && (
            <button type="button" className="button secondary" onClick={() => setF(blank)}>
              Cancel
            </button>
          )}
        </div>
      </form>
    </Card>
  );
}

function HoursCard({ v, propose }: { v: VoiceOverview; propose: Propose }) {
  const [f, setF] = useState({ id: "", name: "", days: ["mon", "tue", "wed", "thu", "fri"], open: "08:00", close: "17:00", holidays: "" });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const schedule = Object.fromEntries(f.days.map((d) => [d, [[f.open, f.close]]]));
    const holidays = f.holidays.split(/[\s,]+/).filter(Boolean);
    const op: Op = { op: "save_hours", name: f.name, schedule, holidays };
    if (f.id) op.id = f.id;
    propose([op], `${f.id ? "change" : "add"} hours ${f.name}`);
  };
  return (
    <Card title="Business hours">
      <ul className="plain-list">
        {(v.hours ?? []).map((h) => (
          <li key={h.id} className="row-between">
            <span>
              <strong>{h.name}</strong>{" "}
              <span className="muted small">
                {Object.entries(h.schedule)
                  .map(([d, spans]) => `${d} ${spans.map((s) => s.join("–")).join(", ")}`)
                  .join("; ")}
                {h.holidays.length ? `; closed ${h.holidays.join(", ")}` : ""}
              </span>
            </span>
            <span>
              <button
                className="button secondary small"
                onClick={() => {
                  const first = Object.values(h.schedule)[0]?.[0] ?? ["08:00", "17:00"];
                  setF({ id: h.id, name: h.name, days: Object.keys(h.schedule), open: first[0], close: first[1], holidays: h.holidays.join(", ") });
                }}
              >
                Edit
              </button>{" "}
              <button className="button danger-text small" onClick={() => propose([{ op: "delete_hours", id: h.id }], `delete hours ${h.name}`)}>
                Delete
              </button>
            </span>
          </li>
        ))}
      </ul>
      <form className="form" onSubmit={submit}>
        <Field label="Name">
          <input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required maxLength={80} />
        </Field>
        <Field label="Opens">
          <input type="time" value={f.open} onChange={(e) => setF({ ...f, open: e.target.value })} required />
        </Field>
        <Field label="Closes">
          <input type="time" value={f.close} onChange={(e) => setF({ ...f, close: e.target.value })} required />
        </Field>
        <fieldset className="wide">
          <legend>Open on</legend>
          <div className="voice-checks">
            {DAYS.map(([d, l]) => (
              <label key={d} className="check">
                <input type="checkbox" checked={f.days.includes(d)} onChange={(e) => setF({ ...f, days: e.target.checked ? [...f.days, d] : f.days.filter((x) => x !== d) })} />
                {l}
              </label>
            ))}
          </div>
        </fieldset>
        <Field label="Closed on (dates, e.g. 2026-12-25)">
          <input value={f.holidays} onChange={(e) => setF({ ...f, holidays: e.target.value })} />
        </Field>
        <div className="actions wide">
          <button className="button">Check hours</button>
        </div>
      </form>
    </Card>
  );
}

function MenusCard({ v, propose }: { v: VoiceOverview; propose: Propose }) {
  const blank = { id: "", name: "", extension: "", greeting: "", hours: "", options: { "1": { type: "none", id: "" }, "2": { type: "none", id: "" }, "3": { type: "none", id: "" } } as Record<string, { type: string; id: string }>, closed: { type: "ai", id: "" } };
  const [f, setF] = useState(blank);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const options = Object.fromEntries(Object.entries(f.options).filter(([, t]) => t.type !== "none").map(([k, t]) => [k, { type: t.type, id: t.id }]));
    const op: Op = { op: "save_menu", name: f.name, extension: f.extension, greeting: f.greeting, options, hours: f.hours || null, closed_target: f.hours ? f.closed : null };
    if (f.id) op.id = f.id;
    propose([op], `${f.id ? "change" : "add"} menu ${f.name}`);
  };
  return (
    <Card title="Menus">
      <ul className="plain-list">
        {(v.menus ?? []).map((m) => (
          <li key={m.id} className="row-between">
            <span>
              <span className="mono">{m.extension}</span> <strong>{m.name}</strong>{" "}
              <span className="muted small">
                {Object.entries(m.options)
                  .map(([k, t]) => `${k}: ${targetName(v, t.type, t.id)}`)
                  .join("; ")}
              </span>
            </span>
            <span>
              <button
                className="button secondary small"
                onClick={() =>
                  setF({
                    id: m.id,
                    name: m.name,
                    extension: m.extension,
                    greeting: m.greeting,
                    hours: m.hours_id ?? "",
                    options: { ...blank.options, ...Object.fromEntries(Object.entries(m.options).map(([k, t]) => [k, { type: t.type, id: t.id ?? "" }])) },
                    closed: { type: m.closed_target.type ?? "ai", id: m.closed_target.id ?? "" },
                  })
                }
              >
                Edit
              </button>{" "}
              <button className="button danger-text small" onClick={() => propose([{ op: "delete_menu", id: m.id }], `delete menu ${m.name}`)}>
                Delete
              </button>
            </span>
          </li>
        ))}
      </ul>
      <form className="form" onSubmit={submit}>
        <Field label="Name">
          <input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required maxLength={80} />
        </Field>
        <Field label="Extension">
          <input value={f.extension} onChange={(e) => setF({ ...f, extension: e.target.value })} required inputMode="numeric" pattern="\d{2,6}" />
        </Field>
        <label className="wide">
          Greeting (read out to callers)
          <textarea value={f.greeting} onChange={(e) => setF({ ...f, greeting: e.target.value })} maxLength={1000} rows={2} />
        </label>
        {Object.keys(f.options).map((k) => (
          <TargetPicker key={k} v={v} label={`Key ${k}`} value={f.options[k]} onChange={(t) => setF({ ...f, options: { ...f.options, [k]: t } })} />
        ))}
        <Field label="Business hours">
          <select value={f.hours} onChange={(e) => setF({ ...f, hours: e.target.value })}>
            <option value="">Always open</option>
            {(v.hours ?? []).map((h) => (
              <option key={h.id} value={h.id}>
                {h.name}
              </option>
            ))}
          </select>
        </Field>
        {f.hours && <TargetPicker v={v} label="When closed" value={f.closed} onChange={(t) => setF({ ...f, closed: t })} />}
        <div className="actions wide">
          <button className="button">Check menu</button>
        </div>
      </form>
    </Card>
  );
}

function AiRulesCard({ v, propose }: { v: VoiceOverview; propose: Propose }) {
  const [f, setF] = useState({ name: "", condition: "after_hours", hours: "", number: "", fallback: "voicemail" });
  const COND: Record<string, string> = { after_hours: "Outside business hours", no_answer: "When nobody answers", busy: "When everyone is busy", always: "Always" };
  return (
    <Card title="AI agent rules">
      <p className="muted small">
        The AI agent answers calls when a rule says so. If it can't answer, calls go to the fallback, so calling never depends on the AI services.
      </p>
      <ul className="plain-list">
        {(v.ai_rules ?? []).map((r) => (
          <li key={r.id} className="row-between">
            <span>
              <strong>{r.name}</strong> <span className="muted small">{COND[r.condition]}; fallback {r.fallback.replace("_", " ")}</span>{" "}
              {!r.enabled && <span className="pill small off">Off</span>}
            </span>
            <button className="button danger-text small" onClick={() => propose([{ op: "delete_ai_rule", id: r.id }], `delete rule ${r.name}`)}>
              Delete
            </button>
          </li>
        ))}
      </ul>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          const op: Op = { op: "save_ai_rule", name: f.name, condition: f.condition, fallback: f.fallback };
          if (f.hours) op.hours = f.hours;
          if (f.number) op.number = f.number;
          propose([op], `add rule ${f.name}`);
        }}
      >
        <Field label="Name">
          <input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required maxLength={80} placeholder="After hours to the AI" />
        </Field>
        <Field label="When">
          <select value={f.condition} onChange={(e) => setF({ ...f, condition: e.target.value })}>
            {Object.entries(COND).map(([k, l]) => (
              <option key={k} value={k}>
                {l}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Business hours">
          <select value={f.hours} onChange={(e) => setF({ ...f, hours: e.target.value })} required={f.condition === "after_hours"}>
            <option value="">—</option>
            {(v.hours ?? []).map((h) => (
              <option key={h.id} value={h.id}>
                {h.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Number">
          <select value={f.number} onChange={(e) => setF({ ...f, number: e.target.value })}>
            <option value="">Every number</option>
            {(v.numbers ?? []).map((n) => (
              <option key={n.id} value={n.id}>
                {n.e164}
              </option>
            ))}
          </select>
        </Field>
        <Field label="If the AI agent is unavailable">
          <select value={f.fallback} onChange={(e) => setF({ ...f, fallback: e.target.value })}>
            <option value="voicemail">Voicemail</option>
            <option value="ring_group">Ring group</option>
            <option value="queue">Queue</option>
          </select>
        </Field>
        <div className="actions wide">
          <button className="button">Check rule</button>
        </div>
      </form>
    </Card>
  );
}

// ---- scheduled changes, history and bulk ------------------------------------------------------

export function Changes({ base, v, reload }: { base: string; v: VoiceOverview; reload: () => void }) {
  return (
    <>
      <ScheduledCard base={base} v={v} reload={reload} />
      <HistoryCard base={base} reload={reload} />
      <BulkCard base={base} reload={reload} />
    </>
  );
}

function ScheduledCard({ base, v, reload }: { base: string; v: VoiceOverview; reload: () => void }) {
  const act = useAction();
  const STATUS: Record<string, [string, string]> = { scheduled: ["warn", "Scheduled"], applied: ["ok", "Applied"], failed: ["bad", "Failed"], cancelled: ["off", "Cancelled"] };
  const rows = v.scheduled ?? [];
  return (
    <Card title="Scheduled changes">
      <ErrorNote error={act.error} />
      {rows.length === 0 && <p className="muted">Nothing scheduled. Choose "At a set time" when you save a change.</p>}
      <ul className="plain-list">
        {rows.map((c) => (
          <li key={c.id} className="row-between">
            <span>
              <span className={`pill small ${STATUS[c.status][0]}`}>{STATUS[c.status][1]}</span> {c.summary}{" "}
              <span className="muted small">
                {new Date(c.run_at).toLocaleString("en-GB")} by {c.created_by.replace("user:", "")}
                {c.version ? `, version ${c.version}` : ""}
                {c.error ? `: ${c.error}` : ""}
              </span>
            </span>
            {c.status === "scheduled" && (
              <button
                className="button secondary small"
                disabled={act.busy}
                onClick={() =>
                  act.run(async () => {
                    await api(`${base}/voice/changes/${c.id}/cancel`, { method: "POST" });
                    reload();
                  })
                }
              >
                Cancel
              </button>
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}

function HistoryCard({ base, reload }: { base: string; reload: () => void }) {
  const h = useApi<Version[]>(`${base}/voice/versions`, 30_000);
  const act = useAction();
  const [shown, setShown] = useState<{ version: number; diff: ChangeResult["diff"]; rollback?: boolean } | null>(null);
  const view = (n: number) =>
    act.run(async () => {
      const d = await api<{ diff: ChangeResult["diff"] }>(`${base}/voice/versions/${n}`);
      setShown({ version: n, diff: d.diff });
    });
  const previewRollback = (n: number) =>
    act.run(async () => {
      const d = await api<{ diff: ChangeResult["diff"] }>(`${base}/voice/versions/${n}/rollback`, { method: "POST", body: JSON.stringify({ preview: true }) });
      setShown({ version: n, diff: d.diff, rollback: true });
    });
  const rollback = (n: number) =>
    act.run(async () => {
      await api(`${base}/voice/versions/${n}/rollback`, { method: "POST", body: JSON.stringify({}) });
      setShown(null);
      h.reload();
      reload();
    });
  return (
    <Card title="History and roll back">
      <p className="muted small">
        Every change is a version. Rolling back restores call routing (ring groups, queues, hours, menus, AI rules and where numbers ring) as it was.
        People, numbers and devices change the bill, so they are added or removed as their own change.
      </p>
      <ErrorNote error={h.error ?? act.error} />
      {shown && (
        <div className="voice-panel" role="region" aria-label={`Version ${shown.version}`}>
          <h3 className="voice-sub">{shown.rollback ? `Rolling back to version ${shown.version} would change` : `Version ${shown.version} changed`}</h3>
          <DiffList diff={shown.diff} />
          <div className="actions">
            {shown.rollback && (
              <button className="button" disabled={act.busy || shown.diff.length === 0} onClick={() => rollback(shown.version)}>
                Roll back
              </button>
            )}{" "}
            <button className="button secondary" onClick={() => setShown(null)}>
              Close
            </button>
          </div>
        </div>
      )}
      <div className="table-wrap">
        <table className="paths dt stack">
          <thead>
            <tr>
              <th scope="col">Version</th>
              <th scope="col">What</th>
              <th scope="col">By</th>
              <th scope="col">When</th>
              <th scope="col" className="actions">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {(h.data ?? []).map((r) => (
              <tr key={r.version}>
                <td className="mono">v{r.version}</td>
                <td data-label="What" className="cell-wrap">
                  {r.summary} <span className="muted small">({r.kind})</span>
                </td>
                <td data-label="By">{r.created_by.replace("user:", "")}</td>
                <td data-label="When">{when(r.created_at)}</td>
                <td className="actions">
                  <RowActions
                    label={`version ${r.version}`}
                    disabled={act.busy}
                    items={[
                      { label: "What changed", onSelect: () => view(r.version) },
                      { label: "Roll back to this", onSelect: () => previewRollback(r.version) },
                    ]}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

interface BulkOut {
  rows: number;
  errors: { row: number; error: string }[];
  applied: boolean;
  version?: number;
  price_impact?: PriceImpact;
  diff?: ChangeResult["diff"];
}

function BulkCard({ base, reload }: { base: string; reload: () => void }) {
  const [text, setText] = useState("");
  const [out, setOut] = useState<BulkOut | null>(null);
  const act = useAction();
  const send = (apply: boolean) =>
    act.run(async () => {
      const body: Record<string, unknown> = { csv: text, apply };
      if (apply && out?.price_impact) body.accepted_price = { monthly_delta: out.price_impact.monthly_delta, one_time: out.price_impact.one_time };
      const r = await api<BulkOut>(`${base}/voice/bulk`, { method: "POST", body: JSON.stringify(body) });
      setOut(r);
      if (r.applied) reload();
    });
  const onFile = (f: File | undefined) => {
    if (!f) return;
    f.text().then((t) => {
      setText(t);
      setOut(null);
    });
  };
  return (
    <Card title="Bulk changes from a spreadsheet">
      <p className="muted small">
        Save your spreadsheet as CSV with one change per row. Every row is checked first; nothing applies unless every row passes.{" "}
        <button className="button secondary small" onClick={() => act.run(() => download(`${base}/voice/bulk/template`, "voice-bulk-template.csv"))}>
          Download template
        </button>
      </p>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          send(false);
        }}
      >
        <label className="wide">
          CSV file
          <input type="file" accept=".csv,text/csv" onChange={(e) => onFile(e.target.files?.[0])} />
        </label>
        <label className="wide">
          Or paste it here
          <textarea
            className="mono"
            rows={6}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              setOut(null);
            }}
            placeholder={"action,name,extension,site\nadd_user,Ana Lee,201,Port of Spain"}
          />
        </label>
        <div className="actions wide">
          <button className="button" disabled={!text || act.busy}>
            Check every row
          </button>
        </div>
      </form>
      <ErrorNote error={act.error} />
      {out && (
        <div className="voice-panel" aria-live="polite">
          {out.applied ? (
            <p className="pill ok">Applied as version {out.version}.</p>
          ) : out.errors.length ? (
            <>
              <p className="pill bad">
                {out.errors.length} of {out.rows} rows need fixing. Nothing was applied.
              </p>
              <ul className="plain-list">
                {out.errors.map((e, i) => (
                  <li key={i}>
                    <span className="mono">Row {e.row}</span>: {e.error}
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <>
              <p className="pill ok">All {out.rows} rows pass.</p>
              {out.diff && <DiffList diff={out.diff} />}
              {out.price_impact && <PriceLines price={out.price_impact} />}
              <button className="button" disabled={act.busy} onClick={() => send(true)}>
                Apply all rows
              </button>
            </>
          )}
        </div>
      )}
    </Card>
  );
}

// ---- access -----------------------------------------------------------------------------------

interface Perms {
  configured: boolean;
  people: { user_id: string; email: string; voice_admin: boolean; spend: boolean }[];
}

export function Access({ base, v, reload }: { base: string; v: VoiceOverview; reload: () => void }) {
  const p = useApi<Perms>(`${base}/voice/permissions`, 0);
  const [draft, setDraft] = useState<Perms["people"] | null>(null);
  const act = useAction();
  const people = draft ?? p.data?.people ?? [];
  const set = (id: string, patch: Partial<Perms["people"][number]>) => setDraft(people.map((x) => (x.user_id === id ? { ...x, ...patch } : x)));
  const save = () =>
    act.run(async () => {
      await api(`${base}/voice/permissions`, { method: "PUT", body: JSON.stringify({ people }) });
      setDraft(null);
      p.reload();
      reload();
    });
  const setPolicy = (value: string) =>
    act.run(async () => {
      await api(`${base}/voice/policy`, { method: "PUT", body: JSON.stringify({ recording_access: value }) });
      reload();
    });
  return (
    <>
      <Card title="Voice admins and spend permission">
        <p className="muted small">
          Voice admins change the phone system. Changes that alter the bill, and orders, also need spend permission.
          {p.data && !p.data.configured && " Until you name someone here, everyone in your company counts as a voice admin with spend permission."}
        </p>
        <ErrorNote error={p.error ?? act.error} />
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Person</th>
                <th scope="col">Voice admin</th>
                <th scope="col">Spend permission</th>
              </tr>
            </thead>
            <tbody>
              {people.map((x) => (
                <tr key={x.user_id}>
                  <td>{x.email}</td>
                  <td data-label="Voice admin">
                    <input type="checkbox" aria-label={`${x.email} is a voice admin`} checked={x.voice_admin} onChange={(e) => set(x.user_id, { voice_admin: e.target.checked, spend: e.target.checked && x.spend })} />
                  </td>
                  <td data-label="Spend permission">
                    <input type="checkbox" aria-label={`${x.email} has spend permission`} checked={x.spend} disabled={!v.can_spend} onChange={(e) => set(x.user_id, { spend: e.target.checked, voice_admin: x.voice_admin || e.target.checked })} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="actions">
          <button className="button" disabled={!draft || act.busy} onClick={save}>
            Save
          </button>
        </div>
      </Card>
      <Card title="Call recordings">
        <label>
          Who can play recordings
          <select value={v.policy.recording_access} onChange={(e) => setPolicy(e.target.value)} disabled={act.busy}>
            <option value="own">Each person, their own calls</option>
            <option value="admins">Voice admins only</option>
            <option value="none">Nobody</option>
          </select>
        </label>
      </Card>
      <Card title="Phone system configuration">
        <p className="muted small">
          The PBX (FreeSWITCH) configuration is written from these settings after every change. Not live yet: there is no SIP provider account (phase
          3), so outside calls don't connect. Calls between extensions, menus and queues work once the PBX runs.
        </p>
        <p className="small">
          {v.pbx ? (
            <>
              Last written {when(v.pbx.rendered_at)}, fingerprint <span className="mono">{v.pbx.digest.slice(0, 12)}</span>
              {v.pbx.written_to ? "" : " (kept in the database; no PBX folder set)"}
            </>
          ) : (
            "Not written yet."
          )}
        </p>
      </Card>
    </>
  );
}
