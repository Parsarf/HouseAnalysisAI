import { useState } from "react";
import { reanalyzeReport } from "../api/client";
import type { BatchStatus } from "../api/types";

export function DocumentAnalysisProgress({ batch, readOnly, refresh }: {
  batch: BatchStatus; readOnly: boolean; refresh: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const rerun = async (id: string, retry: boolean) => {
    setBusy(id); setError(null);
    try { await reanalyzeReport(id, retry); refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to queue analysis"); }
    finally { setBusy(null); }
  };
  return <div aria-live="polite">
    {batch.property_count != null && <p>{batch.property_count} properties available</p>}
    {error && <p role="alert">{error}</p>}
    {batch.documents?.map((doc) => {
      const covered = doc.coverage.filter((page) => page.status !== "unreadable").length;
      const terminal = ["complete", "partial", "needs_review", "failed", "paused_budget", "no_properties"].includes(doc.status);
      return <article className="callout" key={doc.report_id}>
        <strong>{doc.status === "no_properties" ? "No property information found" : doc.status.replace(/_/g, " ")}</strong>
        <p>{covered} / {doc.page_count} pages accounted for · {doc.entities.filter((entity) => entity.kind === "property").length} property records</p>
        {doc.issues.map((issue, i) => <p key={i}>{issue.message ?? issue.code.replace(/_/g, " ")}{issue.pages?.length ? ` — pages ${issue.pages.join(", ")}` : ""}</p>)}
        {doc.entities.map((entity) => <div key={entity.id}>
          <span>{entity.role} · pages {entity.pages.join(", ")} · {entity.status.replace(/_/g, " ")}</span>
          {entity.issues.map((issue, i) => <p key={i}>{issue.message ?? issue.code.replace(/_/g, " ")}</p>)}
        </div>)}
        {terminal && <div className="page-actions">
          {!["complete", "no_properties"].includes(doc.status) && <button className="btn btn-secondary" disabled={readOnly || busy === doc.report_id} onClick={() => rerun(doc.report_id, true)}>Retry incomplete work</button>}
          <button className="btn btn-secondary" disabled={readOnly || busy === doc.report_id} onClick={() => rerun(doc.report_id, false)}>Reanalyze PDF</button>
        </div>}
      </article>;
    })}
  </div>;
}
