import { useCallback, useEffect, useState } from "react";

// Sessions live in a secure, HttpOnly cookie set by the controller (ADR 0017);
// the portal never sees or stores the token. Every call carries the
// X-Requested-With header, which the controller requires on cookie writes.
const PORTAL_HEADER = { name: "X-Requested-With", value: "exa-portal" };

/** Tells the sign-in gate that the session has ended (a 401 from any call). */
export function signedOut() {
  window.dispatchEvent(new Event("exa-signed-out"));
}

/** Tells the sign-in gate to look again (after signing in). */
export function signedIn() {
  window.dispatchEvent(new Event("exa-auth"));
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** Calls the controller API with the session cookie. A 401 signs the user out. */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set(PORTAL_HEADER.name, PORTAL_HEADER.value);
  if (init.body) headers.set("Content-Type", "application/json");
  const r = await fetch(`/api/v1${path}`, { ...init, headers, credentials: "same-origin" });
  if (r.status === 401 && !path.startsWith("/auth/")) signedOut();
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
  sites: OverviewSite[];
  attention: string[];
  /** Per class: share of 10 s windows in the last 24 h that met the class SLA on the path it was on. */
  sla_24h: ClassSla24h[];
  /** The share of windows each class should meet; shown as the target. */
  sla_target_pct: number;
  /** Non-shadow routing moves in the last 24 h, all sites. */
  moves_24h: number;
  /** The last five non-shadow moves, newest first. */
  recent_decisions: RecentMove[];
}

export interface OverviewSteering {
  class_name: string;
  intended: string | null;
  intended_label: string | null;
  since: string | null;
  /** Set only when the agent reports a different path from the intended one. */
  actual: string | null;
  actual_label: string | null;
  paused: boolean | null;
  failover: boolean | null;
  moves_24h: number;
}

export interface OverviewSite extends SiteSummary {
  storm_mode: boolean;
  /** Empty for the PoP. */
  steering: OverviewSteering[];
}

export interface ClassSla24h {
  class_name: string;
  /** Bulk is best effort: reported, never a breach. */
  best_effort: boolean;
  max_latency_ms: number | null;
  max_jitter_ms: number | null;
  max_loss_pct: number | null;
  windows: number;
  met: number;
  pct: number | null;
  sites: { site_id: string; site: string; windows: number; met: number; pct: number | null }[];
}

export interface RecentMove {
  id: number;
  time: string;
  site_id: string;
  site: string;
  class_name: string;
  kind: "move" | "move_back" | "failover" | string;
  from_path: string | null;
  from_label: string | null;
  to_path: string | null;
  to_label: string | null;
  reason: string;
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
  /** Probes behind the point, so loss can be averaged by probes sent. */
  sent?: number | null;
  received?: number | null;
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
  /** Its certificate no longer works; a new enrolment token brings it back. */
  revoked?: boolean;
}

export interface User {
  email: string;
  role: "admin" | "customer" | "carrier";
  customer_id: string | null;
  name?: string;
  two_step?: boolean;
  /** null: the full account. A list: what a directory-provisioned account may do. */
  scopes?: string[] | null;
}

// ---- Sign-in, two-step and single sign-on (ADR 0017) ----

export interface LoginResult {
  token?: string;
  user?: User;
  mfa_required?: boolean;
  challenge?: string;
  methods?: ("totp" | "passkey" | "recovery")[];
}

export interface SignInProvider {
  id: string;
  label: string;
  start_url: string;
}

export interface Discovery {
  method: "password" | "sso";
  name?: string;
  password_allowed?: boolean;
  start_url?: string;
}

export interface Passkey {
  id: number;
  name: string;
  created_at: string;
  last_used_at: string | null;
}

export interface TwoStepStatus {
  enabled: boolean;
  enabled_at: string | null;
  pending: boolean;
  recovery_codes_left: number;
  passkeys_available: boolean;
  passkeys: Passkey[];
}

export interface SsoDomain {
  id: number;
  domain: string;
  status: "pending" | "approved" | "rejected";
  decided_at?: string | null;
}

