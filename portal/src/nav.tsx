import type { ReactNode } from "react";
import { APP_INFO, type AppId } from "./apps";

/* The portal's menu: icons, groups, and every screen and tab for the jump-to search. */

/* ---- Icons: 24 px grid, drawn at 18 px, stroke only, currentColor ---- */

function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      className="shell-icon"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

export const icons = {
  overview: (
    <Icon>
      <rect x="3" y="3" width="7" height="9" rx="1.5" />
      <rect x="14" y="3" width="7" height="5" rx="1.5" />
      <rect x="14" y="12" width="7" height="9" rx="1.5" />
      <rect x="3" y="16" width="7" height="5" rx="1.5" />
    </Icon>
  ),
  sites: (
    <Icon>
      <path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11z" />
      <circle cx="12" cy="10" r="2.4" />
    </Icon>
  ),
  decisions: (
    <Icon>
      <circle cx="6" cy="5" r="2" />
      <circle cx="6" cy="19" r="2" />
      <circle cx="18" cy="8" r="2" />
      <path d="M6 7v10" />
      <path d="M18 10c0 4-6 3.5-11 7.5" />
    </Icon>
  ),
  insights: (
    <Icon>
      <path d="M9 18h6" />
      <path d="M10 21h4" />
      <path d="M12 3a6 6 0 0 0-3.6 10.8c.7.5 1.1 1.3 1.1 2.2h5c0-.9.4-1.7 1.1-2.2A6 6 0 0 0 12 3z" />
    </Icon>
  ),
  ask: (
    <Icon>
      <path d="M20 12a7.5 7.5 0 0 1-11 6.6L4 20l1.4-4.4A7.5 7.5 0 1 1 20 12z" />
      <path d="M9.8 10a2.3 2.3 0 0 1 4.4.8c0 1.5-2.2 1.8-2.2 3" />
      <path d="M12 16.4h.01" />
    </Icon>
  ),
  traffic: (
    <Icon>
      <path d="M4 8h14" />
      <path d="M15 5l3 3-3 3" />
      <path d="M20 16H6" />
      <path d="M9 13l-3 3 3 3" />
    </Icon>
  ),
  fabric: (
    <Icon>
      <circle cx="12" cy="5" r="2" />
      <circle cx="5" cy="18" r="2" />
      <circle cx="19" cy="18" r="2" />
      <path d="M11 6.8L6 16.2" />
      <path d="M13 6.8l5 9.4" />
      <path d="M7 18h10" />
    </Icon>
  ),
  internet: (
    <Icon>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18" />
      <path d="M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z" />
    </Icon>
  ),
  encryption: (
    <Icon>
      <rect x="4.5" y="10.5" width="15" height="10" rx="2" />
      <path d="M8 10.5V7.5a4 4 0 0 1 8 0v3" />
      <path d="M12 14.5v2" />
    </Icon>
  ),
  order: (
    <Icon>
      <path d="M9 4h6" />
      <path d="M9 3h6v3H9z" />
      <path d="M7 4.5H6a1.5 1.5 0 0 0-1.5 1.5v13.5A1.5 1.5 0 0 0 6 21h12a1.5 1.5 0 0 0 1.5-1.5V6A1.5 1.5 0 0 0 18 4.5h-1" />
      <path d="M12 10.5v6" />
      <path d="M9 13.5h6" />
    </Icon>
  ),
  metering: (
    <Icon>
      <path d="M4 20h16" />
      <path d="M7 16v-5" />
      <path d="M12 16V6" />
      <path d="M17 16v-8" />
    </Icon>
  ),
  carrier: (
    <Icon>
      <path d="M12 10v11" />
      <path d="M8.5 21h7" />
      <circle cx="12" cy="8.5" r="1.5" />
      <path d="M8.2 4.8a5.3 5.3 0 0 0 0 7.4" />
      <path d="M15.8 4.8a5.3 5.3 0 0 1 0 7.4" />
      <path d="M5.4 2.5a9 9 0 0 0 0 12" />
      <path d="M18.6 2.5a9 9 0 0 1 0 12" />
    </Icon>
  ),
  admin: (
    <Icon>
      <path d="M4 6h9" />
      <path d="M17 6h3" />
      <circle cx="15" cy="6" r="2" />
      <path d="M4 12h3" />
      <path d="M11 12h9" />
      <circle cx="9" cy="12" r="2" />
      <path d="M4 18h11" />
      <path d="M19 18h1" />
      <circle cx="17" cy="18" r="2" />
    </Icon>
  ),
  account: (
    <Icon>
      <circle cx="12" cy="8" r="4" />
      <path d="M4.5 21a7.5 7.5 0 0 1 15 0" />
    </Icon>
  ),
  signout: (
    <Icon>
      <path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4" />
      <path d="M10 8l-4 4 4 4" />
      <path d="M6 12h10" />
    </Icon>
  ),
  menu: (
    <Icon>
      <path d="M4 7h16" />
      <path d="M4 12h16" />
      <path d="M4 17h16" />
    </Icon>
  ),
  close: (
    <Icon>
      <path d="M6 6l12 12" />
      <path d="M18 6L6 18" />
    </Icon>
  ),
  chevron: (
    <Icon>
      <path d="M7 10l5 5 5-5" />
    </Icon>
  ),
  inbox: (
    <Icon>
      <path d="M3.5 13.5l2.6-7.2A1.5 1.5 0 0 1 7.5 5.3h9a1.5 1.5 0 0 1 1.4 1l2.6 7.2" />
      <path d="M3.5 13.5V18a1.5 1.5 0 0 0 1.5 1.5h14a1.5 1.5 0 0 0 1.5-1.5v-4.5h-5l-1.5 2.5h-4l-1.5-2.5z" />
    </Icon>
  ),
  people: (
    <Icon>
      <circle cx="9" cy="8" r="3.2" />
      <path d="M3 20a6 6 0 0 1 12 0" />
      <path d="M15.5 4.9a3.2 3.2 0 0 1 0 6.2" />
      <path d="M18 14.2A6 6 0 0 1 21 20" />
    </Icon>
  ),
  chat: (
    <Icon>
      <path d="M4 5.5h16v10H9l-4 3.5v-3.5H4z" />
      <path d="M8 9.5h8" />
      <path d="M8 12.5h5" />
    </Icon>
  ),
  phone: (
    <Icon>
      <path d="M6.5 3.5h3l1.5 4.5-2 1.3a10.5 10.5 0 0 0 5.7 5.7l1.3-2 4.5 1.5v3A2 2 0 0 1 18.5 19.5 15.5 15.5 0 0 1 4.5 5.5a2 2 0 0 1 2-2z" />
    </Icon>
  ),
  check: (
    <Icon>
      <rect x="4" y="4" width="16" height="16" rx="3" />
      <path d="M8.5 12.2l2.4 2.4 4.6-5" />
    </Icon>
  ),
  apps: (
    <Icon>
      <rect x="4" y="4" width="6.5" height="6.5" rx="1.5" />
      <rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5" />
      <rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5" />
      <path d="M16.75 13.5v6.5" />
      <path d="M13.5 16.75H20" />
    </Icon>
  ),
  star: (
    <Icon>
      <path d="M12 3.8l2.5 5.1 5.6.8-4 3.9 1 5.6-5.1-2.7-5 2.7 1-5.6-4.1-3.9 5.6-.8z" />
    </Icon>
  ),
  billing: (
    <Icon>
      <path d="M6 3h12v18l-3-2-3 2-3-2-3 2z" />
      <path d="M9 8h6" />
      <path d="M9 12h6" />
      <path d="M9 16h3" />
    </Icon>
  ),
  receipt: (
    <Icon>
      <path d="M6 3.5h12v17l-2.5-1.5-2 1.5-1.5-1.5-1.5 1.5-2-1.5L6 20.5z" />
      <path d="M9 8h6" />
      <path d="M9 11.5h6" />
      <path d="M9 15h3" />
    </Icon>
  ),
  flag: (
    <Icon>
      <path d="M5 21V4" />
      <path d="M5 4.5h11l-2 4 2 4H5" />
    </Icon>
  ),
  language: (
    <Icon>
      <path d="M4 5.5h9" />
      <path d="M8.5 3.5v2" />
      <path d="M11 5.5c-.6 3.6-2.9 6.5-6.5 8" />
      <path d="M6.5 8.5c1 2 2.8 3.7 5 4.5" />
      <path d="M12.5 20.5l3.5-8.5 3.5 8.5" />
      <path d="M13.8 17.5h4.4" />
    </Icon>
  ),
  shield: (
    <Icon>
      <path d="M12 3l7 3v5.5c0 4.4-3 8-7 9.5-4-1.5-7-5.1-7-9.5V6z" />
      <path d="M9.2 12l2 2 3.6-3.8" />
    </Icon>
  ),
  building: (
    <Icon>
      <path d="M4 21V5.5A1.5 1.5 0 0 1 5.5 4h7A1.5 1.5 0 0 1 14 5.5V21" />
      <path d="M14 10h4.5a1.5 1.5 0 0 1 1.5 1.5V21" />
      <path d="M3 21h18" />
      <path d="M7.5 8h3M7.5 12h3M7.5 16h3" />
    </Icon>
  ),
  search: (
    <Icon>
      <circle cx="11" cy="11" r="6.5" />
      <path d="M16 16l4 4" />
    </Icon>
  ),
};

