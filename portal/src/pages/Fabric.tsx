import { Fragment, useState, type FormEvent, type ReactNode } from "react";
import {
  circuitPaths,
  createCircuit,
  deleteCircuit,
  num,
  PSK_HINT,
  PSK_PATTERN,
  setCloudToCloud,
  updateCircuit,
  useApi,
  type Circuit,
  type CircuitCharges,
  type CircuitMetric,
  type CircuitStatus,
  type CloudProviders,
  type CustomerSettings,
} from "../api";
import { ErrorNote, Eyebrow, LineChart, fmt } from "../components";
import { useCustomer, who } from "../customer";
import { Card, useAction } from "../ui";

// ExaConnect Fabric: virtual circuits to clouds and between sites (ADR 0009,
// docs/fabric-contract.md). Bandwidth can change at any time and is billed by
// the hour; the pre-shared key is write-only.

const STATUS: Record<CircuitStatus, { cls: string; word: string }> = {
  up: { cls: "ok", word: "Up" },
  provisioning: { cls: "warn", word: "Provisioning" },
  down: { cls: "bad", word: "Down" },
  off: { cls: "shadow", word: "Off" },
};

const usd = (v: unknown) => {
  const n = num(v);
  return n === null ? "–" : `US$ ${n.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

const mbps = (v: unknown) => {
  const n = num(v);
  return n === null ? "–" : `${n.toLocaleString("en-GB")} Mbps`;
};

const split = (s: string) =>
  s
    .split(/[\s,]+/)
    .map((x) => x.trim())
    .filter(Boolean);

const thisMonth = () => new Date().toISOString().slice(0, 7);

export default function Fabric() {
  const { current } = useCustomer();
  const list = useApi<Circuit[]>(current ? circuitPaths.list(current.id) : null, 10_000);
  const providers = useApi<CloudProviders>("/circuits/providers", 0);
  const classes = useApi<{ name: string }[]>(current ? `/classes?customer_id=${current.id}` : null, 0);
  const [form, setForm] = useState<"cloud" | "site" | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  if (!current) return null;
  const circuits = list.data ?? [];
  const done = () => {
    setForm(null);
    list.reload();
  };
  return (
    <>
      <div className="page-head">
        <Eyebrow>ExaConnect Fabric</Eyebrow>
        <h1>Virtual circuits</h1>
        <p className="muted">
          Private circuits from your sites to your clouds, and between your sites, through ExaCarib's PoP. Change the
          bandwidth whenever you need to; you pay by the hour for what is set.
        </p>
      </div>
      <CloudRouter customerId={current.id} />
      <Card
        title={`Circuits for ${current.name}`}
        note={
          <div className="form-actions">
            <button className="button small" aria-pressed={form === "cloud"} onClick={() => setForm(form === "cloud" ? null : "cloud")}>
              Connect a cloud
            </button>
            <button
              className="button secondary small"
              aria-pressed={form === "site"}
              onClick={() => setForm(form === "site" ? null : "site")}
            >
              Join two sites
            </button>
          </div>
        }
      >
        <ErrorNote error={list.error} />
        {form === "cloud" && (
          <CloudForm
            customerId={current.id}
            providers={providers.data ?? {}}
            providersError={providers.error}
            classes={classes.data ?? []}
            sites={current.sites}
            onDone={done}
            onCancel={() => setForm(null)}
          />
        )}
        {form === "site" && <SiteForm customerId={current.id} sites={current.sites} onDone={done} onCancel={() => setForm(null)} />}
        {list.data && circuits.length === 0 ? (
          <p className="muted">No circuits yet. Connect a cloud, or join two of your sites at layer 2.</p>
        ) : (
          <div className="table-wrap">
            <table className="paths">
              <thead>
                <tr>
                  <th scope="col">Circuit</th>
                  <th scope="col">Ends</th>
                  <th scope="col" className="num">Bandwidth</th>
                  <th scope="col">Status</th>
                  <th scope="col">Health</th>
                  <th scope="col" className="num">This month</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {circuits.map((c) => (
                  <Fragment key={c.id}>
                    <CircuitRow
                      c={c}
                      providers={providers.data ?? {}}
                      open={open === c.id}
                      onToggle={() => setOpen(open === c.id ? null : c.id)}
                    />
                    {open === c.id && (
                      <tr>
                        <td colSpan={7}>
                          <CircuitDetail customerId={current.id} c={c} reload={list.reload} onDeleted={() => setOpen(null)} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}

// ---- Cloud router (cloud_to_cloud) ----

function CloudRouter({ customerId }: { customerId: string }) {
  const settings = useApi<CustomerSettings>(circuitPaths.settings(customerId), 0);
  const { reload } = useCustomer();
  const act = useAction();
  const on = !!settings.data?.cloud_to_cloud;
  const flip = (next: boolean) =>
    act.run(async () => {
      await setCloudToCloud(customerId, next);
      settings.reload();
      reload();
    });
  return (
    <div className="card-inset" style={{ marginBottom: 24 }}>
      <label className="check">
        <input type="checkbox" checked={on} disabled={act.busy || !settings.data} onChange={(e) => flip(e.target.checked)} />{" "}
        <strong>Cloud router</strong>
      </label>
      <p className="small muted" style={{ margin: "4px 0 0" }}>
        When on, your clouds can reach each other through ExaCarib's PoP, without going back to an office.
      </p>
      <ErrorNote error={settings.error ?? act.error} />
    </div>
  );
}

// ---- One circuit in the list ----

function ends(c: Circuit, providers: CloudProviders) {
  if (c.kind === "site") {
    return (
      <>
        {c.a_site ?? "–"} <span className="muted small">VLAN {c.a_vlan ?? "–"}</span> to {c.b_site ?? "–"}{" "}
        <span className="muted small">VLAN {c.b_vlan ?? "–"}</span>
      </>
    );
  }
  const provider = (c.provider && providers[c.provider]?.name) || c.provider || "Cloud";
  return (
    <>
      {c.a_site ?? "All sites"} to {provider}
      {c.region ? `, ${c.region}` : ""}
      <div className="small muted">
        Gateway <span className="mono">{c.peer_address ?? "–"}</span>
        {c.peer_asn ? (
          <>
            , ASN <span className="mono">{c.peer_asn}</span>
          </>
        ) : null}
      </div>
    </>
  );
}

function CircuitRow({
  c,
  providers,
  open,
  onToggle,
}: {
  c: Circuit;
  providers: CloudProviders;
  open: boolean;
  onToggle: () => void;
}) {
  const st = STATUS[c.status] ?? STATUS.provisioning;
  return (
    <tr className={open ? "selected" : undefined}>
      <td>
        <strong>{c.name}</strong>
        <div className="small muted">{c.kind === "cloud" ? "To a cloud" : "Between sites"}</div>
      </td>
      <td className="small">{ends(c, providers)}</td>
      <td className="num mono">{mbps(c.bandwidth_mbps)}</td>
      <td>
        <span className={`pill ${st.cls}`}>{st.word}</span>
      </td>
      <td className="small">
        {c.kind === "cloud" ? (
          <>
            <div>
              BGP <span className="mono">{c.bgp || "–"}</span>
            </div>
            <div className="muted">
              <span className="mono">{c.prefixes_received ?? 0}</span> routes received
            </div>
          </>
        ) : (
          <>
            <div>
              Round trip <span className="mono">{fmt(c.rtt_ms, " ms")}</span>
            </div>
            <div className="muted">
              Loss <span className="mono">{fmt(c.loss_pct, "%")}</span>
            </div>
          </>
        )}
      </td>
      <td className="money">{usd(c.month_to_date)}</td>
      <td>
        <button className="button secondary small" aria-expanded={open} onClick={onToggle}>
          {open ? "Close" : "Manage"}
        </button>
      </td>
    </tr>
  );
}

// ---- Expanded circuit: bandwidth, on/off, key, delete, charges and charts ----

function CircuitDetail({
  customerId,
  c,
  reload,
  onDeleted,
}: {
  customerId: string;
  c: Circuit;
  reload: () => void;
  onDeleted: () => void;
}) {
  const [bw, setBw] = useState(String(c.bandwidth_mbps));
  const [psk, setPsk] = useState("");
  const [saved, setSaved] = useState<string | null>(null);
  const act = useAction();
  const change = (body: Parameters<typeof updateCircuit>[2], note: string) =>
    act.run(async () => {
      setSaved(null);
      await updateCircuit(customerId, c.id, body);
      setSaved(note);
      reload();
    });
  const saveBw = (e: FormEvent) => {
    e.preventDefault();
    change({ bandwidth_mbps: Number(bw) }, `Bandwidth set to ${Number(bw).toLocaleString("en-GB")} Mbps.`);
  };
  const rotate = (e: FormEvent) => {
    e.preventDefault();
    const key = psk;
    setPsk("");
    change({ psk: key }, "New key saved. The tunnel reconnects with it within 10 seconds.");
  };
  const toggle = () => {
    if (c.enabled) {
      const q = `Switch off ${c.name}? Traffic on it stops within 10 seconds. The bandwidth stays reserved and billed until you delete the circuit.`;
      if (!window.confirm(q)) return;
    }
    change({ enabled: !c.enabled }, c.enabled ? "Switched off." : "Switched on. It comes up within 10 seconds.");
  };
  const remove = () => {
    const q =
      c.kind === "cloud"
        ? `Delete ${c.name}? The tunnel to the cloud comes down within 10 seconds and billing stops. This can't be undone.`
        : `Delete ${c.name}? The VLAN stops being carried between ${c.a_site} and ${c.b_site} within 10 seconds and billing stops. This can't be undone.`;
    if (!window.confirm(q)) return;
    act.run(async () => {
      await deleteCircuit(customerId, c.id);
      onDeleted();
      reload();
    });
  };
  return (
    <div className="card-inset">
      <div className="grid">
        <div className="span-6">
          <form className="form" onSubmit={saveBw}>
            <label>
              Bandwidth (Mbps)
              <input type="number" min={1} max={1000} step={1} required value={bw} onChange={(e) => setBw(e.target.value)} />
            </label>
            <div className="actions">
              <button className="button small" disabled={act.busy || Number(bw) === c.bandwidth_mbps}>
                Save
              </button>
            </div>
            <p className="small muted wide" style={{ margin: 0 }}>
              Billed by the hour from now, at {usd(c.price_per_mbps_month)} per Mbps a month. Sites get the change within 10
              seconds.
            </p>
          </form>
          {c.kind === "cloud" && (
            <form className="form" onSubmit={rotate} style={{ marginTop: 16 }}>
              <label>
                New pre-shared key
                <input
                  type="password"
                  autoComplete="new-password"
                  required
                  pattern={PSK_PATTERN}
                  title={PSK_HINT}
                  value={psk}
                  onChange={(e) => setPsk(e.target.value)}
                />
              </label>
              <div className="actions">
                <button className="button secondary small" disabled={act.busy}>
                  Rotate key
                </button>
              </div>
              <p className="small muted wide" style={{ margin: 0 }}>
                {c.has_psk ? "A key is set. " : "No key is set. "}Change it in the cloud console first, then paste the same
                key here. It is never shown again.
              </p>
            </form>
          )}
          <div className="form-actions" style={{ marginTop: 16 }}>
            <button className={c.enabled ? "button secondary small" : "button small"} disabled={act.busy} onClick={toggle}>
              {c.enabled ? "Switch off" : "Switch on"}
            </button>
            <button className="button danger small" disabled={act.busy} onClick={remove}>
              Delete
            </button>
          </div>
          {saved && (
            <p className="ok-note small" role="status">
              {saved}
            </p>
          )}
          <ErrorNote error={act.error} />
          <Facts c={c} />
        </div>
        <div className="span-6">
          <Charges customerId={customerId} c={c} />
        </div>
        <div className="span-12">
          <Charts customerId={customerId} c={c} />
        </div>
      </div>
    </div>
  );
}

