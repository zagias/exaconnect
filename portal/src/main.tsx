import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { applyHostBrand } from "./pages/commai/partner/brand";
import "./brand.css";

// A partner's verified portal domain shows its brand on sign-in (ADR 0025).
applyHostBrand();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
