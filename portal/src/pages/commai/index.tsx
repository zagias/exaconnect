import { Route, Routes } from "react-router-dom";
import AiAgents from "./AiAgents";
import Assistant from "./Assistant";
import Onboarding from "./Onboarding";
import Partner from "./partner";
import GoLive from "./partner/GoLive";
import OAuthConsent from "./partner/OAuthConsent";
import Channels from "./Channels";
import Contacts from "./Contacts";
import Countries from "./Countries";
import Governance from "./Governance";
import { I18nProvider } from "./i18n";
import Inbox from "./Inbox";
import Integrations from "./Integrations";
import Languages from "./Languages";
import Quality from "./Quality";
import Reports from "./Reports";
import Settings from "./Settings";
import Team from "./Team";
import Voice from "./Voice";
import Workflows from "./Workflows";
import MySettings from "../me/MySettings";
import "./commai.css";

/** CommAI screens (ADR 0016). Each screen owns its own file. */
export default function CommAI() {
  return (
    <I18nProvider>
    <Routes>
      <Route path="/" element={<Inbox />} />
      <Route path="/c/:id" element={<Inbox />} />
      <Route path="/contacts/*" element={<Contacts />} />
      <Route path="/channels/*" element={<Channels />} />
      <Route path="/countries/*" element={<Countries />} />
      <Route path="/ai/*" element={<AiAgents />} />
      <Route path="/workflows/*" element={<Workflows />} />
      <Route path="/integrations/*" element={<Integrations />} />
      <Route path="/voice/*" element={<Voice />} />
      <Route path="/reports/*" element={<Reports />} />
      <Route path="/settings/*" element={<Settings />} />
      <Route path="/setup/*" element={<Onboarding />} />
      <Route path="/assistant/*" element={<Assistant />} />
      <Route path="/partner/*" element={<Partner />} />
      <Route path="/golive/*" element={<GoLive />} />
      <Route path="/oauth/authorize" element={<OAuthConsent />} />
      <Route path="/me/*" element={<MySettings />} />
      <Route path="/quality/*" element={<Quality />} />
      <Route path="/governance/*" element={<Governance />} />
      <Route path="/languages/*" element={<Languages />} />
      <Route path="/team/*" element={<Team />} />
    </Routes>
    </I18nProvider>
  );
}
