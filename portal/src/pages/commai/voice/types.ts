/** Shapes returned by the CommAI voice API (ADR 0021). Money is always a string. */

export interface PriceImpact {
  currency: string;
  monthly_delta: string;
  one_time: string;
  changes_bill: boolean;
  lines: { label: string; amount: string; recurring: boolean }[];
  rate_card_version: number;
  example_prices: boolean;
}

export interface DiffItem {
  area: string;
  change: "added" | "removed" | "changed";
  item: string;
  fields?: string[];
}

export interface ChangeResult {
  ok: boolean;
  errors: { index: number; op: string; error: string }[];
  price_impact: PriceImpact;
  diff: DiffItem[];
  results: Record<string, unknown>[];
  version: number | null;
  needs_spend?: boolean;
  can_spend?: boolean;
}

export type Op = Record<string, unknown> & { op: string };

export interface Site {
  id: string;
  name: string;
  timezone: string;
  address_line1: string;
  address_line2: string;
  city: string;
  island: string;
  country: string;
  postcode: string;
  emergency_status: "not_registered" | "pending" | "registered" | "rejected";
}

export interface VoiceUser {
  id: string;
  name: string;
  email: string;
  mobile: string;
  extension: string;
  site_id: string | null;
  site: string | null;
  team_id: string | null;
  team: string | null;
  portal_email: string | null;
  forward_to: string;
  dnd: boolean;
  billing_from: string | null;
  emergency_address: Record<string, string>;
}

export interface VoiceNumber {
  id: string;
  e164: string;
  source: "new" | "ported";
  status: "pending" | "porting" | "active" | "removed";
  target_type: string;
  target_id: string | null;
  site: string | null;
  emergency_address: Record<string, string>;
}

export interface Device {
  id: string;
  kind: "desk" | "softphone";
  mac: string | null;
  model: string;
  status: string;
  extension: string | null;
  user_name: string | null;
}

export interface RingGroup {
  id: string;
  name: string;
  extension: string;
  strategy: "simultaneous" | "sequential";
  members: string[];
  ring_seconds: number;
}

export interface Queue {
  id: string;
  name: string;
  extension: string;
  strategy: string;
  members: string[];
  max_wait_s: number;
}

export interface Hours {
  id: string;
  name: string;
  timezone: string;
  schedule: Record<string, [string, string][]>;
  holidays: string[];
}

export interface Menu {
  id: string;
  name: string;
  extension: string;
  greeting: string;
  options: Record<string, { type: string; id: string | null }>;
  hours_id: string | null;
  closed_target: { type?: string; id?: string | null };
}

export interface AiRule {
  id: string;
  name: string;
  condition: "after_hours" | "no_answer" | "busy" | "always";
  hours_id: string | null;
  number_id: string | null;
  fallback: "voicemail" | "ring_group" | "queue";
  enabled: boolean;
}

export interface Scheduled {
  id: string;
  summary: string;
  run_at: string;
  status: "scheduled" | "applied" | "failed" | "cancelled";
  error: string;
  created_by: string;
  version: number | null;
}

export interface Me {
  id: string;
  name: string;
  extension: string;
  mobile: string;
  email: string;
  forward_to: string;
  forward_until: string | null;
  dnd: boolean;
  dnd_until: string | null;
  voicemail_greeting: string;
  voicemail_to_email: boolean;
  site: string | null;
  recording_access?: string;
}

export interface VoiceOverview {
  is_admin: boolean;
  can_spend: boolean;
  is_exacarib: boolean;
  policy: { recording_access: "none" | "own" | "admins" };
  provider: { name: string; live: boolean };
  me: Me | null;
  sites?: Site[];
  users?: VoiceUser[];
  numbers?: VoiceNumber[];
  devices?: Device[];
  ring_groups?: RingGroup[];
  queues?: Queue[];
  hours?: Hours[];
  menus?: Menu[];
  ai_rules?: AiRule[];
  teams?: { id: string; name: string }[];
  version?: number;
  scheduled?: Scheduled[];
  pbx?: { digest: string; rendered_at: string; written_to: string } | null;
}

export interface Version {
  version: number;
  kind: string;
  summary: string;
  created_by: string;
  created_at: string;
  changes: number;
}

export interface OrderStep {
  step: "tenant" | "numbers" | "devices" | "confirm" | "billing";
  status: "pending" | "done" | "failed";
  attempts: number;
  error: string;
}

export interface Order {
  id: string;
  status: "draft" | "approved" | "provisioning" | "failed" | "active" | "cancelled";
  price: PriceImpact;
  failed_step: string;
  error: string;
  created_by: string;
  approved_by: string;
  created_at: string;
  activated_at: string | null;
  users?: number;
  steps?: OrderStep[];
  ports?: Port[];
  test_calls?: { e164: string; ok: boolean; detail: string; at: string }[];
}

export interface Port {
  id: string;
  e164: string;
  losing_carrier: string;
  status: string;
  switch_date: string | null;
  note: string;
}

export interface RateCard {
  id: string;
  version: number;
  label: string;
  example: boolean;
  currency: string;
  effective_from: string;
  monthly_user: string;
  monthly_number: string;
  ai_minute: string;
  one_time: Record<string, string>;
  destinations: { prefix: string; name: string; per_minute: string }[];
}

export interface SpendGroup {
  id: string | null;
  name: string;
  amount: string;
  minutes: string;
  charges: number;
}

export interface Spend {
  period: string;
  currency: string;
  example_prices: boolean;
  total: string;
  minutes: string;
  spend_today: string;
  by_kind: { kind: string; amount: string }[];
  by_site: SpendGroup[];
  by_team: SpendGroup[];
  by_user: SpendGroup[];
  bundles: { id: string; name: string; minutes: string; prefixes: string[]; alert_pct: number; active: boolean; used: string }[];
}

export interface FraudLimits {
  daily_cap: string | null;
  blocked_prefixes: string[];
  international: boolean;
  calls_per_hour_alert: number;
}

export interface Invoice {
  id: string;
  kind: "invoice" | "credit_note";
  number: string | null;
  status: "draft" | "issued";
  period_start: string;
  period_end: string;
  currency: string;
  total: string;
  credits_number?: string | null;
  reason: string;
  issued_at: string | null;
  lines?: InvoiceLine[];
}

export interface InvoiceLine {
  id: number;
  description: string;
  quantity: string;
  amount: string;
  kind: string | null;
  call_id: string | null;
  to_number: string | null;
  seconds: number | null;
  rate_card_version: number | null;
  credits_line: number | null;
}

export interface CallRow {
  call_id: string;
  direction: string;
  from_number: string;
  to_number: string;
  ended_at: string;
  seconds: number;
  status: string;
  block_reason: string;
  user_name: string | null;
  extension: string | null;
  amount: string | null;
  rate_card_version: number | null;
}

export interface Reconcile {
  period: string;
  calls: number;
  billed_calls: string;
  supplier_cost: string;
  call_margin: string;
  call_margin_pct: string | null;
  other_fees: string;
  simulated_cost_estimate: string | null;
  issues: { call_id: string; issue: string }[];
  unmatched_supplier_rows: number;
  unmatched_supplier_cost: string;
}