export interface SsoConnection {
  id: string;
  alias: string;
  protocol: "saml" | "oidc";
  display_name: string;
  metadata_url: string;
  has_metadata_xml: boolean;
  client_id: string;
  status: "draft" | "tested" | "enabled" | "disabled";
  require_sso: boolean;
  last_test: { ok: boolean; message: string; email: string; at: string } | null;
  tested_at: string | null;
  domains: SsoDomain[];
  provider_setup?: { redirect_uri: string; sp_entity_id: string; sp_metadata_url: string };
}

export interface SsoConnections {
  gateway: { configured: boolean; simulated: boolean };
  callback_url: string;
  items: SsoConnection[];
}

export interface DomainClaim extends SsoDomain {
  customer: string;
  connection: string;
  requested_by: string;
  created_at: string;
}

export interface ScimToken {
  id: number;
  name: string;
  prefix: string;
  created_by: string;
  created_at: string;
  last_used_at: string | null;
  token?: string;
}

export interface DirectoryGroup {
  id: string;
  display_name: string;
  team_id: string | null;
  team: string | null;
  seat: "agent" | "internal";
  business_admin: "none" | "pending" | "approved";
  admin_requested_by: string | null;
  admin_approved_by: string | null;
  members: number;
}

const b64urlToBuf = (s: string): ArrayBuffer => {
  const pad = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
  const bin = atob(pad);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
};

const bufToB64url = (b: ArrayBuffer): string => {
  let bin = "";
  new Uint8Array(b).forEach((c) => (bin += String.fromCharCode(c)));
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
};

export const passkeysSupported = () => typeof window !== "undefined" && "PublicKeyCredential" in window;

type Json = Record<string, unknown>;

/** navigator.credentials.create() from the controller's JSON options; returns JSON for the controller. */
export async function createPasskey(options: Json): Promise<Json> {
  const o = options as {
    challenge: string;
    user: { id: string; name: string; displayName: string };
    excludeCredentials?: { id: string; type: string }[];
  } & Json;
  const publicKey = {
    ...o,
    challenge: b64urlToBuf(o.challenge),
    user: { ...o.user, id: b64urlToBuf(o.user.id) },
    excludeCredentials: (o.excludeCredentials ?? []).map((c) => ({ ...c, id: b64urlToBuf(c.id) })),
  } as unknown as PublicKeyCredentialCreationOptions;
  const cred = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential | null;
  if (!cred) throw new Error("No passkey was created.");
  const r = cred.response as AuthenticatorAttestationResponse;
  return {
    id: cred.id,
    rawId: bufToB64url(cred.rawId),
    type: cred.type,
    response: { clientDataJSON: bufToB64url(r.clientDataJSON), attestationObject: bufToB64url(r.attestationObject) },
  };
}

/** navigator.credentials.get() from the controller's JSON options; returns JSON for the controller. */
export async function getPasskey(options: Json): Promise<Json> {
  const o = options as { challenge: string; allowCredentials?: { id: string; type: string }[] } & Json;
  const publicKey = {
    ...o,
    challenge: b64urlToBuf(o.challenge),
    allowCredentials: (o.allowCredentials ?? []).map((c) => ({ ...c, id: b64urlToBuf(c.id) })),
  } as unknown as PublicKeyCredentialRequestOptions;
  const cred = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential | null;
  if (!cred) throw new Error("No passkey was used.");
  const r = cred.response as AuthenticatorAssertionResponse;
  return {
    id: cred.id,
    rawId: bufToB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: bufToB64url(r.clientDataJSON),
      authenticatorData: bufToB64url(r.authenticatorData),
      signature: bufToB64url(r.signature),
      userHandle: r.userHandle ? bufToB64url(r.userHandle) : null,
    },
  };
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

export interface StormSite {
  id: string;
  name: string;
  location: string;
  storm_mode: boolean;
  storm_since: string | null;
  storm_by: string | null;
}

export interface CustomerSettings {
  id: string;
  name: string;
  shadow_mode: boolean;
  /** True while any site is in Storm Mode. */
  storm_mode: boolean;
  storm_since: string | null;
  storm_by: string | null;
  storm_allow_bulk_sat: boolean;
  /** Apply confident application detections as rules without asking. */
  auto_prioritise: boolean;
  /** A lab or demo organisation: its screens carry "Example data". */
  example?: boolean;
  /** Clouds reach each other through the PoP (ExaConnect Fabric). */
  cloud_to_cloud?: boolean;
  sites: StormSite[];
}

