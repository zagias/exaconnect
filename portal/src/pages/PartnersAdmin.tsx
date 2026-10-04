import { useState, type FormEvent } from "react";
import {
  PSK_HINT,
  PSK_PATTERN,
  circuitPaths,
  completeOrder,
  createPartner,
  deletePartner,
  orderPaths,
  updatePartner,
  useApi,
  type CloudProviders,
  type Order,
  type Partner,
  type PartnerCategory,
  type PartnerIn,
  type PartnerKind,
} from "../api";
import { ErrorNote, ExampleTag } from "../components";
import { who } from "../customer";
import { Card, RowActions, useAction } from "../ui";
import { CATEGORY_LABEL, OrderStatusPill, partnerOf, safeUrl, split, usd, when } from "./Order";

// Admin: the partner directory and service partner orders waiting for ExaCarib
// to enter the partner's gateway details (docs/ordering-contract.md).

const KIND_LABEL: Record<PartnerKind, string> = {
  cloud: "Cloud: the customer sets up the VPN",
  service: "Service: ExaCarib completes the order",
};

export function PartnersAdmin() {
  const list = useApi<Partner[]>(orderPaths.adminPartners, 0);
  const providers = useApi<CloudProviders>(circuitPaths.providers, 0);
  const [editing, setEditing] = useState<Partner | "new" | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const act = useAction();
  const partners = list.data ?? [];

  const unlist = (p: Partner) => {
    if (!window.confirm(`Take ${p.name} out of the directory? If any orders refer to it, it is unlisted rather than removed.`)) return;
    act.run(async () => {
      setNote(null);
      await deletePartner(p.id);
      if (editing !== "new" && editing?.id === p.id) setEditing(null);
      setNote(`${p.name} is no longer in the directory.`);
      list.reload();
    });
  };
  const relist = (p: Partner) =>
    act.run(async () => {
      setNote(null);
      await updatePartner(p.id, { listed: true });
      setNote(`${p.name} is back in the directory.`);
      list.reload();
    });

  return (
    <>
      <Card
        title="Partners"
        note={
          <button className="button small" aria-pressed={editing === "new"} onClick={() => setEditing(editing === "new" ? null : "new")}>
            Add a partner
          </button>
        }
      >
        <p className="small muted" style={{ marginTop: 0 }}>
          Listed partners appear in every customer's directory on the Order screen.
        </p>
        <ErrorNote error={list.error ?? act.error} />
        {note && (
          <p className="ok-note small" role="status">
            {note}
          </p>
        )}
        {editing && (
          <PartnerForm
            key={editing === "new" ? "new" : editing.id}
            partner={editing === "new" ? null : editing}
            providers={providers.data ?? {}}
            onDone={(msg) => {
              setEditing(null);
              setNote(msg);
              list.reload();
            }}
            onCancel={() => setEditing(null)}
          />
        )}
        {list.data && partners.length === 0 ? (
          <p className="muted">No partners yet.</p>
        ) : (
          <div className="table-wrap">
            <table className="paths dt stack">
              <thead>
                <tr>
                  <th scope="col">Partner</th>
                  <th scope="col">Category</th>
                  <th scope="col">Kind</th>
                  <th scope="col" className="num">Per Mbps a month</th>
                  <th scope="col">Directory</th>
                  <th scope="col" className="actions">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {partners.map((p) => (
                  <tr key={p.id} className={p.listed ? undefined : "row-off"}>
                    <td style={{ minWidth: 200 }}>
                      <strong>{p.name}</strong> {p.example && <ExampleTag />}
                      <span className="sub mono">{p.slug}</span>
                      {safeUrl(p.website) && (
                        <a className="small" href={safeUrl(p.website)!} target="_blank" rel="noopener noreferrer">
                          Website<span className="sr-only"> of {p.name} (opens in a new tab)</span>
                        </a>
                      )}
                    </td>
                    <td data-label="Category" className="small">
                      {CATEGORY_LABEL[p.category] ?? p.category}
                    </td>
                    <td data-label="Kind" className="small">
                      {KIND_LABEL[p.kind] ?? p.kind}
                      {p.kind === "cloud" && p.provider && (
                        <div className="muted">{providers.data?.[p.provider]?.name ?? p.provider}</div>
                      )}
                      {p.kind === "service" && p.prefixes.length > 0 && (
                        <div className="mono muted wrap">{p.prefixes.join(", ")}</div>
                      )}
                    </td>
                    <td data-label="Per Mbps" className="num">
                      {usd(p.price_per_mbps_month)}
                    </td>
                    <td data-label="Directory">
                      <span className={`pill ${p.listed ? "ok" : "off"}`}>{p.listed ? "Listed" : "Unlisted"}</span>
                    </td>
                    <td className="actions">
                      <RowActions
                        label={p.name}
                        disabled={act.busy}
                        primary={
                          <button className="button secondary small" aria-label={`Edit ${p.name}`} onClick={() => setEditing(p)}>
                            Edit
                          </button>
                        }
                        items={
                          p.listed
                            ? [{ label: "Unlist", danger: true, onSelect: () => unlist(p) }]
                            : [{ label: "List again", onSelect: () => relist(p) }]
                        }
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <PendingOrders partners={partners} />
    </>
  );
}

function PartnerForm({
  partner,
  providers,
  onDone,
  onCancel,
}: {
  partner: Partner | null;
  providers: CloudProviders;
  onDone: (msg: string) => void;
  onCancel: () => void;
}) {
  const [f, setF] = useState({
    slug: partner?.slug ?? "",
    name: partner?.name ?? "",
    category: (partner?.category ?? "saas") as PartnerCategory,
    kind: (partner?.kind ?? "service") as PartnerKind,
    provider: partner?.provider ?? "",
    description: partner?.description ?? "",
    website: partner?.website ?? "",
    regions: (partner?.regions ?? []).join(", "),
    prefixes: (partner?.prefixes ?? []).join(", "),
    price: partner ? String(Number(partner.price_per_mbps_month)) : "2",
    listed: partner?.listed ?? true,
  });
  const act = useAction();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const cloud = f.kind === "cloud";
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      // Slug and kind are set when a partner is added; they can't be changed afterwards.
      const body: PartnerIn = {
        name: f.name.trim(),
        category: f.category,
        provider: cloud ? f.provider || null : null,
        description: f.description.trim(),
        website: f.website.trim() || null,
        regions: split(f.regions),
        prefixes: cloud ? [] : split(f.prefixes),
        price_per_mbps_month: Number(f.price),
        listed: f.listed,
      };
      if (partner) await updatePartner(partner.id, body);
      else await createPartner({ ...body, slug: f.slug.trim().toLowerCase(), kind: f.kind });
      onDone(partner ? `Saved ${body.name}.` : `Added ${body.name}${body.listed ? " to the directory" : ""}.`);
    });
  };
  return (
    <form className="form card-inset" onSubmit={submit} style={{ marginBottom: 16 }}>
      <h3 className="wide">{partner ? `Edit ${partner.name}` : "New partner"}</h3>
      <label>
        Name
        <input value={f.name} onChange={set("name")} required minLength={2} maxLength={80} placeholder="Example Pay" />
      </label>
      <label>
        Short name (slug)
        <input
          value={f.slug}
          onChange={set("slug")}
          required
          disabled={!!partner}
          maxLength={40}
          pattern="[a-z0-9][a-z0-9\-]{1,39}"
          title="2 to 40 lower-case letters, digits and hyphens. It can't be changed later."
          placeholder="example-pay"
        />
      </label>
      <label>
        Category
        <select value={f.category} onChange={(e) => setF({ ...f, category: e.target.value as PartnerCategory })}>
          {(Object.keys(CATEGORY_LABEL) as PartnerCategory[]).map((c) => (
            <option key={c} value={c}>
              {CATEGORY_LABEL[c]}
            </option>
          ))}
        </select>
      </label>
      <label>
        Kind
        <select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value as PartnerKind })} disabled={!!partner}>
          <option value="service">{KIND_LABEL.service}</option>
          <option value="cloud">{KIND_LABEL.cloud}</option>
        </select>
      </label>
      {cloud ? (
        <label>
          Cloud provider
          <select value={f.provider} onChange={set("provider")} required>
            <option value="" disabled>
              Choose…
            </option>
            {Object.entries(providers)
              .filter(([k]) => k !== "other")
              .map(([k, p]) => (
                <option key={k} value={k}>
                  {p.name}
                </option>
              ))}
          </select>
        </label>
      ) : (
        <label>
          Prefixes it advertises
          <input value={f.prefixes} onChange={set("prefixes")} placeholder="203.0.113.0/24" />
        </label>
      )}
      <label>
        Price per Mbps a month (US$)
        <input type="number" min={0} step="0.01" required value={f.price} onChange={set("price")} />
      </label>
      <label className="wide">
        Description
        <textarea value={f.description} onChange={set("description")} rows={2} maxLength={400} />
      </label>
      <label>
        Website
        <input type="url" value={f.website} onChange={set("website")} maxLength={200} placeholder="https://example.com" />
      </label>
      <label>
        Regions (comma separated)
        <input value={f.regions} onChange={set("regions")} placeholder={cloud ? "us-east-1, us-west-2" : "Caribbean"} />
      </label>
      <label className="check">
        <input type="checkbox" checked={f.listed} onChange={(e) => setF({ ...f, listed: e.target.checked })} /> In the directory
      </label>
      {partner?.example && (
        <p className="small muted" style={{ margin: 0 }}>
          <ExampleTag /> A seeded example entry; customers see it labelled.
        </p>
      )}
      <div className="actions wide">
        <button className="button" disabled={act.busy}>
          {partner ? "Save partner" : "Add partner"}
        </button>
        <button type="button" className="button secondary" onClick={onCancel}>
          Cancel
        </button>
      </div>
      <div className="wide">
        <ErrorNote error={act.error} />
      </div>
    </form>
  );
}

