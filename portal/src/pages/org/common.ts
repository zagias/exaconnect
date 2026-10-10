import { useAuth } from "../../auth";
import { useCustomer } from "../../customer";

/** The organisation these pages act for: the person's own, or the one an ExaCarib admin is working on. */
export function useOrgId(): string | null {
  const { user } = useAuth();
  const { current } = useCustomer();
  return (user?.role === "admin" ? current?.id : user?.customer_id) ?? null;
}

/** Owners and admins of the organisation, and ExaCarib admins, change these pages; everyone else reads them. */
export function useCanManage(): boolean {
  const { user } = useAuth();
  return user?.role === "admin" || (user?.role === "customer" && (user.org_role === "owner" || user.org_role === "admin"));
}

export interface SetupStep {
  id: string;
  app?: "connect" | "commai";
  title: string;
  done: boolean;
  detail: string;
  to: string;
  hub_ready?: boolean;
}

export interface Setup {
  organisation: string;
  steps: SetupStep[];
  done: number;
  total: number;
  complete: boolean;
  dismissed_at: string | null;
}

export interface Company {
  id: string;
  name: string;
  country: string;
  timezone: string;
  address: string;
  phone: string;
  website: string;
  products: string[];
}

export interface OrgLocation {
  id: string;
  name: string;
  address_line1: string;
  address_line2: string;
  city: string;
  island: string;
  country: string;
  postcode: string;
  timezone: string;
  latitude: number | null;
  longitude: number | null;
  connect: { id: string; name: string; node_id: string | null; last_seen: string | null; online: boolean; links: number } | null;
  phone: { id: string; name: string; emergency_status: string; people: number } | null;
  hours: { id: string; is_primary: boolean; intervals: number } | null;
}

export interface Install {
  site: string;
  token: string;
  expires_at: string;
  ca_fingerprint: string;
  agent_url: string;
  public_agent_url: string;
}

/** Common Caribbean time zones first; any IANA name can be typed. */
export const ZONES = [
  "America/Port_of_Spain",
  "America/Jamaica",
  "America/Barbados",
  "America/Santo_Domingo",
  "America/Puerto_Rico",
  "America/Nassau",
  "America/Cayman",
  "America/Guyana",
  "America/Paramaribo",
  "America/Curacao",
  "America/Martinique",
  "America/St_Lucia",
  "America/Grenada",
  "America/Antigua",
  "America/Belize",
  "America/New_York",
  "UTC",
];
