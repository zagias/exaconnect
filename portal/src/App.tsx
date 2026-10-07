import { lazy, Suspense, useEffect } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { AuthGate, useAuth } from "./auth";
import { CustomerProvider } from "./customer";
import Account from "./pages/Account";
import Ask from "./pages/Ask";
import Insights from "./pages/Insights";
import InvitePage, { inviteToken, PENDING_INVITE } from "./pages/Invite";
import People from "./pages/People";
import { ForgotPage, ResetPage, resetRoute } from "./pages/Reset";
import Internet from "./pages/Internet";
import Admin from "./pages/Admin";
import Billing from "./pages/Billing";
import Decisions from "./pages/Decisions";
import Encryption from "./pages/Encryption";
import Fabric from "./pages/Fabric";
import Metering from "./pages/Metering";
import OrderPage from "./pages/Order";
import Overview from "./pages/Overview";
import { SiteList, SitePage } from "./pages/Sites";
import Traffic from "./pages/Traffic";
import { Shell } from "./shell";

// CommAI loads on first visit, so Connect screens stay light.
const CommAI = lazy(() => import("./pages/commai"));

export default function App() {
  // An invitation link works signed out, so it sits outside the sign-in gate.
  const path = useLocation().pathname;
  const token = inviteToken(path);
  if (token) return <InvitePage token={token} />;
  // So do the forgotten-password screens.
  const reset = resetRoute(path);
  if (reset) return reset.kind === "forgot" ? <ForgotPage /> : <ResetPage token={reset.token} />;
  return (
    <AuthGate>
      <CustomerProvider>
        <Layout />
      </CustomerProvider>
    </AuthGate>
  );
}

function Layout() {
  const { user } = useAuth();
  const carrier = user?.role === "carrier";
  const admin = user?.role === "admin";
  const navigate = useNavigate();
  // Back from signing in to accept an invitation: finish joining.
  useEffect(() => {
    let pending: string | null = null;
    try {
      pending = sessionStorage.getItem(PENDING_INVITE);
      sessionStorage.removeItem(PENDING_INVITE);
    } catch {
      /* storage unavailable */
    }
    if (pending) navigate(`/invite/${encodeURIComponent(pending)}`);
  }, [navigate]);
  // An organisation with CommAI but not Connect starts in CommAI.
  const connect = !user?.products || user.products.includes("connect");
  return (
    <Shell>
      {carrier ? (
        <Routes>
          <Route path="/account" element={<Account />} />
          <Route path="*" element={<Metering carrierView />} />
        </Routes>
      ) : (
        <Routes>
          <Route path="/" element={connect ? <Overview /> : <Navigate to="/commai" replace />} />
          <Route path="/sites" element={<SiteList />} />
          <Route path="/sites/:id" element={<SitePage />} />
          <Route path="/traffic/*" element={<Traffic />} />
          <Route path="/decisions" element={<Decisions />} />
          <Route path="/fabric" element={<Fabric />} />
          <Route path="/internet" element={<Internet />} />
          <Route path="/encryption" element={<Encryption />} />
          <Route path="/order" element={<OrderPage />} />
          <Route path="/metering" element={<Metering />} />
          <Route path="/billing/*" element={<Billing />} />
          <Route path="/insights" element={<Insights />} />
          <Route path="/ask" element={<Ask />} />
          <Route path="/carrier" element={<Metering carrierView />} />
          <Route path="/account" element={<Account />} />
          <Route path="/account/people" element={<People />} />
          <Route
            path="/commai/*"
            element={
              <Suspense fallback={<p className="muted">Loading CommAI…</p>}>
                <CommAI />
              </Suspense>
            }
          />
          {admin && <Route path="/admin/*" element={<Admin />} />}
          <Route path="*" element={<NotFound />} />
        </Routes>
      )}
    </Shell>
  );
}

function NotFound() {
  return (
    <div className="page-head">
      <h1>Page not found</h1>
      <p className="muted">That address doesn't match a page in the portal.</p>
    </div>
  );
}