/* ---- Navigation model ---- */

export interface Item {
  to: string;
  label: string;
  icon: ReactNode;
  end?: boolean;
  /** Other paths that belong to this entry (it shows as current on them too). */
  also?: string[];
  /** The Jibsy module a business needs for this entry (commai/entitlements.py). */
  module?: string;
  /** Other words the jump-to search should match. */
  keywords?: string;
}
export interface Group {
  label: string | null;
  items: Item[];
  /** The app this group belongs to (ADR 0041); none for shared groups. */
  app?: AppId;
  /** Folded away until opened (or until one of its screens is current). */
  collapsible?: boolean;
}

/** Whether `path` is this entry's screen (or one of its tabs or detail pages). */
export function matches(i: Item, path: string): boolean {
  const one = (to: string, exact: boolean) => (exact || to === "/" ? path === to : path === to || path.startsWith(to + "/"));
  return one(i.to, !!i.end) || (i.also ?? []).some((a) => one(a, false));
}

/**
 * The portal menu for every app this person may open; the sidebar shows one
 * app's groups at a time. `modules` is the Jibsy plan of the business in view
 * (null while it loads, or when it can't be read): entries for modules the
 * business lacks are left out, and the server refuses their API calls anyway.
 * `apps` is what the person may open (null: everything).
 */
