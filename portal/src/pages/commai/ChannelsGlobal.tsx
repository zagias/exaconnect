import { useState } from "react";
import { Link } from "react-router-dom";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { Card, useAction } from "../../ui";
import { when } from "./lib";

/* Phase 3 channels (ADR 0029): Messenger, Instagram, Telegram, WhatsApp through
   Meta's Cloud API, and click-to-WhatsApp links. Shapes from
   controller/exaconnect_controller/commai/api/channels_global.py. */

type SocialName = "messenger" | "instagram" | "telegram";

interface SocialAccount {
  id: string;
  channel: SocialName;
  provider: string;
  provider_label: string;
  simulated: boolean;
  name: string;
  address: string;
  status: "setup" | "live" | "paused" | "broken";
  missing: string[];
  webhook_url: string;
  token_saved: boolean;
  token_saved_at: string | null;
  connected_at: string | null;
  last_inbound_at: string | null;
  last_sent_at: string | null;
  last_error: string;
  secret?: string;
}

interface SocialList {
  channels: Record<SocialName, { label: string; available: boolean; needs: string[] }>;
  accounts: SocialAccount[];
}

interface CloudAccount {
  id: string;
  address: string;
  status: string;
  provider_label: string;
  simulated: boolean;
  missing: string[];
  webhook_url: string;
  waba_id: string;
  phone_number_id: string;
  verified_name: string;
  secret?: string;
}

interface CloudSetup {
  available: boolean;
  needs: string[];
  embedded_signup: { ready: boolean; missing_env: string[]; app_id: string; config_id: string };
  accounts: CloudAccount[];
}

interface WaAccount {
  id: string;
  channel: string;
  address: string;
  provider_label: string;
}

const STATUS: Record<string, [string, "ok" | "warn" | "bad"]> = {
  live: ["Live", "ok"],
  setup: ["Being set up", "warn"],
  paused: ["Paused", "warn"],
  broken: ["Broken", "bad"],
};

const ADDRESS: Record<SocialName, [string, string]> = {
  messenger: ["Facebook Page id", "1029384756"],
  instagram: ["Instagram account id", "17841400000000"],
  telegram: ["Bot username", "@ExampleBankBot"],
};

function Pill({ word, health }: { word: string; health: "ok" | "warn" | "bad" }) {
  return <span className={`pill ${health}`}>{word}</span>;
}

function SimulatedTag() {
  return (
    <span className="tag" title="Nothing reaches real people">
      Simulated
    </span>
  );
}

function NotOn({ label }: { label: string }) {
  return (
    <p className="callout warn small">
      <strong>{label} isn't switched on for your business yet.</strong> ExaCarib switches each channel on once its written go-live checks have passed. Until then it can't be connected.
    </p>
  );
}

function Needs({ items }: { items: string[] }) {
  return (
    <ol className="ch-steps">
      {items.map((n) => (
        <li key={n}>{n}</li>
      ))}
    </ol>
  );
}

function ShownOnce({ secret, onDone }: { secret: string; onDone: () => void }) {
  return (
    <div className="secret" role="status">
      <p className="callout warn small">
        <strong>The test webhook secret.</strong> It is shown once and only signs simulated traffic.
      </p>
      <code>{secret}</code>
      <div className="secret-actions">
        <button type="button" className="button small" onClick={onDone}>
          Done
        </button>
      </div>
    </div>
  );
}

// ---- Messenger, Instagram, Telegram ------------------------------------------------------------------

