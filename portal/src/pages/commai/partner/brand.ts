import { useEffect, useState } from "react";
import { api } from "../../../api";
import "./partner.css";

/** A partner's white-label brand (ADR 0031). null means ExaCarib's own look. */
export interface Brand {
  id: string;
  product_name: string;
  colour: string;
  ink: string;
  support_email: string;
  logo_url: string | null;
}

const DEFAULT_TITLE = "ExaCarib";

function dark(): boolean {
  const theme = document.documentElement.getAttribute("data-theme");
  if (theme) return theme === "dark";
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

/** Applies a brand's colour and name to the page; null restores ExaCarib's look.
 * In dark mode the portal keeps its own tuned colours, so text stays readable. */
export function applyBrand(b: Brand | null) {
  const root = document.documentElement.style;
  const vars = ["--blue", "--blue-strong", "--blue-hover", "--focus"];
  if (!b || dark()) {
    vars.forEach((v) => root.removeProperty(v));
  } else {
    vars.forEach((v) => root.setProperty(v, b.colour));
  }
  document.title = b ? b.product_name : DEFAULT_TITLE;
}

/** The brand for the signed-in account, applied as it loads. */
export function useBrand(): Brand | null {
  const [brand, setBrand] = useState<Brand | null>(null);
  useEffect(() => {
    let cancelled = false;
    api<Brand | null>("/commai/branding/current")
      .then((b) => {
        if (cancelled) return;
        setBrand(b);
        applyBrand(b);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);
  return brand;
}

/** Before sign-in: the brand of a verified custom portal domain, if this is one. */
export function applyHostBrand() {
  fetch("/api/v1/commai/branding/host")
    .then((r) => (r.ok ? r.json() : null))
    .then((b: Brand | null) => b && applyBrand(b))
    .catch(() => undefined);
}

/** WCAG contrast ratio of two #RRGGBB colours, as the controller checks it. */
export function contrast(a: string, b: string): number {
  const lum = (hex: string) => {
    const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((s) => (s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4));
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  };
  const [x, y] = [lum(a), lum(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}
