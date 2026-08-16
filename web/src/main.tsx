import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Property = { id: string; address: string; score: string; status: string };
const demo: Property[] = [];

function App() {
  const [properties, setProperties] = useState<Property[]>(demo);
  const [message, setMessage] = useState("Loading portfolio…");
  useEffect(() => { fetch("/api/properties").then((response) => response.ok ? response.json() : Promise.reject()).then((data) => { setProperties(data.items); setMessage(""); }).catch(() => setMessage("Sign in to view your portfolio.")); }, []);
  const upload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files;
    if (!files?.length) return;
    const body = new FormData();
    Array.from(files).forEach((file) => body.append("files", file));
    const response = await fetch("/api/uploads", { method: "POST", body });
    setMessage(response.ok ? "Upload queued. Refreshing status will show progress." : "Upload failed. Check authentication or the Problems page.");
  };
  return <main>
    <header><h1>ACQ</h1><span>Property acquisition analysis</span></header>
    <section className="empty"><h2>Portfolio</h2><p>{message || `${properties.length} properties`}</p><label className="button">Upload reports<input type="file" accept="application/pdf,.pdf" multiple onChange={upload} /></label>{properties.length > 0 && <table><tbody>{properties.map((property) => <tr key={property.id}><td>{property.address || "Unknown address"}</td><td>{property.status}</td><td>{property.gut_rating ?? "—"}</td></tr>)}</tbody></table>}</section>
    <footer>All financial values are deterministic and source-traced.</footer>
  </main>;
}

createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
