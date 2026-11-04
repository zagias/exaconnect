import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, useApi, type Product } from "../api";
import { ErrorNote, ExampleTag } from "../components";
import { PageHead } from "../ui";
import { useCustomer } from "../customer";
import { useAuth } from "../auth";
import { canOperateConnect } from "../permissions";
import { JibsyChecks } from "./commai/Assistant";
import { SpeechButton, speechAvailable } from "./commai/voice/SpeechInput";
import { PlanReview } from "./assistant/PlanReview";
import type { AssistantAnswer, AssistantContext, Plan } from "./assistant/types";
import "./assistant/assistant.css";

type Scope = "account" | "connect";
interface Turn extends Partial<AssistantAnswer> {
  id: number;
  q: string;
  scope: Scope;
  answerScope?: Scope;
  error?: string;
}

const PRODUCT: Record<Product, string> = { connect: "Connect", commai: "Jibsy" };
const SOURCES: Record<string, string> = {
  installed_feature_registry: "Implemented product features", active_product_catalogue: "Current catalogue",
  organisation_access: "Organisation access", subscriptions: "Account plans",
  network_snapshot: "Network observations", connect_snapshot: "Network observations",
};
const QUESTIONS = ["Why did voice move today?", "Are we heading for burst charges this month?"];
const COMMANDS = [QUESTIONS[0], "Put Zoom, Teams and Webex in voice at every site", QUESTIONS[1]];
const ACCOUNT_QUESTIONS = ["What is included in our plan?", "What would help us connect another branch?"];

