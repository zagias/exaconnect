import type { Product } from "../../api";

export interface AssistantContext {
  account: {
    customer_id: string;
    organisation: string;
    example?: boolean;
    role: string;
    products: Product[];
    accessible_products: Product[];
    subscriptions: { product: Product; plan: string; state: string }[];
  };
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
  model?: string | null;
  based_on?: { sites?: number; decisions?: number; events?: number; insights?: number; sources?: string[] };
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