export interface Insight {
  id: number;
  customer_id: string;
  kind: "storm_warning" | "hazard" | "bill_shock" | "anomaly";
  severity: "info" | "warning" | "critical";
  title: string;
  detail: string;
  data: Record<string, unknown>;
  example: boolean;
  first_seen: string;
  last_seen: string;
  resolved_at: string | null;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
  site: string | null;
  path: string | null;
  carrier: string | null;
}

export interface AiStatus {
  ask_enabled: boolean;
  model: string | null;
  storm_watch: boolean;
}

export interface LinkSettlement {
  id: string;
  customer: string;
  site: string;
  site_kind: "site" | "pop";
  path: string;
  path_label: string;
  carrier: string;
  underlay_type: string;
  commit_mbps: number | string;
  cost_per_mbps: number | string;
  burst_price: number | string;
  samples: number;
  discarded: number;
  p95_in_mbps: number | null;
  p95_out_mbps: number | null;
  billable_mbps: number | null;
  commit_charge: string;
  burst_mbps: string;
  burst_charge: string;
  total: string;
}

export interface UsagePoint {
  bucket: string;
  in_mbps: number;
  out_mbps: number;
  seconds: number;
}

export interface UsageRow {
  site: string;
  site_id: string;
  path: string;
  path_label: string;
  carrier: string;
  satellite: boolean;
  commit_mbps: number;
  gb: number;
  over_commit_gb: number;
  p95_mbps: number | null;
  reasons: { time: string; class_name: string; reason: string }[];
}

// ---- ExaConnect Fabric: virtual circuits (docs/fabric-contract.md) ----

export type CircuitKind = "cloud" | "site";
export type CircuitStatus = "provisioning" | "up" | "down" | "off";

/** One IPsec tunnel of a cloud circuit; a resilient circuit has two (docs/protection-contract.md). */
export interface CircuitTunnel {
  which: "primary" | "secondary";
  peer_address: string | null;
  ike: string | null;
  bgp: string | null;
  prefixes_received: number | null;
  status: CircuitStatus;
}

