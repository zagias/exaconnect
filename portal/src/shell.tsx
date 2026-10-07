import { useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { api, useApi } from "./api";
import { useAuth } from "./auth";
import type { OrgApps, User } from "./api";
import { APP_INFO, APP_ORDER, AppMark, areaFor, myApps, type AppId, type Area } from "./apps";
import { CustomerPicker, OrganisationSwitcher, StormBanner, StormSwitch } from "./customer";
import { JumpTo } from "./jump";
import { accountGroups, icons, matches, navGroups, type Group } from "./nav";
import { useBrand } from "./pages/commai/partner/brand";
import { useCommaiBase } from "./pages/commai/lib";
import "./shell.css";

/** The current screen's group and name, for the context line in the top bar. */
function pageContext(groups: Group[], path: string): { group: string | null; label: string } | null {
  if (path.startsWith("/commai/me")) return { group: "Your account", label: "My settings" };
  for (const g of groups) for (const i of g.items) if (matches(i, path)) return { group: g.label, label: i.label };
  return null;
}

function initials(email: string | undefined): string {
  if (!email) return "?";
  const local = email.split("@")[0];
  const parts = local.split(/[._-]+/).filter(Boolean);
  const s = parts.length > 1 ? parts[0][0] + parts[1][0] : local.slice(0, 2);
  return s.toUpperCase();
}

const ROLE_NAMES: Record<string, string> = { admin: "ExaCarib admin", customer: "Customer", carrier: "Carrier (read-only)" };
const ORG_ROLE_NAMES: Record<string, string> = { owner: "Owner", admin: "Admin", member: "Member", viewer: "Viewer (read-only)" };

/** "Admin, Example Bank" for organisation members; the account role otherwise. */
function roleLine(user: User | null): string {
  if (user?.role === "customer" && user.org_role)
    return `${ORG_ROLE_NAMES[user.org_role] ?? user.org_role}${user.organisation ? `, ${user.organisation}` : ""}`;
  return ROLE_NAMES[user?.role ?? ""] ?? user?.role ?? "";
}

/* ---- Shell ---- */

const PHONE = "(max-width: 640px)";
const MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

/** The portal frame: navy sidebar (icon rail on tablets, drawer on phones), a slim top bar, the Storm banner and the page. */
export function Shell({ children }: { children: ReactNode }) {
  const { user, signOut } = useAuth();
  const carrier = user?.role === "carrier";
  // The apps this person may open (ADR 0040); the sidebar shows one at a time.
  const mine = myApps(user);
  const connect = mine.includes("connect");
  const jibsy = mine.includes("commai");
  const all = navGroups(user?.role, useModules(!carrier && jibsy), carrier ? null : mine);
  const account = accountGroups({ carrier, jibsy });
  // A partner's white-label brand (ADR 0031); null keeps ExaCarib's own look.
  const brand = useBrand();
  const location = useLocation();
  const asked = areaFor(location.pathname);
  // On a screen of an app the person can't open, the menu stays on one they can.
  const area: Area = carrier ? "connect" : asked === "account" || mine.includes(asked) ? asked : (mine[0] ?? "account");
  const groups = area === "account" ? account : all.filter((g) => !g.app || g.app === area);
  const ctx = pageContext([...all, ...account], location.pathname);

  // The tab says which app you are in.
  useEffect(() => {
    if (brand) return;
    document.title = area === "account" ? "Account · ExaCarib" : `${APP_INFO[area].name} · ExaCarib`;
  }, [area, brand]);
  const [drawer, setDrawer] = useState(false);
  const [jump, setJump] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);

  // Close the drawer on navigation.
  useEffect(() => setDrawer(false), [location.pathname]);

  // Keep the current screen's menu entry in view (the menu is longer than most screens).
  useEffect(() => {
    const side = document.getElementById("shell-nav");
    const link = side?.querySelector<HTMLElement>(".shell-link.active");
    if (!side || !link) return;
    const s = side.getBoundingClientRect();
    const l = link.getBoundingClientRect();
    if (l.top < s.top + 60 || l.bottom > s.bottom - 16) side.scrollTop += l.top - s.top - s.height / 3;
  }, [location.pathname, groups.length, drawer]);

  // While the drawer is open: lock body scroll, close on Escape, and put focus on its close button.
  useEffect(() => {
    if (!drawer) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButton.current?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setDrawer(false);
        menuButton.current?.focus();
      }
    };
    // If the window grows past phone width, the drawer stops being a drawer.
    const mq = window.matchMedia(PHONE);
    const onMq = () => !mq.matches && setDrawer(false);
    document.addEventListener("keydown", onKey);
    mq.addEventListener("change", onMq);
    return () => {
      document.body.style.overflow = prev;
      document.removeEventListener("keydown", onKey);
      mq.removeEventListener("change", onMq);
    };
  }, [drawer]);

  const closeDrawer = () => {
    setDrawer(false);
    menuButton.current?.focus();
  };

  return (
    <div className="shell">
      <aside id="shell-nav" className="shell-side" data-open={drawer || undefined} data-app={area} aria-label="Portal">
        <div className="shell-brand">
          <NavLink to="/" className="shell-brand-link" aria-label={`${brand ? brand.product_name : "ExaCarib"}, home`}>
            {brand?.logo_url ? (
              <img className="shell-wordmark shell-partner-logo" src={brand.logo_url} alt={brand.product_name} />
            ) : (
              <img className="shell-wordmark" src="/brand/exacarib-wordmark-reversed.png" alt="ExaCarib" width={140} height={25} />
            )}
            <span className="shell-symbol" aria-hidden="true">
              <img src="/brand/exacarib-wordmark-reversed.png" alt="" width={141} height={25} />
            </span>
          </NavLink>
          {brand && <span className="shell-product">{brand.product_name}</span>}
          <button ref={closeButton} className="shell-iconbtn shell-close" aria-label="Close menu" onClick={closeDrawer}>
            {icons.close}
          </button>
        </div>

        <div className="shell-apps">
          <AppSwitcher area={area} mine={mine} />
        </div>

        <nav aria-label={area === "account" ? "Account" : APP_INFO[area].name} className="shell-nav">
          {groups.map((g, gi) => (
            <NavGroup key={`${area}${g.label ?? gi}`} g={g} gi={gi} path={location.pathname} />
          ))}
        </nav>

        {/* Phone drawer only: who is signed in, the customer picker, Account and Sign out. */}
        <div className="shell-drawer-user">
          <div className="shell-user-id">
            <span className="shell-avatar" aria-hidden="true">
              {initials(user?.email)}
            </span>
            <span className="shell-user-text">
              <span className="shell-user-email">{user?.email}</span>
              <span className="shell-user-role">{roleLine(user)}</span>
            </span>
          </div>
          {user?.role === "admin" && (
            <div className="shell-drawer-picker">
              <CustomerPicker />
            </div>
          )}
          {(user?.memberships?.length ?? 0) > 1 && (
            <div className="shell-drawer-picker">
              <OrganisationSwitcher />
            </div>
          )}
          <ul>
            <li>
              <NavLink to="/account" end className="shell-link">
                {icons.account}
                <span className="shell-link-text">Account</span>
              </NavLink>
            </li>
            {!carrier && (
              <li>
                <NavLink to="/account/people" className="shell-link">
                  {icons.people}
                  <span className="shell-link-text">People</span>
                </NavLink>
              </li>
            )}
            {!carrier && jibsy && (
              <li>
                <NavLink to="/commai/me" className="shell-link">
                  {icons.admin}
                  <span className="shell-link-text">My settings</span>
                </NavLink>
              </li>
            )}
            <li>
              <button className="shell-link" onClick={signOut}>
                {icons.signout}
                <span className="shell-link-text">Sign out</span>
              </button>
            </li>
          </ul>
        </div>
      </aside>

      {drawer && <div className="shell-backdrop" onClick={closeDrawer} aria-hidden="true" />}

      <div className="shell-main">
        <header className="shell-top">
          <button
            ref={menuButton}
            className="shell-iconbtn shell-menu"
            aria-label="Open menu"
            aria-controls="shell-nav"
            aria-expanded={drawer}
            onClick={() => setDrawer(true)}
          >
            {icons.menu}
          </button>
          <NavLink to="/" className="shell-top-brand" aria-label={`${brand ? brand.product_name : "ExaCarib"}, home`}>
            {brand?.logo_url ? (
              <img className="shell-partner-logo" src={brand.logo_url} alt={brand.product_name} height={20} />
            ) : (
              <img src="/brand/exacarib-wordmark-reversed.png" alt="ExaCarib" width={112} height={20} />
            )}
          </NavLink>
          <div className="shell-context" aria-hidden={ctx ? undefined : true}>
            {ctx?.group && <span className="shell-context-group">{ctx.group}</span>}
            {ctx && <span className="shell-context-page">{ctx.label}</span>}
          </div>
          <div className="shell-actions">
            <button
              className="shell-jump-button"
              onClick={() => setJump(true)}
              aria-haspopup="dialog"
              aria-keyshortcuts="Control+K Meta+K /"
              aria-label="Jump to a screen"
              title="Jump to a screen (Ctrl+K or /)"
            >
              {icons.search}
              <span className="shell-jump-text" aria-hidden="true">
                Jump to…
              </span>
              <kbd className="shell-jump-kbd" aria-hidden="true">
                {MAC ? "⌘K" : "Ctrl K"}
              </kbd>
            </button>
            <div className="shell-top-picker">
              <OrganisationSwitcher />
            </div>
            {user?.role === "admin" && (
              <div className="shell-top-picker">
                <CustomerPicker />
              </div>
            )}
            {/* Storm Mode switch: on every Connect screen, never for carriers. */}
            {!carrier && connect && area === "connect" && <StormSwitch />}
            <UserMenu />
          </div>
        </header>
        {!carrier && connect && <StormBanner />}
        <main className="page" id="main" tabIndex={-1}>
          {children}
        </main>
      </div>
      <JumpTo groups={[...all, ...account]} open={jump} setOpen={setJump} />
    </div>
  );
}

