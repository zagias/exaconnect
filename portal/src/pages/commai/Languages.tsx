import { useState } from "react";
import { api, useApi } from "../../api";
import { useAuth } from "../../auth";
import { ErrorNote } from "../../components";
import { Card, PageHead, useAction } from "../../ui";
import { DraftLabel, useT } from "./i18n";
import { useCommaiBase } from "./lib";
import "./quality.css";

/* Shapes from controller/exaconnect_controller/commai/api/languages.py (ADR 0026). */
interface LocaleRow {
  locale: string;
  name: string;
  native: string;
  ai_language: string;
  available: boolean;
  status: "source" | "reviewed" | "machine-drafted";
  reviewed_by: string;
  reviewed_at: string | null;
  missing_keys: number;
}
interface Languages {
  source: string;
  my_locale: string;
  locales: LocaleRow[];
  ai: { business_language: string; languages: string[]; options: { code: string; name: string }[] };
}

/** Languages: each person's interface language, the AI's reply languages and catalogue review. */
export default function LanguagesPage() {
  const base = useCommaiBase();
  const i = useT();
  const { t } = i;
  const { user } = useAuth();
  const data = useApi<Languages>(base && `${base}/languages`, 0);
  const pick = useAction();
  const saveAi = useAction();
  const review = useAction();
  const [ai, setAi] = useState<string[] | null>(null);
  const [saved, setSaved] = useState(false);
  if (!base) return <p className="muted">{t("common.chooseBusiness")}</p>;
  const d = data.data;
  const aiLangs = ai ?? d?.ai.languages ?? [];

  const choose = (locale: string) =>
    pick.run(async () => {
      await api(`${base}/languages/me`, { method: "PUT", body: JSON.stringify({ locale }) });
      data.reload();
      i.reload();
    });
  const toggle = (code: string, on: boolean) => {
    setSaved(false);
    setAi(on ? [...new Set([...aiLangs, code])] : aiLangs.filter((x) => x !== code));
  };
  const submitAi = () =>
    saveAi.run(async () => {
      const r = await api<{ languages: string[] }>(`${base}/languages/ai`, { method: "PUT", body: JSON.stringify({ languages: aiLangs }) });
      setAi(r.languages);
      setSaved(true);
      data.reload();
    });
  const signOff = (locale: string, undo: boolean) =>
    review.run(async () => {
      await api(`/commai/i18n/catalogues/${locale}/review`, undo ? { method: "DELETE" } : { method: "POST", body: JSON.stringify({}) });
      data.reload();
      i.reload();
    });
  const statusLabel = (s: LocaleRow["status"]) =>
    s === "source" ? t("lang.status.source") : s === "reviewed" ? t("lang.status.reviewed") : t("lang.status.draft");
  const sample = new Date(Date.UTC(2026, 9, 7, 14, 30)).toISOString();

  return (
    <>
      <PageHead title={t("lang.title")}>
        {t("lang.intro")}
      </PageHead>
      <DraftLabel />
      <ErrorNote error={data.error} />
      {d && (
        <>
          <Card title={t("lang.mine")}>
            <p className="muted small">{t("lang.mineHint")}</p>
            <div className="q-choices" role="radiogroup" aria-label={t("lang.mine")}>
              {d.locales.map((l) => (
                <label key={l.locale} className={`q-choice ${l.available ? "" : "off"}`}>
                  <input
                    type="radio"
                    name="my-locale"
                    disabled={!l.available || pick.busy}
                    checked={d.my_locale === l.locale}
                    onChange={() => choose(l.locale)}
                  />
                  <span>
                    <strong lang={l.locale}>{l.native}</strong>
                    <span className="muted small">
                      {" "}
                      {l.available ? statusLabel(l.status) : t("lang.notOn")}
                    </span>
                  </span>
                </label>
              ))}
            </div>
            <ErrorNote error={pick.error} />
            <dl className="q-formats small">
              <dt>{t("lang.sampleDate")}</dt>
              <dd className="mono">{i.date(sample)}</dd>
              <dt>{t("lang.sampleNumber")}</dt>
              <dd className="mono">{i.number(1234567.891, 2)}</dd>
              <dt>{t("lang.sampleMoney")}</dt>
              <dd className="mono">{i.money(1250.5, "TTD")}</dd>
            </dl>
          </Card>

          <Card title={t("lang.ai")}>
            <p className="muted small">{t("lang.aiHint")}</p>
            <fieldset className="q-langs">
              <legend className="sr-only">{t("lang.ai")}</legend>
              {d.ai.options.map((o) => (
                <label key={o.code} className="check">
                  <input
                    type="checkbox"
                    checked={aiLangs.includes(o.code)}
                    disabled={o.code === d.ai.business_language}
                    onChange={(e) => toggle(o.code, e.target.checked)}
                  />
                  {o.name}
                  {o.code === d.ai.business_language && <span className="muted small"> ({t("lang.businessLanguage")})</span>}
                </label>
              ))}
            </fieldset>
            <div className="actions">
              <button className="button" type="button" disabled={saveAi.busy} onClick={submitAi}>
                {saveAi.busy ? t("common.saving") : t("common.save")}
              </button>
              {saved && <span className="muted small">{t("common.saved")}</span>}
            </div>
            <ErrorNote error={saveAi.error} />
          </Card>

          <Card title={t("lang.catalogues")}>
            <p className="muted small">{t("lang.cataloguesHint")}</p>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t("lang.col.language")}</th>
                    <th>{t("lang.col.offered")}</th>
                    <th>{t("lang.col.review")}</th>
                    <th>{t("lang.col.missing")}</th>
                    {user?.role === "admin" && <th aria-label={t("common.actions")} />}
                  </tr>
                </thead>
                <tbody>
                  {d.locales.map((l) => (
                    <tr key={l.locale}>
                      <td>
                        {l.name} <span className="muted" lang={l.locale}>· {l.native}</span>
                      </td>
                      <td>{l.available ? t("common.yes") : t("lang.notOn")}</td>
                      <td>
                        <span className={`pill ${l.status === "machine-drafted" ? "warn" : "ok"}`}>{statusLabel(l.status)}</span>
                        {l.reviewed_by && <span className="muted small"> {t("lang.reviewedBy", { who: l.reviewed_by, when: i.date(l.reviewed_at, false) })}</span>}
                      </td>
                      <td className="mono">{i.number(l.missing_keys)}</td>
                      {user?.role === "admin" && (
                        <td>
                          {l.status === "machine-drafted" && (
                            <button className="button secondary small" type="button" disabled={review.busy} onClick={() => signOff(l.locale, false)}>
                              {t("lang.signOff")}
                            </button>
                          )}
                          {l.status === "reviewed" && (
                            <button className="button secondary small" type="button" disabled={review.busy} onClick={() => signOff(l.locale, true)}>
                              {t("lang.withdraw")}
                            </button>
                          )}
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <ErrorNote error={review.error} />
          </Card>
        </>
      )}
    </>
  );
}
