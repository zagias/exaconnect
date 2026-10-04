import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { Eyebrow } from "./components";
import "./tables.css";

export { RowActions, type RowAction } from "./menu";

/** Runs a form action with a busy flag and an error message. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run, setError };
}

export function Card({ title, children, note }: { title: string; children: ReactNode; note?: ReactNode }) {
  return (
    <section className="card" style={{ marginBottom: 24 }}>
      <div className="card-head">
        <h2>{title}</h2>
        {note}
      </div>
      {children}
    </section>
  );
}

/** Eyebrow, title and one short muted line. */
export function PageHead({ eyebrow, title, children }: { eyebrow: string; title: string; children?: ReactNode }) {
  return (
    <div className="page-head">
      <Eyebrow>{eyebrow}</Eyebrow>
      <h1>{title}</h1>
      {children && <p className="muted">{children}</p>}
    </div>
  );
}

/**
 * A row of section tabs (NavLinks). On phones it stays one row and scrolls
 * sideways, with a fade on whichever edge has more tabs behind it, and keeps
 * the current tab in view.
 */
export function Tabs({ label, children }: { label: string; children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null);
  const nav = useRef<HTMLElement>(null);
  const [more, setMore] = useState({ left: false, right: false });
  const { pathname } = useLocation();

  const measure = useCallback(() => {
    const n = nav.current;
    if (!n) return;
    const left = n.scrollLeft > 2;
    const right = n.scrollLeft + n.clientWidth < n.scrollWidth - 2;
    setMore((m) => (m.left === left && m.right === right ? m : { left, right }));
  }, []);

  useEffect(() => {
    const n = nav.current;
    if (!n) return;
    const active = n.querySelector<HTMLElement>("a.active");
    if (active && n.scrollWidth > n.clientWidth) {
      const target = active.offsetLeft - (n.clientWidth - active.offsetWidth) / 2;
      n.scrollTo({ left: Math.max(0, target) });
    }
    measure();
  }, [pathname, measure]);

  useEffect(() => {
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [measure]);

  return (
    <div ref={box} className="tabs-scroll" data-more-left={more.left || undefined} data-more-right={more.right || undefined}>
      <nav ref={nav} className="tabs" aria-label={label} onScroll={measure}>
        {children}
      </nav>
    </div>
  );
}