/** Setup for one of Messenger, Instagram or Telegram. */
export function SocialChannel({ base, channel }: { base: string; channel: SocialName }) {
  const list = useApi<SocialList>(`${base}/social-accounts`, 30_000);
  const add = useAction();
  const [address, setAddress] = useState("");
  const [simulated, setSimulated] = useState(true);
  const [fresh, setFresh] = useState<SocialAccount | null>(null);
  const info = list.data?.channels[channel];
  const accounts = (list.data?.accounts ?? []).filter((a) => a.channel === channel);
  const label = info?.label ?? channel;
  return (
    <>
      <Card title="What it takes">
        {info && !info.available && <NotOn label={label} />}
        {info && <Needs items={info.needs} />}
        <p className="muted small">Private notes never leave Jibsy on any channel.</p>
      </Card>
      <Card title={channel === "telegram" ? "Bots" : channel === "instagram" ? "Instagram accounts" : "Pages"}>
        <ErrorNote error={list.error} />
        {fresh?.secret && <ShownOnce secret={fresh.secret} onDone={() => setFresh(null)} />}
        {list.data && accounts.length === 0 && <p className="muted">None yet.</p>}
        {accounts.map((a) => (
          <SocialAccountCard key={a.id} base={base} a={a} reload={list.reload} />
        ))}
        {info?.available && (
          <>
            <h3 className="ch-h3">Add</h3>
            <form
              className="form"
              onSubmit={(e) => {
                e.preventDefault();
                add.run(async () => {
                  const a = await api<SocialAccount>(`${base}/social-accounts`, { method: "POST", body: JSON.stringify({ channel, address, simulated }) });
                  setFresh(a);
                  setAddress("");
                  list.reload();
                });
              }}
            >
              <label>
                Kind
                <select value={simulated ? "sim" : "real"} onChange={(e) => setSimulated(e.target.value === "sim")}>
                  <option value="sim">Simulated (nothing reaches real people)</option>
                  <option value="real">Real {channel === "telegram" ? "bot" : "account"}</option>
                </select>
              </label>
              <label>
                {ADDRESS[channel][0]}
                <input value={address} onChange={(e) => setAddress(e.target.value)} placeholder={ADDRESS[channel][1]} required />
              </label>
              <div className="actions">
                <button className="button" disabled={add.busy}>
                  Add
                </button>
              </div>
            </form>
            <ErrorNote error={add.error} />
          </>
        )}
      </Card>
    </>
  );
}

function SocialAccountCard({ base, a, reload }: { base: string; a: SocialAccount; reload: () => void }) {
  const act = useAction();
  const [token, setToken] = useState("");
  const [saved, setSaved] = useState(false);
  const u = `${base}/social-accounts/${a.id}`;
  const [word, health] = STATUS[a.status] ?? [a.status, "warn"];
  const tokenName = a.channel === "telegram" ? "Bot token from @BotFather" : "Page access token";
  return (
    <div className="card-inset ch-account">
      <div className="ch-account-head">
        <h3>
          {a.address} {a.simulated && <SimulatedTag />}
        </h3>
        <Pill word={word} health={health} />
      </div>
      <dl className="ch-dl">
        <dt>Provider</dt>
        <dd>{a.provider_label}</dd>
        <dt>Webhook</dt>
        <dd className="mono wrap small">{a.webhook_url}</dd>
        {!a.simulated && (
          <>
            <dt>Token</dt>
            <dd>{a.token_saved ? `Saved ${when(a.token_saved_at)} (never shown again)` : "Not saved yet"}</dd>
            <dt>Connected</dt>
            <dd>{a.connected_at ? when(a.connected_at) : "Not yet"}</dd>
          </>
        )}
        <dt>Last message in</dt>
        <dd>{a.last_inbound_at ? when(a.last_inbound_at) : "None yet"}</dd>
      </dl>
      {a.missing.length > 0 && (
        <p className="callout warn small">
          <strong>Not live yet.</strong> Still needed: {a.missing.join("; ")}.
        </p>
      )}
      {a.last_error && <p className="pill bad small">Last error: {a.last_error}</p>}
      {!a.simulated && (
        <form
          className="form"
          onSubmit={(e) => {
            e.preventDefault();
            setSaved(false);
            act.run(async () => {
              await api(`${u}/token`, { method: "PUT", body: JSON.stringify({ token }) });
              setToken("");
              setSaved(true);
              reload();
            });
          }}
        >
          <label className="wide">
            {tokenName} (secure entry)
            <input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" spellCheck={false} required />
          </label>
          <p className="muted small wide" style={{ margin: 0 }}>
            Stored encrypted. It is never shown again, not even to you; to change it, enter a new one.
          </p>
          <div className="actions">
            <button className="button secondary small" disabled={act.busy}>
              Save token
            </button>
            {saved && <span className="ok-note">Saved.</span>}
          </div>
        </form>
      )}
      <div className="form-actions">
        {!a.simulated && (
          <button type="button" className="button secondary small" disabled={act.busy || !a.token_saved} onClick={() => act.run(async () => { await api(`${u}/connect`, { method: "POST" }); reload(); })}>
            Connect
          </button>
        )}
        {a.status !== "live" ? (
          <button type="button" className="button small" disabled={act.busy} onClick={() => act.run(async () => { await api(u, { method: "PATCH", body: JSON.stringify({ status: "live" }) }); reload(); })}>
            Set live
          </button>
        ) : (
          <button type="button" className="button secondary small" disabled={act.busy} onClick={() => act.run(async () => { await api(u, { method: "PATCH", body: JSON.stringify({ status: "paused" }) }); reload(); })}>
            Pause
          </button>
        )}
        <button
          type="button"
          className="button danger-text small"
          disabled={act.busy}
          onClick={() => {
            if (!window.confirm(`Remove ${a.address}? Its stored token is deleted too.`)) return;
            act.run(async () => {
              await api(u, { method: "DELETE" });
              reload();
            });
          }}
        >
          Remove
        </button>
      </div>
      <ErrorNote error={act.error} />
      {a.simulated && <SimulateIn base={base} path={`${u}/simulate-inbound`} from={a.channel === "telegram" ? "5551001" : "PSID-1001"} onDone={reload} />}
    </div>
  );
}

