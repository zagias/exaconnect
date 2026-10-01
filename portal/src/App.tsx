import { NavLink, Route, Routes } from "react-router-dom";
import { AuthGate, useAuth } from "./auth";
import { CustomerPicker, CustomerProvider, StormBanner, StormSwitch } from "./customer";
import Account from "./pages/Account";
import Ask from "./pages/Ask";
import Insights from "./pages/Insights";
import Admin from "./pages/Admin";
import Decisions from "./pages/Decisions";
import Metering from "./pages/Metering";
import Overview from "./pages/Overview";
import { SiteList, SitePage } from "./pages/Sites";
import Traffic from "./pages/Traffic";

export default function App() {
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
  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/" aria-label="ExaCarib, home">
            <img src="/brand/exacarib-wordmark-reversed.png" alt="ExaCarib" width={140} height={26} />
          </NavLink>
          <nav aria-label="Main">
            {carrier ? (
              <NavLink to="/" end>
                Carrier view
              </NavLink>
            ) : (
              <>
                <NavLink to="/" end>
                  Overview
                </NavLink>
                <NavLink to="/sites">Sites</NavLink>
                <NavLink to="/traffic">Traffic</NavLink>
                <NavLink to="/decisions">Decisions</NavLink>
                <NavLink to="/metering">Metering</NavLink>
                <NavLink to="/insights">Insights</NavLink>
                <NavLink to="/ask">Ask</NavLink>
                {admin && <NavLink to="/carrier">Carrier view</NavLink>}
                {admin && <NavLink to="/admin">Admin</NavLink>}
              </>
            )}
          </nav>
          {/* Storm Mode switch: in the bar on every screen (not for carriers). */}
          {!carrier && <StormSwitch />}
        </div>
      </header>
      {!carrier && <StormBanner />}
      <main className="page">
        <SignedInBar />
        {carrier ? (
          <Routes>
            <Route path="/account" element={<Account />} />
            <Route path="*" element={<Metering carrierView />} />
          </Routes>
        ) : (
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/sites" element={<SiteList />} />
            <Route path="/sites/:id" element={<SitePage />} />
            <Route path="/traffic/*" element={<Traffic />} />
            <Route path="/decisions" element={<Decisions />} />
            <Route path="/metering" element={<Metering />} />
            <Route path="/insights" element={<Insights />} />
            <Route path="/ask" element={<Ask />} />
            <Route path="/carrier" element={<Metering carrierView />} />
            <Route path="/account" element={<Account />} />
            {admin && <Route path="/admin/*" element={<Admin />} />}
            <Route path="*" element={<NotFound />} />
          </Routes>
        )}
      </main>
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

function SignedInBar() {
  const { user, signOut } = useAuth();
  return (
    <div className="signed-in muted">
      Signed in as {user?.email} ({user?.role}) · <NavLink to="/account">Account</NavLink> ·{" "}
      <button className="link" onClick={signOut}>
        Sign out
      </button>
      {user?.role === "admin" && <CustomerPicker />}
    </div>
  );
}
