import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import { destinations, type Destination, type Group } from "./nav";
import "./jump.css";

const ACCOUNT: Destination[] = [
  { to: "/account", label: "Profile and sign-in", where: "You", keywords: "account password passkeys two-step api keys" },
  { to: "/commai/me", label: "My settings", where: "You", keywords: "language notifications preferences" },
];

/** Typing in a field: "/" is a character there, not a shortcut. */
function typing(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  if (!el) return false;
  return el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName);
}

/** Best matches first: every word must appear; a name that starts with the query ranks highest. */
export function search(all: Destination[], q: string): Destination[] {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return all;
  const scored: [number, number, Destination][] = [];
  all.forEach((d, n) => {
    const label = d.label.toLowerCase();
    const hay = `${label} ${d.where.toLowerCase()} ${d.keywords.toLowerCase()}`;
    if (!words.every((w) => hay.includes(w))) return;
    const q0 = words.join(" ");
    const score = label.startsWith(q0) ? 0 : label.includes(q0) ? 1 : words.every((w) => label.includes(w)) ? 2 : 3;
    scored.push([score, n, d]);
  });
  return scored.sort((a, b) => a[0] - b[0] || a[1] - b[1]).map((s) => s[2]);
}

/**
 * "Jump to": Ctrl+K, Cmd+K or "/" opens a small dialog that finds any screen
 * or tab by name and goes there. Arrow keys move, Enter opens, Escape closes
 * and returns focus to where it was. Focus stays inside while it is open.
 */
export function JumpTo({ groups, open, setOpen }: { groups: Group[]; open: boolean; setOpen: (v: boolean) => void }) {
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const back = useRef<HTMLElement | null>(null);
  const id = useId();
  const all = useMemo(() => {
    const seen = new Set<string>();
    // One entry per screen and name (a tab that is also the screen's first page keeps its tab name).
    return destinations(groups, ACCOUNT).filter((d) => {
      const k = `${d.to}|${d.label}`;
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });
  }, [groups]);
  const found = useMemo(() => search(all, q).slice(0, 50), [all, q]);

  // The shortcut, anywhere in the portal.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const k = e.key.toLowerCase();
      if ((e.ctrlKey || e.metaKey) && !e.altKey && k === "k") {
        e.preventDefault();
        setOpen(!open);
      } else if (k === "/" && !e.ctrlKey && !e.metaKey && !e.altKey && !open && !typing(e.target)) {
        e.preventDefault();
        setOpen(true);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, setOpen]);

  useEffect(() => {
    if (!open) return;
    back.current = document.activeElement as HTMLElement | null;
    setQ("");
    setActive(0);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.setTimeout(() => input.current?.focus(), 0);
    // Focus stays in the dialog, and Escape closes it wherever focus is.
    const onFocus = (e: FocusEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) input.current?.focus();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("focusin", onFocus);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("focusin", onFocus);
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
      back.current?.focus?.();
    };
  }, [open, setOpen]);

  useEffect(() => setActive(0), [q]);

  // Keep the highlighted result in view.
  useEffect(() => {
    document.getElementById(`${id}-opt-${active}`)?.scrollIntoView({ block: "nearest" });
  }, [active, id]);

  if (!open) return null;

  const go = (d: Destination | undefined) => {
    if (!d) return;
    back.current = null; // focus moves to the new page, not back to the old button
    setOpen(false);
    navigate(d.to);
    window.setTimeout(() => document.getElementById("main")?.focus({ preventScroll: true }), 0);
  };

  const onKey = (e: ReactKeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => (found.length ? (a + 1) % found.length : 0));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => (found.length ? (a - 1 + found.length) % found.length : 0));
    } else if (e.key === "Home" && e.ctrlKey) {
      setActive(0);
    } else if (e.key === "End" && e.ctrlKey) {
      setActive(Math.max(0, found.length - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      go(found[active]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
    } else if (e.key === "Tab") {
      // Focus trap: the field and the close button take turns.
      const items = Array.from(box.current?.querySelectorAll<HTMLElement>("input, button") ?? []);
      const i = items.indexOf(document.activeElement as HTMLElement);
      e.preventDefault();
      items[(i + (e.shiftKey ? -1 : 1) + items.length) % items.length]?.focus();
    }
  };

  return createPortal(
    <div className="jump-backdrop" onMouseDown={(e) => e.target === e.currentTarget && setOpen(false)}>
      <div ref={box} className="jump card" role="dialog" aria-modal="true" aria-labelledby={`${id}-title`} onKeyDown={onKey}>
        <div className="jump-head">
          <h2 id={`${id}-title`} className="jump-title">
            Jump to a screen
          </h2>
          <button type="button" className="button secondary small" onClick={() => setOpen(false)}>
            Close
          </button>
        </div>
        <input
          ref={input}
          autoFocus
          className="jump-input"
          type="text"
          role="combobox"
          aria-expanded="true"
          aria-controls={`${id}-list`}
          aria-autocomplete="list"
          aria-activedescendant={found.length ? `${id}-opt-${active}` : undefined}
          aria-label="Screen or tab name"
          placeholder="Type a screen or tab, such as Numbers or Security"
          autoComplete="off"
          spellCheck={false}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <ul id={`${id}-list`} className="jump-list" role="listbox" aria-label="Screens">
          {found.map((d, i) => (
            <li
              key={`${d.to}|${d.label}`}
              id={`${id}-opt-${i}`}
              role="option"
              aria-selected={i === active}
              className="jump-option"
              onMouseMove={() => i !== active && setActive(i)}
              onClick={() => go(d)}
            >
              <span className="jump-label">{d.label}</span>
              <span className="jump-where">{d.where}</span>
            </li>
          ))}
        </ul>
        {found.length === 0 && (
          <p className="jump-none" role="status">
            No screen called “{q}”. Try another word, such as bill, numbers or WhatsApp.
          </p>
        )}
        <p className="jump-help small muted" aria-hidden="true">
          <kbd>↑</kbd> <kbd>↓</kbd> to move, <kbd>Enter</kbd> to open, <kbd>Esc</kbd> to close
        </p>
      </div>
    </div>,
    document.body,
  );
}
