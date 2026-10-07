import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import App from "./App";
import "./brand.css";

// The public help centre (ADR 0031) lives outside the staff sign-in and shell.
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
