import type { Product, User } from "./api";

/* One portal, two apps (ADR 0041). Each app is sold on its own plan; an
   organisation sees an app only while it holds that plan, and its owners and
   admins choose which people may open it. The server enforces all of this; the
   portal only follows it. Internal names stay "commai"; people see "Jibsy". */

export type AppId = Product;
/** Which part of the portal a screen belongs to: an app, the organisation's
 * own set-up ("account": company, locations, people, plans, your profile), or
 * ExaCarib's operations screens (staff only). ADR 0043. */
export type Area = AppId | "account" | "ops";

/** What each non-app area is called in the switcher. */
export const AREA_NAME: Record<"account" | "ops", string> = { account: "Organisation", ops: "Operations" };

/** The conversations app's name, shown as "Jibsy by ExaCarib" where it stands alone. */
export const JIBSY = "Jibsy";

export const APP_INFO: Record<AppId, { name: string; full: string; blurb: string; home: string }> = {
  connect: { name: "Connect", full: "ExaCarib Connect", blurb: "Sites, paths, SLA and Storm Mode", home: "/" },
  commai: { name: JIBSY, full: "Jibsy by ExaCarib", blurb: "Inbox, AI agents, phone and workflows", home: "/commai" },
};

export const APP_ORDER: AppId[] = ["connect", "commai"];

/** Shared pages that belong to no single app. */
const ACCOUNT_PATHS = ["/org", "/account", "/billing"];
/** ExaCarib staff screens. */
const OPS_PATHS = ["/ops", "/admin", "/commai/golive", "/commai/support"];

const under = (path: string, p: string) => path === p || path.startsWith(p + "/");

/** Which part of the portal a path belongs to. */
export function areaFor(path: string): Area {
  if (OPS_PATHS.some((p) => under(path, p))) return "ops";
  if (path === "/commai" || path.startsWith("/commai/")) return "commai";
  if (ACCOUNT_PATHS.some((p) => under(path, p))) return "account";
  return "connect";
}

/** The apps this person may open in the organisation they act for. ExaCarib
 * admins see both; carrier accounts have Connect's carrier view only. */
export function myApps(user: User | null): AppId[] {
  if (!user) return [];
  if (user.role === "carrier") return ["connect"];
  const allowed = user.apps ?? user.products ?? null;
  return allowed ? APP_ORDER.filter((a) => allowed.includes(a)) : APP_ORDER;
}

/** Each app's mark: a tinted tile with the app's own glyph. */
export function AppMark({ app, size = 28 }: { app: Area; size?: number }) {
  const glyph =
    app === "connect" ? (
      <>
        <circle cx="5" cy="12" r="2.5" />
        <circle cx="19" cy="5" r="2.5" />
        <circle cx="19" cy="19" r="2.5" />
        <path d="M7.3 11L16.7 6M7.3 13l9.4 5" />
      </>
    ) : app === "commai" ? (
      <>
        <path d="M4 5h16v11H9l-5 4z" />
        <path d="M8 9h8M8 12h5" />
      </>
    ) : app === "ops" ? (
      <>
        <path d="M12 3l7 3v5.5c0 4.4-3 8-7 9.5-4-1.5-7-5.1-7-9.5V6z" />
        <path d="M9 12l2 2 4-4" />
      </>
    ) : (
      <>
        <path d="M4 20V9l8-5 8 5v11" />
        <path d="M9 20v-6h6v6" />
      </>
    );
  return (
    <span className={`app-mark app-mark-${app}`} style={{ width: size, height: size }} aria-hidden="true">
      <svg
        width={size * 0.58}
        height={size * 0.58}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {glyph}
      </svg>
    </span>
  );
}
