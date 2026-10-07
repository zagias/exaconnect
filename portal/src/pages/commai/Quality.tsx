import { useState, type FormEvent } from "react";
import { Link, NavLink, Route, Routes } from "react-router-dom";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { Card, PageHead, Tabs, useAction } from "../../ui";
import { DraftLabel, useT } from "./i18n";
import { useCommaiBase } from "./lib";
import "./quality.css";

/* Shapes from controller/exaconnect_controller/commai/api/quality.py and team.py (ADR 0026). */
interface Criterion {
  id: string;
  text: string;
  enabled: boolean;
}
interface Verdict {
  id: string;
  criterion: string;
  verdict: "pass" | "fail" | "unclear";
  quote: string;
  quote_dropped: boolean;
  why: string;
}
interface Result {
  id: string;
  conversation_id: string;
  handled_by: "ai" | "human";
  score: number | string;
  results: Verdict[];
  flagged: boolean;
  flag_status: string;
  subject?: string;
  channel?: string;
  contact_name?: string;
  review_note?: string;
  reviewed_by?: string;
}
interface Review {
  id: string;
  status: "queued" | "done" | "failed";
  sample_size: number;
  days: number;
  error: string;
  created_at: string;
  summary: { judged?: number; flagged?: number; average?: { ai: number | null; human: number | null } };
}
interface GapGroup {
  topic: string;
  times: number;
  reasons: string[];
  gap_ids: string[];
  questions: { id: string; question: string; times: number; reason: string }[];
  draft_source_id: string | null;
}
interface Reminder {
  id: string;
  conversation_id: string;
  promise: string;
  due_at: string;
  owner: string | null;
  subject: string;
  status: string;
}
interface Csat {
  settings: { enabled: boolean; delay_minutes: number; cooldown_days: number };
  last_30_days: {
    value: number | null;
    sent: number;
    blocked: number;
    answered: number;
    response_rate: number | null;
    satisfied_share: number | null;
    by_handler: { ai: number | null; human: number | null };
  };
  recent: { id: string; conversation_id: string; channel: string; status: string; reason: string; rating: number | null; comment: string; created_at: string; subject: string }[];
}

type Tab = "review" | "flags" | "gaps" | "followups" | "csat";

/** AI quality: review against the business's own criteria, knowledge gaps, follow-ups and satisfaction. */
export default function Quality() {
  const base = useCommaiBase();
  const { t } = useT();
  if (!base) return <p className="muted">{t("common.chooseBusiness")}</p>;
  // Each section has its own address, like every other screen's tabs.
  const tabs: [Tab, string, string][] = [
    ["review", "/commai/quality", t("q.tab.review")],
    ["flags", "/commai/quality/flags", t("q.tab.flags")],
    ["gaps", "/commai/quality/gaps", t("q.tab.gaps")],
    ["followups", "/commai/quality/follow-ups", t("q.tab.followups")],
    ["csat", "/commai/quality/satisfaction", t("q.tab.csat")],
  ];
  return (
    <>
      <PageHead title={t("q.title")}>
        {t("q.intro")}
      </PageHead>
      <DraftLabel />
      <Tabs label={t("q.title")}>
        {tabs.map(([id, to, label]) => (
          <NavLink key={id} to={to} end>
            {label}
          </NavLink>
        ))}
      </Tabs>
      <Routes>
        <Route index element={<ReviewTab base={base} />} />
        <Route path="flags" element={<FlagsTab base={base} />} />
        <Route path="gaps" element={<GapsTab base={base} />} />
        <Route path="follow-ups" element={<FollowupsTab base={base} />} />
        <Route path="satisfaction" element={<CsatTab base={base} />} />
      </Routes>
    </>
  );
}

