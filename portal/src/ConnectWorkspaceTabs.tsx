import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import "./connect-workspace.css";

const SITE_LINKS = [
  { to: "/sites", label: "Connected sites" },
  { to: "/locations", label: "Add or connect" },
];
const ACTIVITY_LINKS = [
  { to: "/decisions", label: "Routing history" },
  { to: "/insights", label: "Risk and cost alerts" },
  { to: "/notices", label: "Carrier notices" },
];

const under = (path: string, base: string) => path === base || path.startsWith(`${base}/`);

/** Related tasks share a workspace; these remain ordinary, bookmarkable page links. */
export function ConnectWorkspaceTabs({ path }: { path: string }) {
  const pathname = path.split(/[?#]/, 1)[0].replace(/\/+$/, "") || "/";
  const locationPage = under(pathname, "/locations") || under(pathname, "/org/locations");
  const sites = under(pathname, "/sites") || locationPage;
  const activity = ACTIVITY_LINKS.some((link) => under(pathname, link.to));
  const links = sites ? SITE_LINKS : activity ? ACTIVITY_LINKS : null;
  const selected = locationPage ? "/locations" : sites ? "/sites" : ACTIVITY_LINKS.find((link) => under(pathname, link.to))?.to;
  const nav = useRef<HTMLElement>(null);

  useEffect(() => {
    const element = nav.current;
    const current = element?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!element || !current || element.scrollWidth <= element.clientWidth) return;
    const left = current.offsetLeft;
    const right = left + current.offsetWidth;
    if (left < element.scrollLeft) element.scrollTo({ left });
    else if (right > element.scrollLeft + element.clientWidth) element.scrollTo({ left: right - element.clientWidth });
  }, [pathname]);

  if (!links) return null;
  return (
    <nav ref={nav} className="connect-workspace-tabs" aria-label={sites ? "Sites workspace" : "Activity workspace"}>
      {links.map((link) => (
        <Link key={link.to} to={link.to} aria-current={selected === link.to ? "page" : undefined}>
          {link.label}
        </Link>
      ))}
    </nav>
  );
}
