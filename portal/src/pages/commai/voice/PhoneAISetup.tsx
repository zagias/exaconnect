import { Link } from "react-router-dom";
import { useApi } from "../../../api";
import type { VoiceOverview } from "./types";

type SetupStatus = "missing" | "blocked" | "configured" | "verified" | "expired" | "not_verified";
type SetupAction = "agent_profile" | "numbers" | "call_routing" | "apps_plans";

export interface PhoneSetupComponent {
  id: string;
  status: SetupStatus;
  title: string;
  detail: string;
  next_action: string;
  responsibility: "business" | "operator";
  setup_action?: SetupAction | null;
  missing?: string[];
  version?: string;
  checked_at?: string | null;
  speech_verified_at?: string | null;
  transfer_verified_at?: string | null;
  expires_at?: string | null;
}

export interface PhoneSetup {
  ready: boolean;
  status: "ready" | "setup_required";
  summary: string;
  components: PhoneSetupComponent[];
  blockers: string[];
  routing: { active_numbers: number; routed_numbers: number };
  can_manage_agent: boolean;
  human_answered: boolean;
}

const STATUS: Record<SetupStatus, [string, string]> = {
  missing: ["off", "Not set up"],
  blocked: ["warn", "Needs attention"],
  configured: ["shadow", "Configured"],
  verified: ["ok", "Verified"],
  expired: ["warn", "Check expired"],
  not_verified: ["off", "Not verified"],
};
const ACTION: Record<SetupAction, { to: string; label: string }> = {
  agent_profile: { to: "/commai/ai", label: "Open agent profile" },
  numbers: { to: "/commai/voice/numbers", label: "Manage numbers" },
  call_routing: { to: "/commai/voice/routing#ai-rules", label: "Prepare call rules" },
  apps_plans: { to: "/account/apps", label: "Review Apps & plans" },
};
const complete = (status: SetupStatus) => status === "configured" || status === "verified";

function time(value: string | null | undefined) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isFinite(date.valueOf()) ? date.toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : null;
}

function Evidence({ component }: { component: PhoneSetupComponent }) {
  const facts = [
    ["PBX version", component.version],
    ["Checked", time(component.checked_at)],
    ["Speech checked", time(component.speech_verified_at)],
    ["SIP transfer accepted", time(component.transfer_verified_at)],
    ["Evidence expires", time(component.expires_at)],
  ].filter(([, value]) => value);
  if (!facts.length && !component.missing?.length) return null;
  return <details className="phone-ai-evidence">
    <summary>Operator details</summary>
    {component.missing?.length ? <p className="small">Missing settings: {component.missing.map((name, index) =>
      <span key={name}>{index > 0 && ", "}<code>{name}</code></span>)}.</p> : null}
    {facts.length > 0 && <dl>{facts.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>}
  </details>;
}

/** Reads scoped configuration/evidence only. It never starts a call or refreshes a worker lease. */
export default function PhoneAISetup({ base, v, onRefresh }: { base: string; v: VoiceOverview; onRefresh: () => void }) {
  const setup = useApi<PhoneSetup>(v.is_admin ? `${base}/voice/phone/setup` : null, 30_000);
  if (!v.is_admin) return <p>Ask a voice admin to review telephone AI setup.</p>;
  const d = setup.data;
  // A failed refresh must never leave a cached green readiness claim on screen.
  const current = d && !setup.error ? d : null;
  const refresh = () => { setup.reload(); onRefresh(); };
  return <section className="phone-ai-setup" aria-labelledby="phone-ai-title">
    <div className="phone-ai-heading">
      <div><h2 id="phone-ai-title">Telephone AI</h2><p className="muted">Prepare an agent to answer callers and hand over to your team.</p></div>
      <button className="button secondary small" onClick={refresh}>Refresh status</button>
    </div>
    {setup.error ? <div className="phone-ai-status" role="alert">
      <strong>Readiness could not be checked</strong><p>{setup.error}</p><p>Refresh before changing call routing.</p>
    </div> : !current ? <p role="status">Checking telephone AI setup…</p> : <>
      <div className="phone-ai-status" role="status">
        <span className={`pill ${current.ready ? "ok" : "off"}`}>{current.ready ? "Ready for call routing" : "Setup required"}</span>
        <p>{current.summary}</p>
      </div>
      <ol className="phone-ai-checklist" aria-label="Telephone AI setup steps">
        {current.components.map((component) => {
          const [colour, word] = STATUS[component.status] ?? STATUS.not_verified;
          const action = component.setup_action && (component.setup_action !== "agent_profile" || current.can_manage_agent)
            ? ACTION[component.setup_action] : null;
          return <li key={component.id}>
            <div className="phone-ai-step-heading"><h3>{component.title}</h3><span className={`pill small ${colour}`}>{word}</span></div>
            {complete(component.status) ? <p>{component.detail}</p> : <>
              <p className="phone-ai-next">{component.next_action}</p>
              <details className="phone-ai-detail"><summary>Current details</summary><p>{component.detail}</p></details>
            </>}
            <div className="phone-ai-step-actions">
              <span className="small muted">{component.responsibility === "operator" ? "XaKore operator" : "Your business"}</span>
              {action && <Link to={action.to}>{action.label}</Link>}
            </div>
            {v.is_exacarib && <Evidence component={component} />}
          </li>;
        })}
      </ol>
      <div className="phone-ai-footer">
        <p>You can prepare people, business hours and call fallbacks while service setup is completed.</p>
        <div className="row-actions"><Link to="/commai/voice">People and numbers</Link><Link to="/commai/voice/routing#ai-rules">{current.ready ? "Review AI call rules" : "Prepare AI call rules"}</Link>{!v.provider.live && <Link to="/commai/voice/trunks">Connect your own SIP trunk</Link>}</div>
      </div>
    </>}
  </section>;
}