function ReviewTab({ base }: { base: string }) {
  const i = useT();
  const { t } = i;
  const crit = useApi<Criterion[]>(`${base}/quality/criteria`, 0);
  const reviews = useApi<Review[]>(`${base}/quality/reviews`, 10_000);
  const [text, setText] = useState("");
  const [size, setSize] = useState(20);
  const [open, setOpen] = useState<string | null>(null);
  const act = useAction();

  const add = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api(`${base}/quality/criteria`, { method: "POST", body: JSON.stringify({ text }) });
      setText("");
      crit.reload();
    });
  };
  const toggle = (c: Criterion) =>
    act.run(async () => {
      await api(`${base}/quality/criteria/${c.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !c.enabled }) });
      crit.reload();
    });
  const remove = (c: Criterion) =>
    act.run(async () => {
      await api(`${base}/quality/criteria/${c.id}`, { method: "DELETE" });
      crit.reload();
    });
  const start = () =>
    act.run(async () => {
      await api(`${base}/quality/reviews`, { method: "POST", body: JSON.stringify({ sample_size: size, days: 30 }) });
      reviews.reload();
    });

  return (
    <>
      <Card title={t("q.criteria")}>
        <p className="muted small">{t("q.criteriaHint")}</p>
        <ul className="q-list">
          {(crit.data ?? []).map((c) => (
            <li key={c.id} className={c.enabled ? "" : "off"}>
              <span>{c.text}</span>
              <span className="q-row-actions">
                <button className="button secondary small" type="button" onClick={() => toggle(c)}>
                  {c.enabled ? t("q.pause") : t("q.resume")}
                </button>
                <button className="button secondary small" type="button" onClick={() => remove(c)}>
                  {t("common.delete")}
                </button>
              </span>
            </li>
          ))}
        </ul>
        {crit.data && crit.data.length === 0 && <p className="muted">{t("q.noCriteria")}</p>}
        <form className="form" onSubmit={add}>
          <label className="wide">
            {t("q.newCriterion")}
            <input value={text} minLength={3} maxLength={300} required placeholder={t("q.criterionExample")} onChange={(e) => setText(e.target.value)} />
          </label>
          <div className="actions">
            <button className="button" disabled={act.busy}>
              {t("q.addCriterion")}
            </button>
          </div>
        </form>
      </Card>

      <Card title={t("q.reviews")}>
        <div className="form">
          <label>
            {t("q.sample")}
            <input type="number" min={1} max={100} value={size} onChange={(e) => setSize(Number(e.target.value))} />
          </label>
          <div className="actions">
            <button className="button" type="button" disabled={act.busy || !crit.data?.some((c) => c.enabled)} onClick={start}>
              {t("q.run")}
            </button>
          </div>
        </div>
        <ErrorNote error={act.error ?? reviews.error} />
        <ul className="q-list">
          {(reviews.data ?? []).map((r) => (
            <li key={r.id}>
              <span>
                <strong>{i.date(r.created_at)}</strong>{" "}
                <span className="muted small">
                  {r.status === "queued"
                    ? t("q.queued")
                    : r.status === "failed"
                      ? t("q.failed", { why: r.error })
                      : t("q.summary", {
                          judged: r.summary.judged ?? 0,
                          flagged: r.summary.flagged ?? 0,
                          ai: score(i, r.summary.average?.ai),
                          human: score(i, r.summary.average?.human),
                        })}
                </span>
              </span>
              {r.status === "done" && (
                <button className="button secondary small" type="button" onClick={() => setOpen(open === r.id ? null : r.id)}>
                  {open === r.id ? t("common.hide") : t("common.show")}
                </button>
              )}
            </li>
          ))}
        </ul>
        {open && <ReviewResults base={base} id={open} />}
      </Card>
    </>
  );
}

function score(i: ReturnType<typeof useT>, v: number | string | null | undefined): string {
  return v === null || v === undefined ? "–" : i.percent(Number(v));
}

function ReviewResults({ base, id }: { base: string; id: string }) {
  const { t } = useT();
  const r = useApi<{ results: Result[] }>(`${base}/quality/reviews/${id}`, 0);
  return (
    <div className="q-results">
      <ErrorNote error={r.error} />
      {(r.data?.results ?? []).map((x) => (
        <ResultCard key={x.id} r={x} />
      ))}
      {r.data && r.data.results.length === 0 && <p className="muted">{t("q.nothingJudged")}</p>}
    </div>
  );
}

function ResultCard({ r, children }: { r: Result; children?: React.ReactNode }) {
  const i = useT();
  const { t } = i;
  return (
    <article className={`q-result ${r.flagged ? "flagged" : ""}`}>
      <header>
        <Link to={`/commai/c/${r.conversation_id}`}>{r.contact_name || r.subject || t("q.conversation")}</Link>
        <span className="tag">{r.handled_by === "ai" ? t("q.byAi") : t("q.byPerson")}</span>
        <span className="mono">{score(i, r.score)}</span>
        {r.flagged && <span className="pill bad">{t("q.flagged")}</span>}
      </header>
      <ul>
        {r.results.map((v) => (
          <li key={v.id}>
            <span className={`pill ${v.verdict === "pass" ? "ok" : v.verdict === "fail" ? "bad" : "shadow"}`}>{t(`q.verdict.${v.verdict}`)}</span>{" "}
            <strong>{v.criterion}</strong>
            {v.quote && <blockquote>“{v.quote}”</blockquote>}
            {v.quote_dropped && <p className="muted small">{t("q.quoteDropped")}</p>}
            {v.why && <p className="muted small">{v.why}</p>}
          </li>
        ))}
      </ul>
      {children}
    </article>
  );
}

function FlagsTab({ base }: { base: string }) {
  const { t } = useT();
  const flags = useApi<Result[]>(`${base}/quality/flags`, 15_000);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const act = useAction();
  const done = (id: string) =>
    act.run(async () => {
      await api(`${base}/quality/flags/${id}/review`, { method: "POST", body: JSON.stringify({ note: notes[id] ?? "" }) });
      flags.reload();
    });
  return (
    <Card title={t("q.flagsTitle")}>
      <p className="muted small">{t("q.flagsHint")}</p>
      <ErrorNote error={flags.error ?? act.error} />
      {flags.data && flags.data.length === 0 && <p className="muted">{t("q.noFlags")}</p>}
      {(flags.data ?? []).map((f) => (
        <ResultCard key={f.id} r={f}>
          <div className="form">
            <label className="wide">
              {t("q.reviewNote")}
              <input value={notes[f.id] ?? ""} maxLength={1000} onChange={(e) => setNotes({ ...notes, [f.id]: e.target.value })} />
            </label>
            <div className="actions">
              <button className="button small" type="button" disabled={act.busy} onClick={() => done(f.id)}>
                {t("q.markReviewed")}
              </button>
            </div>
          </div>
        </ResultCard>
      ))}
    </Card>
  );
}

function GapsTab({ base }: { base: string }) {
  const { t } = useT();
  const rep = useApi<{ groups: GapGroup[]; open: number }>(`${base}/ai/gaps/report`, 0);
  const act = useAction();
  const [drafted, setDrafted] = useState<{ id: string; title: string; body: string; placeholders: boolean } | null>(null);
  const [body, setBody] = useState("");
  const draft = (g: GapGroup) =>
    act.run(async () => {
      const d = await api<{ id: string; title: string; body: string; placeholders: boolean }>(`${base}/ai/gaps/draft`, {
        method: "POST",
        body: JSON.stringify({ gap_ids: g.gap_ids }),
      });
      setDrafted(d);
      setBody(d.body);
      rep.reload();
    });
  const approve = () =>
    act.run(async () => {
      if (!drafted) return;
      await api(`${base}/ai/knowledge/${drafted.id}`, { method: "PATCH", body: JSON.stringify({ body }) });
      await api(`${base}/ai/gaps/drafts/${drafted.id}/approve`, { method: "POST" });
      setDrafted(null);
      rep.reload();
    });
  return (
    <>
      <Card title={t("q.gapsTitle")}>
        <p className="muted small">{t("q.gapsHint", { n: rep.data?.open ?? 0 })}</p>
        <ErrorNote error={rep.error ?? act.error} />
        {rep.data && rep.data.groups.length === 0 && <p className="muted">{t("q.noGaps")}</p>}
        <ul className="q-list">
          {(rep.data?.groups ?? []).map((g) => (
            <li key={g.gap_ids.join()}>
              <span>
                <strong>{g.topic || t("q.otherQuestions")}</strong> <span className="muted small">{t("q.asked", { n: g.times })}</span>
                <ul className="q-questions small">
                  {g.questions.map((q) => (
                    <li key={q.id}>
                      {q.question} {q.reason === "contradictory" && <span className="pill warn">{t("q.contradictory")}</span>}
                    </li>
                  ))}
                </ul>
              </span>
              {g.draft_source_id ? (
                <span className="muted small">{t("q.draftWaiting")}</span>
              ) : (
                <button className="button secondary small" type="button" disabled={act.busy} onClick={() => draft(g)}>
                  {t("q.draftArticle")}
                </button>
              )}
            </li>
          ))}
        </ul>
      </Card>
      {drafted && (
        <Card title={t("q.draftTitle", { title: drafted.title })}>
          <p className="muted small">{t("q.draftHint")}</p>
          <div className="form">
            <label className="wide">
              {t("q.draftBody")}
              <textarea rows={10} value={body} onChange={(e) => setBody(e.target.value)} />
            </label>
            <div className="actions">
              <button className="button" type="button" disabled={act.busy || /\[(Check|Write)/i.test(body)} onClick={approve}>
                {t("q.approve")}
              </button>
              {/\[(Check|Write)/i.test(body) && <span className="muted small">{t("q.fillPlaceholders")}</span>}
            </div>
          </div>
        </Card>
      )}
    </>
  );
}

function FollowupsTab({ base }: { base: string }) {
  const i = useT();
  const { t } = i;
  const [mine, setMine] = useState(false);
  const list = useApi<Reminder[]>(`${base}/followups?mine=${mine}`, 30_000);
  const act = useAction();
  const close = (id: string, status: string) =>
    act.run(async () => {
      await api(`${base}/followups/${id}`, { method: "POST", body: JSON.stringify({ status }) });
      list.reload();
    });
  const now = Date.now();
  return (
    <Card title={t("q.followupsTitle")}>
      <p className="muted small">{t("q.followupsHint")}</p>
      <label className="check small">
        <input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} /> {t("q.onlyMine")}
      </label>
      <ErrorNote error={list.error ?? act.error} />
      {list.data && list.data.length === 0 && <p className="muted">{t("q.noFollowups")}</p>}
      <ul className="q-list">
        {(list.data ?? []).map((r) => (
          <li key={r.id}>
            <span>
              <strong>“{r.promise}”</strong>
              <span className="muted small">
                {" "}
                {t("q.due", { when: i.date(r.due_at), who: r.owner ?? t("q.noOwner") })}{" "}
                <Link to={`/commai/c/${r.conversation_id}`}>{r.subject || t("q.conversation")}</Link>
              </span>
              {new Date(r.due_at).getTime() < now && <span className="pill bad"> {t("q.overdue")}</span>}
            </span>
            <span className="q-row-actions">
              <button className="button small" type="button" onClick={() => close(r.id, "done")}>
                {t("q.done")}
              </button>
              <button className="button secondary small" type="button" onClick={() => close(r.id, "dismissed")}>
                {t("q.dismiss")}
              </button>
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function CsatTab({ base }: { base: string }) {
  const i = useT();
  const { t } = i;
  const d = useApi<Csat>(`${base}/csat`, 30_000);
  const act = useAction();
  const save = (patch: Partial<Csat["settings"]>) =>
    act.run(async () => {
      await api(`${base}/csat`, { method: "PUT", body: JSON.stringify(patch) });
      d.reload();
    });
  if (!d.data) return <ErrorNote error={d.error} />;
  const s = d.data.settings;
  const l = d.data.last_30_days;
  const status = (x: string) => t(`q.csat.status.${x}`);
  return (
    <>
      <Card title={t("q.csatTitle")}>
        <p className="muted small">{t("q.csatHint")}</p>
        <div className="form">
          <label className="check wide">
            <input type="checkbox" checked={s.enabled} disabled={act.busy} onChange={(e) => save({ enabled: e.target.checked })} />
            {t("q.csatOn")}
          </label>
          <label>
            {t("q.csatDelay")}
            <input type="number" min={0} max={10080} defaultValue={s.delay_minutes} onBlur={(e) => save({ delay_minutes: Number(e.target.value) })} />
          </label>
          <label>
            {t("q.csatCooldown")}
            <input type="number" min={0} max={365} defaultValue={s.cooldown_days} onBlur={(e) => save({ cooldown_days: Number(e.target.value) })} />
          </label>
        </div>
        <ErrorNote error={act.error} />
      </Card>
      <Card title={t("q.csatResults")}>
        <dl className="q-stats">
          <div>
            <dt>{t("q.csatAverage")}</dt>
            <dd className="mono">{l.value === null ? "–" : i.number(l.value, 2)}</dd>
          </div>
          <div>
            <dt>{t("q.csatSatisfied")}</dt>
            <dd className="mono">{i.percent(l.satisfied_share)}</dd>
          </div>
          <div>
            <dt>{t("q.csatRate")}</dt>
            <dd className="mono">{i.percent(l.response_rate)}</dd>
          </div>
          <div>
            <dt>{t("q.csatSent")}</dt>
            <dd className="mono">
              {i.number(l.sent)} · {t("q.csatBlocked", { n: l.blocked })}
            </dd>
          </div>
          <div>
            <dt>{t("q.csatAiHuman")}</dt>
            <dd className="mono">
              {l.by_handler.ai === null ? "–" : i.number(l.by_handler.ai, 2)} / {l.by_handler.human === null ? "–" : i.number(l.by_handler.human, 2)}
            </dd>
          </div>
        </dl>
        <ul className="q-list">
          {d.data.recent.map((r) => (
            <li key={r.id}>
              <span>
                <Link to={`/commai/c/${r.conversation_id}`}>{r.subject || t("q.conversation")}</Link>{" "}
                <span className="muted small">
                  {i.date(r.created_at)} · {r.channel} · {status(r.status)}
                  {r.reason && ` · ${r.reason}`}
                </span>
                {r.comment && <blockquote>“{r.comment}”</blockquote>}
              </span>
              {r.rating !== null && <span className="mono">{"★".repeat(r.rating)}</span>}
            </li>
          ))}
        </ul>
      </Card>
    </>
  );
}
