import { useCallback, useEffect, useState } from "react";

// The help centre is public: it doesn't use the staff session. An end user's
// session is an HttpOnly cookie scoped to /api/v1/commai/help/<slug>, set by
// the controller (ADR 0031). Writes carry X-Requested-With: exa-help.

export class HelpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function helpApi<T>(slug: string, path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-Requested-With", "exa-help");
  if (init.body) headers.set("Content-Type", "application/json");
  const r = await fetch(`/api/v1/commai/help/${encodeURIComponent(slug)}${path}`, {
    ...init,
    headers,
    credentials: "same-origin",
  });
  if (!r.ok) {
    let msg = "Something went wrong. Try again.";
    try {
      const body = await r.json();
      if (typeof body.detail === "string") msg = body.detail;
    } catch {
      /* not JSON */
    }
    throw new HelpError(r.status, msg);
  }
  return (r.status === 204 ? undefined : await r.json()) as T;
}

export function useHelp<T>(slug: string, path: string | null, intervalMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<HelpError | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    if (!path) return;
    let cancelled = false;
    const load = () =>
      helpApi<T>(slug, path)
        .then((d) => {
          if (!cancelled) {
            setData(d);
            setError(null);
          }
        })
        .catch((e: HelpError) => {
          if (!cancelled) setError(e);
        });
    load();
    const id = intervalMs > 0 ? setInterval(load, intervalMs) : undefined;
    return () => {
      cancelled = true;
      if (id) clearInterval(id);
    };
  }, [slug, path, intervalMs, tick]);
  return { data, error, reload };
}

export function clientId(): string {
  const a = new Uint8Array(12);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
}

export interface HelpHome {
  slug: string;
  enabled: boolean;
  business: string;
  colour: string;
  logo_url: string;
  platform?: string;
  title: string;
  intro: string;
  show: Record<"articles" | "search" | "ask" | "contact" | "hours" | "channels" | "signin" | "bookings" | "data_requests", boolean>;
  signin_with_business: boolean;
  categories?: { name: string; articles: { id: string; title: string }[] }[];
  hours?: { text: string; open_now: boolean; timezone: string };
  channels?: { kind: "whatsapp" | "phone" | "email"; label: string; value: string; href: string }[];
  signed_in?: { name: string; email: string; via: string } | null;
}

export interface PublicMessage {
  id: string;
  from: "you" | "team" | "assistant" | "system";
  body: string;
  at: string;
}
