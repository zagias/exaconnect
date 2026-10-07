import type { Product, User } from "./api";

/* One portal, two apps (ADR 0040). Each app is sold on its own plan; an
   organisation sees an app only while it holds that plan, and its owners and
   admins choose which people may open it. The server enforces all of this; the
   portal only follows it. Internal names stay "commai"; people see "Jibsy". */

export type AppId = Product;
/** Which part of the portal a screen belongs to: an app, or the shared account pages. */
export type Area = AppId | "account";

/** The conversations app's name, shown as "Jibsy by ExaCarib" where it stands alone. */
export const JIBSY = "Jibsy";

export const APP_INFO: Record<AppId, { name: string; full: string; blurb: string; home: string }> = {
  connect: { name: "Connect", full: "ExaCarib Connect", blurb: "Sites, paths, SLA and Storm Mode", home: "/" },
  commai: { name: JIBSY, full: "Jibsy by ExaCarib", blurb: "Inbox, AI agents, phone and workflows", home: "/commai" },
};

export const APP_ORDER: AppId[] = ["connect", "commai"];

/** Shared pages that belong to no single app. */
const ACCOUNT_PATHS = ["/account", "/billing"];

/** Which part of the portal a path belongs to. */
export function areaFor(path: string): Area {
  if (path === "/commai" || path.startsWith("/commai/")) return "commai";
  if (ACCOUNT_PATHS.some((p) => path === p || path.startsWith(p + "/"))) return "account";
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
    ) : (
      <>
        <circle cx="12" cy="8" r="3.5" />
        <path d="M5 20c1-4 4-5.5 7-5.5s6 1.5 7 5.5" />
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