export function navGroups(
  role: string | undefined,
  modules: Record<string, boolean> | null = null,
  apps: readonly string[] | null = null,
): Group[] {
  if (role === "carrier")
    return [
      {
        label: null,
        items: [
          { to: "/", label: "Carrier view", icon: icons.carrier, end: true },
          { to: "/notices", label: "Notices", icon: icons.insights },
          { to: "/integrations", label: "Integrations", icon: icons.fabric },
        ],
      },
    ];
  const admin = role === "admin";
  const has = (i: Item) => !i.module || !modules || modules[i.module] !== false;
  // The apps this person may open (ADR 0023, 0041); null: not limited.
  const plan = (p: string) => !apps || apps.includes(p);
  // Plain names first; the old screen names stay searchable in jump-to.
  const connect: Group[] = [
    {
      app: "connect",
      label: "Monitor",
      items: [
        { to: "/", label: "Overview", icon: icons.overview, end: true },
        { to: "/sites", label: "Sites", icon: icons.sites },
        { to: "/decisions", label: "Routing moves", icon: icons.decisions, keywords: "decisions why traffic moved" },
        { to: "/insights", label: "Alerts", icon: icons.insights, keywords: "insights hurricane bill anomaly" },
        { to: "/notices", label: "Carrier notices", icon: icons.carrier, keywords: "faults maintenance" },
        { to: "/ask", label: "Ask", icon: icons.ask },
      ],
    },
    {
      app: "connect",
      label: "Set up",
      items: [
        { to: "/traffic", label: "Traffic rules", icon: icons.traffic, keywords: "traffic applications priorities classes" },
        { to: "/fabric", label: "Private links", icon: icons.fabric, keywords: "fabric virtual circuits cloud router" },
        { to: "/internet", label: "Internet access", icon: icons.internet, keywords: "breakout firewall port forwards" },
        { to: "/encryption", label: "Encryption", icon: icons.encryption },
        { to: "/integrations", label: "Integrations", icon: icons.fabric, keywords: "monitoring ticketing snmp syslog" },
      ],
    },
    {
      app: "connect",
      label: "Orders and usage",
      items: [
        { to: "/order", label: "Orders", icon: icons.order, keywords: "order circuits bandwidth" },
        { to: "/metering", label: "Usage", icon: icons.metering, keywords: "metering 95th percentile burst" },
        ...(admin ? [{ to: "/carrier", label: "Carrier view", icon: icons.carrier }] : []),
      ],
    },
  ];
  const groups: Group[] = plan("connect") ? connect : [];
  // Jibsy (ADR 0016), grouped by task: daily work first, set-up folded away.
  const commai: Group[] = [
    {
      label: "Conversations",
      items: [
        { to: "/commai", label: "Inbox", icon: icons.inbox, end: true, also: ["/commai/c"], keywords: "conversations messages chats" },
        { to: "/commai/contacts", label: "Contacts", icon: icons.people, keywords: "customers people" },
        { to: "/commai/voice", label: "Phone", icon: icons.phone, module: "voice", keywords: "voice calls phone system" },
        { to: "/commai/team", label: "Team", icon: icons.chat, keywords: "notifications staff chat colleagues" },
      ],
    },
    {
      label: "Automation",
      items: [
        { to: "/commai/ai", label: "AI agents", icon: icons.insights, module: "ai_agents", keywords: "bot assistant" },
        { to: "/commai/workflows", label: "Workflows", icon: icons.traffic, module: "automation", keywords: "automations" },
        { to: "/commai/approvals", label: "Approvals", icon: icons.check, keywords: "waiting sensitive actions" },
        { to: "/commai/integrations", label: "Apps", icon: icons.apps, module: "automation", also: ["/commai/catalogue"], keywords: "integrations catalogue connectors api" },
        { to: "/commai/assistant", label: "Assistant", icon: icons.ask, keywords: "help support cases" },
      ],
    },
    {
      label: "Reports",
      items: [
        { to: "/commai/reports", label: "Reports", icon: icons.metering, keywords: "outcomes usage" },
        { to: "/commai/quality", label: "Quality", icon: icons.star, keywords: "csat reviews satisfaction" },
        { to: "/commai/bill", label: "Usage and bill", icon: icons.receipt, keywords: "invoice billing budget plan" },
      ],
    },
    {
      label: "Set up",
      collapsible: true,
      items: [
        { to: "/commai/setup", label: "Getting started", icon: icons.flag, keywords: "set up onboarding" },
        { to: "/commai/channels", label: "Channels", icon: icons.internet, module: "messaging", keywords: "whatsapp sms email website chat" },
        { to: "/commai/countries", label: "Countries", icon: icons.sites, module: "messaging", keywords: "sms senders" },
        { to: "/commai/languages", label: "Languages", icon: icons.language, keywords: "translation" },
        { to: "/commai/governance", label: "AI governance", icon: icons.shield, keywords: "limits risk" },
        { to: "/commai/settings", label: "Settings", icon: icons.admin, keywords: "service teams routing sign-in security" },
        { to: "/commai/partner", label: admin ? "Partners" : "Partners and apps", icon: icons.carrier, keywords: "white-label oauth" },
      ],
    },
  ];
  if (plan("commai"))
    groups.push(
      ...commai.map((g) => ({ ...g, app: "commai" as const, items: g.items.filter(has) })).filter((g) => g.items.length > 0),
    );
  return groups;
}

