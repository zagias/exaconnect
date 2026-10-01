import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, type CustomerSettings } from "./api";

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

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api<CustomerSettings[]>("/customers/mine")
        .then((c) => !cancelled && setCustomers(c))
        .catch(() => {});
    load();
    const t = setInterval(load, 10_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [tick]);

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

/** The Storm Mode switch: coral when on, with who switched it and when. */
export function StormSwitch() {
  const { current, reload } = useCustomer();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!current) return null;
  const on = current.storm_mode;
  const toggle = async () => {
    const question = on
      ? `Switch Storm Mode off for ${current.name}? Classes on satellite move back to terrestrial paths.`
      : `Switch Storm Mode on for ${current.name}? The satellite path is kept warm and voice and business may use it if both terrestrial paths fail.`;
    if (!window.confirm(question)) return;
    setBusy(true);
    setError(null);
    try {
      await api(`/customers/${current.id}/storm`, { method: "POST", body: JSON.stringify({ on: !on }) });
      reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <button
      className="storm-switch"
      aria-pressed={on}
      disabled={busy}
      onClick={toggle}
      title={error ?? (on ? `On since ${new Date(current.storm_since ?? "").toLocaleString()} by ${who(current.storm_by)}` : "Off")}
    >
      <span className="dot" aria-hidden="true" />
      {busy ? "Switching…" : on ? "Storm Mode on" : "Storm Mode off"}
    </button>
  );
}

export function who(actor: string | null): string {
  if (!actor) return "unknown";
  return actor.replace(/^user:/, "");
}

/** A coral band under the header while Storm Mode is on. */
export function StormBanner() {
  const { current } = useCustomer();
  if (!current?.storm_mode) return null;
  return (
    <div className="storm-banner" role="status">
      <strong>Storm Mode is on</strong> for {current.name} since{" "}
      {new Date(current.storm_since ?? "").toLocaleString()}, switched on by {who(current.storm_by)}. The satellite path
      is warm; voice and business may use it if both terrestrial paths fail.
      {current.storm_allow_bulk_sat ? " Bulk may use it too." : " Bulk pauses rather than use it."}
    </div>
  );
}