/** Initials and email; opens a small menu with the role, Account and Sign out. */
function UserMenu() {
  const { user, signOut } = useAuth();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const location = useLocation();

  useEffect(() => setOpen(false), [location.pathname]);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        button.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="shell-user" ref={box}>
      <button
        ref={button}
        className="shell-user-button"
        aria-haspopup="true"
        aria-expanded={open}
        aria-label={`Account menu for ${user?.email ?? "you"}`}
        onClick={() => setOpen(!open)}
      >
        <span className="shell-avatar" aria-hidden="true">
          {initials(user?.email)}
        </span>
        <span className="shell-user-email">{user?.email}</span>
        {icons.chevron}
      </button>
      {open && (
        <div className="shell-user-menu card">
          <div className="shell-user-menu-head">
            <span className="shell-user-menu-email">{user?.email}</span>
            <span className="shell-user-role">{roleLine(user)}</span>
          </div>
          <ul>
            <li>
              <NavLink to="/account" end className="shell-menu-item">
                {icons.account}
                Account
              </NavLink>
            </li>
            {user?.role !== "carrier" && (
              <li>
                <NavLink to="/account/people" className="shell-menu-item">
                  {icons.people}
                  People
                </NavLink>
              </li>
            )}
            {user?.role !== "carrier" && myApps(user).includes("commai") && (
              <li>
                <NavLink to="/commai/me" className="shell-menu-item">
                  {icons.admin}
                  My settings
                </NavLink>
              </li>
            )}
            <li>
              <button className="shell-menu-item" onClick={signOut}>
                {icons.signout}
                Sign out
              </button>
            </li>
          </ul>
        </div>
      )}
    </div>
  );
}

