/**
 * App shell (WP-12): header, route outlet, footer. Routes come from the
 * local mini-router; main.tsx (owned elsewhere) renders this component.
 */
import { DealPage } from "./pages/DealPage";
import { PropertiesPage } from "./pages/PropertiesPage";
import { Link, matchRoute, usePath } from "./router";
import { palette } from "./components/ui";

export default function App() {
  const route = matchRoute(usePath());
  return (
    <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column", color: palette.text }}>
      <header
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: 12,
          padding: "12px 24px",
          borderBottom: `1px solid ${palette.border}`,
          background: palette.surface,
        }}
      >
        <Link to="/" style={{ fontSize: 20, fontWeight: 700, color: palette.text, textDecoration: "none" }}>
          ACQ
        </Link>
        <span style={{ color: palette.muted, fontSize: 13 }}>Property acquisition analysis</span>
        <nav style={{ marginLeft: "auto", fontSize: 14 }}>
          <Link to="/properties" style={{ color: palette.accent }}>
            Portfolio
          </Link>
        </nav>
      </header>
      <main style={{ flex: 1, padding: "20px 24px", maxWidth: 1200, width: "100%", margin: "0 auto", boxSizing: "border-box" }}>
        {route.name === "properties" && <PropertiesPage />}
        {route.name === "deal" && <DealPage propertyId={route.propertyId} />}
        {route.name === "not-found" && (
          <p>
            Page not found.{" "}
            <Link to="/" style={{ color: palette.accent }}>
              Back to the portfolio.
            </Link>
          </p>
        )}
      </main>
      <footer
        style={{
          padding: "12px 24px",
          borderTop: `1px solid ${palette.border}`,
          color: palette.muted,
          fontSize: 12,
        }}
      >
        All financial values are deterministic and source-traced.
      </footer>
    </div>
  );
}
