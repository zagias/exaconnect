// Box ports set in the portal (ADR 0051): the same choices as the box
// console's port page, for a box managed from the cloud or both. The box
// keeps a change only once it can still reach ExaCarib with it, and puts its
// earlier ports back after two minutes otherwise.
import { useMemo, useState } from "react";
import { api, type BoxInfo, type BoxManaged, type BoxNetwork, type BoxPort } from "./api";
import { ago } from "./components";

type Use = "" | "carrier-a" | "carrier-b" | "sat" | "lan";
const USES: [Use, string][] = [
  ["", "Not used"],
  ["carrier-a", "Internet link 1"],
  ["carrier-b", "Internet link 2"],
  ["sat", "Satellite"],
  ["lan", "Office network"],
];
const MODES: Record<string, string> = { dhcp: "Automatic (DHCP)", static: "Fixed address", pppoe: "PPPoE", keep: "Leave as it is" };

interface Row {
  iface: string;
  use: Use;
  mode: string;
  address: string;
  gateway: string;
  dns: string;
  pppoe_user: string;
  pppoe_password: string;
  pppoe_set: boolean;
}

function rowsFrom(names: string[], wan: BoxPort[], lan: BoxPort | null | undefined): Row[] {
  return names.map((iface) => {
    const w = wan.find((p) => p.iface === iface);
    const l = lan && lan.iface === iface ? lan : null;
    const p = w ?? l;
    const set = !!(p?.pppoe_set || p?.pppoe_password);
    return {
      iface,
      use: w ? ((w.path || "carrier-a") as Use) : l ? "lan" : "",
      mode: p?.mode ?? "dhcp",
      address: p?.address ?? "",
      gateway: p?.gateway ?? "",
      dns: (p?.dns ?? []).join(", "),
      pppoe_user: p?.pppoe_user ?? "",
      pppoe_password: set ? "set" : "",
      pppoe_set: set,
    };
  });
}

const STATE_WORDS: Record<string, [string, string]> = {
  trying: ["warn", "The box is trying these ports. It keeps them once it can still reach ExaCarib with them."],
  kept: ["ok", "The box is using the ports set here"],
  undone: ["bad", "The box put its earlier ports back"],
  refused: ["bad", "The box did not use the ports set here"],
  replaced: ["warn", "The ports were changed again on the box"],
};

