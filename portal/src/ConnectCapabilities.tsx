import { createContext, useContext, type ReactNode } from "react";
import { integrationPaths, useApi, type AiStatus, type IntegrationCatalogue, type OnrampAdapterInfo } from "./api";

export interface ConnectCapabilities {
  loading: boolean;
  ask: boolean;
  integrations: boolean;
  /** Only adapters that are configured and report live mode may be offered. */
  onramps: OnrampAdapterInfo[];
  catalogue: IntegrationCatalogue | null;
  error: string | null;
  catalogueError: string | null;
}

const unavailable: ConnectCapabilities = {
  loading: false, ask: false, integrations: false, onramps: [], catalogue: null, error: null, catalogueError: null,
};
const Context = createContext<ConnectCapabilities>(unavailable);

/** One readiness check for Connect's optional services. Unknown and failed checks never enable a feature. */
export function ConnectCapabilitiesProvider({ children, enabled = true }: { children: ReactNode; enabled?: boolean }) {
  const ai = useApi<AiStatus>(enabled ? "/ai/status" : null, 30_000);
  const catalogue = useApi<IntegrationCatalogue>(enabled ? integrationPaths.catalogue : null, 30_000);
  const live = enabled && !catalogue.error && catalogue.data?.live === true;
  const value: ConnectCapabilities = enabled ? {
    loading: (!ai.data && !ai.error) || (!catalogue.data && !catalogue.error),
    ask: !ai.error && ai.data?.ask_enabled === true,
    integrations: live,
    onramps: live ? (catalogue.data?.onramp_adapters ?? []).filter((adapter) => adapter.configured === true && adapter.mode === "live") : [],
    catalogue: catalogue.data,
    error: ai.error ?? catalogue.error,
    catalogueError: catalogue.error,
  } : unavailable;
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useConnectCapabilities(): ConnectCapabilities {
  return useContext(Context);
}
