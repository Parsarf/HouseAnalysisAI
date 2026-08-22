/**
 * Explainability UI (audit-trace system): a consistent "Explain" control and
 * drawer for every material figure. The drawer renders the structured
 * ExplanationTrace returned by GET /api/properties/{id}/explain/{key} — the
 * frontend never re-implements formulas.
 */
import { useEffect, useState } from "react";
import { getExplanation, getReportSourcePage, type ExplanationSource, type ExplanationTrace } from "../api/explain";
import { button, palette } from "./ui";

const KIND_COLORS: Record<string, string> = {
  reported: palette.good,
  extracted: palette.good,
  manual: palette.accent,
  resolved: palette.accent,
  derived: palette.warn,
  estimated: palette.warn,
  calculated: "#1c2430",
};

function KindChip(props: { kind: string }) {
  return (
    <span
      style={{
        fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.04em",
        color: KIND_COLORS[props.kind] ?? palette.muted,
        border: `1px solid ${palette.subtle}`, borderRadius: 10, padding: "1px 8px",
      }}
      title={`This figure is ${props.kind}`}
    >
      {props.kind}
    </span>
  );
}

function Section(props: { title: string; children: React.ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(props.defaultOpen ?? true);
  return (
    <div style={{ borderTop: `1px solid ${palette.subtle}`, paddingTop: 10, marginTop: 10 }}>
      <button
        onClick={() => setOpen(!open)}
        style={{ ...button, border: "none", background: "transparent", padding: 0, fontSize: 12, fontWeight: 700, color: palette.muted, cursor: "pointer" }}
      >
        {open ? "▾ " : "▸ "}
        {props.title}
      </button>
      {open && <div style={{ marginTop: 8 }}>{props.children}</div>}
    </div>
  );
}

function SourceRow(props: { source: ExplanationSource }) {
  const s = props.source;
  const [pageText, setPageText] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const viewSource = async () => {
    if (!s.report_id) return;
    setLoading(true);
    try {
      const page = await getReportSourcePage(s.report_id, s.page_number ?? 1);
      setPageText(page.text);
    } catch {
      setPageText("(source page could not be loaded)");
    } finally {
      setLoading(false);
    }
  };
  return (
    <div style={{ borderBottom: `1px solid ${palette.subtle}`, padding: "8px 0" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
        <strong style={{ fontSize: 13 }}>{s.report_name ?? "Source report"}</strong>
        {s.is_winner && <span style={{ fontSize: 11, color: palette.good, fontWeight: 700 }}>winning fact</span>}
        {!s.is_active && <span style={{ fontSize: 11, color: palette.muted }}>inactive</span>}
        {s.is_superseded && <span style={{ fontSize: 11, color: palette.muted }}>superseded</span>}
      </div>
      <div style={{ fontSize: 12, color: palette.muted }}>
        {[s.vendor, s.report_type].filter(Boolean).join(" · ") || "—"}
        {s.page_number != null && ` · page ${s.page_number}`}
        {s.ocr_applied ? " · OCR" : ""}
      </div>
      {s.snippet && (
        <div style={{ fontSize: 12, fontStyle: "italic", margin: "4px 0" }}>“{s.snippet}”</div>
      )}
      <div style={{ fontSize: 12 }}>
        {s.value_raw && <>raw: <code>{s.value_raw}</code>{" · "}</>}
        {s.value_parsed && <>parsed: <code>{s.value_parsed}</code>{" · "}</>}
        {s.extraction_confidence != null && <>extraction confidence: {(s.extraction_confidence * 100).toFixed(0)}%</>}
      </div>
      <div style={{ fontSize: 11, color: palette.muted }}>
        Extraction confidence reflects the system's confidence in the extracted information,
        not a guaranteed probability that the underlying fact is valid.
      </div>
      {s.source_url?.startsWith("/api/") && (
        <button className="btn btn-ghost btn-small" disabled={loading} onClick={viewSource}>
          {loading ? "Loading…" : `View source${s.page_number ? ` (page ${s.page_number})` : ""}`}
        </button>
      )}
      {pageText !== null && (
        <pre style={{ maxHeight: 220, overflow: "auto", background: "#f6f7f9", padding: 8, fontSize: 11, whiteSpace: "pre-wrap" }}>
          {pageText}
        </pre>
      )}
    </div>
  );
}

export function ExplanationDrawer(props: { propertyId: string; explainKey: string; onClose: () => void }) {
  const { propertyId, explainKey } = props;
  const [trace, setTrace] = useState<ExplanationTrace | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setTrace(null);
    setError(null);
    getExplanation(propertyId, explainKey)
      .then((data) => !cancelled && setTrace(data))
      .catch((reason: unknown) => !cancelled && setError(reason instanceof Error ? reason.message : "failed to load explanation"));
    return () => { cancelled = true; };
  }, [propertyId, explainKey]);

  return (
    <div
      role="dialog" aria-modal="true" aria-label="Figure explanation"
      style={{
        position: "fixed", inset: 0, background: "rgba(15,20,30,0.45)", zIndex: 60,
        display: "flex", justifyContent: "flex-end",
      }}
      onClick={(event) => { if (event.target === event.currentTarget) props.onClose(); }}
    >
      <div
        style={{
          width: 520, maxWidth: "94vw", height: "100%", overflowY: "auto", background: "#fff",
          padding: "18px 20px", boxShadow: "-8px 0 30px rgba(0,0,0,0.2)",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "start", gap: 8 }}>
          <h3 style={{ margin: 0, fontSize: 17 }}>{trace?.title ?? "Explanation"}</h3>
          <button className="btn btn-ghost btn-small" onClick={props.onClose}>Close</button>
        </div>

        {error && <p style={{ color: palette.bad }}>{error}</p>}
        {!trace && !error && <p style={muted}>Loading…</p>}

        {trace && (
          <>
            <div style={{ display: "flex", alignItems: "baseline", gap: 10, margin: "10px 0 4px" }}>
              <span style={{ fontSize: 26, fontWeight: 700 }}>
                {trace.display_value ?? (trace.value == null ? "—" : String(trace.value))}
              </span>
              <KindChip kind={trace.value_kind} />
            </div>
            <p style={{ color: palette.muted, fontSize: 13, margin: "0 0 6px" }}>{trace.description}</p>

            {(trace.warnings.length > 0 || trace.unresolved_dependencies.length > 0) && (
              <div className="inline-warning" style={{ background: "#fff7e6", border: `1px solid #f0d9a8`, borderRadius: 8, padding: "8px 10px", margin: "8px 0" }}>
                {trace.warnings.map((warning, index) => <div key={index} style={{ fontSize: 13 }}>⚠ {warning}</div>)}
                {trace.unresolved_dependencies.map((dep, index) => (
                  <div key={`u${index}`} style={{ fontSize: 13, color: palette.bad }}>Unresolved: {dep}</div>
                ))}
              </div>
            )}

            {trace.confidence != null && (
              <Section title="Confidence">
                <div style={{ fontSize: 13 }}>Valuation / model confidence: <strong>{Number(trace.confidence).toFixed(2)}</strong></div>
                {trace.data_confidence != null && (
                  <div style={{ fontSize: 13 }}>Mean extraction confidence of underlying facts: <strong>{Number(trace.data_confidence).toFixed(2)}</strong></div>
                )}
                <div style={{ fontSize: 11, color: palette.muted }}>
                  These reflect the system's confidence in the extracted information, not a guaranteed probability.
                </div>
              </Section>
            )}

            {trace.formula && (
              <Section title="How ACQ calculated it">
                <div style={{ fontSize: 12, fontFamily: "monospace", background: "#f6f7f9", padding: 8, borderRadius: 6 }}>
                  {trace.formula_display ?? trace.formula}
                </div>
                {trace.engine && (
                  <div style={{ fontSize: 11, color: palette.muted, marginTop: 4 }}>
                    Engine: {trace.engine} {trace.engine_version ? `(${trace.engine_version})` : ""}
                  </div>
                )}
                {trace.steps.map((step) => (
                  <div key={step.order} style={{ borderTop: `1px dashed ${palette.subtle}`, paddingTop: 6, marginTop: 6 }}>
                    <strong style={{ fontSize: 13 }}>{step.label}</strong>
                    {step.substitution && (
                      <div style={{ fontSize: 12, fontFamily: "monospace", color: palette.muted }}>{step.substitution} → <strong>{step.display_result ?? String(step.result)}</strong></div>
                    )}
                  </div>
                ))}
              </Section>
            )}

            {trace.inputs.length > 0 && (
              <Section title="Inputs">
                <table className="data-table" style={{ width: "100%", fontSize: 13 }}>
                  <tbody>
                    {trace.inputs.map((input, index) => (
                      <tr key={index}>
                        <td>{input.name}</td>
                        <td style={{ textAlign: "right" }}><strong>{input.display_value ?? String(input.value)}</strong>{input.note ? <div style={{ fontSize: 11, color: palette.muted }}>{input.note}</div> : null}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Section>
            )}

            {trace.assumptions.length > 0 && (
              <Section title="Assumptions used">
                <ul style={{ margin: 0, paddingLeft: 16, fontSize: 13 }}>
                  {trace.assumptions.map((assumption, index) => (
                    <li key={index}>
                      {assumption.name}: <strong>{assumption.display_value ?? String(assumption.value)}</strong>
                      {assumption.note ? <span style={{ color: palette.muted }}> — {assumption.note}</span> : null}
                    </li>
                  ))}
                </ul>
              </Section>
            )}

            {trace.candidates.length > 0 && (
              <Section title="Competing values">
                <table className="data-table" style={{ width: "100%", fontSize: 13 }}>
                  <thead><tr><th>Value</th><th>Origin</th><th>Confidence</th><th>Outcome</th></tr></thead>
                  <tbody>
                    {trace.candidates.map((candidate, index) => (
                      <tr key={index}>
                        <td><strong>{candidate.display_value ?? String(candidate.value)}</strong>{candidate.reason ? <div style={{ fontSize: 11, color: palette.muted }}>{candidate.reason}</div> : null}</td>
                        <td>{candidate.origin ?? "—"}</td>
                        <td>{candidate.confidence != null ? Number(candidate.confidence).toFixed(2) : "—"}</td>
                        <td>{candidate.is_winner ? <span style={{ color: palette.good, fontWeight: 700 }}>selected</span> : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Section>
            )}

            {trace.resolution && (
              <Section title={`Resolution (${trace.resolution.method ?? "method"})`}>
                <div style={{ fontSize: 13 }}>{trace.resolution.winner_description}</div>
                {trace.resolution.reason && <div style={{ fontSize: 13, color: palette.muted }}>{trace.resolution.reason}</div>}
              </Section>
            )}

            {trace.conflicts.length > 0 && (
              <Section title="Material conflicts">
                {trace.conflicts.map((conflict, index) => (
                  <div key={index} style={{ fontSize: 13 }}>⚡ {conflict.description}{conflict.magnitude ? ` (magnitude ${conflict.magnitude})` : ""}</div>
                ))}
              </Section>
            )}

            {trace.sensitivity.length > 0 && (
              <Section title="What could change this?">
                <ul style={{ margin: 0, paddingLeft: 16, fontSize: 13 }}>
                  {trace.sensitivity.map((entry, index) => (
                    <li key={index}><strong>{entry.question}</strong> {entry.effect}</li>
                  ))}
                </ul>
              </Section>
            )}

            {trace.source_facts.length > 0 && (
              <Section title={`Source evidence (${trace.source_facts.length})`}>
                {trace.source_facts.map((source, index) => <SourceRow key={source.fact_id ?? index} source={source} />)}
              </Section>
            )}

            {trace.children.length > 0 && (
              <Section title="Related figures" defaultOpen={false}>
                {trace.children.map((child) => (
                  <ChildRow key={child.key} propertyId={propertyId} child={child} />
                ))}
              </Section>
            )}

            <Section title="Technical details" defaultOpen={false}>
              <pre style={{ fontSize: 11, overflowX: "auto", background: "#f6f7f9", padding: 8, borderRadius: 6 }}>
                {JSON.stringify({ key: trace.key, engine: trace.engine, engine_version: trace.engine_version, formula: trace.formula, assumption_set_id: trace.assumption_set_id, scoring_config_id: trace.scoring_config_id, computed_at: trace.computed_at }, null, 2)}
              </pre>
            </Section>
          </>
        )}
      </div>
    </div>
  );
}

function ChildRow(props: { propertyId: string; child: ExplanationTrace }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "4px 0" }}>
      <span style={{ fontSize: 13 }}>
        {props.child.title}
        {props.child.display_value ? <> — <strong>{props.child.display_value}</strong></> : null}
      </span>
      <ExplainButtonInline propertyId={props.propertyId} explainKey={props.child.key} />
    </div>
  );
}

function ExplainButtonInline(props: { propertyId: string; explainKey: string }) {
  // Child rows swap the parent drawer content via a nested open state.
  const [open, setOpen] = useState(false);
  return (
    <>
      <button style={{ ...button, border: "none", background: "transparent", padding: "0 4px", fontSize: 11, color: palette.accent }} onClick={() => setOpen(true)}>
        explain
      </button>
      {open && <ExplanationDrawer propertyId={props.propertyId} explainKey={props.explainKey} onClose={() => setOpen(false)} />}
    </>
  );
}

const muted = { color: palette.muted, fontSize: 13 };

export function ExplainButton(props: { propertyId: string; explainKey: string; label?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        style={{ ...button, border: "none", background: "transparent", padding: "0 4px", fontSize: 11, color: palette.accent }}
        title="Show how this number was produced, its sources and assumptions"
        onClick={() => setOpen(true)}
      >
        {props.label ?? "explain"}
      </button>
      {open && <ExplanationDrawer propertyId={props.propertyId} explainKey={props.explainKey} onClose={() => setOpen(false)} />}
    </>
  );
}