// ---- Orders waiting for a partner ----

function PendingOrders({ partners }: { partners: Partner[] }) {
  const orders = useApi<Order[]>(orderPaths.adminOrders("pending_partner"), 10_000);
  const [open, setOpen] = useState<number | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const list = orders.data ?? [];
  return (
    <Card title="Orders waiting for a partner">
      <p className="small muted" style={{ marginTop: 0 }}>
        Once the partner has set up their side, enter their gateway details to create the circuit. The order is then done.
      </p>
      <ErrorNote error={orders.error} />
      {note && (
        <p className="ok-note small" role="status">
          {note}
        </p>
      )}
      {orders.data && list.length === 0 ? (
        <p className="muted">Nothing waiting.</p>
      ) : (
        <div style={{ display: "grid", gap: 12 }}>
          {list.map((o) => {
            const slug = partnerOf(o);
            const partner = partners.find((p) => p.slug === slug) ?? null;
            return (
              <div key={o.id} className="card-inset">
                <div className="draft-head">
                  <strong>Order {o.id}</strong>
                  <OrderStatusPill status={o.status} />
                  <span className="small muted">
                    {o.customer ?? o.customer_id ?? ""}
                    {o.customer || o.customer_id ? " · " : ""}
                    {partner?.name ?? slug ?? "Partner"}
                  </span>
                </div>
                {o.summary.length > 0 && <div className="small">{o.summary.join(" ")}</div>}
                <div className="small muted">
                  Ordered by {who(o.created_by)}, {when(o.created_at)}
                  {o.confirmed_by && `; confirmed by ${who(o.confirmed_by)}, ${when(o.confirmed_at)}`}
                </div>
                {open === o.id ? (
                  <CompleteForm
                    order={o}
                    partner={partner}
                    onDone={() => {
                      setOpen(null);
                      setNote(`Order ${o.id} is done. The circuit comes up at the PoP within a minute.`);
                      orders.reload();
                    }}
                    onCancel={() => setOpen(null)}
                  />
                ) : (
                  <div className="actions" style={{ marginTop: 8 }}>
                    <button
                      className="button small"
                      onClick={() => {
                        setNote(null);
                        setOpen(o.id);
                      }}
                    >
                      Complete
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

function CompleteForm({
  order,
  partner,
  onDone,
  onCancel,
}: {
  order: Order;
  partner: Partner | null;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [f, setF] = useState({
    peer_address: "",
    peer_asn: "",
    psk: "",
    inside_cidr: "",
    prefixes: (partner?.prefixes ?? []).join(", "),
  });
  const act = useAction();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await completeOrder(order.id, {
        peer_address: f.peer_address.trim(),
        peer_asn: Number(f.peer_asn),
        psk: f.psk,
        inside_cidr: f.inside_cidr.trim() || null,
        prefixes: split(f.prefixes),
      });
      setF((old) => ({ ...old, psk: "" }));
      onDone();
    });
  };
  return (
    <form className="form" onSubmit={submit} style={{ marginTop: 8 }}>
      <label>
        Partner gateway address
        <input value={f.peer_address} onChange={set("peer_address")} required inputMode="decimal" placeholder="198.51.100.10" />
      </label>
      <label>
        Partner ASN
        <input value={f.peer_asn} onChange={set("peer_asn")} required inputMode="numeric" pattern="[0-9]+" placeholder="64700" />
      </label>
      <label>
        Pre-shared key
        <input
          type="password"
          autoComplete="off"
          value={f.psk}
          onChange={set("psk")}
          required
          pattern={PSK_PATTERN}
          title={PSK_HINT}
        />
      </label>
      <label>
        Inside addresses /30 <span className="muted">(optional)</span>
        <input value={f.inside_cidr} onChange={set("inside_cidr")} placeholder="Leave blank to pick one" />
      </label>
      <label className="wide">
        Partner prefixes (comma separated)
        <input value={f.prefixes} onChange={set("prefixes")} required placeholder="203.0.113.0/24" />
      </label>
      <div className="actions wide">
        <button className="button" disabled={act.busy}>
          Complete order
        </button>
        <button type="button" className="button secondary" onClick={onCancel}>
          Cancel
        </button>
      </div>
      <div className="wide">
        <ErrorNote error={act.error} />
      </div>
    </form>
  );
}
