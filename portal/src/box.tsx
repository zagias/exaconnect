// The box console (ADR 0046): a local web page on the ExaCarib box, for people
// at the branch. The portal shows which boxes also run it, what was last
// changed there, and gives a one-paste "box setup code" for joining a new box.
import type { BoxInfo } from "./api";
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
  const wan = (box.wan ?? []).map((p) => `${PATH_NAMES[p.path] ?? p.path} on ${p.iface}`).join(", ");
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