function Facts({ c }: { c: Circuit }) {
  const rows: [string, ReactNode][] =
    c.kind === "cloud"
      ? [
          ["Tunnel", c.ike || "–"],
          ["Inside addresses", c.inside_cidr ? `${c.our_inside ?? "–"} (ours), ${c.cloud_inside ?? "–"} (cloud)` : "–"],
          ["Cloud subnets", (c.cloud_prefixes ?? []).length ? c.cloud_prefixes.join(", ") : "Any the cloud announces"],
          ["The cloud may reach", (c.a_prefixes ?? []).length ? c.a_prefixes.join(", ") : c.a_site ?? "All your sites"],
          ["Routes received", (c.routes ?? []).length ? (c.routes ?? []).join(", ") : "None yet"],
          ["Class", c.class_name ?? "By your traffic rules"],
        ]
      : [["VLANs", `${c.a_vlan ?? "–"} at ${c.a_site ?? "A"}, ${c.b_vlan ?? "–"} at ${c.b_site ?? "B"}`]];
  return (
    <dl className="small" style={{ marginTop: 16 }}>
      {rows.map(([k, v]) => (
        <div key={k}>
          <dt className="muted" style={{ display: "inline" }}>
            {k}:{" "}
          </dt>
          <dd className="mono" style={{ display: "inline", margin: 0, whiteSpace: "normal" }}>
            {v}
          </dd>
        </div>
      ))}
      <div>
        <dt className="muted" style={{ display: "inline" }}>
          Traffic now:{" "}
        </dt>
        <dd className="mono" style={{ display: "inline", margin: 0 }}>
          {fmt(c.mbps_in, " Mbps")} in, {fmt(c.mbps_out, " Mbps")} out
        </dd>
      </div>
      <div className="muted">
        Added by {who(c.created_by)} on {new Date(c.created_at).toLocaleDateString("en-GB")}
      </div>
    </dl>
  );
}

