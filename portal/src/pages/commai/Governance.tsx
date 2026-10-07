import { useState, type FormEvent } from "react";
import { api, useApi } from "../../api";
import { useAuth } from "../../auth";
import { ErrorNote } from "../../components";
import { Card, PageHead, useAction } from "../../ui";
import { DraftLabel, useT } from "./i18n";
import { useCommaiBase } from "./lib";
import "./quality.css";

/* Shapes from controller/exaconnect_controller/commai/api/quality.py (ADR 0026). */
interface Case {
  id: string;
  question: string;
  expected: string;
  forbidden: string;
}
interface CaseResult {
  case_id: string;
  suite: "global" | "business";
  question: string;
  expected: string;
  forbidden: string;
  answer: string;
  escalated: boolean;
  passed: boolean;
  verdicts: { id: string; verdict: string; quote: string; why: string }[];
}
interface Run {
  id: string;
  status: string;
  total: number;
  passed: number;
  failed: number;
  error: string;
  started_at: string;
  results?: CaseResult[];
}
interface Candidate {
  id: string;
  kind: "model" | "instructions";
  change: Record<string, string>;
  previous: Record<string, string>;
  status: "testing" | "passed" | "failed" | "promoted" | "withdrawn";
  created_by: string;
  created_at: string;
  decided_by: string;
  latest_run: Run | null;
}
interface Limit {
  role: string;
  daily_limit: number | null;
  used_today: number;
}
interface Overview {
  cases: Case[];
  global_cases: number;
  candidates: Candidate[];
  limits: Limit[];
  model: { model: string; live: boolean; live_model: { model: string; promoted_by: string; promoted_at: string } | null };
}

/** AI governance: evaluation suites, changes waiting to go live, and daily action limits. */
export default function Governance() {
  const base = useCommaiBase();
  const { t } = useT();
  const { user } = useAuth();
  const ov = useApi<Overview>(base && `${base}/ai/governance`, 15_000);
  if (!base) return <p className="muted">{t("common.chooseBusiness")}</p>;
  return (
    <>
      <PageHead title={t("g.title")}>
        {t("g.intro")}
      </PageHead>
      <DraftLabel />
      <ErrorNote error={ov.error} />
      {ov.data && (
        <>
          <Candidates prefix={base + "/ai/governance"} items={ov.data.candidates} reload={ov.reload} />
          <Suite
            title={t("g.suite")}
            hint={t("g.suiteHint", { n: ov.data.global_cases })}
            cases={ov.data.cases}
            path={`${base}/ai/governance/cases`}
            reload={ov.reload}
          />
          <Limits base={base} limits={ov.data.limits} reload={ov.reload} />
        </>
      )}
      {user?.role === "admin" && <GlobalSuite />}
    </>
  );
}

