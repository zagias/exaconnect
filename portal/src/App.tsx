import { lazy, Suspense, useEffect } from "react";
import { Link, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { PRODUCT_NAMES } from "./api";
import { APP_INFO, areaFor, myApps, type AppId } from "./apps";
import { AuthGate, useAuth } from "./auth";
import { CustomerProvider } from "./customer";
import Account from "./pages/Account";
import AppsAndPlans from "./pages/AppsAndPlans";
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
import Integrations from "./pages/Integrations";
import Notices from "./pages/Notices";
import Metering from "./pages/Metering";
import OrderPage from "./pages/Order";
import Overview from "./pages/Overview";
import { SiteList, SitePage } from "./pages/Sites";
import Traffic from "./pages/Traffic";
import CompanyPage from "./pages/org/Company";
import LocationsPage from "./pages/org/Locations";
import SecurityPage from "./pages/org/Security";
import SetupPage from "./pages/org/Setup";
import Organisations from "./pages/ops/Organisations";
import { Shell } from "./shell";
import { PageHead } from "./ui";

// Jibsy loads on first visit, so Connect screens stay light.
const Jibsy = lazy(() => import("./pages/commai"));

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
  // Someone with Jibsy but not Connect starts in Jibsy (ADR 0041).
  const path = useLocation().pathname;
  const mine = myApps(user);
  const connect = mine.includes("connect");
  const area = areaFor(path);
  // A screen of an app the person can't open says why, instead of failing call by call.
  const blocked =
    !carrier && !admin && area !== "account" && area !== "ops" && !mine.includes(area) && !(path === "/" && mine.length > 0);
  return (
    <Shell>
      {blocked ? (
        <NoAccess app={area as AppId} mine={mine} />
      ) : carrier ? (
        <Routes>
          <Route path="/account" element={<Account />} />
          <Route path="/notices" element={<Notices />} />
          <Route path="/integrations" element={<Integrations />} />
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
          <Route path="/notices" element={<Notices />} />
          <Route path="/integrations" element={<Integrations />} />
          <Route path="/ask" element={<Ask />} />
          <Route path="/carrier" element={<Metering carrierView />} />
          <Route path="/account" element={<Account />} />
          <Route path="/account/people" element={<People />} />
          <Route path="/account/apps" element={<AppsAndPlans />} />
          <Route path="/org" element={<Navigate to="/org/setup" replace />} />
          <Route path="/org/setup" element={<SetupPage />} />
          <Route path="/org/company" element={<CompanyPage />} />
          <Route path="/org/locations" element={<LocationsPage />} />
          <Route path="/org/security" element={<SecurityPage />} />
          <Route
            path="/commai/*"
            element={
              <Suspense fallback={<p className="muted">Loading Jibsy…</p>}>
                <Jibsy />
              </Suspense>
            }
          />
          {admin && <Route path="/admin/*" element={<Admin />} />}
          {admin && <Route path="/ops" element={<Navigate to="/ops/organisations" replace />} />}
          {admin && <Route path="/ops/organisations" element={<Organisations />} />}
          <Route path="*" element={<NotFound />} />
        </Routes>
      )}
    </Shell>
  );
}

/** A screen of an app the person can't open: why, and where to go instead. */
function NoAccess({ app, mine }: { app: AppId; mine: AppId[] }) {
  const { user } = useAuth();
  const onPlan = !user?.products || user.products.includes(app);
  const manager = user?.org_role === "owner" || user?.org_role === "admin";
  const name = PRODUCT_NAMES[app];
  const org = user?.organisation ?? "Your organisation";
  return (
    <>
      <PageHead eyebrow={APP_INFO[app].full} title={onPlan ? `You don't have access to ${name}` : `${name} isn't on your plan`}>
        {onPlan
          ? `${org} has ${name}, but you haven't been given it. Ask an owner or admin of your organisation.`
          : manager
            ? `${org} doesn't have ${name} yet. You can ask ExaCarib to add it.`
            : `${org} doesn't have ${name}. An owner or admin can ask ExaCarib to add it.`}
      </PageHead>
      <div className="actions" style={{ justifyContent: "flex-start", marginTop: 16 }}>
        {mine.map((a) => (
          <Link key={a} className="button" to={APP_INFO[a].home}>
            Go to {APP_INFO[a].name}
          </Link>
        ))}
        {!onPlan && manager && (
          <Link className="button secondary" to="/account/apps">
            Apps and plans
          </Link>
        )}
      </div>
    </>
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