function Charges({ customerId, c }: { customerId: string; c: Circuit }) {
  const month = thisMonth();
  const { data, error } = useApi<CircuitCharges>(circuitPaths.charges(customerId, c.id, month), 60_000);
  const when = (iso: string) =>
    new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  return (
    <>
      <h3 style={{ marginTop: 0 }}>Charges this month</h3>
      <ErrorNote error={error} />
      {data && (
        <table className="paths small">
          <thead>
            <tr>
              <th scope="col" className="num">Speed</th>
              <th scope="col">From</th>
              <th scope="col">To</th>
              <th scope="col" className="num">Hours</th>
              <th scope="col" className="num">Amount</th>
            </tr>
          </thead>
          <tbody>
            {data.segments.map((s) => (
              <tr key={s.from}>
                <td className="num mono">{mbps(s.mbps)}</td>
                <td className="mono">{when(s.from)}</td>
                <td className="mono">{when(s.to)}</td>
                <td className="num mono">{fmt(s.hours, "", 2)}</td>
                <td className="money">{usd(s.amount)}</td>
              </tr>
            ))}
            <tr>
              <td colSpan={4}>
                <strong>Total so far</strong>
              </td>
              <td className="money">
                <strong>{usd(data.total)}</strong>
              </td>
            </tr>
          </tbody>
        </table>
      )}
      {data && (
        <p className="small muted">
          Each speed is charged for the hours it was set, at {usd(data.price_per_mbps_month)} per Mbps a month (730 hours).
        </p>
      )}
    </>
  );
}