export function BoxPorts({
  siteId,
  box,
  managed,
  portal,
  canManage,
  onChange,
}: {
  siteId: string;
  box?: BoxInfo | null;
  managed?: BoxManaged | null;
  portal?: BoxNetwork | null;
  canManage: boolean;
  onChange?: () => void;
}) {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [dhcp, setDhcp] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const names = useMemo(() => {
    const n = (box?.ports ?? []).map((p) => p.name);
    for (const p of [...(box?.wan ?? []), ...(box?.lan ? [box.lan] : [])]) if (!n.includes(p.iface)) n.push(p.iface);
    return n;
  }, [box]);
  if (!box?.console) return null;
  const mode = managed?.mode ?? "both";
  const editable = canManage && mode !== "site";
  // The box's own ports unless the portal's set-up is newer and still on its way.
  const portalNewer = portal && (!box.network_at || Date.parse(portal.at) > Date.parse(box.network_at));
  const start = () => {
    const src = portalNewer ? portal!.network : { wan: box.wan ?? [], lan: box.lan, lan_dhcp: box.lan_dhcp };
    setRows(rowsFrom(names, src.wan, src.lan));
    setDhcp(!!src.lan_dhcp);
    setError(null);
  };
  const set = (i: number, k: keyof Row, v: string) =>
    setRows((rs) => rs!.map((r, j) => (j === i ? { ...r, [k]: v, ...(k === "use" && v === "lan" ? { mode: "static" } : {}) } : r)));
  const port = (r: Row): BoxPort => {
    const p: BoxPort = { iface: r.iface, mode: r.mode };
    if (r.mode === "static") Object.assign(p, { address: r.address.trim(), gateway: r.gateway.trim() });
    if (r.mode === "pppoe") Object.assign(p, { pppoe_user: r.pppoe_user, pppoe_password: r.pppoe_password });
    const dns = r.dns.split(/[\s,]+/).filter(Boolean);
    if (dns.length) p.dns = dns;
    return p;
  };
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!rows) return;
    const order: Use[] = ["carrier-a", "carrier-b", "sat"];
    const wan = rows
      .filter((r) => r.use && r.use !== "lan")
      .sort((a, b) => order.indexOf(a.use) - order.indexOf(b.use))
      .map((r) => ({ ...port(r), path: r.use }));
    const l = rows.find((r) => r.use === "lan");
    setBusy(true);
    setError(null);
    try {
      await api(`/sites/${siteId}/box/network`, {
        method: "POST",
        body: JSON.stringify({ wan, lan: l ? port(l) : null, lan_dhcp: !!l && dhcp }),
      });
      setRows(null);
      onChange?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const st = box.portal && portal && box.portal.at && Date.parse(box.portal.at) === Date.parse(portal.at) ? box.portal : null;
  const waiting = portalNewer && !st;
  const used = (rows ?? []).map((r) => r.use).filter(Boolean);
  return (
    <section className="card span-12" aria-label="Box ports">
      <div className="card-head">
        <div>
          <div className="eyebrow">Box ports</div>
          <h2>Ports on this box</h2>
        </div>
        {editable && !rows && (
          <button className="button secondary small" onClick={start}>
            Change the ports
          </button>
        )}
      </div>
      {!rows && (
        <>
          <p className="small">
            {(box.wan ?? []).map((p) => `${USES.find((u) => u[0] === p.path)?.[1] ?? p.path} on ${p.iface} (${MODES[p.mode] ?? p.mode}${p.address ? ` ${p.address}` : ""})`).join(", ") ||
              "No internet port set yet."}
            {box.lan ? ` Office network on ${box.lan.iface}${box.lan.address ? ` ${box.lan.address}` : ""}${box.lan_dhcp ? ", handing out addresses" : ""}.` : ""}
          </p>
          {portal && (
            <p className="small muted" role="status">
              {st ? (
                <span className={`pill ${STATE_WORDS[st.state][0]}`}>
                  {STATE_WORDS[st.state][1]}
                  {st.detail ? `: ${st.detail}` : ""}
                </span>
              ) : waiting ? (
                <span className="pill warn">Sent to the box, waiting for it to pick them up</span>
              ) : null}{" "}
              Last set here by {portal.by}, {ago(portal.at)}.
            </p>
          )}
          {mode === "site" && <p className="small muted">This box is managed on site, so its ports are changed in its box console. Switch Managed from to change them here.</p>}
        </>
      )}
      {rows && (
        <form className="form" onSubmit={save}>
          <p className="small muted wide">
            The box tries the new ports and keeps them once it can still reach ExaCarib with them. If it can't, it puts the earlier ports back after two minutes.
          </p>
          {rows.map((r, i) => (
            <fieldset key={r.iface} className="wide form" style={{ border: "1px solid var(--line)", borderRadius: 8, padding: 12 }}>
              <legend className="mono">{r.iface}</legend>
              <label>
                Use
                <select aria-label={`Use of ${r.iface}`} value={r.use} onChange={(e) => set(i, "use", e.target.value)}>
                  {USES.map(([v, n]) => (
                    <option key={v} value={v} disabled={!!v && v !== r.use && used.includes(v)}>
                      {n}
                    </option>
                  ))}
                </select>
              </label>
              {r.use && (
                <label>
                  Address
                  <select aria-label={`Address of ${r.iface}`} value={r.mode} onChange={(e) => set(i, "mode", e.target.value)}>
                    {Object.entries(MODES)
                      .filter(([m]) => (r.use === "lan" ? m === "static" || m === "keep" : true))
                      .map(([m, n]) => (
                        <option key={m} value={m}>
                          {n}
                        </option>
                      ))}
                  </select>
                </label>
              )}
              {r.use && r.mode === "static" && (
                <>
                  <label>
                    Address and size
                    <input aria-label={`Address and size for ${r.iface}`} className="mono" value={r.address} placeholder="192.168.1.2/24" onChange={(e) => set(i, "address", e.target.value)} />
                  </label>
                  <label>
                    Gateway{r.use === "lan" ? " (optional)" : ""}
                    <input aria-label={`Gateway for ${r.iface}`} className="mono" value={r.gateway} onChange={(e) => set(i, "gateway", e.target.value)} />
                  </label>
                </>
              )}
              {r.use && r.mode === "pppoe" && (
                <>
                  <label>
                    PPPoE user name
                    <input aria-label={`PPPoE user name for ${r.iface}`} value={r.pppoe_user} autoComplete="off" onChange={(e) => set(i, "pppoe_user", e.target.value)} />
                  </label>
                  <label>
                    PPPoE password
                    <input
                      aria-label={`PPPoE password for ${r.iface}`}
                      type="password"
                      autoComplete="new-password"
                      value={r.pppoe_password === "set" ? "" : r.pppoe_password}
                      placeholder={r.pppoe_set ? "Saved on the box; type to change" : ""}
                      onChange={(e) => set(i, "pppoe_password", e.target.value || (r.pppoe_set ? "set" : ""))}
                    />
                  </label>
                </>
              )}
              {r.use && r.use !== "lan" && r.mode !== "keep" && (
                <label>
                  DNS servers (optional)
                  <input aria-label={`DNS servers for ${r.iface}`} className="mono" value={r.dns} placeholder="1.1.1.1, 8.8.8.8" onChange={(e) => set(i, "dns", e.target.value)} />
                </label>
              )}
            </fieldset>
          ))}
          {rows.some((r) => r.use === "lan") && (
            <label className="check wide">
              <input type="checkbox" checked={dhcp} onChange={(e) => setDhcp(e.target.checked)} /> Hand out addresses on the office network
            </label>
          )}
          {error && <p className="pill bad wide">{error}</p>}
          <div className="actions wide">
            <button className="button" disabled={busy}>
              Send to the box
            </button>
            <button type="button" className="button secondary" onClick={() => setRows(null)} disabled={busy}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