function SimulateIn({ path, from: initial, onDone }: { base: string; path: string; from: string; onDone: () => void }) {
  const [from, setFrom] = useState(initial);
  const [body, setBody] = useState("Hello, I'd like some help please.");
  const run = useAction();
  const [ok, setOk] = useState(false);
  return (
    <details className="ch-sim">
      <summary>Send a test message in (simulated)</summary>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          setOk(false);
          run.run(async () => {
            await api(path, { method: "POST", body: JSON.stringify({ from, body, name: "Test customer" }) });
            setOk(true);
            onDone();
          });
        }}
      >
        <label>
          From
          <input value={from} onChange={(e) => setFrom(e.target.value)} required />
        </label>
        <label className="wide">
          Message
          <input value={body} onChange={(e) => setBody(e.target.value)} required />
        </label>
        <div className="actions">
          <button className="button secondary small" disabled={run.busy}>
            Send in
          </button>
          {ok && (
            <span className="ok-note" role="status">
              In the inbox. <Link to="/commai">Open the inbox</Link>
            </span>
          )}
        </div>
      </form>
      <ErrorNote error={run.error} />
    </details>
  );
}

// ---- WhatsApp: Meta's Cloud API and click-to-chat -------------------------------------------------------

/** Extra cards on the WhatsApp tab. */
export function WhatsAppExtras({ base }: { base: string }) {
  return (
    <>
      <CloudApi base={base} />
      <ClickToChat base={base} />
    </>
  );
}