/** One signed-in workspace. Each tool uses the organisation and access checked by the controller. */
export default function Ask() {
  const { current } = useCustomer();
  const [params, setParams] = useSearchParams();
  const context = useApi<AssistantContext>(current ? `/assistant/context?customer_id=${encodeURIComponent(current.id)}` : null, 0);
  const selected = params.get("scope") ?? "account";
  const c = context.error || context.data?.account.customer_id !== current?.id ? null : context.data;
  const scope = selected === "connect" || selected === "jibsy" ? selected : "account";
  const allowed = scope === "account" || (scope === "connect" ? c?.tools.connect_read === true : !!c?.tools.jibsy_diagnostics_path);
  const choose = (next: string) => {
    const updated = new URLSearchParams(params);
    updated.set("scope", next);
    updated.delete("q");
    setParams(updated);
  };

  return (
    <div className="assistant-workspace">
      <PageHead eyebrow="XaKore Hub" title="XaKore assistant">Work through a question, review a proposal, and choose what changes.</PageHead>
      <ErrorNote error={context.error} />
      {!current && <p className="muted">Choose an organisation to use the assistant.</p>}
      {current && !c && !context.error && <p className="muted" role="status">Checking this organisation's access…</p>}
      {current && c && (
        <div className="assistant-layout">
          <div className="assistant-main">
            <nav className="assistant-scopes" aria-label="Assistant tools">
              <button type="button" aria-current={scope === "account" ? "page" : undefined} onClick={() => choose("account")}>Account advice</button>
              {c.tools.connect_read && <button type="button" aria-current={scope === "connect" ? "page" : undefined} onClick={() => choose("connect")}>Connect network</button>}
              {c.tools.jibsy_diagnostics_path && <button type="button" aria-current={scope === "jibsy" ? "page" : undefined} onClick={() => choose("jibsy")}>Jibsy checks</button>}
            </nav>
            {!allowed ? (
              <div className="assistant-unavailable">
                <h2>{scope === "jibsy" ? "Jibsy checks need administrator access" : "Network tools aren't available with this access"}</h2>
                <p className="muted">You can ask about your account and available services. An organisation owner or admin can help with access.</p>
                <button className="button secondary" onClick={() => choose("account")}>Ask about this account</button>
              </div>
            ) : scope === "jibsy" ? (
              <JibsyChecks key={current.id} />
            ) : (
              <Advice key={current.id} context={c} scope={scope} />
            )}
          </div>
          <aside className="assistant-context" aria-label="Assistant account context">
            <span className="eyebrow">Working with</span>
            <h2>{c.account.organisation}</h2>
            {c.account.example && <ExampleTag />}
            <p className="small muted assistant-role">{c.account.role === "viewer" ? "Viewer · read only" : c.account.role.replaceAll("_", " ")}</p>
            <dl className="assistant-account">
              {(["connect", "commai"] as Product[]).map((product) => {
                const access = c.account.product_access?.[product];
                const enabled = access?.organisation_access ?? c.account.products.includes(product);
                const assigned = access?.member_access ?? c.account.accessible_products.includes(product);
                return (
                <div key={product}>
                  <dt>{PRODUCT[product]}</dt>
                  <dd>{!enabled ? "Not enabled" : assigned ? "Access enabled" : "Access restricted"}</dd>
                </div>
                );
              })}
            </dl>
            <Link to="/account/apps">Apps and plans</Link>
            <details className="assistant-details">
              <summary>Context and sources</summary>
              <ul>{c.sources.map((source) => <li key={source}>{SOURCES[source] ?? source.replaceAll("_", " ")}</li>)}</ul>
              <p className="small muted">Account checked {new Date(c.generated_at).toLocaleString("en-GB")}. Network observations are read when you ask in Connect.</p>
              <p className="small muted">This signed-in session uses this organisation's permitted data. Public website conversations are separate.</p>
              <button className="linklike" onClick={context.reload}>Refresh account context</button>
            </details>
            <p className="small muted">Plan changes and quotes are reviewed in <Link to="/account/apps">Apps and plans</Link> and <Link to="/quotes">Quotes and orders</Link>.</p>
          </aside>
        </div>
      )}
      {context.error && <button className="button secondary" onClick={context.reload}>Try again</button>}
    </div>
  );
}

function Advice({ context, scope }: { context: AssistantContext; scope: Scope }) {
  const { current } = useCustomer();
  const { user } = useAuth();
  const canAsk = context.ask_enabled || context.tools.connect_read;
  const canOperate = scope === "connect" && context.tools.connect_changes && canOperateConnect(user);
  const recent = useApi<Plan[]>(scope === "connect" && context.tools.connect_read && current ? `/assistant/plans?customer_id=${encodeURIComponent(current.id)}&limit=10` : null, 0);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [changedPlans, setChangedPlans] = useState<Record<number, Plan>>({});
  const [params] = useSearchParams();
  const draftQuestion = params.get("q");
  const [question, setQuestion] = useState(draftQuestion?.slice(0, 500) ?? "");
  const [busy, setBusy] = useState(false);
  const sequence = useRef(0);
  const pending = useRef(false);
  const [hasSpeech] = useState(speechAvailable);
  useEffect(() => { if (draftQuestion !== null) setQuestion(draftQuestion.slice(0, 500)); }, [draftQuestion]);

  const send = async (input: string) => {
    const q = input.trim();
    if (q.length < 2 || pending.current || !canAsk || !current || context.account.customer_id !== current.id) return;
    pending.current = true;
    setBusy(true);
    setQuestion("");
    const id = ++sequence.current;
    const history = turns.filter((t) => (t.answerScope ?? t.scope) === scope && t.answer).slice(-4).map((t) => ({ q: t.q, a: t.answer ?? "" }));
    setTurns((t) => [...t, { id, q, scope }]);
    try {
      const out = await api<AssistantAnswer>("/assistant/ask", {
        method: "POST", body: JSON.stringify({ question: q, customer_id: current.id, history, scope }),
      });
      const { scope: answerScope, ...fields } = out;
      setTurns((t) => t.map((turn) => turn.id === id ? { ...turn, ...fields, answerScope: answerScope ?? turn.scope } : turn));
      if (out.plan) recent.reload();
    } catch (e) {
      setTurns((t) => t.map((turn) => turn.id === id ? { ...turn, error: (e as Error).message } : turn));
    } finally {
      pending.current = false;
      setBusy(false);
    }
  };
  const submit = (e: FormEvent) => { e.preventDefault(); void send(question); };
  const update = (plan: Plan) => {
    setChangedPlans((p) => ({ ...p, [plan.id]: plan }));
    setTurns((t) => t.map((x) => x.plan?.id === plan.id ? { ...x, plan } : x));
    recent.reload();
  };
  const visible = turns.filter((t) => t.scope === scope);
  const shown = new Set(turns.map((t) => t.plan?.id));
  const earlier = (recent.data ?? []).map((p) => changedPlans[p.id] ?? p).filter((p) => !shown.has(p.id) && p.status !== "cancelled");
  const suggestions = scope === "account" ? ACCOUNT_QUESTIONS : canOperate ? COMMANDS : QUESTIONS;

  return (
    <>
      <div className="assistant-intro">
        <h2>{scope === "account" ? "What would you like to work through?" : "Ask about your network"}</h2>
        <p className="muted">{scope === "account" ? "Advice uses your account, available features and the current catalogue." : canOperate ? "Check real network observations or ask for a configuration proposal." : "Check your sites, routing decisions and usage. Your access is read only."}</p>
      </div>
      {!context.ask_enabled && <p className="assistant-notice" role="status">{context.tools.connect_read ? "Site status checks are available. Other advice needs an AI connection." : context.ask_unavailable_reason ?? "The assistant is not connected to an AI service yet."}</p>}
      {visible.length === 0 && !context.ask_enabled && context.tools.connect_read && <button className="linklike" disabled={busy} onClick={() => send("Check the status of my sites")}>Check my sites</button>}
      {visible.length === 0 && context.ask_enabled && <div className="assistant-suggestions" role="group" aria-label="Suggested questions">{suggestions.map((s) => <button key={s} className="linklike" disabled={busy} onClick={() => send(s)}>{s}</button>)}</div>}
      <div className="assistant-conversation" aria-live="polite" aria-busy={busy}>
        {visible.map((t) => (
          <article key={t.id} className="assistant-turn">
            <p className="assistant-question"><span className="small muted">You</span>{t.q}</p>
            {t.answer !== undefined ? (
              <>
                {t.based_on?.grounding === "unverified_advice_replaced_with_grounded_facts" && (
                  <p className="small muted">I couldn’t verify that recommendation. Here are the facts I can confirm.</p>
                )}
                <div className="assistant-answer">{t.answer}</div>
                <p className="small muted assistant-source">{t.based_on?.sources?.length === 0 ? "No account data was queried for this reply." : (t.answerScope ?? t.scope) === "connect" ? `Network observations${t.based_on?.sites !== undefined ? ` · ${t.based_on.sites} sites` : ""}${t.model !== "rules" && t.based_on?.decisions !== undefined ? ` · ${t.based_on.decisions} decisions in 7 days` : ""}` : t.model === "rules" ? "Organisation context" : "Account access, plans and current catalogue"}</p>
                {t.based_on?.sources?.includes("network_snapshot") && context.tools.connect_read && <Link to="/sites">View sites</Link>}
                {t.plan && <PlanReview plan={t.plan} canOperate={canOperate} onChange={update} />}
              </>
            ) : t.error ? <ErrorNote error={t.error} /> : <p className="muted" role="status">{t.scope === "connect" ? "Checking your network…" : "Checking your account and available services…"}</p>}
          </article>
        ))}
      </div>
      <form className="assistant-composer" onSubmit={submit}>
        <label htmlFor="assistant-question">{scope === "connect" ? canOperate ? "Your question or request" : "Your question" : "Your account question"}</label>
        <textarea id="assistant-question" rows={3} value={question} onChange={(e) => setQuestion(e.target.value)} maxLength={500} disabled={!canAsk} placeholder={scope === "connect" ? "Why did voice move to the backup path?" : "Tell me what you need, or what you want to improve."} />
        <div className="assistant-composer-foot">
          <span className="small muted">{scope === "connect" && canOperate ? "Proposals need your approval before they can run." : "Advice does not change your account."}</span>
          <div className="assistant-actions">
            {canAsk && <SpeechButton onText={setQuestion} label="Dictate your question into the text box" />}
            <button className="button" disabled={!canAsk || busy || question.trim().length < 2}>{busy ? "Checking…" : "Send"}</button>
          </div>
        </div>
        {hasSpeech && <p className="small muted">Speak fills the text box. Check the words, then send.</p>}
      </form>
      {scope === "connect" && <ErrorNote error={recent.error} />}
      {scope === "connect" && earlier.length > 0 && (
        <details className="assistant-history" open>
          <summary>Recent proposals and changes <span className="small muted">{earlier.length}</span></summary>
          <ErrorNote error={recent.error} />
          {earlier.map((p) => <div key={p.id}><p className="small muted">{p.question}</p><PlanReview plan={p} canOperate={canOperate} onChange={update} /></div>)}
        </details>
      )}
    </>
  );
}
