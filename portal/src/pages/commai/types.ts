/** Shapes returned by the CommAI inbox API (controller/exaconnect_controller/commai/api/inbox.py). */

export interface Conversation {
  id: string;
  channel: string;
  subject: string;
  state: string;
  priority: "low" | "normal" | "high" | "urgent";
  team_id: string | null;
  team_name: string | null;
  queue: string;
  assignee_id: string | null;
  assignee_email: string | null;
  handler: "none" | "ai" | "human";
  handler_user_id: string | null;
  handler_email: string | null;
  language: string;
  intent?: string;
  tags: string[];
  contact_id: string | null;
  contact_name: string | null;
  contact_email: string | null;
  contact_phone: string | null;
  contact_address: string | null;
  identity_verified: boolean | null;
  preview: string | null;
  first_reply_due: string | null;
  resolve_due: string | null;
  first_reply_at: string | null;
  first_reply_overdue: boolean;
  resolve_overdue: boolean;
  last_inbound_at: string | null;
  last_message_at: string | null;
  created_at: string;
}

export interface Message {
  id: string;
  direction: "in" | "out";
  author_kind: "contact" | "user" | "ai" | "system" | "workflow";
  author: string;
  body: string;
  original_body: string;
  original_language: string;
  template: string;
  status: string;
  error: string;
  created_at: string;
  attachments?: Attachment[];
}

export interface Attachment {
  id: string;
  name: string;
  type: string;
  size: number;
}

/** Someone typing in a conversation, from the inbox live feed (ADR 0038). */
export interface Typing {
  name: string;
  who_kind: "user" | "contact";
  until: number;
}

export interface Note {
  id: string;
  author: string;
  body: string;
  mentions: string[];
  attachments?: { id: string; name: string; type: string; size: number }[];
  created_at: string;
}

export interface LogEntry {
  at: string;
  actor: string;
  kind: string;
  from_value: string;
  to_value: string;
  reason: string;
}

export interface Handover {
  at: string;
  reason: string;
  packet: Record<string, unknown>;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
  log: LogEntry[];
  handovers: Handover[];
  your_seat: "admin" | "agent" | "internal";
}

export interface Team {
  id: string;
  name: string;
  skills: string[];
  members: string[];
}

export interface Member {
  id: string;
  email: string;
  seat: "agent" | "internal";
  skills: string[];
  languages: string[];
  available: boolean;
  open_conversations: number;
}

export interface Contact {
  id: string;
  name: string;
  email: string;
  phone: string;
  language: string;
  external_ref: string;
  created_at: string;
  conversations?: number;
}
