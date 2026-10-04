import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";

export interface RowAction {
  label: string;
  onSelect: () => void;
  /** Destructive: red text, kept last in the menu. The caller still confirms. */
  danger?: boolean;
  disabled?: boolean;
}

/**
 * Row actions for a table: an optional primary action kept visible as a small
 * secondary button, and the rest behind a "More actions" menu button.
 *
 * Keyboard: Enter, Space or ArrowDown on the button opens the menu on its first
 * item (ArrowUp on the last); ArrowUp/ArrowDown move, Home/End jump, Escape
 * closes and returns focus to the button, Tab closes. A click outside closes it.
 * The menu is portalled to the body so a scrolling table never clips it.
 */
export function RowActions({
  label,
  primary,
  items,
  disabled,
}: {
  /** What the row is, for the menu button's accessible name: "rule 2", "site-a". */
  label: string;
  primary?: ReactNode;
  items: RowAction[];
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number; up: boolean } | null>(null);
  const button = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const focusOnOpen = useRef<"first" | "last">("first");
  const id = useId();

  const ordered = [...items.filter((i) => !i.danger), ...items.filter((i) => i.danger)];
  const firstDanger = ordered.findIndex((i) => i.danger);

  const enabledItems = () =>
    Array.from(menu.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not([disabled])') ?? []);

  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    if (refocus) button.current?.focus();
  }, []);

  const place = useCallback(() => {
    const b = button.current?.getBoundingClientRect();
    if (!b) return;
    const h = menu.current?.offsetHeight ?? 0;
    const w = menu.current?.offsetWidth ?? 200;
    const up = b.bottom + 6 + h > window.innerHeight - 8 && b.top - 6 - h > 8;
    const left = Math.max(8, Math.min(b.right - w, window.innerWidth - w - 8));
    setPos({ top: up ? b.top - 6 - h : b.bottom + 6, left, up });
  }, []);

  useLayoutEffect(() => {
    if (!open) {
      setPos(null);
      return;
    }
    place();
  }, [open, place]);

  // Focus the first or last item once the menu is placed.
  useEffect(() => {
    if (!open || !pos) return;
    const list = enabledItems();
    if (document.activeElement && menu.current?.contains(document.activeElement)) return;
    (focusOnOpen.current === "last" ? list[list.length - 1] : list[0])?.focus();
  }, [open, pos]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!menu.current?.contains(t) && !button.current?.contains(t)) close(false);
    };
    const onMove = () => place();
    document.addEventListener("mousedown", onDown);
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
  }, [open, close, place]);

  const onButtonKey = (e: KeyboardEvent) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      focusOnOpen.current = e.key === "ArrowUp" ? "last" : "first";
      setOpen(true);
    }
  };

  const onMenuKey = (e: KeyboardEvent) => {
    const list = enabledItems();
    const i = list.indexOf(document.activeElement as HTMLButtonElement);
    const go = (n: number) => {
      e.preventDefault();
      list[(n + list.length) % list.length]?.focus();
    };
    if (e.key === "ArrowDown") go(i + 1);
    else if (e.key === "ArrowUp") go(i - 1);
    else if (e.key === "Home") go(0);
    else if (e.key === "End") go(list.length - 1);
    else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close(true);
    } else if (e.key === "Tab") close(false);
  };

  const choose = (a: RowAction) => {
    close(true);
    // Let the menu close (and focus return) before a confirm dialog opens.
    window.setTimeout(a.onSelect, 0);
  };

  return (
    <div className="row-actions">
      {primary}
      {/* Keeps primary buttons lined up in rows that have nothing more to offer. */}
      {ordered.length === 0 && <span className="row-actions-spacer" aria-hidden="true" />}
      {ordered.length > 0 && (
        <button
          ref={button}
          type="button"
          className="button secondary small row-actions-more"
          aria-haspopup="menu"
          aria-expanded={open}
          aria-controls={open ? id : undefined}
          aria-label={`More actions for ${label}`}
          title="More actions"
          disabled={disabled}
          onClick={() => {
            focusOnOpen.current = "first";
            setOpen(!open);
          }}
          onKeyDown={onButtonKey}
        >
          <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            <circle cx="3" cy="8" r="1.5" fill="currentColor" />
            <circle cx="8" cy="8" r="1.5" fill="currentColor" />
            <circle cx="13" cy="8" r="1.5" fill="currentColor" />
          </svg>
        </button>
      )}
      {open &&
        createPortal(
          <div
            ref={menu}
            id={id}
            role="menu"
            aria-label={`Actions for ${label}`}
            className="row-menu"
            data-up={pos?.up || undefined}
            style={pos ? { top: pos.top, left: pos.left } : { top: -9999, left: -9999 }}
            onKeyDown={onMenuKey}
          >
            {ordered.map((a, i) => (
              <div key={a.label} role="none">
                {i === firstDanger && i > 0 && <div role="separator" className="row-menu-sep" />}
                <button
                  type="button"
                  role="menuitem"
                  tabIndex={-1}
                  className={a.danger ? "row-menu-item danger" : "row-menu-item"}
                  disabled={a.disabled}
                  onClick={() => choose(a)}
                >
                  {a.label}
                </button>
              </div>
            ))}
          </div>,
          document.body,
        )}
    </div>
  );
}