export interface Circuit {
  id: number;
  name: string;
  kind: CircuitKind;
  a_site_id: string | null;
  a_site: string | null;
  b_site_id: string | null;
  b_site: string | null;
  a_vlan: number | null;
  b_vlan: number | null;
  provider: string | null;
  region: string | null;
  peer_address: string | null;
  peer_asn: number | null;
  inside_cidr: string | null;
  /** Resilient circuits: a second tunnel to a second gateway address. */
  resilient?: boolean;
  secondary_peer_address?: string | null;
  secondary_inside_cidr?: string | null;
  /** One entry, or two for a resilient circuit. Absent from older controllers. */
  tunnels?: CircuitTunnel[];
  our_inside: string | null;
  cloud_inside: string | null;
  cloud_prefixes: string[];
  a_prefixes: string[];
  class_name: string | null;
  bandwidth_mbps: number;
  price_per_mbps_month: number | string;
  enabled: boolean;
  /** The pre-shared key is write-only; this says whether one is set. */
  has_psk: boolean;
  status: CircuitStatus;
  ike: string | null;
  bgp: string | null;
  prefixes_received: number | null;
  routes: string[] | null;
  rtt_ms: number | string | null;
  loss_pct: number | string | null;
  mbps_in: number | string | null;
  mbps_out: number | string | null;
  month_to_date: number | string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

/** What the portal sends to create or change a circuit. `psk` is never read back. */
export interface CircuitIn {
  name?: string;
  kind?: CircuitKind;
  bandwidth_mbps?: number;
  enabled?: boolean;
  a_site_id?: string;
  a_prefixes?: string[];
  a_vlan?: number;
  b_site_id?: string;
  b_vlan?: number;
  provider?: string;
  region?: string;
  peer_address?: string;
  peer_asn?: number;
  inside_cidr?: string;
  /** null in a PATCH removes the second tunnel. */
  secondary_peer_address?: string | null;
  secondary_inside_cidr?: string | null;
  psk?: string;
  cloud_prefixes?: string[];
  class_name?: string;
}

export interface CloudProvider {
  name: string;
  asn: number;
  /** Where in the provider's console the tunnel details are. */
  where: string;
}

export type CloudProviders = Record<string, CloudProvider>;

export interface ChargeSegment {
  mbps: number;
  from: string;
  to: string;
  hours: number;
  amount: number | string;
}

export interface CircuitCharges {
  price_per_mbps_month: number | string;
  segments: ChargeSegment[];
  total: number | string;
}

export interface CircuitMetric {
  time: string;
  sent: number | null;
  received: number | null;
  rtt_ms: number | string | null;
  mbps_in: number | string | null;
  mbps_out: number | string | null;
}

export const circuitPaths = {
  providers: "/circuits/providers",
  list: (cid: string) => `/customers/${cid}/circuits`,
  one: (cid: string, id: number) => `/customers/${cid}/circuits/${id}`,
  charges: (cid: string, id: number, month: string) => `/customers/${cid}/circuits/${id}/charges?month=${month}`,
  metrics: (cid: string, id: number, minutes = 60) => `/customers/${cid}/circuits/${id}/metrics?minutes=${minutes}`,
  settings: (cid: string) => `/customers/${cid}/settings`,
};

export const createCircuit = (cid: string, body: CircuitIn) =>
  api<Circuit>(circuitPaths.list(cid), { method: "POST", body: JSON.stringify(body) });

/** Any of the create fields; `psk` rotates the key, `bandwidth_mbps` is billed from now. */
export const updateCircuit = (cid: string, id: number, body: CircuitIn) =>
  api<Circuit>(circuitPaths.one(cid, id), { method: "PATCH", body: JSON.stringify(body) });

export const deleteCircuit = (cid: string, id: number) => api<void>(circuitPaths.one(cid, id), { method: "DELETE" });

export const setCloudToCloud = (cid: string, on: boolean) =>
  api<CustomerSettings>(circuitPaths.settings(cid), { method: "PATCH", body: JSON.stringify({ cloud_to_cloud: on }) });

// ---- Internet breakout, NAT gateway and firewall (docs/internet-contract.md) ----

/** pop: through ExaCarib's PoP (the default); local: straight out at the site; off: no internet. */
export type BreakoutMode = "pop" | "local" | "off";
export type FirewallAction = "allow" | "deny";
export type FirewallProtocol = "any" | "tcp" | "udp" | "icmp";

export interface InternetSite {
  id: string;
  name: string;
  mode: BreakoutMode;
  /** The interface or tunnel the site's internet route points at; "" until reported. */
  via: string | null;
  /** "Carrier A" for a local uplink, "ExaCarib PoP over Carrier A" for a tunnel. */
  via_label: string | null;
  /** The site's carrier links, in failover order. */
  uplinks: string[];
  updated_at: string | null;
}

export interface FirewallRule {
  id: number;
  position: number;
  /** null applies to all sites. */
  site_id: string | null;
  site: string | null;
  action: FirewallAction;
  src: string[];
  dst: string[];
  protocol: FirewallProtocol;
  ports: string;
  description: string;
  enabled: boolean;
  packets: number | string | null;
  bytes: number | string | null;
}

export interface PortForward {
  id: number;
  description: string;
  protocol: "tcp" | "udp";
  port: number;
  to_site_id: string;
  to_site: string | null;
  to_address: string;
  /** null forwards to the same port as the public one. */
  to_port: number | null;
  allow_from: string[];
  enabled: boolean;
  packets: number | string | null;
  bytes: number | string | null;
}

/** DDoS protection on the PoP's shared public address; customers see counts only. */
export interface ProtectionDropped {
  /** On ExaCarib's block list. */
  blocked: number | string | null;
  /** From sources blocked automatically. */
  auto: number | string | null;
  /** Over the per-source new connection limit. */
  flood: number | string | null;
  /** Over the SYN flood limit, all sources together. */
  syn: number | string | null;
}

export interface ProtectionSummary {
  enabled: boolean;
  new_per_source: number;
  syn_per_s: number;
  block_minutes: number;
  dropped: ProtectionDropped | null;
  /** How many sources are blocked right now. */
  auto_blocked: number | null;
  updated_at: string | null;
}

export interface InternetState {
  /** The PoP address port forwards listen on; shared with other customers. */
  public_address: string | null;
  sites: InternetSite[];
  rules: FirewallRule[];
  forwards: PortForward[];
  inbound_dropped: number | string | null;
  /** Absent from controllers older than ADR 0012. */
  protection?: ProtectionSummary | null;
}

export interface FirewallRuleIn {
  site_id?: string | null;
  action?: FirewallAction;
  src?: string[];
  dst?: string[];
  protocol?: FirewallProtocol;
  ports?: string;
  description?: string;
  enabled?: boolean;
  position?: number;
}

export interface PortForwardIn {
  description?: string;
  protocol?: "tcp" | "udp";
  port?: number;
  to_site_id?: string;
  to_address?: string;
  to_port?: number | null;
  allow_from?: string[];
  enabled?: boolean;
}

export const internetPaths = {
  state: (cid: string) => `/customers/${cid}/internet`,
  site: (cid: string, siteId: string) => `/customers/${cid}/internet/sites/${siteId}`,
  rules: (cid: string) => `/customers/${cid}/firewall/rules`,
  rule: (cid: string, id: number) => `/customers/${cid}/firewall/rules/${id}`,
  order: (cid: string) => `/customers/${cid}/firewall/order`,
  forwards: (cid: string) => `/customers/${cid}/port-forwards`,
  forward: (cid: string, id: number) => `/customers/${cid}/port-forwards/${id}`,
};

export const setBreakout = (cid: string, siteId: string, mode: BreakoutMode) =>
  api<InternetSite>(internetPaths.site(cid, siteId), { method: "PATCH", body: JSON.stringify({ mode }) });

export const createFirewallRule = (cid: string, body: FirewallRuleIn) =>
  api<FirewallRule>(internetPaths.rules(cid), { method: "POST", body: JSON.stringify(body) });

export const updateFirewallRule = (cid: string, id: number, body: FirewallRuleIn) =>
  api<FirewallRule>(internetPaths.rule(cid, id), { method: "PATCH", body: JSON.stringify(body) });

export const deleteFirewallRule = (cid: string, id: number) => api<void>(internetPaths.rule(cid, id), { method: "DELETE" });

/** Sets the order of all the customer's rules; the first match wins. */
export const orderFirewallRules = (cid: string, ids: number[]) =>
  api<unknown>(internetPaths.order(cid), { method: "POST", body: JSON.stringify({ ids }) });

export const createPortForward = (cid: string, body: PortForwardIn) =>
  api<PortForward>(internetPaths.forwards(cid), { method: "POST", body: JSON.stringify(body) });

export const updatePortForward = (cid: string, id: number, body: PortForwardIn) =>
  api<PortForward>(internetPaths.forward(cid, id), { method: "PATCH", body: JSON.stringify(body) });

export const deletePortForward = (cid: string, id: number) => api<void>(internetPaths.forward(cid, id), { method: "DELETE" });

/** Downloads a file from the API with the session cookie, saving it under a chosen name. */
export async function download(path: string, filename: string) {
  const r = await fetch(`/api/v1${path}`, {
    headers: { [PORTAL_HEADER.name]: PORTAL_HEADER.value },
    credentials: "same-origin",
  });
  if (r.status === 401) signedOut();
  if (!r.ok) throw new ApiError(r.status, `The controller answered ${r.status}.`);
  const url = URL.createObjectURL(await r.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---- Partner directory and plain-English ordering (docs/ordering-contract.md) ----

export type PartnerCategory = "cloud" | "saas" | "payments" | "internet" | "security" | "content" | "other";
/** cloud: the customer sets up the VPN in their own cloud console; service: ExaCarib completes the order. */
export type PartnerKind = "cloud" | "service";

export interface Partner {
  id: number;
  slug: string;
  name: string;
  category: PartnerCategory;
  kind: PartnerKind;
  /** Cloud partners only: the circuit provider preset. */
  provider: string | null;
  description: string;
  website: string | null;
  regions: string[];
  /** Service partners only: what the partner advertises. */
  prefixes: string[];
  price_per_mbps_month: number | string;
  listed: boolean;
  example: boolean;
}

export interface PartnerIn {
  slug?: string;
  name?: string;
  category?: PartnerCategory;
  kind?: PartnerKind;
  provider?: string | null;
  description?: string;
  website?: string | null;
  regions?: string[];
  prefixes?: string[];
  price_per_mbps_month?: number;
  listed?: boolean;
  example?: boolean;
}

export type OrderStatus = "draft" | "done" | "pending_partner" | "cancelled" | "failed";
export type OrderEngine = "ai" | "rules" | "form";

/** One action in an order, normalised by the controller. */
export type OrderAction =
  | {
      action: "cloud_circuit";
      name?: string;
      provider?: string;
      region?: string | null;
      site?: string | null;
      bandwidth_mbps?: number;
      cloud_prefixes?: string[];
      class_name?: string | null;
    }
  | { action: "site_circuit"; name?: string; a_site?: string; b_site?: string; a_vlan?: number; b_vlan?: number; bandwidth_mbps?: number }
  | {
      action: "partner_connection";
      partner: string;
      site?: string | null;
      bandwidth_mbps?: number;
      region?: string | null;
      cloud_prefixes?: string[];
    }
  | { action: "bandwidth"; circuit: string; bandwidth_mbps: number }
  | { action: "internet_mode"; site: string; mode: BreakoutMode };

export interface OrderNeed {
  /** Index of the action this input belongs to. */
  action: number;
  field: string;
  label: string;
  secret: boolean;
  default?: string | number | null;
  /** Set on inputs that may be left blank (inside_cidr). */
  optional?: boolean;
}

export interface OrderResult {
  action: number;
  ok: boolean;
  circuit_id?: number | null;
  /** The partner still has to act (service partner connections). */
  pending?: boolean;
  message: string;
}

export interface Order {
  id: number;
  status: OrderStatus;
  engine: OrderEngine;
  text: string | null;
  actions: OrderAction[];
  summary: string[];
  needs: OrderNeed[];
  problems: string[];
  monthly_estimate: number | string | null;
  results: OrderResult[] | null;
  created_by: string | null;
  created_at: string;
  confirmed_by: string | null;
  confirmed_at: string | null;
  /** Admin listings may name the customer. */
  customer_id?: string;
  customer?: string | null;
}

export interface CompleteOrderIn {
  peer_address: string;
  peer_asn: number;
  psk: string;
  inside_cidr?: string | null;
  prefixes: string[];
}

export const orderPaths = {
  partners: (category = "", q = "") => {
    const p = new URLSearchParams();
    if (category) p.set("category", category);
    if (q) p.set("q", q);
    const s = p.toString();
    return `/partners${s ? `?${s}` : ""}`;
  },
  list: (cid: string) => `/customers/${cid}/orders`,
  one: (cid: string, id: number) => `/customers/${cid}/orders/${id}`,
  adminPartners: "/admin/partners",
  adminPartner: (id: number) => `/admin/partners/${id}`,
  adminOrders: (status: OrderStatus) => `/admin/orders?status=${status}`,
};

/** Drafts an order from plain English. Nothing changes until it is confirmed. */
export const draftOrder = (cid: string, text: string, engine: "auto" | "rules" = "auto") =>
  api<Order>(`${orderPaths.list(cid)}/draft`, { method: "POST", body: JSON.stringify({ text, engine }) });

/** Drafts an order from a form (engine "form"). */
export const createOrder = (cid: string, actions: OrderAction[]) =>
  api<Order>(orderPaths.list(cid), { method: "POST", body: JSON.stringify({ actions }) });

/** Applies every action, all or nothing; one inputs object per action. */
export const confirmOrder = (cid: string, id: number, inputs: Record<string, unknown>[]) =>
  api<Order>(`${orderPaths.one(cid, id)}/confirm`, { method: "POST", body: JSON.stringify({ inputs }) });

export const cancelOrder = (cid: string, id: number) => api<Order>(`${orderPaths.one(cid, id)}/cancel`, { method: "POST" });

export const createPartner = (body: PartnerIn) =>
  api<Partner>(orderPaths.adminPartners, { method: "POST", body: JSON.stringify(body) });

export const updatePartner = (id: number, body: PartnerIn) =>
  api<Partner>(orderPaths.adminPartner(id), { method: "PATCH", body: JSON.stringify(body) });

/** Removes a partner, or unlists it if orders refer to it. */
export const deletePartner = (id: number) => api<unknown>(orderPaths.adminPartner(id), { method: "DELETE" });

export const completeOrder = (id: number, body: CompleteOrderIn) =>
  api<Order>(`/admin/orders/${id}/complete`, { method: "POST", body: JSON.stringify(body) });

// The PoP checks pre-shared keys the same way (controller fabric.PSK_RE).
export const PSK_PATTERN = "[A-Za-z1-9._][A-Za-z0-9._]{7,63}";
export const PSK_HINT = "8 to 64 letters, digits, dots and underscores; it can't start with 0.";

// ---- DDoS protection at the PoP (docs/protection-contract.md, admin only) ----

export interface ProtectionSettings {
  enabled: boolean;
  new_per_source: number;
  syn_per_s: number;
  block_minutes: number;
}

export interface BlockedSource {
  id: number;
  prefix: string;
  reason: string | null;
  created_by: string | null;
  created_at: string;
  /** null: until removed. */
  expires_at: string | null;
}

export interface ProtectionAdminState {
  settings: ProtectionSettings;
  dropped: ProtectionDropped | null;
  auto_blocked: { address: string; expires_s: number }[];
  blocklist: BlockedSource[];
}

export const protectionPaths = {
  admin: "/admin/protection",
  blocklist: "/admin/protection/blocklist",
  blocked: (id: number) => `/admin/protection/blocklist/${id}`,
};

export const updateProtection = (body: Partial<ProtectionSettings>) =>
  api<unknown>(protectionPaths.admin, { method: "PATCH", body: JSON.stringify(body) });

/** hours null blocks the prefix until it is removed. */
export const addBlockedSource = (body: { prefix: string; reason?: string; hours: number | null }) =>
  api<BlockedSource>(protectionPaths.blocklist, { method: "POST", body: JSON.stringify(body) });

export const removeBlockedSource = (id: number) => api<void>(protectionPaths.blocked(id), { method: "DELETE" });

// ---- Encryption report (docs/protection-contract.md §3) ----

export type PathEncryption = "encrypted" | "idle" | "down";

export interface EncryptionPath {
  site: string;
  path: string;
  label: string;
  protocol: string;
  cipher: string;
  /** -1 or null when there has been no handshake. */
  handshake_age_s: number | null;
  status: PathEncryption;
}

export interface EncryptionCircuit {
  id: number;
  name: string;
  kind: CircuitKind;
  tunnel: "primary" | "secondary";
  protocol: string;
  ike_cipher: string;
  esp_cipher: string;
  /** Seconds since the IKE SA was established; -1 unknown. */
  established_s: number | null;
  status: "encrypted" | "down";
  /** Weak algorithms the cloud negotiated, such as "Uses SHA-1". */
  notes: string[];
}

export interface EncryptionReport {
  summary: { encrypted: number; total: number };
  paths: EncryptionPath[];
  circuits: EncryptionCircuit[];
  layer2: { id: number; name: string; protocol: string; status: PathEncryption }[];
  control: { protocol: string };
  internet: string;
}

export const encryptionPath = (cid: string) => `/customers/${cid}/encryption`;

// ---- API keys (docs/automation-contract.md, ADR 0013) ----
// A key acts as the person who made it. The token comes back once, from create;
// the portal keeps it only in component state and never stores or logs it.

export interface ApiKey {
  id: number;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
}

export interface ApiKeyCreated {
  id: number;
  name: string;
  prefix: string;
  created_at: string;
  expires_at: string | null;
  token: string;
}

export const apiKeysPath = "/auth/api-keys";

export const createApiKey = (body: { name: string; days?: number }) =>
  api<ApiKeyCreated>(apiKeysPath, { method: "POST", body: JSON.stringify(body) });

export const revokeApiKey = (id: number) => api<void>(`${apiKeysPath}/${id}`, { method: "DELETE" });
