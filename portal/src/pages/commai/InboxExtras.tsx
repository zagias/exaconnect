import { useState } from "react";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { useAction } from "../../ui";
import { useT } from "./i18n";
import "./quality.css";

/* Inbox additions (ADR 0032): saved views, files on notes and what the AI read
   from a customer's attachments. Shapes from commai/api/team.py and quality.py. */

export type Filters = Record<string, string>;

interface SavedView {
  id: string;
  name: string;
  filters: Filters;
  shared: boolean;
  mine: boolean;
  owner: string;
}

/** Pick a saved view, or save the filters in use now. */
export function SavedViews({ base, current, onApply }: { base: string; current: Filters; onApply: (f: Filters) => void }) {
  const { t } = useT();
  const views = useApi<SavedView[]>(`${base}/saved-views`, 0);
  const [name, setName] = useState("");
  const [shared, setShared] = useState(false);
  const [saving, setSaving] = useState(false);
  const act = useAction();
  const save = () =>
    act.run(async () => {
      await api(`${base}/saved-views`, { method: "POST", body: JSON.stringify({ name, filters: current, shared }) });
      setName("");
      setSaving(false);
      views.reload();
    });
  const remove = (id: string) =>
    act.run(async () => {
      await api(`${base}/saved-views/${id}`, { method: "DELETE" });
      views.reload();
    });
  const list = views.data ?? [];
  return (
    <div className="saved-views small">
      <label>
        <span className="sr-only">{t("inbox.savedViews")}</span>
        <select
          value=""
          onChange={(e) => {
            const v = list.find((x) => x.id === e.target.value);
            if (v) onApply(v.filters);
          }}
        >
          <option value="">{t("inbox.savedViews")}</option>
          {list.map((v) => (
            <option key={v.id} value={v.id}>
              {v.name}
              {v.mine ? "" : ` · ${v.owner}`}
            </option>
          ))}
        </select>
      </label>
      {!saving ? (
        <button className="button secondary small" type="button" onClick={() => setSaving(true)}>
          {t("inbox.saveView")}
        </button>
      ) : (
        <span className="saved-views-form">
          <label>
            <span className="sr-only">{t("inbox.viewName")}</span>
            <input value={name} maxLength={60} placeholder={t("inbox.viewName")} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="check">
            <input type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} /> {t("inbox.shareView")}
          </label>
          <button className="button small" type="button" disabled={!name.trim() || act.busy} onClick={save}>
            {t("common.save")}
          </button>
        </span>
      )}
      {list.some((v) => v.mine) && (
        <details>
          <summary>{t("inbox.manageViews")}</summary>
          <ul>
            {list
              .filter((v) => v.mine)
              .map((v) => (
                <li key={v.id}>
                  {v.name} {v.shared && <span className="tag">{t("inbox.shared")}</span>}{" "}
                  <button className="button secondary small" type="button" onClick={() => remove(v.id)}>
                    {t("common.delete")}
                  </button>
                </li>
              ))}
          </ul>
        </details>
      )}
      <ErrorNote error={act.error} />
    </div>
  );
}

export interface FileItem {
  id: string;
  name: string;
  type: string;
  size: number;
}

/** Upload files to a note after it is written. */
export async function uploadNoteFiles(base: string, noteId: string, files: File[]): Promise<void> {
  for (const f of files) {
    const data = await new Promise<string>((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(",", 2)[1] ?? "");
      r.onerror = () => reject(new Error("The file couldn't be read."));
      r.readAsDataURL(f);
    });
    await api(`${base}/notes/${noteId}/files`, { method: "POST", body: JSON.stringify({ name: f.name, type: f.type, data }) });
  }
}

/** Files on a private note: staff with note access only. */
export function NoteFiles({ base, files }: { base: string; files: FileItem[] | undefined }) {
  const i = useT();
  if (!files?.length) return null;
  return (
    <ul className="note-files small" aria-label={i.t("inbox.noteFiles")}>
      {files.map((f) => (
        <li key={f.id}>
          <a href={`/api/v1${base}/note-files/${f.id}`} download={f.name}>
            {f.name}
          </a>{" "}
          <span className="muted">{i.number(Math.ceil(f.size / 1024))} KB</span>
        </li>
      ))}
    </ul>
  );
}

interface Reading {
  file_id: string;
  name: string;
  content_type: string;
  kind: string | null;
  status: string | null;
  text: string | null;
  reason: string | null;
  reader: string | null;
}

/** What the AI read from each file the customer sent. */
export function AttachmentReadings({ base, conversationId }: { base: string; conversationId: string }) {
  const { t } = useT();
  const r = useApi<Reading[]>(`${base}/conversations/${conversationId}/attachments`, 30_000);
  if (!r.data?.length) return null;
  return (
    <section className="readings">
      <h3>{t("inbox.aiRead")}</h3>
      <ul className="small">
        {r.data.map((x) => (
          <li key={x.file_id}>
            <a href={`/api/v1${base}/files/${x.file_id}`}>{x.name}</a>{" "}
            {x.status === "read" ? (
              <span className="pill ok">{t(`inbox.kind.${x.kind ?? "other"}`)}</span>
            ) : x.status ? (
              <span className="pill warn">{x.reason}</span>
            ) : (
              <span className="muted">{t("inbox.notRead")}</span>
            )}
            {x.status === "read" && x.text && <blockquote>{x.text.length > 300 ? x.text.slice(0, 299) + "…" : x.text}</blockquote>}
          </li>
        ))}
      </ul>
    </section>
  );
}
