import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./style.css";

async function main() {
  // In dev, with no real backend configured, serve the API from the MSW mocks
  // (spec WP-12: the UI runs against fixtures with zero backend running).
  if (import.meta.env.DEV && !import.meta.env.VITE_API_URL) {
    const { startMocks } = await import("./mocks");
    await startMocks();
  }
  createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}

main();
