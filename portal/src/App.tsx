import { NavLink, Route, Routes } from "react-router-dom";
import Overview from "./pages/Overview";
import Placeholder from "./pages/Placeholder";

const screens = [
  { path: "/sites", label: "Sites", milestone: "M3", about: "Every path with live latency, jitter and loss, and the current steering map." },
  { path: "/decisions", label: "Decisions", milestone: "M4", about: "Every routing decision with its reason, filterable by site and class." },
  { path: "/metering", label: "Metering", milestone: "M6", about: "Usage per link, the 95th percentile, the commit line and burst." },
  { path: "/carrier", label: "Carrier view", milestone: "M6", about: "Read only: a carrier's own links and the exact samples used for settlement." },
  { path: "/admin", label: "Admin", milestone: "M2", about: "Sites, links, classes, SLA policies and enrolment tokens." },
];

export default function App() {
  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/" aria-label="ExaCarib, overview">
            <img src="/brand/exacarib-wordmark-reversed.png" alt="ExaCarib" width={140} height={26} />
          </NavLink>
          <nav aria-label="Main">
            <NavLink to="/" end>Overview</NavLink>
            {screens.map((s) => (
              <NavLink key={s.path} to={s.path}>{s.label}</NavLink>
            ))}
          </nav>
          {/* Storm Mode switch: in the bar on every screen, off by default. Wired up in M5. */}
          <button className="storm-switch" aria-pressed={false} disabled title="Storm Mode arrives in M5">
            <span className="dot" aria-hidden="true" />
            Storm Mode off
          </button>
        </div>
      </header>
      <main className="page">
        <Routes>
          <Route path="/" element={<Overview />} />
          {screens.map((s) => (
            <Route key={s.path} path={s.path} element={<Placeholder title={s.label} milestone={s.milestone} about={s.about} />} />
          ))}
        </Routes>
      </main>
    </>
  );
}