/** ExaCarib's own screens (ADR 0043): kept apart from every customer's menus. */
export function opsGroups(): Group[] {
  return [
    {
      label: "Customers",
      items: [
        { to: "/ops/organisations", label: "Organisations", icon: icons.building, keywords: "customers new organisation onboard" },
        { to: "/admin/sites", label: "Sites and links", icon: icons.sites, keywords: "enrolment tokens customers" },
        { to: "/admin/classes", label: "Classes and SLA", icon: icons.traffic },
        { to: "/admin/users", label: "Accounts", icon: icons.people, keywords: "users staff carrier logins" },
        { to: "/admin/billing", label: "Billing", icon: icons.billing, keywords: "plans prices invoices" },
      ],
    },
    {
      label: "Platform",
      items: [
        { to: "/admin/agents", label: "Agents", icon: icons.carrier, keywords: "nodes boxes" },
        { to: "/admin/partners", label: "Partners", icon: icons.fabric },
        { to: "/admin/protection", label: "Protection", icon: icons.shield, keywords: "ddos" },
        { to: "/admin/integrations", label: "Integrations", icon: icons.apps },
        { to: "/admin/settings", label: "Settings", icon: icons.admin, keywords: "shadow mode storm" },
        { to: "/admin/releases", label: "Releases", icon: icons.order, keywords: "deploy rollback" },
      ],
    },
    {
      label: "Service",
      items: [
        { to: "/commai/golive", label: "Go-live", icon: icons.flag, keywords: "criteria switch on" },
        { to: "/commai/support", label: "Support queue", icon: icons.chat, keywords: "support cases" },
        { to: "/admin/audit", label: "Audit log", icon: icons.receipt },
      ],
    },
  ];
}

