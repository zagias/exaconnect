import type { Product } from "../../api";

export interface AssistantProductAccess {
  organisation_access: boolean;
  member_access: boolean;
  operator_access: boolean;
  access_source: "legacy_unspecified_products" | "organisation_products";
  active_subscription_recorded: boolean;
  scheduled_subscription_recorded: boolean;
  payment_verified: false;
  provider_readiness: "not_checked";
}

export interface AssistantFeature {
  id: string;
  product: Product;
  name: string;
  description: string;
  path: string | null;
  source: string;
  availability: AssistantProductAccess & { implemented: boolean; permitted_now: boolean };
}

export interface AssistantContext {
  account: {
    customer_id: string;
    organisation: string;
    example?: boolean;
    role: string;
    products: Product[];
    accessible_products: Product[];
    subscriptions: { product: Product; plan: string; state: string }[];
    product_access?: Partial<Record<Product, AssistantProductAccess>>;
  };
  verified_features?: AssistantFeature[];
  tools: {
    connect_read: boolean;
    connect_changes: boolean;
    jibsy_changes: boolean;
    subscription_changes: boolean;
    jibsy_diagnostics_path: string | null;
  };
  upgrade_guidance: { path: string; quote_path: string; can_request: boolean; policy: string };
  generated_at: string;
  sources: string[];
  ask_enabled: boolean;
  ask_unavailable_reason?: string | null;
  model?: string | null;
}

export interface Plan {
  id: number;
  customer_id?: string;
  status: "draft" | "applied" | "undone" | "cancelled";
  question: string;
  summary: string[];
  actions?: Record<string, unknown>[];
  problems: string[];
  results: { action: number; message: string; undone?: string; order_id?: number }[];
  created_at: string;
  applied_by: string | null;
  undone_by: string | null;
  revision?: string | null;
  expires_at?: string | null;
  approval?: { approved_by: string; approved_at: string; revision: string } | null;
  can_execute?: boolean;
}

export interface AssistantAnswer {
  answer: string;
  plan: Plan | null;
  answer_scope?: "account" | "connect";
  model?: string | null;
  based_on?: {
    sites?: number; decisions?: number; events?: number; insights?: number; sources?: string[];
    grounding?: "reviewed_typed_proposal" | "verified_advisory_selection" | "unverified_advice_replaced_with_grounded_facts" | "verified_read_only_status";
  };
}

/** Old bookmarks open the same workspace, retaining a drafted question. */
export function assistantAddress(search: string, scope?: "connect" | "jibsy"): string {
  const params = new URLSearchParams(search);
  if (scope) params.set("scope", scope);
  const query = params.toString();
  return `/assistant${query ? `?${query}` : ""}`;
}

export function isAssistantPath(path: string): boolean {
  return path === "/assistant" || path === "/ask" || path === "/commai/assistant" || path.startsWith("/commai/assistant/");
}
