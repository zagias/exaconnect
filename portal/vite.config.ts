import { resolve } from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev the portal proxies /api and /healthz to the controller.
// EXA_CONTROLLER defaults to the lab VM's compose port.
const controller = process.env.EXA_CONTROLLER ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      // The portal, and Jibsy Phone as its own small app (served at /phone).
      input: { main: resolve(__dirname, "index.html"), phone: resolve(__dirname, "phone.html") },
    },
  },
  server: {
    proxy: {
      "/api": controller,
      "/healthz": controller,
    },
  },
});