function Charts({ customerId, c }: { customerId: string; c: Circuit }) {
  const { data, error } = useApi<CircuitMetric[]>(circuitPaths.metrics(customerId, c.id, 60), 60_000);
  const to = Date.now();
  const from = to - 60 * 60_000;
  const pts = (data ?? []).map((p) => ({ ...p, t: new Date(p.time).getTime() }));
  const traffic = [
    { key: "in", label: "In", points: pts.map((p) => ({ t: p.t, v: num(p.mbps_in) })) },
    { key: "out", label: "Out", points: pts.map((p) => ({ t: p.t, v: num(p.mbps_out) })) },
  ];
  const loss = (p: CircuitMetric) => {
    const sent = num(p.sent);
    const received = num(p.received);
    return sent && received !== null ? Math.max(0, (100 * (sent - received)) / sent) : null;
  };
  return (
    <>
      <h3>Last hour</h3>
      <ErrorNote error={error} />
      <div className="charts">
        <LineChart title="Traffic" unit=" Mbps" series={traffic} from={from} to={to} refs={[{ value: c.bandwidth_mbps, label: "Bandwidth", kind: "commit" }]} />
        {c.kind === "site" && (
          <>
            <LineChart
              title="Round trip"
              unit=" ms"
              series={[{ key: "in", label: "Round trip", points: pts.map((p) => ({ t: p.t, v: num(p.rtt_ms) })) }]}
              from={from}
              to={to}
            />
            <LineChart
              title="Loss"
              unit="%"
              series={[{ key: "out", label: "Loss", points: pts.map((p) => ({ t: p.t, v: loss(p) })) }]}
              from={from}
              to={to}
            />
          </>
        )}
      </div>
    </>
  );
}

