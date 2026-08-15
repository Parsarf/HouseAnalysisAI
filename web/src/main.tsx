import React from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Property = { id: string; address: string; score: string; status: string };
const demo: Property[] = [];

function App() {
  return <main>
    <header><h1>ACQ</h1><span>Property acquisition analysis</span></header>
    <section className="empty"><h2>Portfolio</h2><p>{demo.length ? `${demo.length} properties` : "No properties yet"}</p><button>Upload reports</button></section>
    <footer>All financial values are deterministic and source-traced.</footer>
  </main>;
}

createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
