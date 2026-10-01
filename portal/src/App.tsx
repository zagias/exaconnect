import { NavLink, Route, Routes } from "react-router-dom";
import { AuthGate, useAuth } from "./auth";
import { CustomerProvider, StormBanner, StormSwitch } from "./customer";
import Admin from "./pages/Admin";
import Decisions from "./pages/Decisions";
import Overview from "./pages/Overview";
import Placeholder from "./pages/Placeholder";
import { SiteList, SitePage } from "./pages/Sites";

const later = [
  { path: "/metering", label: "Metering", milestone: "M6", about: "Usage per link, the 95th percentile, the commit line and burst." },
  { path: "/carrier", label: "Carrier view", milestone: "M6", about: "Read only: a carrier's own links and the exact samples used for settlement." },
];

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
  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/" aria-label="ExaCarib, overview">
            <img src="/brand/exacarib-wordmark-reversed.png" alt="ExaCarib" width={140} height={26} />
          </NavLink>
          <nav aria-label="Main">
            <NavLink to="/" end>
              Overview
            </NavLink>
            <NavLink to="/sites">Sites</NavLink>
            <NavLink to="/decisions">Decisions</NavLink>
            {later.map((s) => (
              <NavLink key={s.path} to={s.path}>
                {s.label}
              </NavLink>
            ))}
            <NavLink to="/admin">Admin</NavLink>
          </nav>
          {/* Storm Mode switch: in the bar on every screen. */}
          <StormSwitch />
        </div>
      </header>
      <StormBanner />
      <main className="page">
        <>
          <SignedInBar />
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/sites" element={<SiteList />} />
            <Route path="/sites/:id" element={<SitePage />} />
            <Route path="/decisions" element={<Decisions />} />
            <Route path="/admin" element={<Admin />} />
            {later.map((s) => (
              <Route key={s.path} path={s.path} element={<Placeholder title={s.label} milestone={s.milestone} about={s.about} />} />
            ))}
          </Routes>
        </>
      </main>
    </>
  );
}

function SignedInBar() {
  const { user, signOut } = useAuth();
  return (
    <div className="signed-in muted">
      Signed in as {user?.email} ({user?.role}) ·{" "}
      <button className="link" onClick={signOut}>
        Sign out
      </button>
    </div>
  );
}
