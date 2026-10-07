import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, type CustomerSettings, type StormSite } from "./api";
import { useAuth } from "./auth";

const KEY = "exa.customer";

interface CustomerCtx {
  customers: CustomerSettings[];
  current: CustomerSettings | null;
  select: (id: string) => void;
  reload: () => void;
}

const Ctx = createContext<CustomerCtx>({ customers: [], current: null, select: () => {}, reload: () => {} });
export const useCustomer = () => useContext(Ctx);

function readKey(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

/** The customer the signed-in user is acting for: their own, or the one an admin picked. */
export function CustomerProvider({ children }: { children: ReactNode }) {
  const [customers, setCustomers] = useState<CustomerSettings[]>([]);
  const [id, setId] = useState<string | null>(readKey());
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const { user } = useAuth();
  const own = user?.customer_id ?? null;

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api<CustomerSettings[]>("/customers/mine")
        .then((c) => !cancelled && setCustomers(c))
        .catch(() => {
          // An account limited to CommAI (a partner acting for a business, a
          // directory-provisioned person) can't read Connect's customer list:
          // it still acts for its own business.
          if (!cancelled && own)
            setCustomers([{ id: own, name: "", shadow_mode: false, storm_mode: false, storm_since: null, storm_by: null, storm_allow_bulk_sat: false, auto_prioritise: false, sites: [] }]);
        });
    load();
    const t = setInterval(load, 10_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [tick, own]);

  const select = (next: string) => {
    setId(next);
    try {
      localStorage.setItem(KEY, next);
    } catch {
      /* storage unavailable */
    }
  };
  const current = customers.find((c) => c.id === id) ?? customers[0] ?? null;
  return <Ctx.Provider value={{ customers, current, select, reload }}>{children}</Ctx.Provider>;
}

/** Switches Storm Mode for one site, after a confirmation. */
export function useStormToggle() {
  const { current, reload } = useCustomer();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toggle = async (site: StormSite, on: boolean) => {
    const question = on
      ? `Switch Storm Mode on for ${site.name}? Its satellite path is kept warm, and voice and business may use it if both terrestrial paths fail. Other sites are not affected.`
      : `Switch Storm Mode off for ${site.name}? Classes on satellite there move back to terrestrial paths.`;
    if (!window.confirm(question)) return;
    setBusy(true);
    setError(null);
    try {
      await api(`/sites/${site.id}/storm`, { method: "POST", body: JSON.stringify({ on }) });
      reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return { current, busy, error, toggle };
}

/** The Storm Mode control in the bar: coral while any site is on; opens a per-site list. */
export function StormSwitch() {
  const { current, busy, error, toggle } = useStormToggle();
  const [open, setOpen] = useState(false);
  if (!current) return null;
  const on = current.sites.filter((s) => s.storm_mode);
  const label = on.length === 0 ? "Storm Mode off" : on.length === 1 ? `Storm Mode: ${on[0].name}` : `Storm Mode: ${on.length} sites`;
  return (
    <div className="storm-control">
      <button
        className="storm-switch"
        aria-pressed={on.length > 0}
        aria-expanded={open}
        aria-label={label}
        title={label}
        onClick={() => setOpen(!open)}
      >
        <span className="dot" aria-hidden="true" />
        {/* The shell shows the short label on phones. */}
        <span className="storm-label">{label}</span>
        <span className="storm-short" aria-hidden="true">
          {on.length === 0 ? "Storm" : on.length === 1 ? "Storm on" : `Storm ${on.length}`}
        </span>
      </button>
      {open && (
        <div className="storm-panel card" role="dialog" aria-label="Storm Mode per site">
          <p className="small muted" style={{ marginTop: 0 }}>
            Storm Mode is set per site, so only the sites in a storm's path switch to their satellite backup.
          </p>
          <ul>
            {current.sites.map((s) => (
              <li key={s.id}>
                <span>
                  <strong>{s.name}</strong> <span className="muted small">{s.location}</span>
                  {s.storm_mode && (
                    <div className="small muted">
                      On since {new Date(s.storm_since ?? "").toLocaleString()} by {who(s.storm_by)}
                    </div>
                  )}
                </span>
                <button
                  className={s.storm_mode ? "button small storm-on" : "button secondary small"}
                  disabled={busy}
                  onClick={() => toggle(s, !s.storm_mode)}
                >
                  {s.storm_mode ? "Switch off" : "Switch on"}
                </button>
              </li>
            ))}
          </ul>
          {error && <p className="small" style={{ color: "var(--danger)" }}>{error}</p>}
        </div>
      )}
    </div>
  );
}

export function who(actor: string | null): string {
  if (!actor) return "unknown";
  return actor.replace(/^user:/, "");
}

/** A coral band under the header while any site is in Storm Mode. */
export function StormBanner() {
  const { current } = useCustomer();
  const on = current?.sites.filter((s) => s.storm_mode) ?? [];
  if (!current || on.length === 0) return null;
  return (
    <div className="storm-banner" role="status">
      <strong>Storm Mode is on</strong> at{" "}
      {on.map((s, i) => (
        <span key={s.id}>
          {i > 0 && (i === on.length - 1 ? " and " : ", ")}
          {s.name} (since {new Date(s.storm_since ?? "").toLocaleTimeString()}, by {who(s.storm_by)})
        </span>
      ))}
      . The satellite path there is warm; voice and business may use it if both terrestrial paths fail.
      {current.storm_allow_bulk_sat ? " Bulk may use it too." : " Bulk pauses rather than use it."}
    </div>
  );
}

/** Lets an admin choose which customer the switch and admin screens act for. */
export function CustomerPicker() {
  const { customers, current, select } = useCustomer();
  if (customers.length < 2 || !current) return null;
  return (
    <label className="picker">
      Customer{" "}
      <select value={current.id} onChange={(e) => select(e.target.value)}>
        {customers.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </select>
    </label>
  );
}
