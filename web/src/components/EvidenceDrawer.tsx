/**
 * Evidence drawer (spec §11.7 item 7): for one field path, shows the resolved
 * value, the resolution method, every competing candidate with source and
 * confidence, the grounding snippet and page, and any human overrides.
 * pdf.js in-document highlighting is out of scope here (no new deps); the
 * page number and verbatim snippet are shown so the source PDF can be opened
 * to the right page.
 */
import { useEffect, useState } from "react";
import { getEvidence, type EvidenceCandidate, type EvidenceResponse } from "../api";
import { ConfidenceBadge } from "./ConfidenceBadge";
import { MoneyText } from "./Money";
import { button, palette, table, td, th } from "./ui";

function CandidateRow(props: { candidate: EvidenceCandidate }) {
  const c = props.candidate;
  return (
    <tr>
      <td style={td}>
        <strong>{c.value_text ?? c.value_parsed ?? c.value_raw ?? "—"}</strong>
        {(c.is_resolved || c.is_winner) && (
          <span style={{ marginLeft: 6, fontSize: 11, color: palette.good, fontWeight: 600 }}>resolved</span>
        )}
        {c.snippet && (
          <div style={{ fontSize: 12, color: palette.muted, fontStyle: "italic", marginTop: 4 }}>
            “{c.snippet}”
          </div>
        )}
      </td>
      <td style={td}>{c.source_kind}</td>
      <td style={td}>
        <ConfidenceBadge confidence={c.extraction_confidence} sourceKind={c.source_kind} />
      </td>
      <td style={td}>{c.page_number ?? "—"}</td>
    </tr>
  );
}

export function EvidenceDrawer(props: { propertyId: string; fieldPath: string | null; onClose: () => void }) {
  const { propertyId, fieldPath, onClose } = props;
  const [state, setState] = useState<
    { status: "idle" } | { status: "loading" } | { status: "error"; message: string } | { status: "ok"; data: EvidenceResponse }
  >({ status: "idle" });

  useEffect(() => {
    if (fieldPath === null) {
      setState({ status: "idle" });
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    getEvidence(propertyId, fieldPath)
      .then((data) => !cancelled && setState({ status: "ok", data }))
      .catch((error: unknown) =>
        !cancelled &&
        setState({ status: "error", message: error instanceof Error ? error.message : "failed to load evidence" }),
      );
    return () => {
      cancelled = true;
    };
  }, [propertyId, fieldPath]);

  if (fieldPath === null) return null;

  return (
    <>
      <div
        onClick={onClose}
        style={{ position: "fixed", inset: 0, background: "rgba(15, 20, 30, 0.35)", zIndex: 40 }}
      />
      <aside
        role="dialog"
        aria-label={`Evidence for ${fieldPath}`}
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          bottom: 0,
          width: 420,
          maxWidth: "90vw",
          background: palette.surface,
          borderLeft: `1px solid ${palette.border}`,
          boxShadow: "-8px 0 24px rgba(0,0,0,0.12)",
          zIndex: 50,
          overflowY: "auto",
          padding: 20,
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
          <h3 style={{ margin: 0, fontSize: 15 }}>Evidence</h3>
          <button style={button} onClick={onClose}>
            Close
          </button>
        </div>
        <p style={{ fontSize: 12, color: palette.muted, fontFamily: "monospace", marginTop: 0 }}>{fieldPath}</p>

        {state.status === "loading" && <p style={{ color: palette.muted }}>Loading evidence…</p>}
        {state.status === "error" && <p style={{ color: palette.bad }}>{state.message}</p>}
        {state.status === "ok" && (
          <>
            <section style={{ marginBottom: 16 }}>
              <div style={{ fontSize: 12, color: palette.muted }}>Resolved value</div>
              <div style={{ fontSize: 20 }}>
                {state.data.resolved ? <MoneyText money={state.data.resolved} cents /> : (() => {
                  const winner = state.data.candidates.find((candidate) => candidate.is_winner || candidate.fact_id === state.data.resolution?.winning_fact_id);
                  return winner?.value_parsed ?? winner?.value_text ?? winner?.value_raw ?? "Not resolved";
                })()}
              </div>
              {(state.data.method || state.data.resolution?.method) && (
                <div style={{ fontSize: 12, color: palette.muted, marginTop: 4 }}>method: {state.data.method ?? state.data.resolution?.method}</div>
              )}
              {state.data.resolution && <div className="evidence-resolution"><span>Score {state.data.resolution.score ?? "—"}</span><span>{state.data.resolution.has_conflict ? "Conflict detected" : "No material conflict"}</span><span>{state.data.resolution.verification_state ?? "Unverified"}</span></div>}
            </section>

            <section style={{ marginBottom: 16 }}>
              <h4 style={{ margin: "0 0 8px", fontSize: 13 }}>Candidates ({state.data.candidates.length})</h4>
              {state.data.candidates.length === 0 ? (
                <p style={{ fontSize: 13, color: palette.muted }}>No extracted candidates recorded for this field.</p>
              ) : (
                <table style={table}>
                  <thead>
                    <tr>
                      <th style={th}>Value</th>
                      <th style={th}>Source</th>
                      <th style={th}>Confidence</th>
                      <th style={th}>Page</th>
                    </tr>
                  </thead>
                  <tbody>
                    {state.data.candidates.map((candidate, index) => (
                      <CandidateRow key={candidate.fact_id ?? index} candidate={candidate} />
                    ))}
                  </tbody>
                </table>
              )}
            </section>

            {(state.data.overrides?.length ?? 0) > 0 && (
              <section>
                <h4 style={{ margin: "0 0 8px", fontSize: 13 }}>Human overrides</h4>
                <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                  {state.data.overrides?.map((override, index) => (
                    <li key={index}>
                      {override.actor} {override.action}
                      {override.value ? ` → ${override.value}` : ""} on {override.at}
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </>
        )}
      </aside>
    </>
  );
}
