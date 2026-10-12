// The box console (ADR 0046): a local web page on the ExaCarib box, for people
// at the branch. The portal shows which boxes also run it, what was last
// changed there, and gives a one-paste "box setup code" for joining a new box.
import { useState } from "react";
import { api, type BoxInfo, type BoxManaged, type BoxManagedMode } from "./api";
import { ago } from "./components";

const PATH_NAMES: Record<string, string> = { "carrier-a": "Internet link 1", "carrier-b": "Internet link 2", sat: "Satellite" };

/** "EXA1." and the controller address, token, CA fingerprint and site name as
 *  base64url JSON: what the box console's join step takes in one paste. */
export function boxSetupCode(i: { controller: string; token: string; ca_fingerprint: string; site: string }): string {
  const json = JSON.stringify({ c: i.controller, t: i.token, f: i.ca_fingerprint, n: i.site });
  const bytes = new TextEncoder().encode(json);
  let bin = "";
  bytes.forEach((b) => (bin += String.fromCharCode(b)));
  return "EXA1." + btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** A small tag for boxes that run the box console. */
export function BoxTag({ box }: { box?: BoxInfo | null }) {
  if (!box?.console) return null;
  return (
    <span className="tag" title="Someone at the location can also set this box up in its local box console">
      Box console
    </span>
  );
}

/** One line saying the box is also managed locally, and what was last changed there. */
export function BoxManagedNote({ box }: { box?: BoxInfo | null }) {
  if (!box?.console) return null;
  const wan = (box.wan ?? []).map((p) => `${(p.path && PATH_NAMES[p.path]) || p.path || "Internet"} on ${p.iface}`).join(", ");
  return (
    <p className="small muted box-note">
      <BoxTag box={box} /> Also managed on the box itself
      {box.url ? (
        <>
          {" "}
          at <span className="mono">{box.url}</span> from the office network
        </>
      ) : null}
      .{wan ? ` Ports: ${wan}${box.lan ? `, office network on ${box.lan.iface}` : ""}.` : ""}
      {box.last_change ? ` Last change there: ${box.last_change.what} (${box.last_change.by}, ${ago(box.last_change.at)}).` : ""}
    </p>
  );
}

export const MANAGED_NAMES: Record<BoxManagedMode, string> = { site: "On site", cloud: "Cloud", both: "On site and cloud" };
const MANAGED_LINES: Record<BoxManagedMode, string> = {
  site: "Only the box console at the location changes this box's settings. Connect still runs its routing.",
  cloud: "Only this portal changes this box's settings. The box console shows them but can't change them, apart from taking back control in an emergency.",
  both: "The box console and this portal can both change this box's settings. The latest change wins.",
};

/** A small tag saying where a box is managed from (ADR 0049). */
export function ManagedTag({ managed }: { managed?: BoxManaged | null }) {
  if (!managed) return null;
  return (
    <span className="tag" title={MANAGED_LINES[managed.mode]}>
      Managed: {MANAGED_NAMES[managed.mode]}
    </span>
  );
}

/** The "Managed from" switch for a box that runs the box console (ADR 0049).
 *  Switching keeps the box's settings as they are. */
export function ManagedFrom({
  siteId,
  box,
  managed,
  canManage,
  onChange,
}: {
  siteId: string;
  box?: BoxInfo | null;
  managed?: BoxManaged | null;
  canManage: boolean;
  onChange?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!box?.console) return null;
  const m = managed ?? { mode: "both" as const, at: null, by: null };
  // The box reports its own copy; until it catches up the portal's switch is on its way.
  const pending = box.managed && box.managed.mode !== m.mode;
  const pick = async (mode: BoxManagedMode) => {
    setBusy(true);
    setError(null);
    try {
      await api(`/sites/${siteId}/box/managed`, { method: "POST", body: JSON.stringify({ mode }) });
      onChange?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="managed-from">
      <div className="form-actions" style={{ alignItems: "center", marginTop: 8 }}>
        <span className="small">
          <strong>Managed from</strong>
        </span>
        <div className="segmented" role="group" aria-label="Managed from">
          {(Object.keys(MANAGED_NAMES) as BoxManagedMode[]).map((mode) => (
            <button
              key={mode}
              aria-pressed={m.mode === mode}
              disabled={!canManage || busy || m.mode === mode}
              onClick={() => void pick(mode)}
            >
              {MANAGED_NAMES[mode]}
            </button>
          ))}
        </div>
      </div>
      <p className="small muted">
        {MANAGED_LINES[m.mode]}
        {m.at && m.by ? ` Set by ${m.by}, ${ago(m.at)}.` : ""}
        {pending ? " Waiting for the box to pick this up." : ""}
        {canManage ? " Switching keeps the box's settings as they are." : ""}
      </p>
      {error && <p className="pill bad">{error}</p>}
    </div>
  );
}
