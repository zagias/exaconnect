import { useCallback, useEffect, useState } from "react";

const TOKEN_KEY = "exa.session";

// The session token lives in localStorage so a reload keeps you signed in.
// Storage can be blocked (private windows), so every access is guarded.
export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: the session lasts until reload */
  }
  window.dispatchEvent(new Event("exa-auth"));
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** Calls the controller API with the session token. A 401 signs the user out. */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");
  const r = await fetch(`/api/v1${path}`, { ...init, headers });
  if (r.status === 401 && token) setToken(null);
  if (!r.ok) {
    let msg = `The controller answered ${r.status}.`;
    try {
      const body = await r.json();
      if (typeof body.detail === "string") msg = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(r.status, msg);
  }
  return (r.status === 204 ? undefined : await r.json()) as T;
}

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  reload: () => void;
}

/** Fetches a path and refreshes it on an interval (0 = once). */
export function useApi<T>(path: string | null, intervalMs = 10_000): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    if (!path) return;
    let cancelled = false;
    const load = () =>
      api<T>(path)
        .then((d) => {
          if (!cancelled) {
            setData(d);
            setError(null);
          }
        })
        .catch((e: Error) => {
          if (!cancelled) setError(e.message);
        });
    load();
    const id = intervalMs > 0 ? setInterval(load, intervalMs) : undefined;
    return () => {
      cancelled = true;
      if (id) clearInterval(id);
    };
  }, [path, intervalMs, tick]);
  return { data, error, reload };
}

export interface ControllerStatus {
  ok: boolean;
  version?: string;
}

/** Polls the controller's version endpoint so the header shows whether it is reachable. */
export function useControllerStatus(intervalMs = 10_000): ControllerStatus {
  const [status, setStatus] = useState<ControllerStatus>({ ok: false });
  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        const r = await fetch("/api/v1/version");
        const body = r.ok ? await r.json() : null;
        if (!cancelled) setStatus(body ? { ok: true, version: body.version } : { ok: false });
      } catch {
        if (!cancelled) setStatus({ ok: false });
      }
    };
    check();
    const id = setInterval(check, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [intervalMs]);
  return status;
}

/** Numeric fields can arrive as strings (Postgres numeric); null stays null. */
export function num(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// ---- Shapes returned by the controller (controller/exaconnect_controller/api) ----

export type Health = "ok" | "warn" | "bad";

export interface PathRow {
  path: string;
  label: string;
  carrier: string;
  underlay_type: string;
  commit_mbps: number;
  rtt_avg_ms: number | string | null;
  jitter_ms: number | string | null;
  loss_pct: number | string | null;
  sent: number | null;
  received: number | null;
  at: string | null;
  health: Health;
}

export interface SiteSummary {
  id: string;
  name: string;
  kind: "site" | "pop";
  location: string | null;
  node_id: string | null;
  last_seen: string | null;
  online: boolean | null;
  applied_version: number | null;
  desired_version: number | null;
  apply_ok: boolean | null;
  paths: PathRow[];
}

export interface Overview {
  sites: SiteSummary[];
  attention: string[];
}

export interface SiteDetail extends SiteSummary {
  asn: number;
  lan_prefixes: string[];
  node_name: string | null;
  apply_error: string | null;
  agent_version: string | null;
  tunnels: { tunnel: string; path: string; handshake_age_s: number; bfd: string | null; updated_at: string }[];
  steering: SteeringRow[];
  customer_id: string;
  slas: {
    class_name: string;
    max_latency_ms: number | null;
    max_jitter_ms: number | null;
    max_loss_pct: number | string | null;
    allow_satellite: boolean;
  }[];
}

export interface MetricPoint {
  time: string;
  path: string;
  rtt_avg_ms: number | string | null;
  jitter_ms: number | string | null;
  loss_pct: number | string | null;
}

export interface EventRow {
  time: string;
  kind: string;
  detail: Record<string, string> | null;
  node: string | null;
}

export interface NodeRow {
  id: string;
  name: string;
  role: string;
  enrolled_at: string;
  last_seen: string | null;
  applied_version: number;
  desired_version: number | null;
  apply_ok: boolean | null;
  apply_error: string | null;
  agent_version: string | null;
}

export interface User {
  email: string;
  role: "admin" | "customer" | "carrier";
  customer_id: string | null;
}

export interface MetricInput {
  now: number;
  ahead: number;
  slope_per_min: number;
  se: number;
  limit: number;
}

export interface DecisionRow {
  id: number;
  time: string;
  site_id: string;
  site: string;
  class_name: string;
  kind: "move" | "move_back" | "failover" | "hold";
  from_path: string | null;
  from_label: string | null;
  to_path: string | null;
  to_label: string | null;
  shadow: boolean;
  engine: string;
  reason: string;
  inputs: {
    policy?: { horizon_s: number; hold_s: number; return_after_s: number; confidence: number };
    storm?: boolean;
    paths?: Record<
      string,
      { up: boolean; known: boolean; over_commit: boolean; score_now: number; score_ahead: number; metrics: Record<string, MetricInput> }
    >;
  };
}

export interface SteeringRow {
  class_name: string;
  intended: string | null;
  intended_label: string | null;
  since: string | null;
  actual: string | null;
  actual_label: string | null;
  paused: boolean | null;
  failover: boolean | null;
  reported_at: string | null;
  last_reason: string | null;
}

export interface CustomerSettings {
  id: string;
  name: string;
  shadow_mode: boolean;
  storm_mode: boolean;
  storm_since: string | null;
  storm_by: string | null;
  storm_allow_bulk_sat: boolean;
}