/** The organisation's own pages (ADR 0041, 0043): set-up, company, locations,
 * people, plans, and your own profile, in one place for both apps. */
export function accountGroups(opts: { carrier: boolean; jibsy: boolean; manager?: boolean }): Group[] {
  if (opts.carrier)
    return [{ label: "You", items: [{ to: "/account", label: "Profile and sign-in", icon: icons.account, end: true }] }];
  return [
    ...(opts.manager !== false
      ? [{ label: "Set up", items: [{ to: "/org/setup", label: "Set-up checklist", icon: icons.flag, keywords: "getting started onboarding steps" }] }]
      : []),
    {
      label: "Your organisation",
      items: [
        { to: "/org/company", label: "Company details", icon: icons.building, keywords: "name country address organisation" },
        { to: "/org/locations", label: "Locations", icon: icons.sites, keywords: "sites branches offices addresses emergency connect a location" },
        { to: "/account/people", label: "People and access", icon: icons.people, keywords: "members invite roles users apps" },
        { to: "/org/security", label: "Sign-in and security", icon: icons.shield, keywords: "single sign-on sso saml scim directory" },
      ],
    },
    {
      label: "Plans",
      items: [
        { to: "/account/apps", label: "Apps and plans", icon: icons.apps, keywords: "plan subscription add jibsy connect" },
        { to: "/billing", label: "Billing", icon: icons.billing, keywords: "invoices payments" },
      ],
    },
    {
      label: "You",
      items: [
        { to: "/account", label: "Profile and sign-in", icon: icons.account, end: true, keywords: "password two-step api keys" },
        ...(opts.jibsy ? [{ to: "/commai/me", label: "My settings", icon: icons.admin, keywords: "notifications phone" }] : []),
      ],
    },
  ];
}

/** The menu group a path belongs to ("Conversations", "Set up"...), for page eyebrows. */
export function groupOf(path: string): string | null {
  if (path === "/commai/me" || path.startsWith("/commai/me/")) return "You";
  for (const g of [...opsGroups(), ...navGroups("admin"), ...accountGroups({ carrier: false, jibsy: true })])
    for (const i of g.items) if (matches(i, path)) return g.label;
  return null;
}

