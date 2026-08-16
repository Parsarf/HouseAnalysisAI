import { useEffect, useState } from "react";
import { ApiError, me, setUnauthorizedHandler, type MeResponse } from "./api";
import { AuthProvider } from "./auth";
import { DealPage } from "./pages/DealPage";
import { LoginPage } from "./pages/LoginPage";
import {
  AssumptionsPage,
  BatchesPage,
  ChangesPage,
  DashboardPage,
  FlagsPage,
  ProblemsPage,
  RankingsPage,
  SettingsPage,
} from "./pages/Operations";
import { PropertiesPage } from "./pages/PropertiesPage";
import { Link, matchRoute, navigate, usePath } from "./router";

const NAV = [
  ["properties", "/", "Portfolio", "▦"],
  ["dashboard", "/dashboard", "Dashboard", "◫"],
  ["rankings", "/rankings", "Rankings", "↗"],
  ["flags", "/flags", "Flags", "◇"],
  ["changes", "/changes", "Changes", "◌"],
  ["batches", "/batches", "Batches", "↑"],
  ["problems", "/problems", "Problems", "!"],
  ["assumptions", "/assumptions", "Assumptions", "≋"],
  ["settings", "/settings", "Settings", "⚙"],
] as const;

function AppShell(props: { user: MeResponse; onSignOut: () => void }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const path = usePath();
  const route = matchRoute(path);
  useEffect(() => setMobileOpen(false), [path]);
  return <AuthProvider value={{ user: props.user, signOut: props.onSignOut }}>
    <div className="app-shell">
      <aside className={`sidebar ${mobileOpen ? "open" : ""}`}>
        <div className="sidebar-brand"><span className="brand-mark small">A</span><div><strong>ACQ</strong><small>Acquisition intelligence</small></div></div>
        <nav aria-label="Primary navigation">
          {NAV.map(([name, to, label, icon]) => <Link key={name} to={to} className={`nav-link ${route.name === name || (name === "properties" && route.name === "deal") ? "active" : ""}`}><span>{icon}</span>{label}</Link>)}
        </nav>
        <div className="sidebar-footer">
          {props.user.read_only && <span className="readonly-chip">Read-only session</span>}
          <div className="user-row"><span className="avatar">{props.user.id.slice(0,1).toUpperCase()}</span><div><strong>{props.user.id}</strong><small>{props.user.read_only ? "Reviewer" : "Workspace owner"}</small></div><button title="Log out" onClick={props.onSignOut}>↪</button></div>
        </div>
      </aside>
      <div className="app-stage">
        <header className="mobile-header"><button className="menu-button" onClick={() => setMobileOpen((value) => !value)} aria-label="Toggle navigation">☰</button><Link to="/" className="mobile-brand">ACQ</Link>{props.user.read_only && <span className="readonly-chip">Read only</span>}</header>
        <main className="app-main">
          {route.name === "properties" && <PropertiesPage />}
          {route.name === "deal" && <DealPage propertyId={route.propertyId} />}
          {route.name === "dashboard" && <DashboardPage />}
          {route.name === "rankings" && <RankingsPage />}
          {route.name === "flags" && <FlagsPage />}
          {route.name === "changes" && <ChangesPage />}
          {route.name === "batches" && <BatchesPage />}
          {route.name === "problems" && <ProblemsPage />}
          {route.name === "assumptions" && <AssumptionsPage />}
          {route.name === "settings" && <SettingsPage />}
          {route.name === "login" && <DashboardPage />}
          {route.name === "not-found" && <div className="empty-state panel"><strong>Page not found</strong><Link to="/">Return to portfolio</Link></div>}
        </main>
      </div>
      {mobileOpen && <button className="sidebar-scrim" aria-label="Close navigation" onClick={() => setMobileOpen(false)} />}
    </div>
  </AuthProvider>;
}

export default function App() {
  const [user, setUser] = useState<MeResponse | null>(null);
  const [booting, setBooting] = useState(true);
  useEffect(() => {
    setUnauthorizedHandler(() => { setUser(null); navigate("/login"); });
    me().then((value) => { setUser(value); if (window.location.pathname === "/login") navigate("/"); })
      .catch((error: ApiError | Error) => { if (!(error instanceof ApiError) || error.status !== 401) console.error(error); setUser(null); if (window.location.pathname !== "/login") navigate("/login"); })
      .finally(() => setBooting(false));
    return () => setUnauthorizedHandler(null);
  }, []);
  const signOut = () => {
    // There is no logout endpoint; the HttpOnly session cookie remains valid until expiry.
    setUser(null);
    navigate("/login");
  };
  if (booting) return <div className="boot-splash"><span className="brand-mark">A</span><div className="boot-line"><i /></div><p>Opening your workspace</p></div>;
  if (!user) return <LoginPage onAuthenticated={(value) => { setUser(value); navigate("/"); }} />;
  return <AppShell user={user} onSignOut={signOut} />;
}
