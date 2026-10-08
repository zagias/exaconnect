import { useState, type FormEvent } from "react";
import { api, useApi } from "../../../api";
import { ErrorNote } from "../../../components";
import "../../identity.css";
import { Card, useAction } from "../../../ui";

interface Hours {
  weekday: number;
  opens: string;
  closes: string;
}
interface Location {
  id: string;
  name: string;
  country: string;
  timezone: string;
  address: string;
  is_primary: boolean;
  after_hours_team_id: string | null;
  hours: Hours[];
  now: { open: boolean | null; next_open: string | null; always_open?: boolean } | undefined;
}
interface Org {
  locations: Location[];
  brands: { id: string; name: string; location_id: string | null }[];
  holidays: { id: string; country: string; day: string; name: string }[];
  closures: { id: string; location_id: string | null; starts_at: string; ends_at: string; reason: string }[];
  teams: { id: string; name: string; location_id: string | null; brand_id: string | null }[];
}

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function hhmm(t: string): string {
  return t.slice(0, 5);
}

function stamp(iso: string): string {
  return new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

/** Locations, opening hours, holidays, closures and brands (ADR 0030). Service targets pause outside hours. */
export default function Organisation({ base }: { base: string }) {
  const org = useApi<Org>(`${base}/organisation`, 60_000);
  const act = useAction();
  const call = (path: string, init: RequestInit) =>
    act.run(async () => {
      await api(`${base}/organisation/${path}`, init);
      org.reload();
    });
  if (!org.data) return <ErrorNote error={org.error} />;
  const d = org.data;
  const locName = (id: string | null) => (id ? (d.locations.find((l) => l.id === id)?.name ?? "") : "Every location");

  return (
    <>
      <p className="muted small">
        Service targets count opening hours only: they pause at night, at weekends, on public holidays and during
        closures. A team works to its location&apos;s calendar; teams without one use the primary location. With no
        locations, targets run around the clock.
      </p>
      <ErrorNote error={act.error} />
      <Card title="Locations and opening hours">
        {d.locations.length === 0 && <p className="muted">No locations yet.</p>}
        {d.locations.map((l) => (
          <LocationRow key={l.id} loc={l} teams={d.teams} busy={act.busy} call={call} />
        ))}
        <NewLocation call={call} busy={act.busy} />
      </Card>
      <Card title="Teams">
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Team</th>
                <th scope="col">Location (calendar)</th>
                <th scope="col">Brand</th>
              </tr>
            </thead>
            <tbody>
              {d.teams.map((t) => (
                <tr key={t.id}>
                  <td>{t.name}</td>
                  <td data-label="Location">
                    <select
                      aria-label={`Location for ${t.name}`}
                      value={t.location_id ?? ""}
                      disabled={act.busy}
                      onChange={(e) =>
                        call(`teams/${t.id}`, {
                          method: "PUT",
                          body: JSON.stringify({ location_id: e.target.value || null, brand_id: t.brand_id }),
                        })
                      }
                    >
                      <option value="">Primary location</option>
                      {d.locations.map((l) => (
                        <option key={l.id} value={l.id}>
                          {l.name}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td data-label="Brand">
                    <select
                      aria-label={`Brand for ${t.name}`}
                      value={t.brand_id ?? ""}
                      disabled={act.busy}
                      onChange={(e) =>
                        call(`teams/${t.id}`, {
                          method: "PUT",
                          body: JSON.stringify({ location_id: t.location_id, brand_id: e.target.value || null }),
                        })
                      }
                    >
                      <option value="">No brand</option>
                      {d.brands.map((b) => (
                        <option key={b.id} value={b.id}>
                          {b.name}
                        </option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
              {d.teams.length === 0 && (
                <tr>
                  <td colSpan={3} className="muted">
                    No teams yet. Add them under Teams and people.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="Brands">
        <ul>
          {d.brands.map((b) => (
            <li key={b.id}>
              {b.name} <span className="muted small">{b.location_id ? locName(b.location_id) : ""}</span>{" "}
              <button className="link" disabled={act.busy} onClick={() => call(`brands/${b.id}`, { method: "DELETE" })}>
                Remove
              </button>
            </li>
          ))}
        </ul>
        <OneField label="New brand" placeholder="Brand name" busy={act.busy} onAdd={(name) => call("brands", { method: "POST", body: JSON.stringify({ name }) })} />
      </Card>
      <Card title="Public holidays">
        <p className="muted small">Enter your own list per country. A holiday closes every location in that country all day.</p>
        <ul>
          {d.holidays.map((h) => (
            <li key={h.id}>
              <span className="mono">{h.day}</span> {h.country} {h.name}{" "}
              <button className="link" disabled={act.busy} onClick={() => call(`holidays/${h.id}`, { method: "DELETE" })}>
                Remove
              </button>
            </li>
          ))}
        </ul>
        <NewHoliday busy={act.busy} call={call} />
      </Card>
      <Card title="Closures">
        <ul>
          {d.closures.map((c) => (
            <li key={c.id}>
              {locName(c.location_id)}: {stamp(c.starts_at)} to {stamp(c.ends_at)} {c.reason && `(${c.reason})`}{" "}
              <button className="link" disabled={act.busy} onClick={() => call(`closures/${c.id}`, { method: "DELETE" })}>
                Remove
              </button>
            </li>
          ))}
        </ul>
        <NewClosure busy={act.busy} call={call} locations={d.locations} />
      </Card>
    </>
  );
}

type Call = (path: string, init: RequestInit) => void;

function LocationRow({ loc, teams, busy, call }: { loc: Location; teams: Org["teams"]; busy: boolean; call: Call }) {
  const [hours, setHours] = useState(loc.hours.map((h) => ({ ...h, opens: hhmm(h.opens), closes: hhmm(h.closes) })));
  const dirty = JSON.stringify(hours) !== JSON.stringify(loc.hours.map((h) => ({ ...h, opens: hhmm(h.opens), closes: hhmm(h.closes) })));
  const status = loc.now?.always_open
    ? "Open all the time (no hours set)"
    : loc.now?.open
      ? "✓ Open now"
      : `Closed now${loc.now?.next_open ? `, opens ${stamp(loc.now.next_open)}` : ""}`;
  const save = (patch: Partial<Location>) =>
    call(`locations/${loc.id}`, {
      method: "PUT",
      body: JSON.stringify({
        name: loc.name,
        country: loc.country,
        timezone: loc.timezone,
        address: loc.address,
        is_primary: loc.is_primary,
        after_hours_team_id: loc.after_hours_team_id,
        ...patch,
      }),
    });
  return (
    <div className="identity-conn">
      <div className="identity-row" style={{ justifyContent: "space-between" }}>
        <h3>
          {loc.name} {loc.is_primary && <span className="muted small">Primary</span>}
        </h3>
        <span className={`identity-status ${loc.now?.open ? "on" : "off"}`}>{status}</span>
      </div>
      <p className="muted small">
        {loc.timezone}
        {loc.country ? ` · holidays for ${loc.country}` : " · no country set, so no public holidays apply"}
      </p>
      <div className="form">
        <label>
          After hours, conversations go to
          <select value={loc.after_hours_team_id ?? ""} disabled={busy} onChange={(e) => save({ after_hours_team_id: e.target.value || null })}>
            <option value="">Wait for opening (nobody assigned)</option>
            {teams.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </label>
        {!loc.is_primary && (
          <div className="actions">
            <button className="button secondary" disabled={busy} onClick={() => save({ is_primary: true })}>
              Make primary
            </button>
          </div>
        )}
      </div>
      <table className="paths dt">
        <caption className="muted small" style={{ textAlign: "left" }}>
          Opening hours (local time)
        </caption>
        <tbody>
          {hours.map((h, i) => (
            <tr key={i}>
              <td>
                <select aria-label="Day" value={h.weekday} onChange={(e) => setHours(hours.map((x, j) => (j === i ? { ...x, weekday: Number(e.target.value) } : x)))}>
                  {DAYS.map((dname, n) => (
                    <option key={n} value={n}>
                      {dname}
                    </option>
                  ))}
                </select>
              </td>
              <td>
                <input aria-label="Opens" type="time" value={h.opens} onChange={(e) => setHours(hours.map((x, j) => (j === i ? { ...x, opens: e.target.value } : x)))} />
              </td>
              <td>
                <input aria-label="Closes" type="time" value={h.closes} onChange={(e) => setHours(hours.map((x, j) => (j === i ? { ...x, closes: e.target.value } : x)))} />
              </td>
              <td>
                <button className="link" onClick={() => setHours(hours.filter((_, j) => j !== i))}>
                  Remove
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="actions">
        <button className="button secondary" onClick={() => setHours([...hours, { weekday: 0, opens: "08:00", closes: "16:30" }])}>
          Add hours
        </button>
        {hours.length === 0 && (
          <button
            className="button secondary"
            onClick={() => setHours([0, 1, 2, 3, 4].map((w) => ({ weekday: w, opens: "08:00", closes: "16:30" })))}
          >
            Weekdays 08:00 to 16:30
          </button>
        )}
        <button
          className="button"
          disabled={busy || !dirty}
          onClick={() => call(`locations/${loc.id}/hours`, { method: "PUT", body: JSON.stringify(hours) })}
        >
          Save hours
        </button>
        <button
          className="button secondary"
          disabled={busy}
          onClick={() => window.confirm(`Delete the location ${loc.name}?`) && call(`locations/${loc.id}`, { method: "DELETE" })}
        >
          Delete location
        </button>
      </div>
    </div>
  );
}

function NewLocation({ call, busy }: { call: Call; busy: boolean }) {
  const [f, setF] = useState({ name: "", country: "TT", timezone: "America/Port_of_Spain", address: "" });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    call("locations", { method: "POST", body: JSON.stringify(f) });
    setF({ ...f, name: "", address: "" });
  };
  return (
    <form className="form" onSubmit={submit}>
      <label>
        Location name
        <input value={f.name} required maxLength={120} onChange={(e) => setF({ ...f, name: e.target.value })} />
      </label>
      <label>
        Country (two letters)
        <input value={f.country} maxLength={2} onChange={(e) => setF({ ...f, country: e.target.value.toUpperCase() })} />
      </label>
      <label>
        Time zone
        <input value={f.timezone} maxLength={64} onChange={(e) => setF({ ...f, timezone: e.target.value })} />
      </label>
      <label>
        Address
        <input value={f.address} maxLength={500} onChange={(e) => setF({ ...f, address: e.target.value })} />
      </label>
      <div className="actions wide">
        <button className="button" disabled={busy || !f.name}>
          Add location
        </button>
      </div>
    </form>
  );
}

function OneField({ label, placeholder, busy, onAdd }: { label: string; placeholder: string; busy: boolean; onAdd: (v: string) => void }) {
  const [v, setV] = useState("");
  return (
    <form
      className="form"
      onSubmit={(e) => {
        e.preventDefault();
        onAdd(v);
        setV("");
      }}
    >
      <label>
        {label}
        <input value={v} placeholder={placeholder} maxLength={120} onChange={(e) => setV(e.target.value)} />
      </label>
      <div className="actions">
        <button className="button" disabled={busy || !v.trim()}>
          Add
        </button>
      </div>
    </form>
  );
}

function NewHoliday({ call, busy }: { call: Call; busy: boolean }) {
  const [f, setF] = useState({ country: "TT", day: "", name: "" });
  return (
    <form
      className="form"
      onSubmit={(e) => {
        e.preventDefault();
        call("holidays", { method: "POST", body: JSON.stringify(f) });
        setF({ ...f, day: "", name: "" });
      }}
    >
      <label>
        Country
        <input value={f.country} maxLength={2} required onChange={(e) => setF({ ...f, country: e.target.value.toUpperCase() })} />
      </label>
      <label>
        Date
        <input type="date" value={f.day} required onChange={(e) => setF({ ...f, day: e.target.value })} />
      </label>
      <label>
        Name
        <input value={f.name} maxLength={120} onChange={(e) => setF({ ...f, name: e.target.value })} />
      </label>
      <div className="actions wide">
        <button className="button" disabled={busy || !f.day}>
          Add holiday
        </button>
      </div>
    </form>
  );
}

function NewClosure({ call, busy, locations }: { call: Call; busy: boolean; locations: Location[] }) {
  const [f, setF] = useState({ location_id: "", starts_at: "", ends_at: "", reason: "" });
  return (
    <form
      className="form"
      onSubmit={(e) => {
        e.preventDefault();
        call("closures", {
          method: "POST",
          body: JSON.stringify({
            location_id: f.location_id || null,
            starts_at: new Date(f.starts_at).toISOString(),
            ends_at: new Date(f.ends_at).toISOString(),
            reason: f.reason,
          }),
        });
        setF({ location_id: "", starts_at: "", ends_at: "", reason: "" });
      }}
    >
      <label>
        Where
        <select value={f.location_id} onChange={(e) => setF({ ...f, location_id: e.target.value })}>
          <option value="">Every location</option>
          {locations.map((l) => (
            <option key={l.id} value={l.id}>
              {l.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        From
        <input type="datetime-local" required value={f.starts_at} onChange={(e) => setF({ ...f, starts_at: e.target.value })} />
      </label>
      <label>
        Until
        <input type="datetime-local" required value={f.ends_at} onChange={(e) => setF({ ...f, ends_at: e.target.value })} />
      </label>
      <label>
        Reason
        <input value={f.reason} maxLength={200} placeholder="Storm, staff day" onChange={(e) => setF({ ...f, reason: e.target.value })} />
      </label>
      <div className="actions wide">
        <button className="button" disabled={busy || !f.starts_at || !f.ends_at}>
          Add closure
        </button>
      </div>
    </form>
  );
}