/**
 * Tabs inside screens, for the jump-to search. Keep in step with each screen's
 * <Tabs>; an entry whose screen is hidden from someone is hidden here too.
 */
export const TABS: Record<string, [string, string, string?][]> = {
  "/traffic": [
    ["/traffic/applications", "Seen on your network"],
    ["/traffic/rules", "Rules"],
    ["/traffic/classes", "Classes"],
  ],
  "/commai/voice": [
    ["/commai/voice", "People and numbers", "phone system extensions sites"],
    ["/commai/voice/routing", "Call routing", "ivr hunt groups opening hours"],
    ["/commai/voice/changes", "Scheduled, history and bulk"],
    ["/commai/voice/access", "Access", "permissions"],
    ["/commai/voice/orders", "Orders"],
    ["/commai/voice/numbers", "Numbers", "dids"],
    ["/commai/voice/ports", "Ports", "porting bring numbers"],
    ["/commai/voice/emergency", "Emergency addresses", "911 999"],
    ["/commai/voice/fraud", "Fraud", "limits"],
    ["/commai/voice/billing", "Billing", "call charges"],
    ["/commai/voice/me", "My phone", "forwarding voicemail do not disturb"],
    ["/commai/voice/phone", "Browser phone", "dialer softphone call"],
  ],
  "/commai/ai": [
    ["/commai/ai", "Agent profile and mode"],
    ["/commai/ai/tools", "Tools it may use"],
    ["/commai/ai/knowledge", "Knowledge", "articles faq"],
    ["/commai/ai/gaps", "Knowledge gaps"],
    ["/commai/ai/call", "Try a browser call"],
  ],
  "/commai/quality": [
    ["/commai/quality", "Review", "criteria"],
    ["/commai/quality/flags", "Flags"],
    ["/commai/quality/gaps", "Knowledge gaps", "unanswered questions"],
    ["/commai/quality/follow-ups", "Follow-ups", "promises"],
    ["/commai/quality/satisfaction", "Satisfaction", "csat survey"],
  ],
  "/commai/integrations": [
    ["/commai/integrations", "Your apps", "integrations connections"],
    ["/commai/catalogue", "App catalogue", "webhooks your own api data exchange standards"],
  ],
  "/commai/channels": [
    ["/commai/channels/web", "Website chat", "widget"],
    ["/commai/channels/whatsapp", "WhatsApp"],
    ["/commai/channels/sms", "SMS"],
    ["/commai/channels/email", "Email"],
    ["/commai/channels/messenger", "Messenger", "facebook"],
    ["/commai/channels/instagram", "Instagram"],
    ["/commai/channels/telegram", "Telegram"],
  ],
  "/commai/settings": [
    ["/commai/settings", "Service", "reply targets hours mode"],
    ["/commai/settings/teams", "Teams and people", "seats members"],
    ["/commai/settings/routing", "Routing", "assignment rules"],
    ["/commai/settings/developers", "Webhooks and keys", "api developers"],
    ["/commai/settings/organisation", "Organisation", "business units"],
    ["/commai/settings/roles", "Roles", "permissions"],
    ["/commai/settings/security", "Security", "audit"],
    ["/commai/settings/data", "Data", "retention export delete"],
    ["/commai/settings/help-centre", "Help centre", "articles self-service"],
  ],
};

export interface Destination {
  to: string;
  label: string;
  /** Where it sits: "Conversations", "Settings"... */
  where: string;
  keywords: string;
}

/** Every screen and tab someone can open, for the jump-to search. */
export function destinations(groups: Group[], extra: Destination[] = []): Destination[] {
  const out: Destination[] = [];
  for (const g of groups)
    for (const i of g.items) {
      const where = [g.app ? APP_INFO[g.app].name : null, g.label].filter(Boolean).join(" · ");
      out.push({ to: i.to, label: i.label, where, keywords: i.keywords ?? "" });
      for (const [to, label, kw] of TABS[i.to] ?? []) out.push({ to, label, where: i.label, keywords: kw ?? "" });
    }
  return [...out, ...extra];
}
