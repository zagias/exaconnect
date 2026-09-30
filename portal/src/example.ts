// Example data for the M0 overview. Every card that shows it carries an "Example data" tag.
import type { Health } from "./components";

export interface ExampleSite {
  name: string;
  location: string;
  paths: { name: "Carrier A" | "Carrier B" | "Satellite"; health: Health; latencyMs: number; lossPct: number }[];
}

export const exampleSites: ExampleSite[] = [
  {
    name: "Site A",
    location: "Kingston",
    paths: [
      { name: "Carrier A", health: "ok", latencyMs: 24.8, lossPct: 0.02 },
      { name: "Carrier B", health: "ok", latencyMs: 35.2, lossPct: 0 },
      { name: "Satellite", health: "ok", latencyMs: 45.6, lossPct: 0.4 },
    ],
  },
  {
    name: "Site B",
    location: "Port of Spain",
    paths: [
      { name: "Carrier A", health: "ok", latencyMs: 26.1, lossPct: 0 },
      { name: "Carrier B", health: "warn", latencyMs: 118.4, lossPct: 0.8 },
      { name: "Satellite", health: "ok", latencyMs: 46.3, lossPct: 0.5 },
    ],
  },
];