function Candidates({ prefix, items, reload }: { prefix: string; items: Candidate[]; reload: () => void }) {
  const i = useT();
  const { t } = i;
  const act = useAction();
  const [open, setOpen] = useState<string | null>(null);
  const verb = (id: string, v: string) =>
    act.run(async () => {
      await api(`${prefix}/candidates/${id}/${v}`, { method: "POST" });
      reload();
    });
  return (
    <Card title={t("g.candidates")}>
      <p className="muted small">{t("g.candidatesHint")}</p>
      <ErrorNote error={act.error} />
      {items.length === 0 && <p className="muted">{t("g.noCandidates")}</p>}
      <ul className="q-list">
        {items.map((c) => (
          <li key={c.id} className="q-candidate">
            <div>
              <strong>{c.kind === "model" ? t("g.kind.model") : t("g.kind.instructions")}</strong>{" "}
              <span className={`pill ${c.status === "passed" || c.status === "promoted" ? "ok" : c.status === "failed" ? "bad" : "shadow"}`}>
                {t(`g.status.${c.status}`)}
              </span>
              <span className="muted small"> · {i.date(c.created_at)} · {c.created_by}</span>
              <dl className="q-change small">
                {Object.entries(c.change).map(([k, v]) => (
                  <div key={k}>
                    <dt>{k}</dt>
                    <dd>
                      <span className="muted">{c.previous[k] || "–"}</span> → {v || "–"}
                    </dd>
                  </div>
                ))}
              </dl>
              {c.latest_run && (
                <p className="small">
                  {t("g.run", { passed: c.latest_run.passed, total: c.latest_run.total })}
                  {c.latest_run.error && <span className="pill bad"> {c.latest_run.error}</span>}
                </p>
              )}
              {open === c.id && <RunDetail prefix={prefix} id={c.id} />}
            </div>
            <span className="q-row-actions">
              {c.latest_run && (
                <button className="button secondary small" type="button" onClick={() => setOpen(open === c.id ? null : c.id)}>
                  {open === c.id ? t("common.hide") : t("g.results")}
                </button>
              )}
              {c.status === "passed" && (
                <button className="button small" type="button" disabled={act.busy} onClick={() => verb(c.id, "promote")}>
                  {t("g.promote")}
                </button>
              )}
              {["testing", "passed", "failed"].includes(c.status) && (
                <>
                  <button className="button secondary small" type="button" disabled={act.busy} onClick={() => verb(c.id, "rerun")}>
                    {t("g.rerun")}
                  </button>
                  <button className="button secondary small" type="button" disabled={act.busy} onClick={() => verb(c.id, "withdraw")}>
                    {t("g.withdraw")}
                  </button>
                </>
              )}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function RunDetail({ prefix, id }: { prefix: string; id: string }) {
  const { t } = useT();
  const d = useApi<{ runs: Run[] }>(`${prefix}/candidates/${id}`, 0);
  const run = d.data?.runs[0];
  return (
    <div className="q-results">
      <ErrorNote error={d.error} />
      {(run?.results ?? []).map((r) => (
        <article key={r.case_id + r.suite} className={`q-result ${r.passed ? "" : "flagged"}`}>
          <header>
            <span className="tag">{r.suite === "global" ? t("g.global") : t("g.business")}</span>
            <strong>{r.question}</strong>
            <span className={`pill ${r.passed ? "ok" : "bad"}`}>{r.passed ? t("q.verdict.pass") : t("q.verdict.fail")}</span>
          </header>
          <p className="small">
            {r.escalated ? t("g.escalated") : t("g.answered")} {r.answer && <q>{r.answer}</q>}
          </p>
          <ul className="small">
            {r.verdicts.map((v) => (
              <li key={v.id}>
                {v.id === "expected" ? t("g.expected") : t("g.forbidden")}: {t(`q.verdict.${v.verdict}`)} {v.why && <span className="muted">· {v.why}</span>}
              </li>
            ))}
          </ul>
        </article>
      ))}
    </div>
  );
}

function Suite({ title, hint, cases, path, reload }: { title: string; hint: string; cases: Case[]; path: string; reload: () => void }) {
  const { t } = useT();
  const act = useAction();
  const [q, setQ] = useState({ question: "", expected: "", forbidden: "" });
  const add = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api(path, { method: "POST", body: JSON.stringify(q) });
      setQ({ question: "", expected: "", forbidden: "" });
      reload();
    });
  };
  const remove = (id: string) =>
    act.run(async () => {
      await api(`${path}/${id}`, { method: "DELETE" });
      reload();
    });
  return (
    <Card title={title}>
      <p className="muted small">{hint}</p>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>{t("g.question")}</th>
              <th>{t("g.expected")}</th>
              <th>{t("g.forbidden")}</th>
              <th aria-label={t("common.actions")} />
            </tr>
          </thead>
          <tbody>
            {cases.map((c) => (
              <tr key={c.id}>
                <td>{c.question}</td>
                <td>{c.expected || "–"}</td>
                <td>{c.forbidden || "–"}</td>
                <td>
                  <button className="button secondary small" type="button" onClick={() => remove(c.id)}>
                    {t("common.delete")}
                  </button>
                </td>
              </tr>
            ))}
            {cases.length === 0 && (
              <tr>
                <td colSpan={4} className="muted">
                  {t("g.noCases")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <form className="form" onSubmit={add}>
        <label className="wide">
          {t("g.question")}
          <input required minLength={3} maxLength={1000} value={q.question} placeholder={t("g.questionExample")} onChange={(e) => setQ({ ...q, question: e.target.value })} />
        </label>
        <label>
          {t("g.expected")}
          <input maxLength={300} value={q.expected} placeholder={t("g.expectedExample")} onChange={(e) => setQ({ ...q, expected: e.target.value })} />
        </label>
        <label>
          {t("g.forbidden")}
          <input maxLength={300} value={q.forbidden} placeholder={t("g.forbiddenExample")} onChange={(e) => setQ({ ...q, forbidden: e.target.value })} />
        </label>
        <div className="actions">
          <button className="button" disabled={act.busy}>
            {t("g.addCase")}
          </button>
        </div>
      </form>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function Limits({ base, limits, reload }: { base: string; limits: Limit[]; reload: () => void }) {
  const i = useT();
  const { t } = i;
  const act = useAction();
  const set = (role: string, v: string) =>
    act.run(async () => {
      await api(`${base}/ai/governance/limits/${role}`, { method: "PUT", body: JSON.stringify({ daily_limit: v === "" ? null : Number(v) }) });
      reload();
    });
  return (
    <Card title={t("g.limits")}>
      <p className="muted small">{t("g.limitsHint")}</p>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>{t("g.role")}</th>
              <th>{t("g.usedToday")}</th>
              <th>{t("g.dailyLimit")}</th>
            </tr>
          </thead>
          <tbody>
            {limits.map((l) => (
              <tr key={l.role}>
                <td>{t(`g.role.${l.role}`)}</td>
                <td className="mono">{i.number(l.used_today)}</td>
                <td>
                  <label className="sr-only" htmlFor={`lim-${l.role}`}>
                    {t("g.dailyLimit")}
                  </label>
                  <input
                    id={`lim-${l.role}`}
                    className="q-limit"
                    type="number"
                    min={0}
                    placeholder={t("g.noLimit")}
                    defaultValue={l.daily_limit ?? ""}
                    onBlur={(e) => e.target.value !== String(l.daily_limit ?? "") && set(l.role, e.target.value)}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function GlobalSuite() {
  const { t } = useT();
  const g = useApi<{ cases: Case[]; candidates: Candidate[]; live_model: { model: string } | null }>("/commai/ai-governance", 15_000);
  if (!g.data) return <ErrorNote error={g.error} />;
  return (
    <>
      <Candidates prefix="/commai/ai-governance" items={g.data.candidates} reload={g.reload} />
      <Suite
        title={t("g.globalSuite")}
        hint={t("g.globalHint", { model: g.data.live_model?.model ?? "–" })}
        cases={g.data.cases}
        path="/commai/ai-governance/cases"
        reload={g.reload}
      />
    </>
  );
}