// ---- Connect a cloud ----

function CloudForm({
  customerId,
  providers,
  providersError,
  classes,
  sites,
  onDone,
  onCancel,
}: {
  customerId: string;
  providers: CloudProviders;
  providersError: string | null;
  classes: { name: string }[];
  sites: { id: string; name: string }[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const [f, setF] = useState({
    provider: "",
    name: "",
    region: "",
    peer_address: "",
    peer_asn: "",
    psk: "",
    inside_cidr: "",
    cloud_prefixes: "",
    a_site_id: "",
    a_prefixes: "",
    class_name: "",
    bandwidth_mbps: "50",
  });
  const act = useAction();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const pick = (provider: string) => {
    const p = providers[provider];
    setF({ ...f, provider, peer_asn: p ? String(p.asn) : f.peer_asn });
  };
  const chosen = providers[f.provider];
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await createCircuit(customerId, {
        kind: "cloud",
        name: f.name.trim(),
        provider: f.provider,
        region: f.region.trim(),
        peer_address: f.peer_address.trim(),
        peer_asn: Number(f.peer_asn),
        psk: f.psk,
        inside_cidr: f.inside_cidr.trim() || undefined,
        cloud_prefixes: split(f.cloud_prefixes),
        a_site_id: f.a_site_id || undefined,
        a_prefixes: split(f.a_prefixes),
        class_name: f.class_name || undefined,
        bandwidth_mbps: Number(f.bandwidth_mbps),
      });
      setF((old) => ({ ...old, psk: "" }));
      onDone();
    });
  };
  return (
    <form className="form card-inset" onSubmit={submit} style={{ marginBottom: 16 }}>
      <h3 className="wide">Connect a cloud</h3>
      <p className="small muted wide" style={{ margin: 0 }}>
        Create a site-to-site VPN in your cloud console first, pointing at the PoP address we give you, then copy its
        details here. The PoP runs BGP with your gateway over the tunnel.
      </p>
      <ErrorNote error={providersError} />
      <label>
        Provider
        <select value={f.provider} onChange={(e) => pick(e.target.value)} required>
          <option value="" disabled>
            Choose…
          </option>
          {Object.entries(providers).map(([k, p]) => (
            <option key={k} value={k}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        Name
        <input value={f.name} onChange={set("name")} required maxLength={80} placeholder="Production VPC" />
      </label>
      <label>
        Region
        <input value={f.region} onChange={set("region")} placeholder="us-east-1" />
      </label>
      {chosen && (
        <p className="small muted wide" style={{ margin: 0 }}>
          Find these in: <strong>{chosen.where}</strong>
        </p>
      )}
      <label>
        Gateway public IP
        <input value={f.peer_address} onChange={set("peer_address")} required inputMode="decimal" placeholder="52.1.2.3" />
      </label>
      <label>
        Gateway ASN
        <input value={f.peer_asn} onChange={set("peer_asn")} required inputMode="numeric" pattern="[0-9]+" placeholder="64512" />
      </label>
      <label>
        Pre-shared key
        <input
          type="password"
          autoComplete="new-password"
          value={f.psk}
          onChange={set("psk")}
          required
          pattern={PSK_PATTERN}
          title={PSK_HINT}
        />
      </label>
      <label>
        Inside addresses /30
        <input value={f.inside_cidr} onChange={set("inside_cidr")} placeholder="Leave blank to pick one" />
      </label>
      <label className="wide">
        Cloud subnets (comma separated)
        <input value={f.cloud_prefixes} onChange={set("cloud_prefixes")} placeholder="10.100.0.0/16, 10.101.0.0/16" />
      </label>
      <label>
        The cloud may reach
        <select value={f.a_site_id} onChange={set("a_site_id")}>
          <option value="">All your sites</option>
          {sites.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        Only these site subnets
        <input value={f.a_prefixes} onChange={set("a_prefixes")} placeholder="Optional, e.g. 192.168.10.0/24" />
      </label>
      <label>
        Class for traffic to this cloud
        <select value={f.class_name} onChange={set("class_name")}>
          <option value="">By your traffic rules</option>
          {classes.map((c) => (
            <option key={c.name} value={c.name}>
              {c.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        Bandwidth (Mbps)
        <input type="number" min={1} max={1000} step={1} required value={f.bandwidth_mbps} onChange={set("bandwidth_mbps")} />
      </label>
      <div className="actions wide">
        <button className="button" disabled={act.busy}>
          Connect
        </button>
        <button type="button" className="button secondary" onClick={onCancel}>
          Cancel
        </button>
        <span className="small muted">1 to 1,000 Mbps, billed by the hour.</span>
      </div>
      <div className="wide">
        <ErrorNote error={act.error} />
      </div>
    </form>
  );
}

// ---- Join two sites ----

function SiteForm({
  customerId,
  sites,
  onDone,
  onCancel,
}: {
  customerId: string;
  sites: { id: string; name: string }[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const [f, setF] = useState({
    name: "",
    a_site_id: sites[0]?.id ?? "",
    b_site_id: sites[1]?.id ?? "",
    a_vlan: "",
    b_vlan: "",
    bandwidth_mbps: "20",
  });
  const act = useAction();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await createCircuit(customerId, {
        kind: "site",
        name: f.name.trim(),
        a_site_id: f.a_site_id,
        b_site_id: f.b_site_id,
        a_vlan: Number(f.a_vlan),
        b_vlan: f.b_vlan ? Number(f.b_vlan) : Number(f.a_vlan),
        bandwidth_mbps: Number(f.bandwidth_mbps),
      });
      onDone();
    });
  };
  if (sites.length < 2) {
    return (
      <div className="card-inset" style={{ marginBottom: 16 }}>
        <p className="muted" style={{ margin: 0 }}>
          You need two sites to join them.{" "}
          <button className="link" onClick={onCancel}>
            Close
          </button>
        </p>
      </div>
    );
  }
  return (
    <form className="form card-inset" onSubmit={submit} style={{ marginBottom: 16 }}>
      <h3 className="wide">Join two sites</h3>
      <p className="small muted wide" style={{ margin: 0 }}>
        Carries a VLAN between two sites at layer 2, through the PoP, as if they shared a switch.
      </p>
      <label>
        Name
        <input value={f.name} onChange={set("name")} required maxLength={80} placeholder="Branch voice VLAN" />
      </label>
      <label>
        A site
        <select value={f.a_site_id} onChange={set("a_site_id")} required>
          {sites.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        B site
        <select value={f.b_site_id} onChange={set("b_site_id")} required>
          {sites.map((s) => (
            <option key={s.id} value={s.id} disabled={s.id === f.a_site_id}>
              {s.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        VLAN at A
        <input type="number" min={1} max={4094} required value={f.a_vlan} onChange={set("a_vlan")} placeholder="100" />
      </label>
      <label>
        VLAN at B
        <input type="number" min={1} max={4094} value={f.b_vlan} onChange={set("b_vlan")} placeholder={f.a_vlan ? `${f.a_vlan}, as at A` : "Same as A"} />
      </label>
      <label>
        Bandwidth (Mbps)
        <input type="number" min={1} max={1000} step={1} required value={f.bandwidth_mbps} onChange={set("bandwidth_mbps")} />
      </label>
      <div className="actions wide">
        <button className="button" disabled={act.busy || f.a_site_id === f.b_site_id}>
          Join
        </button>
        <button type="button" className="button secondary" onClick={onCancel}>
          Cancel
        </button>
        <span className="small muted">1 to 1,000 Mbps, billed by the hour.</span>
      </div>
      <div className="wide">
        <ErrorNote error={act.error} />
      </div>
    </form>
  );
}