function CloudApi({ base }: { base: string }) {
  const setup = useApi<CloudSetup>(`${base}/whatsapp-cloud`, 30_000);
  const add = useAction();
  const act = useAction();
  const [number, setNumber] = useState("");
  const [fresh, setFresh] = useState<CloudAccount | null>(null);
  const d = setup.data;
  return (
    <Card title="Meta's Cloud API (direct)">
      <ErrorNote error={setup.error} />
      <p className="muted">A third way to connect WhatsApp, straight to Meta without Twilio or 360dialog. The same rules apply: the 24-hour window, approved templates and opt-out words.</p>
      {d && !d.available && <NotOn label="WhatsApp through Meta's Cloud API" />}
      {d && <Needs items={d.needs} />}
      {fresh?.secret && <ShownOnce secret={fresh.secret} onDone={() => setFresh(null)} />}
      {d?.accounts.map((a) => (
        <div key={a.id} className="card-inset ch-account">
          <div className="ch-account-head">
            <h3>
              {a.address} {a.simulated && <SimulatedTag />}
            </h3>
            <Pill word={(STATUS[a.status] ?? [a.status])[0]} health={(STATUS[a.status] ?? ["", "warn"])[1]} />
          </div>
          <dl className="ch-dl">
            <dt>WhatsApp Business Account</dt>
            <dd className="mono">{a.waba_id}</dd>
            <dt>Phone number id</dt>
            <dd className="mono">{a.phone_number_id}</dd>
            <dt>Webhook</dt>
            <dd className="mono wrap small">{a.webhook_url}</dd>
          </dl>
          {a.missing.length > 0 && <p className="callout warn small">Still needed: {a.missing.join("; ")}.</p>}
          <div className="form-actions">
            <button
              type="button"
              className="button secondary small"
              disabled={act.busy}
              onClick={() =>
                act.run(async () => {
                  const r = await api<{ remote: number; updated: number }>(`${base}/whatsapp-cloud/${a.id}/templates/sync`, { method: "POST" });
                  window.alert(`Meta has ${r.remote} templates; ${r.updated} changed here.`);
                })
              }
            >
              Sync template approvals
            </button>
          </div>
          {a.simulated && <SimulateIn base={base} path={`${base}/whatsapp-cloud/${a.id}/simulate-inbound`} from="+18685550101" onDone={setup.reload} />}
        </div>
      ))}
      <ErrorNote error={act.error} />
      {d?.available && (
        <>
          <h3 className="ch-h3">Connect a number</h3>
          {d.embedded_signup.ready ? (
            <p className="muted small">
              Embedded Signup is configured. Opening Meta's window from here needs Meta's JavaScript SDK on this page, which is added when ExaCarib's Meta app passes review.
            </p>
          ) : (
            <p className="callout small">
              Meta's Embedded Signup isn't set up on the server yet (needs {d.embedded_signup.missing_env.join(", ")}). You can try everything with a simulated number.
            </p>
          )}
          <form
            className="form"
            onSubmit={(e) => {
              e.preventDefault();
              add.run(async () => {
                const a = await api<CloudAccount>(`${base}/whatsapp-cloud/signup`, { method: "POST", body: JSON.stringify({ simulated: true, number }) });
                setFresh(a);
                setNumber("");
                setup.reload();
              });
            }}
          >
            <label>
              Simulated number
              <input value={number} onChange={(e) => setNumber(e.target.value)} placeholder="+1 868 555 0100" required />
            </label>
            <div className="actions">
              <button className="button" disabled={add.busy}>
                Add simulated number
              </button>
            </div>
          </form>
          <ErrorNote error={add.error} />
        </>
      )}
    </Card>
  );
}

function ClickToChat({ base }: { base: string }) {
  const all = useApi<WaAccount[]>(`${base}/channel-accounts`, 60_000);
  const numbers = (all.data ?? []).filter((a) => a.channel === "whatsapp");
  const [account, setAccount] = useState("");
  const [text, setText] = useState("Hello, I'd like some help.");
  const [link, setLink] = useState<{ url: string; qr_svg: string } | null>(null);
  const run = useAction();
  const chosen = account || numbers[0]?.id || "";
  return (
    <Card title="Click to WhatsApp">
      <p className="muted">A link and QR code that open a WhatsApp chat with your number, with a message ready to send. Put them on your website, receipts or posters.</p>
      {all.data && numbers.length === 0 && <p className="muted">Add a WhatsApp number first.</p>}
      {numbers.length > 0 && (
        <form
          className="form"
          onSubmit={(e) => {
            e.preventDefault();
            run.run(async () => {
              const q = new URLSearchParams({ account_id: chosen, text });
              setLink(await api(`${base}/whatsapp-link?${q}`));
            });
          }}
        >
          <label>
            Number
            <select value={chosen} onChange={(e) => setAccount(e.target.value)}>
              {numbers.map((n) => (
                <option key={n.id} value={n.id}>
                  {n.address} ({n.provider_label})
                </option>
              ))}
            </select>
          </label>
          <label className="wide">
            Message ready to send
            <input value={text} onChange={(e) => setText(e.target.value)} maxLength={500} />
          </label>
          <div className="actions">
            <button className="button secondary" disabled={run.busy}>
              Make link
            </button>
          </div>
        </form>
      )}
      <ErrorNote error={run.error} />
      {link && (
        <div className="ch-link">
          <p className="mono wrap small">
            <a href={link.url} target="_blank" rel="noreferrer">
              {link.url}
            </a>
          </p>
          <img className="ch-qr" alt={`QR code for ${link.url}`} src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(link.qr_svg)}`} width={200} height={200} />
          <p>
            <a className="button secondary small" download="whatsapp-qr.svg" href={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(link.qr_svg)}`}>
              Download QR code
            </a>
          </p>
        </div>
      )}
    </Card>
  );
}
