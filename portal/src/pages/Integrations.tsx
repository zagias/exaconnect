import { useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import {
  createIntegration,
  deleteIntegration,
  integrationPaths,
  retryDelivery,
  syncIntegration,
  testIntegration,
  updateIntegration,
  useApi,
  type Delivery,
  type Integration,
  type IntegrationCatalogue,
  type IntegrationIn,
  type ProviderInfo,
} from "../api";
import { useAuth } from "../auth";
import { ErrorNote, ago } from "../components";
import { useCustomer } from "../customer";
import { Card, PageHead, RowActions, useAction } from "../ui";
import { useConnectCapabilities } from "../ConnectCapabilities";
import { canOperateConnect } from "../permissions";

const CATEGORY: Record<string, string> = {
  webhook: "Webhooks and standards",
  alerting: "Alerting",
  itsm: "Service management",
  chat: "Chat",
  monitoring: "Monitoring",
  siem: "Security (SIEM)",
  logs: "Logs and telemetry",
  inventory: "Inventory",
  flows: "Flow export",
};

const STATUS_WORD: Record<Delivery["status"], string> = {
  pending: "Waiting to retry",
  delivered: "Delivered",
  simulated: "Simulated",
  failed: "Failed",
  skipped: "Not needed",
  digest: "In a digest",
};

/** "Simulated" or "Live", always in words. */
function ModeTag({ mode }: { mode: Integration["mode"] }) {
  return mode === "live" ? (
    <span className="tag">Live</span>
  ) : (
    <span className="tag" title="Nothing leaves Connect: requests are recorded so you can see what would be sent.">
      Simulated
    </span>
  );
}

/** Integrations (ADR 0026): connectors and webhooks, their delivery log, and how to scrape metrics. */
export default function Integrations() {
  const { user } = useAuth();
  const { current } = useCustomer();
  const admin = user?.role === "admin";
  const carrier = user?.role === "carrier";
  const capabilities = useConnectCapabilities();
  const canManage = canOperateConnect(user) || (carrier && (!user?.scopes || user.scopes.includes("connect")));
  const accessible = admin || capabilities.integrations;
  const cid = carrier ? null : current?.id ?? null;
  const list = useApi<Integration[]>(accessible ? carrier ? integrationPaths.list() : cid ? integrationPaths.list(cid) : null : null, 15_000);
  const catalogue = capabilities.catalogue;
  const [open, setOpen] = useState<number | null>(null);
  const [shownSecret, setShownSecret] = useState<{ name: string; secret: string } | null>(null);
  const [awaitingActivation, setAwaitingActivation] = useState(false);
  const act = useAction();

  const providers = useMemo(
    () => (catalogue?.providers ?? []).filter((p) => p.owners.includes(carrier ? "carrier" : "customer")),
    [catalogue, carrier],
  );
  const rows = (list.data ?? []).filter((integration) => admin || integration.mode === "live");

  const onTest = (i: Integration) =>
    act.run(async () => {
      const d = await testIntegration(i.id);
      setOpen(i.id);
      if (!admin && d.status === "simulated") throw new Error("Nothing was sent. This integration is not active yet. Contact ExaCarib for help.");
      if (d.status === "failed") throw new Error(`The test failed: ${d.last_error || "no reason given"}.`);
      list.reload();
    });
  const onSync = (i: Integration) =>
    act.run(async () => {
      const result = await syncIntegration(i.id);
      if (!admin && result.mode === "simulated") throw new Error("No live sync was run. Contact ExaCarib to finish activating this integration.");
      list.reload();
    });
  const onToggle = (i: Integration) =>
    act.run(async () => {
      await updateIntegration(i.id, { enabled: !i.enabled });
      list.reload();
    });
  const onDelete = (i: Integration) => {
    if (!window.confirm(`Delete "${i.name}"? Connect stops sending to it straight away.`)) return;
    act.run(async () => {
      await deleteIntegration(i.id);
      if (open === i.id) setOpen(null);
      list.reload();
    });
  };

  if (!admin && !capabilities.integrations)
    return (
      <>
        <PageHead title={capabilities.loading ? "Checking integrations…" : "Integrations are not available right now"}>
          {capabilities.loading ? "Checking which services are ready for your organisation." : "Contact ExaCarib to discuss connecting your monitoring and business tools."}
        </PageHead>
        <ErrorNote error={capabilities.catalogueError} />
        <Link className="button secondary" to="/">Back to your network</Link>
      </>
    );

  return (
    <>
      <PageHead eyebrow="Integrations" title="Integrations">
        Send network events to your monitoring, support and business tools. Review each connection’s status and delivery history here.
      </PageHead>
      {admin && catalogue && !catalogue.live && <p className="callout warn">Live delivery is off. Connections and tests below are for configuration only; no events are sent to providers.</p>}
      {!admin && awaitingActivation && <p className="callout warn" role="status">Your connection is saved. ExaCarib needs to finish activating it before it can send events.</p>}

      <Card title="Your integrations" note={admin && current ? <span className="small muted">For {current.name}</span> : undefined}>
        <ErrorNote error={list.error ?? capabilities.catalogueError} />
        {list.data && rows.length === 0 && (
          <div className="empty">
            <p>{canManage ? "No active integrations yet. Add a connection below, or contact ExaCarib to finish activating an existing one." : "No active integrations to show."}</p>
          </div>
        )}
        {rows.length > 0 && (
          <div className="table-wrap">
            <table className="paths dt stack">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Sends to</th>
                  <th scope="col">Events</th>
                  <th scope="col">Mode</th>
                  <th scope="col">Last delivery</th>
                  <th scope="col" className="actions">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((i) => (
                  <tr key={i.id}>
                    <td className="cell-wrap">
                      <strong>{i.name}</strong>
                      {!i.enabled && <span className="sub">Switched off</span>}
                      {i.platform && <span className="sub">From {i.platform === "n8n" ? "n8n" : i.platform[0].toUpperCase() + i.platform.slice(1)}</span>}
                    </td>
                    <td data-label="Sends to">{i.provider_name}</td>
                    <td data-label="Events" className="cell-wrap small">
                      {i.event_types.length === 0 ? <span className="muted">None (works another way)</span> : i.event_types.join(", ")}
                      {i.min_severity !== "info" && <span className="sub">{i.min_severity} and above</span>}
                    </td>
                    <td data-label="Mode">
                      <ModeTag mode={i.mode} />
                    </td>
                    <td data-label="Last delivery" className="cell-wrap small">
                      {i.last_delivery_at ? ago(i.last_delivery_at) : <span className="muted">Never</span>}
                      {i.last_status && <span className="sub">{i.last_status}</span>}
                    </td>
                    <td className="actions">
                      <RowActions
                        label={i.name}
                        disabled={act.busy}
                        primary={
                          canManage && i.event_types.length > 0 ? (
                            <button type="button" className="button secondary small" onClick={() => onTest(i)} disabled={act.busy}>
                              {i.mode === "live" ? "Send test" : "Preview test"}
                            </button>
                          ) : canManage && i.provider === "netbox" ? (
                            <button type="button" className="button secondary small" onClick={() => onSync(i)} disabled={act.busy}>
                              Sync now
                            </button>
                          ) : undefined
                        }
                        items={[
                          ...(i.event_types.length > 0
                            ? [{ label: open === i.id ? "Hide delivery log" : "Delivery log", onSelect: () => setOpen(open === i.id ? null : i.id) }]
                            : []),
                          ...(canManage ? [
                            { label: i.enabled ? "Switch off" : "Switch on", onSelect: () => onToggle(i) },
                            { label: "Delete", danger: true, onSelect: () => onDelete(i) },
                          ] : []),
                        ]}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <ErrorNote error={act.error} />
        {open !== null && <DeliveryLog integration={rows.find((r) => r.id === open)} canManage={canManage} onClose={() => setOpen(null)} />}
      </Card>

      {canManage && catalogue && (
        <AddIntegration
          providers={providers}
          catalogue={catalogue}
          customerId={admin ? cid : null}
          onAdded={(i) => {
            setAwaitingActivation(i.mode !== "live");
            if (i.signing_secret) setShownSecret({ name: i.name, secret: i.signing_secret });
            list.reload();
          }}
        />
      )}
      {shownSecret && (
        <div className="secret" role="status" style={{ marginBottom: 24 }}>
          <p className="callout warn small">
            <strong>⚠ Copy it now.</strong> The signing secret for "{shownSecret.name}" won't be shown again. Use it to check the
            webhook-signature header on each delivery.
          </p>
          <code>{shownSecret.secret}</code>{" "}
          <button type="button" className="button small" onClick={() => setShownSecret(null)}>
            Done
          </button>
        </div>
      )}

      {!carrier && <ScrapeAndHooks />}
    </>
  );
}

function DeliveryLog({ integration, canManage, onClose }: { integration?: Integration; canManage: boolean; onClose: () => void }) {
  const log = useApi<Delivery[]>(integration ? integrationPaths.deliveries(integration.id) : null, 10_000);
  const retry = useAction();
  if (!integration) return null;
  return (
    <div style={{ marginTop: 16 }}>
      <div className="card-head">
        <h3 style={{ margin: 0 }}>Delivery log: {integration.name}</h3>
        <button type="button" className="button secondary small" onClick={onClose}>
          Close
        </button>
      </div>
      <p className="muted small">The latest 50. Addresses and keys are never shown.</p>
      <ErrorNote error={log.error ?? retry.error} />
      {log.data && log.data.length === 0 && <p className="muted">{canManage ? "No deliveries yet. Use the test action to check this connection." : "No deliveries yet."}</p>}
      {log.data && log.data.length > 0 && (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Event</th>
                <th scope="col">Result</th>
                <th scope="col">Tries</th>
                <th scope="col" className="actions">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {log.data.map((d) => (
                <tr key={d.id}>
                  <td data-label="When" className="mono">
                    {ago(d.created_at)}
                  </td>
                  <td data-label="Event" className="mono">
                    {d.event_type}
                    {d.test && <span className="sub">Test</span>}
                  </td>
                  <td data-label="Result" className="cell-wrap">
                    {STATUS_WORD[d.status]}
                    {d.response_code ? <span className="mono"> ({d.response_code})</span> : null}
                    {(d.last_error || d.detail?.skipped) && <span className="sub">{d.last_error || d.detail.skipped}</span>}
                  </td>
                  <td data-label="Tries" className="mono">
                    {d.attempts}
                  </td>
                  <td className="actions">
                    {canManage && d.status === "failed" && (
                      <button
                        type="button"
                        className="button secondary small"
                        disabled={retry.busy}
                        onClick={() =>
                          retry.run(async () => {
                            await retryDelivery(integration.id, d.id);
                            log.reload();
                          })
                        }
                      >
                        Retry
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function AddIntegration({
  providers,
  catalogue,
  customerId,
  onAdded,
}: {
  providers: ProviderInfo[];
  catalogue: IntegrationCatalogue;
  customerId: string | null;
  onAdded: (i: Integration) => void;
}) {
  const [key, setKey] = useState(providers[0]?.key ?? "webhook");
  const [label, setLabel] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [events, setEvents] = useState("*");
  const [severity, setSeverity] = useState<IntegrationIn["min_severity"]>("info");
  const add = useAction();
  const p = providers.find((x) => x.key === key);
  const groups = useMemo(() => {
    const out: Record<string, ProviderInfo[]> = {};
    for (const x of providers) (out[x.category] ??= []).push(x);
    return out;
  }, [providers]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!p) return;
    add.run(async () => {
      const config: Record<string, unknown> = {};
      const secrets: Record<string, string> = {};
      for (const f of p.fields) {
        const v = (values[f.name] ?? "").trim();
        if (!v) continue;
        if (f.secret) secrets[f.name] = v;
        else if (f.kind === "int") config[f.name] = Number(v);
        else if (f.kind === "bool") config[f.name] = v === "true";
        else if (f.kind === "list") config[f.name] = v.split(",").map((x) => x.trim()).filter(Boolean);
        else config[f.name] = v;
      }
      const body: IntegrationIn = {
        provider: p.key,
        name: label.trim() || p.name,
        config,
        secrets,
        event_types: events.split(",").map((x) => x.trim()).filter(Boolean),
        min_severity: severity,
      };
      if (customerId) body.customer_id = customerId;
      onAdded(await createIntegration(body));
      setValues({});
      setLabel("");
    });
  };

  return (
    <Card title="Add an integration">
      <form className="form" onSubmit={submit}>
        <label>
          Send to
          <select
            value={key}
            onChange={(e) => {
              setKey(e.target.value);
              setValues({});
            }}
          >
            {Object.entries(groups).map(([c, ps]) => (
              <optgroup key={c} label={CATEGORY[c] ?? c}>
                {ps.map((x) => (
                  <option key={x.key} value={x.key}>
                    {x.name}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        <label>
          Name
          <input value={label} onChange={(e) => setLabel(e.target.value)} maxLength={80} placeholder={p?.name} />
        </label>
        {p?.fields.map((f) => (
          <label key={f.name} className={f.kind === "pem" ? "wide" : undefined}>
            {f.label}
            {f.required ? "" : " (optional)"}
            {f.kind === "choice" ? (
              <select value={values[f.name] ?? String(f.default ?? "")} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}>
                {f.choices?.map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </select>
            ) : f.kind === "bool" ? (
              <select value={values[f.name] ?? String(f.default ?? false)} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}>
                <option value="true">Yes</option>
                <option value="false">No</option>
              </select>
            ) : f.kind === "pem" ? (
              <textarea rows={4} required={f.required} value={values[f.name] ?? ""} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })} />
            ) : (
              <input
                type={f.secret ? "password" : f.kind === "int" ? "number" : "text"}
                autoComplete="off"
                required={f.required}
                value={values[f.name] ?? ""}
                placeholder={f.default !== undefined ? String(f.default) : f.help}
                onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
              />
            )}
          </label>
        ))}
        {p?.receives_events && (
          <>
            <label>
              Events
              <input value={events} onChange={(e) => setEvents(e.target.value)} placeholder="*, path.*, sla.breach" list="event-kinds" />
              <datalist id="event-kinds">
                {catalogue.events.map((k) => (
                  <option key={k.name} value={k.name}>
                    {k.title}
                  </option>
                ))}
              </datalist>
            </label>
            <label>
              Severity
              <select value={severity} onChange={(e) => setSeverity(e.target.value as IntegrationIn["min_severity"])}>
                <option value="info">Everything</option>
                <option value="warning">Warnings and critical</option>
                <option value="critical">Critical only</option>
              </select>
            </label>
          </>
        )}
        <div className="actions wide">
          <button className="button" disabled={add.busy || !p}>
            {add.busy ? "Adding…" : "Add integration"}
          </button>
        </div>
      </form>
      {p && (
        <p className="muted small">
          {p.api}. {p.live_needs}{" "}
          <a href={p.docs} target="_blank" rel="noreferrer">
            Provider documentation
          </a>
          . Secrets are stored encrypted and never shown again.
        </p>
      )}
      <ErrorNote error={add.error} />
    </Card>
  );
}

function ScrapeAndHooks() {
  const origin = typeof window !== "undefined" ? window.location.origin : "https://connect.exacarib.com";
  return (
    <Card title="Metrics, APIs and automation">
      <div className="snippets">
        <div>
          <h3>Prometheus</h3>
          <p className="muted small">
            Create an API key with the <span className="mono">metrics</span> scope under Account. It sees your organisation only.
          </p>
          <pre className="code">
            {`scrape_configs:
  - job_name: exacarib-connect
    scheme: https
    metrics_path: /api/v1/metrics
    authorization:
      credentials_file: /etc/prometheus/connect.key
    static_configs:
      - targets: ["${origin.replace(/^https?:\/\//, "")}"]`}
          </pre>
        </div>
        <div>
          <h3>Zapier, Make and n8n</h3>
          <p className="muted small">
            They subscribe with REST hooks: <span className="mono">POST /api/v1/hooks</span> with a target URL and the events,
            and <span className="mono">DELETE /api/v1/hooks/&#123;id&#125;</span> when a flow is switched off. Hooks they add are
            listed above.
          </p>
          <h3>Standards</h3>
          <p className="muted small">
            CloudEvents webhooks signed per Standard Webhooks, OTLP, syslog (RFC 5424), SNMP traps, IPFIX, RESTCONF on{" "}
            <span className="mono">/api/v1/restconf</span>, TM Forum TMF621, TMF622 and TMF688, and MEF LSO Sonata. The event
            catalogue is at <span className="mono">/api/v1/integrations/asyncapi.json</span>.
          </p>
        </div>
      </div>
    </Card>
  );
}
