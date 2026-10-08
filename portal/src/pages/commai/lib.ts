import { useCustomer } from "../../customer";

/** The base path of the Jibsy API for the business being viewed. */
export function useCommaiBase(): string | null {
  const { current } = useCustomer();
  return current ? `/commai/customers/${current.id}` : null;
}

export interface Page<T> {
  items: T[];
  next: string | null;
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)} h ago`;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

export const CHANNEL_LABEL: Record<string, string> = {
  web: "Website chat",
  whatsapp: "WhatsApp",
  sms: "SMS",
  email: "Email",
  messenger: "Messenger",
  instagram: "Instagram",
  telegram: "Telegram",
  voice: "Call",
  api: "API",
};

export const STATE_LABEL: Record<string, string> = {
  open: "Open",
  awaiting_customer: "Waiting on customer",
  awaiting_internal: "Waiting on us",
  snoozed: "Snoozed",
  resolved: "Resolved",
  reopened: "Reopened",
};
