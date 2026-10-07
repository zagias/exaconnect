import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import App from "./App";
import { applyHostBrand } from "./pages/commai/partner/brand";
import "./brand.css";

// A partner's verified portal domain shows its brand on sign-in (ADR 0031).
applyHostBrand();
// The public help centre (ADR 0037) lives outside the staff sign-in and shell.
const HelpCentre = lazy(() => import("./pages/help/HelpCentre"));

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route
          path="/help/:slug/*"
          element={
            <Suspense fallback={<p style={{ padding: 16 }}>Loading…</p>}>
              <HelpCentre />
            </Suspense>
          }
        />
        <Route path="*" element={<App />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
