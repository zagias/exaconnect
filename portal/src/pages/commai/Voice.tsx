import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useApi } from "../../api";
import { ErrorNote } from "../../components";
import { PageHead, Tabs } from "../../ui";
import { useCommaiBase } from "./lib";
import Billing from "./voice/Billing";
import { Carriers, Emergency, EmergencyNotice, Fraud, Numbers, Ports, VoiceNotices } from "./voice/Global";
import Dialer from "./voice/Dialer";
import MySettings from "./voice/MySettings";
import Orders from "./voice/Orders";
import { Access, Changes, PeopleAndNumbers, PendingChange, Routing, usePending } from "./voice/PhoneSystem";
import type { VoiceOverview } from "./voice/types";
import "./voice/voice.css";

const OTHER_TABS = [
  "/commai/voice/orders",
  "/commai/voice/billing",
  "/commai/voice/me",
  "/commai/voice/numbers",
  "/commai/voice/ports",
  "/commai/voice/emergency",
  "/commai/voice/fraud",
  "/commai/voice/carriers",
  "/commai/voice/phone",
];

/** Jibsy voice (ADR 0021): phone system, orders, billing and each person's own settings. */
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
        <PageHead title="My phone">
          Your own forwarding, do not disturb, voicemail and calls.
        </PageHead>
        <VoiceNotices base={base} />
        <EmergencyNotice base={base} />
        <MySettings base={base} />
        <Dialer base={base} />
      </>
    );
  }

  return (
    <>
      <PageHead title="Phone">
        Your phone system: people, numbers, call routing, orders and billing.{" "}
        {v && !v.provider.live && <span className="tag">Simulated SIP provider: no real calls yet</span>}
      </PageHead>
      <Tabs label="Voice sections">
        <NavLink to="/commai/voice" className={() => (inPhoneSystem ? "active" : "")} end>
          Phone system
        </NavLink>
        <NavLink to="/commai/voice/orders">Orders</NavLink>
        <NavLink to="/commai/voice/numbers">Numbers</NavLink>
        <NavLink to="/commai/voice/ports">Ports</NavLink>
        <NavLink to="/commai/voice/emergency">Emergency</NavLink>
        <NavLink to="/commai/voice/fraud">Fraud</NavLink>
        <NavLink to="/commai/voice/billing">Billing</NavLink>
        <NavLink to="/commai/voice/me">My phone</NavLink>
        {v?.is_exacarib && <NavLink to="/commai/voice/carriers">Carriers</NavLink>}
        <NavLink to="/commai/voice/phone">Browser phone</NavLink>
      </Tabs>
      <ErrorNote error={ov.error} />
      <VoiceNotices base={base} />
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
          <Route path="/me" element={<><EmergencyNotice base={base} /><MySettings base={base} onChange={ov.reload} /></>} />
          <Route path="/numbers" element={<Numbers base={base} v={v} />} />
          <Route path="/ports" element={<Ports base={base} v={v} />} />
          <Route path="/emergency" element={<Emergency base={base} />} />
          <Route path="/fraud" element={<Fraud base={base} v={v} />} />
          {v.is_exacarib && <Route path="/carriers" element={<Carriers customerId={base.split("/").pop() ?? ""} />} />}
          <Route path="/phone" element={<Dialer base={base} />} />
        </Routes>
      )}
    </>
  );
}
