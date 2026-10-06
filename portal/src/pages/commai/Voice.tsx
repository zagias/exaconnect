import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useApi } from "../../api";
import { ErrorNote } from "../../components";
import { PageHead, Tabs } from "../../ui";
import { useCommaiBase } from "./lib";
import Billing from "./voice/Billing";
import MySettings from "./voice/MySettings";
import Orders from "./voice/Orders";
import { Access, Changes, PeopleAndNumbers, PendingChange, Routing, usePending } from "./voice/PhoneSystem";
import type { VoiceOverview } from "./voice/types";
import "./voice/voice.css";

const OTHER_TABS = ["/commai/voice/orders", "/commai/voice/billing", "/commai/voice/me"];

/** CommAI voice (ADR 0021): phone system, orders, billing and each person's own settings. */
export default function Voice() {
  const base = useCommaiBase();
  const ov = useApi<VoiceOverview>(base ? `${base}/voice` : null, 30_000);
  const { pathname } = useLocation();
  const pending = usePending();
  if (!base) return null;
  const v = ov.data;
  const inPhoneSystem = !OTHER_TABS.some((p) => pathname.startsWith(p));

  if (v && !v.is_admin) {
    return (
      <>
        <PageHead eyebrow="CommAI" title="My phone">
          Your own forwarding, do not disturb, voicemail and calls.
        </PageHead>
        <MySettings base={base} />
      </>
    );
  }

  return (
    <>
      <PageHead eyebrow="CommAI" title="Voice">
        Your phone system: people, numbers, call routing, orders and billing.{" "}
        {v && !v.provider.live && <span className="tag">Simulated SIP provider: no real calls yet</span>}
      </PageHead>
      <Tabs label="Voice sections">
        <NavLink to="/commai/voice" className={() => (inPhoneSystem ? "active" : "")} end>
          Phone system
        </NavLink>
        <NavLink to="/commai/voice/orders">Orders</NavLink>
        <NavLink to="/commai/voice/billing">Billing</NavLink>
        <NavLink to="/commai/voice/me">My settings</NavLink>
      </Tabs>
      <ErrorNote error={ov.error} />
      {v && inPhoneSystem && (
        <>
          <nav className="voice-subnav" aria-label="Phone system sections">
            <NavLink to="/commai/voice" end>
              People and numbers
            </NavLink>
            <NavLink to="/commai/voice/routing">Call routing</NavLink>
            <NavLink to="/commai/voice/changes">Scheduled, history and bulk</NavLink>
            <NavLink to="/commai/voice/access">Access</NavLink>
            <span className="muted small">Version {v.version ?? 0}</span>
          </nav>
          <PendingChange base={base} p={pending} reload={ov.reload} />
        </>
      )}
      {v && (
        <Routes>
          <Route path="/" element={<PeopleAndNumbers base={base} v={v} propose={pending.propose} />} />
          <Route path="/routing" element={<Routing v={v} propose={pending.propose} />} />
          <Route path="/changes" element={<Changes base={base} v={v} reload={ov.reload} />} />
          <Route path="/access" element={<Access base={base} v={v} reload={ov.reload} />} />
          <Route path="/orders" element={<Orders base={base} v={v} />} />
          <Route path="/billing" element={<Billing base={base} v={v} />} />
          <Route path="/me" element={<MySettings base={base} onChange={ov.reload} />} />
        </Routes>
      )}
    </>
  );
}
