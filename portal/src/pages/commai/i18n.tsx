import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../../api";
import { useCommaiBase } from "./lib";

/*
 * A small i18n layer for the Jibsy screens (ADR 0032). Catalogues live in the
 * controller (commai/i18n/*.json); English (en-GB) is the source and fills any
 * key a draft lacks. A language is offered only once ExaCarib switches it on
 * in the go-live registry; drafts show a "Machine-drafted" label until a
 * reviewer signs them off. Dates, numbers and currency use Intl in the
 * person's locale.
 */

type Vars = Record<string, string | number>;
type Messages = Record<string, string>;

interface Catalogue {
  locale: string;
  status: string;
  machine_drafted: boolean;
  messages: Messages;
}

export interface I18n {
  locale: string;
  machineDrafted: boolean;
  ready: boolean;
  t: (key: string, vars?: Vars) => string;
  date: (iso: string | null | undefined, withTime?: boolean) => string;
  number: (n: number | null | undefined, digits?: number) => string;
  money: (n: number | null | undefined, currency?: string) => string;
  percent: (share: number | null | undefined) => string;
  reload: () => void;
}

/** The Intl locale for a catalogue locale (Caribbean variants where they exist). */
export function intlLocale(locale: string): string {
  return { "en-GB": "en-GB", es: "es-419", fr: "fr", nl: "nl", ht: "fr-HT" }[locale] ?? "en-GB";
}

function format(msg: string, vars?: Vars): string {
  if (!vars) return msg;
  return msg.replace(/\{(\w+)\}/g, (m, k: string) => (k in vars ? String(vars[k]) : m));
}

function make(locale: string, messages: Messages, english: Messages, machineDrafted: boolean, ready: boolean, reload: () => void): I18n {
  const il = intlLocale(locale);
  const safe = <T,>(fn: () => T, fallback: T): T => {
    try {
      return fn();
    } catch {
      return fallback;
    }
  };
  return {
    locale,
    machineDrafted,
    ready,
    reload,
    t: (key, vars) => format(messages[key] ?? english[key] ?? key, vars),
    date: (iso, withTime = true) => {
      if (!iso) return "";
      const d = new Date(iso);
      return safe(
        () =>
          d.toLocaleString(il, withTime ? { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" } : { day: "numeric", month: "short", year: "numeric" }),
        d.toISOString(),
      );
    },
    number: (n, digits = 0) =>
      n === null || n === undefined ? "–" : safe(() => n.toLocaleString(il, { maximumFractionDigits: digits }), String(n)),
    money: (n, currency = "USD") =>
      n === null || n === undefined ? "–" : safe(() => n.toLocaleString(il, { style: "currency", currency }), `${currency} ${n}`),
    percent: (share) =>
      share === null || share === undefined ? "–" : safe(() => share.toLocaleString(il, { style: "percent", maximumFractionDigits: 0 }), `${Math.round(share * 100)}%`),
  };
}

const Ctx = createContext<I18n>(make("en-GB", {}, {}, false, false, () => {}));

/** Use inside the Jibsy screens. */
export const useT = () => useContext(Ctx);

export function I18nProvider({ children }: { children: ReactNode }) {
  const base = useCommaiBase();
  const [english, setEnglish] = useState<Messages>({});
  const [cat, setCat] = useState<Catalogue | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    api<Catalogue>("/commai/i18n/catalogues/en-GB")
      .then((c) => setEnglish(c.messages))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!base) return;
    let cancelled = false;
    api<{ my_locale: string }>(`${base}/languages`)
      .then((l) => (l.my_locale === "en-GB" ? null : api<Catalogue>(`/commai/i18n/catalogues/${l.my_locale}`)))
      .then((c) => {
        if (!cancelled) setCat(c);
      })
      .catch(() => {
        if (!cancelled) setCat(null);
      });
    return () => {
      cancelled = true;
    };
  }, [base, tick]);

  const value = useMemo(
    () => make(cat?.locale ?? "en-GB", cat?.messages ?? english, english, cat?.machine_drafted ?? false, Object.keys(english).length > 0, reload),
    [cat, english, reload],
  );
  useEffect(() => {
    document.documentElement.lang = value.locale;
  }, [value.locale]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/** Shown on screens while the interface language is a machine draft. */
export function DraftLabel() {
  const { machineDrafted, t } = useT();
  if (!machineDrafted) return null;
  return (
    <p className="pill warn small" role="note" title={t("i18n.draftHint")}>
      {t("i18n.draft")}
    </p>
  );
}
