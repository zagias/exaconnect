import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { applyHostBrand } from "../pages/commai/partner/brand";
import "../brand.css";
import "../tables.css";
import PhoneApp from "./PhoneApp";
import "./phone.css";

// A partner's verified domain shows its brand (ADR 0031), here as on the portal.
applyHostBrand();

// Installable, and opens offline (public/phone-sw.js). Scoped to the app only.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/phone-sw.js", { scope: "/phone" }).catch(() => {
    /* still works as a web page */
  });
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <PhoneApp />
  </StrictMode>,
);