/** The app switcher at the top of the sidebar: the apps this person may open,
 * apps the organisation could add (owners and admins only), and the shared
 * account pages. With one app and nothing to offer it is a plain label. */
function AppSwitcher({ area, mine }: { area: Area; mine: AppId[] }) {
  const { user } = useAuth();
  const manager = user?.role === "customer" && (user.org_role === "owner" || user.org_role === "admin");
  const plan = useApi<OrgApps>(manager && user?.customer_id ? `/orgs/${user.customer_id}/apps` : null, 300_000);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number }>({ top: 0, left: 0 });
  const box = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const location = useLocation();
  const offers = (plan.data?.apps ?? []).filter((a) => a.status !== "active");
  const name = area === "account" ? "Account" : APP_INFO[area].name;

  useEffect(() => setOpen(false), [location.pathname]);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        button.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // One app and nothing to offer: a plain label, or a way home from Account.
  if (mine.length <= 1 && offers.length === 0) {
    const only = mine[0];
    if (area !== "account" || !only)
      return (
        <span className="app-switch app-switch-static">
          <AppMark app={area} />
          <span className="app-switch-name">{name}</span>
        </span>
      );
    return (
      <NavLink to={APP_INFO[only].home} className="app-switch app-switch-static" title={`Back to ${APP_INFO[only].name}`}>
        <AppMark app={only} />
        <span className="app-switch-name">{APP_INFO[only].name}</span>
        <span className="app-switch-hint">Back</span>
      </NavLink>
    );
  }

  const ask = async (app: AppId) => {
    setError(null);
    try {
      await api(`/orgs/${user?.customer_id}/apps/${app}/request`, { method: "POST" });
      plan.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="app-switch-box" ref={box}>
      <button
        ref={button}
        className="app-switch"
        aria-haspopup="true"
        aria-expanded={open}
        aria-controls="app-switch-menu"
        title="Switch app"
        onClick={() => {
          // The sidebar scrolls, so the menu is placed on the page: beside the
          // button on the tablet rail and under it elsewhere.
          const r = button.current?.getBoundingClientRect();
          if (r) setPos(r.width < 80 ? { top: r.top, left: r.right + 8 } : { top: r.bottom + 6, left: r.left });
          setOpen(!open);
        }}
      >
        <AppMark app={area} />
        <span className="app-switch-name">{name}</span>
        {icons.chevron}
      </button>
      {open && (
        <div id="app-switch-menu" className="app-switch-menu card" style={{ top: pos.top, left: pos.left }}>
          <div className="app-switch-head">Your apps</div>
          <ul>
            {APP_ORDER.filter((a) => mine.includes(a)).map((a) => (
              <li key={a}>
                <NavLink to={APP_INFO[a].home} end className="app-switch-item" aria-current={area === a ? "true" : undefined}>
                  <AppMark app={a} size={36} />
                  <span className="app-switch-text">
                    <span className="app-switch-title">{APP_INFO[a].full}</span>
                    <span className="app-switch-blurb">{APP_INFO[a].blurb}</span>
                  </span>
                  {area === a && (
                    <span className="app-switch-current">
                      {icons.check}
                      <span className="sr-only">Current app</span>
                    </span>
                  )}
                </NavLink>
              </li>
            ))}
          </ul>
          {offers.length > 0 && (
            <>
              <div className="app-switch-head">Add to your plan</div>
              <ul>
                {offers.map((o) => (
                  <li key={o.id} className="app-switch-offer">
                    <AppMark app={o.id} size={36} />
                    <span className="app-switch-text">
                      <span className="app-switch-title">{APP_INFO[o.id].full}</span>
                      <span className="app-switch-blurb">{APP_INFO[o.id].blurb}</span>
                      {o.status === "requested" ? (
                        <span className="app-switch-note">Requested. ExaCarib will be in touch.</span>
                      ) : (
                        <button type="button" className="button small" onClick={() => ask(o.id)}>
                          Ask to add {APP_INFO[o.id].name}
                        </button>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
              {error && <p className="app-switch-note error">{error}</p>}
            </>
          )}
          <NavLink to={manager ? "/account/apps" : "/account"} className="app-switch-foot">
            {manager ? "Account, people and plans" : "Your account"}
          </NavLink>
        </div>
      )}
    </div>
  );
}

/** The Jibsy modules of the business in view (null until known, or when the plan can't be read). */
function useModules(on: boolean): Record<string, boolean> | null {
  const base = useCommaiBase();
  const plan = useApi<{ modules: { module: string; enabled: boolean }[] }>(on && base ? `${base}/entitlements` : null, 120_000);
  if (!plan.data) return null;
  return Object.fromEntries(plan.data.modules.map((m) => [m.module, m.enabled]));
}

/** One menu group: an optional product heading, a label and its links. A
 * collapsible group starts folded unless one of its screens is current. */
function NavGroup({ g, gi, path }: { g: Group; gi: number; path: string }) {
  const current = g.items.some((i) => matches(i, path));
  const [open, setOpen] = useState(current);
  useEffect(() => {
    if (current) setOpen(true);
  }, [current]);
  const id = `nav-g-${gi}`;
  return (
    <>
      <div className="shell-group" data-collapsed={(g.collapsible && !open) || undefined}>
        {g.label &&
          (g.collapsible ? (
            <button
              type="button"
              className="shell-group-label shell-group-toggle"
              id={id}
              aria-expanded={open}
              aria-controls={`${id}-list`}
              onClick={() => setOpen(!open)}
            >
              <span>{g.label}</span>
              {icons.chevron}
            </button>
          ) : (
            <div className="shell-group-label" id={id}>
              {g.label}
            </div>
          ))}
        <ul id={`${id}-list`} aria-labelledby={g.label ? id : undefined}>
          {g.items.map((i) => {
            const here = matches(i, path);
            return (
              <li key={i.to}>
                <NavLink
                  to={i.to}
                  end={i.end}
                  className={({ isActive }) => (isActive || here ? "shell-link active" : "shell-link")}
                  aria-current={here ? "page" : undefined}
                  title={i.label}
                >
                  {i.icon}
                  <span className="shell-link-text">{i.label}</span>
                </NavLink>
              </li>
            );
          })}
        </ul>
      </div>
    </>
  );
}
