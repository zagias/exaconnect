import { useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useAuth } from "./auth";
import { CustomerPicker, StormBanner, StormSwitch } from "./customer";
import { useBrand } from "./pages/commai/partner/brand";
import "./shell.css";

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

const icons = {
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
};

/* ---- Navigation model ---- */

interface Item {
  to: string;
  label: string;
  icon: ReactNode;
  end?: boolean;
}
interface Group {
  label: string | null;
  items: Item[];
}

function navGroups(role: string | undefined): Group[] {
  if (role === "carrier") return [{ label: null, items: [{ to: "/", label: "Carrier view", icon: icons.carrier, end: true }] }];
  const admin = role === "admin";
  const groups: Group[] = [
    {
      label: "Monitor",
      items: [
        { to: "/", label: "Overview", icon: icons.overview, end: true },
        { to: "/sites", label: "Sites", icon: icons.sites },
        { to: "/decisions", label: "Decisions", icon: icons.decisions },
        { to: "/insights", label: "Insights", icon: icons.insights },
        { to: "/ask", label: "Ask", icon: icons.ask },
      ],
    },
    {
      label: "Network",
      items: [
        { to: "/traffic", label: "Traffic", icon: icons.traffic },
        { to: "/fabric", label: "Fabric", icon: icons.fabric },
        { to: "/internet", label: "Internet", icon: icons.internet },
        { to: "/encryption", label: "Encryption", icon: icons.encryption },
      ],
    },
    {
      label: "Commercial",
      items: [
        { to: "/order", label: "Order", icon: icons.order },
        { to: "/metering", label: "Metering", icon: icons.metering },
        ...(admin ? [{ to: "/carrier", label: "Carrier view", icon: icons.carrier }] : []),
      ],
    },
  ];
  // CommAI (ADR 0016): customer service on the same sign-in and customers.
  groups.push({
    label: "CommAI",
    items: [
      { to: "/commai", label: "Inbox", icon: icons.ask, end: true },
      { to: "/commai/contacts", label: "Contacts", icon: icons.sites },
      { to: "/commai/channels", label: "Channels", icon: icons.internet },
      { to: "/commai/countries", label: "Countries", icon: icons.sites },
      { to: "/commai/ai", label: "AI agents", icon: icons.insights },
      { to: "/commai/workflows", label: "Workflows", icon: icons.traffic },
      { to: "/commai/integrations", label: "Integrations", icon: icons.fabric },
      { to: "/commai/voice", label: "Voice", icon: icons.decisions },
      { to: "/commai/reports", label: "Reports", icon: icons.metering },
      { to: "/commai/assistant", label: "Assistant", icon: icons.ask },
      { to: "/commai/setup", label: "Set up", icon: icons.order },
      { to: "/commai/settings", label: "Settings", icon: icons.admin },
      { to: "/commai/partner", label: admin ? "Partners" : "Partners and apps", icon: icons.carrier },
      { to: "/commai/me", label: "My settings", icon: icons.account },
    ],
  });
  if (admin)
    groups.push({
      label: "Manage",
      items: [
        { to: "/admin", label: "Admin", icon: icons.admin },
        { to: "/commai/golive", label: "Go-live", icon: icons.order },
      ],
    });
  return groups;
}

/** The current screen's group and name, for the context line in the top bar. */
function pageContext(groups: Group[], path: string): { group: string | null; label: string } | null {
  if (path === "/account") return { group: null, label: "Account" };
  for (const g of groups)
    for (const i of g.items) {
      const hit = i.end || i.to === "/" ? path === i.to : path === i.to || path.startsWith(i.to + "/");
      if (hit) return { group: g.label, label: i.label };
    }
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

/* ---- Shell ---- */

const PHONE = "(max-width: 640px)";

/** The portal frame: navy sidebar (icon rail on tablets, drawer on phones), a slim top bar, the Storm banner and the page. */
export function Shell({ children }: { children: ReactNode }) {
  const { user, signOut } = useAuth();
  const carrier = user?.role === "carrier";
  const groups = navGroups(user?.role);
  // A partner's white-label brand (ADR 0025); null keeps ExaCarib's own look.
  const brand = useBrand();
  const location = useLocation();
  const ctx = pageContext(groups, location.pathname);
  const [drawer, setDrawer] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);

  // Close the drawer on navigation.
  useEffect(() => setDrawer(false), [location.pathname]);

  // While the drawer is open: lock body scroll, close on Escape, and put focus on its close button.
  useEffect(() => {
    if (!drawer) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButton.current?.focus();
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
      <aside id="shell-nav" className="shell-side" data-open={drawer || undefined} aria-label="Portal">
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
          <span className="shell-product">{brand ? brand.product_name : "Connect"}</span>
          <button ref={closeButton} className="shell-iconbtn shell-close" aria-label="Close menu" onClick={closeDrawer}>
            {icons.close}
          </button>
        </div>

        <nav aria-label="Main" className="shell-nav">
          {groups.map((g, gi) => (
            <div className="shell-group" key={g.label ?? gi}>
              {g.label && (
                <div className="shell-group-label" id={`nav-g-${gi}`}>
                  {g.label}
                </div>
              )}
              <ul aria-labelledby={g.label ? `nav-g-${gi}` : undefined}>
                {g.items.map((i) => (
                  <li key={i.to}>
                    <NavLink to={i.to} end={i.end} className="shell-link" title={i.label}>
                      {i.icon}
                      <span className="shell-link-text">{i.label}</span>
                    </NavLink>
                  </li>
                ))}
              </ul>
            </div>
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
              <span className="shell-user-role">{ROLE_NAMES[user?.role ?? ""] ?? user?.role}</span>
            </span>
          </div>
          {user?.role === "admin" && (
            <div className="shell-drawer-picker">
              <CustomerPicker />
            </div>
          )}
          <ul>
            <li>
              <NavLink to="/account" className="shell-link">
                {icons.account}
                <span className="shell-link-text">Account</span>
              </NavLink>
            </li>
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
            {user?.role === "admin" && (
              <div className="shell-top-picker">
                <CustomerPicker />
              </div>
            )}
            {/* Storm Mode switch: on every screen, never for carriers. */}
            {!carrier && <StormSwitch />}
            <UserMenu />
          </div>
        </header>
        {!carrier && <StormBanner />}
        <main className="page" id="main">
          {children}
        </main>
      </div>
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
            <span className="shell-user-role">{ROLE_NAMES[user?.role ?? ""] ?? user?.role}</span>
          </div>
          <ul>
            <li>
              <NavLink to="/account" className="shell-menu-item">
                {icons.account}
                Account
              </NavLink>
            </li>
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
