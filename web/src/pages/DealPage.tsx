/**
 * Property deal page (spec §11.7, WP-13). One `/analysis` call loads the full
 * payload; the scenario toggle and the offer slider then work locally with
 * zero further requests. Material numbers carry an evidence button that opens
 * the drawer for that field path.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  getAnalysis,
  SCENARIOS,
  type AnalysisPayload,
  type Scenario,
  type StrategyResult,
} from "../api";
import { EvidenceDrawer } from "../components/EvidenceDrawer";
import { formatDecimalString, formatPercentString, MoneyText } from "../components/Money";
import { OfferSimulator } from "../components/OfferSimulator";
import { parseScore, ScoreBar } from "../components/ScoreBar";
import {
  activeButton,
  button,
  card,
  cardTitle,
  mutedText,
  palette,
  severityColor,
  table,
  td,
  th,
} from "../components/ui";
import { Link } from "../router";

const STRATEGY_LABELS: Record<string, string> = {
  cash: "Cash",
  flip: "Fix & flip",
  wholesale: "Wholesale",
  rental: "Rental",
  subject_to: "Subject-to",
  foreclosure: "Foreclosure",
};

function EvidenceButton(props: { onEvidence: (fieldPath: string) => void; fieldPath: string }) {
  return (
    <button
      style={{ ...button, border: "none", background: "transparent", padding: "0 4px", fontSize: 11, color: palette.accent }}
      title="Show the evidence behind this figure"
      onClick={() => props.onEvidence(props.fieldPath)}
    >
      evidence
    </button>
  );
}

function SummaryStat(props: { label: string; children: ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 12, color: palette.muted }}>{props.label}</div>
      <div style={{ fontSize: 17, fontWeight: 600 }}>{props.children}</div>
    </div>
  );
}

export function DealPage(props: { propertyId: string }) {
  const { propertyId } = props;
  const [payload, setPayload] = useState<AnalysisPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scenario, setScenario] = useState<Scenario>("expected");
  const [evidenceField, setEvidenceField] = useState<string | null>(null);
  const [activeStrategy, setActiveStrategy] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getAnalysis(propertyId)
      .then((data) => {
        if (cancelled) return;
        setPayload(data);
        setScenario(data.scenario ?? "expected");
      })
      .catch((err: unknown) => !cancelled && setError(err instanceof Error ? err.message : "failed to load analysis"));
    return () => {
      cancelled = true;
    };
  }, [propertyId]);

  const strategiesForScenario = useMemo(
    () => (payload?.strategies ?? []).filter((s) => s.scenario === scenario),
    [payload, scenario],
  );

  if (error) {
    return (
      <section style={card}>
        <p style={{ color: palette.bad }}>{error}</p>
        <Link to="/" style={{ color: palette.accent }}>
          ← Back to portfolio
        </Link>
      </section>
    );
  }
  if (!payload) return <p style={mutedText}>Loading analysis…</p>;

  const { normalized, underwriting, scores, offers } = payload;
  const equity = underwriting?.equity?.[scenario];
  const costs = underwriting?.costs?.[scenario];
  const address = normalized?.address;
  const addressLine = address
    ? [address.line1, [address.city, address.state].filter(Boolean).join(", "), address.zip5]
        .filter(Boolean)
        .join(" · ")
    : propertyId;

  const recommendation = scores?.recommended_strategy
    ? [scores.recommended_strategy, ...scores.recommended_alternatives]
        .map((s) => STRATEGY_LABELS[s] ?? s)
        .join(" / ")
    : null;

  return (
    <section>
      <p style={{ margin: "0 0 8px" }}>
        <Link to="/" style={{ color: palette.accent }}>
          ← Portfolio
        </Link>
      </p>
      <h2 style={{ margin: "0 0 4px", fontSize: 20 }}>{addressLine || "Unknown address"}</h2>
      <p style={{ ...mutedText, marginTop: 0 }}>
        {normalized?.apn ? `APN ${normalized.apn}` : "APN missing"}
        {payload.flags.length > 0 && ` · ${payload.flags.length} open flag${payload.flags.length === 1 ? "" : "s"}`}
      </p>

      {!underwriting && (
        <section style={card}>
          <p style={mutedText}>
            Analysis has not been computed for this property yet — valuation, underwriting, offers, and scores will
            appear here once the pipeline finishes.
          </p>
        </section>
      )}

      {/* 1. Executive summary */}
      {underwriting && (
        <section style={card}>
          <h3 style={cardTitle}>Executive summary</h3>
          <div style={{ display: "flex", gap: 28, flexWrap: "wrap" }}>
            <SummaryStat label="Est. value (expected)">
              {formatDecimalString(underwriting.value.v_expected)}
              <EvidenceButton onEvidence={setEvidenceField} fieldPath="valuation.v_expected" />
            </SummaryStat>
            <SummaryStat label="Confirmed debt">
              {formatDecimalString(underwriting.liabilities.confirmed)}
              <EvidenceButton onEvidence={setEvidenceField} fieldPath="liabilities.confirmed" />
            </SummaryStat>
            <SummaryStat label="Potential obligations">
              {formatDecimalString(underwriting.liabilities.potential)}
            </SummaryStat>
            <SummaryStat label={`Equity (${scenario})`}>
              {formatDecimalString(equity?.adjusted)}
              {equity?.equity_pct !== null && equity?.equity_pct !== undefined && (
                <span style={{ fontSize: 13, color: palette.muted }}> ({formatPercentString(equity.equity_pct)})</span>
              )}
            </SummaryStat>
            {scores && (
              <>
                <SummaryStat label="Distress">
                  <ScoreBar value={parseScore(scores.distress) ?? 0} />
                </SummaryStat>
                <SummaryStat label="Confidence">
                  <ScoreBar value={parseScore(scores.data_confidence) ?? 0} />
                </SummaryStat>
              </>
            )}
          </div>
          {recommendation && (
            <p style={{ marginBottom: 0 }}>
              <strong>Recommended:</strong> {recommendation}
              {!scores?.is_rankable && (
                <span style={{ color: palette.bad, marginLeft: 8 }}>not rankable until gating flags are resolved</span>
              )}
            </p>
          )}
          {underwriting.status === "insufficient_data" && (
            <p style={{ color: palette.warn }}>Underwriting: insufficient data — {underwriting.unavailable_reason}</p>
          )}
        </section>
      )}

      {/* 2. Scenario toggle — local state, no refetch */}
      {underwriting && (
        <section style={card}>
          <h3 style={cardTitle}>Scenario</h3>
          <div style={{ display: "flex", gap: 6 }}>
            {SCENARIOS.map((s) => (
              <button key={s} style={s === scenario ? activeButton : button} onClick={() => setScenario(s)}>
                {s}
              </button>
            ))}
          </div>
          <div style={{ display: "flex", gap: 28, flexWrap: "wrap", marginTop: 12 }}>
            <SummaryStat label="Value band">
              {formatDecimalString(underwriting.value.v_low)} – {formatDecimalString(underwriting.value.v_high)}
            </SummaryStat>
            <SummaryStat label={`Value (${scenario})`}>
              {formatDecimalString(underwriting.value.arv_by_scenario?.[scenario])}
            </SummaryStat>
            <SummaryStat label="Net realizable equity">{formatDecimalString(equity?.net_realizable)}</SummaryStat>
          </div>
          {costs && (
            <table style={{ ...table, marginTop: 12 }}>
              <thead>
                <tr>
                  <th style={th}>Acquisition</th>
                  <th style={th}>Repairs</th>
                  <th style={th}>Holding</th>
                  <th style={th}>Resale</th>
                  <th style={th}>Financing</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td style={td}>{formatDecimalString(costs.acquisition)}</td>
                  <td style={td}>{formatDecimalString(costs.repairs)}</td>
                  <td style={td}>{formatDecimalString(costs.holding)}</td>
                  <td style={td}>{formatDecimalString(costs.resale)}</td>
                  <td style={td}>{formatDecimalString(costs.financing)}</td>
                </tr>
              </tbody>
            </table>
          )}
        </section>
      )}

      {/* 3. Financial breakdown */}
      {normalized && (
        <section style={card}>
          <h3 style={cardTitle}>Financial breakdown</h3>
          {normalized.valuation_candidates.length > 0 && (
            <>
              <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>Value candidates</h4>
              <table style={{ ...table, marginBottom: 16 }}>
                <thead>
                  <tr>
                    <th style={th}>Type</th>
                    <th style={th}>Value</th>
                    <th style={th}>Range</th>
                    <th style={th}>As of</th>
                    <th style={th}>Weight</th>
                  </tr>
                </thead>
                <tbody>
                  {normalized.valuation_candidates.map((candidate, index) => (
                    <tr key={index}>
                      <td style={td}>{candidate.valuation_type.replace(/_/g, " ")}</td>
                      <td style={td}>
                        <MoneyText money={candidate.value} />
                        <EvidenceButton onEvidence={setEvidenceField} fieldPath={`valuation_candidates.${index}.value`} />
                      </td>
                      <td style={td}>
                        {candidate.value_low || candidate.value_high
                          ? `${formatDecimalString(candidate.value_low)} – ${formatDecimalString(candidate.value_high)}`
                          : "—"}
                      </td>
                      <td style={td}>{candidate.as_of ?? "—"}</td>
                      <td style={td}>{candidate.weight_hint ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          {normalized.mortgages.length > 0 && (
            <>
              <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>Debt</h4>
              <table style={{ ...table, marginBottom: 16 }}>
                <thead>
                  <tr>
                    <th style={th}>Position</th>
                    <th style={th}>Lender</th>
                    <th style={th}>Original</th>
                    <th style={th}>Est. balance</th>
                    <th style={th}>Method</th>
                    <th style={th}>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {normalized.mortgages.map((mortgage, index) => (
                    <tr key={index}>
                      <td style={td}>{mortgage.position}</td>
                      <td style={td}>{mortgage.lender ?? "—"}</td>
                      <td style={td}>
                        <MoneyText money={mortgage.original_amount} />
                      </td>
                      <td style={td}>
                        <MoneyText money={mortgage.estimated_balance} />
                        <EvidenceButton
                          onEvidence={setEvidenceField}
                          fieldPath={`mortgages.${index}.estimated_balance`}
                        />
                      </td>
                      <td style={td}>{mortgage.balance_method.replace(/_/g, " ")}</td>
                      <td style={td}>{mortgage.is_open ? "open" : "closed"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          {normalized.liens.length > 0 && (
            <>
              <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>Liens</h4>
              <table style={table}>
                <thead>
                  <tr>
                    <th style={th}>Type</th>
                    <th style={th}>Amount</th>
                    <th style={th}>Attachment</th>
                    <th style={th}>Status</th>
                    <th style={th}>Recorded</th>
                  </tr>
                </thead>
                <tbody>
                  {normalized.liens.map((lien, index) => (
                    <tr key={index}>
                      <td style={td}>{lien.lien_type.replace(/_/g, " ")}</td>
                      <td style={td}>
                        <MoneyText money={lien.amount} />
                        <EvidenceButton onEvidence={setEvidenceField} fieldPath={`liens.${index}.amount`} />
                      </td>
                      <td style={td}>
                        <span
                          style={{
                            fontSize: 12,
                            color:
                              lien.attachment_basis === "recorded_against_property"
                                ? palette.good
                                : lien.attachment_basis === "owner_named_only"
                                  ? palette.warn
                                  : palette.muted,
                          }}
                          title={`attachment confidence ${Math.round(lien.attachment_confidence * 100)}%`}
                        >
                          {lien.attachment_basis.replace(/_/g, " ")}
                        </span>
                      </td>
                      <td style={td}>{lien.status}</td>
                      <td style={td}>{lien.recording_date ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </section>
      )}

      {/* 4. Strategy tabs */}
      {strategiesForScenario.length > 0 && (
        <section style={card}>
          <h3 style={cardTitle}>Strategies ({scenario})</h3>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 12 }}>
            {strategiesForScenario.map((result) => (
              <button
                key={result.strategy}
                style={(activeStrategy ?? strategiesForScenario[0].strategy) === result.strategy ? activeButton : button}
                onClick={() => setActiveStrategy(result.strategy)}
              >
                {STRATEGY_LABELS[result.strategy] ?? result.strategy}
              </button>
            ))}
          </div>
          {strategiesForScenario
            .filter((result) => result.strategy === (activeStrategy ?? strategiesForScenario[0].strategy))
            .map((result: StrategyResult) => (
              <div key={result.strategy}>
                <p style={{ margin: "0 0 8px" }}>
                  <strong style={{ color: result.status === "viable" ? palette.good : palette.muted }}>
                    {result.status.replace(/_/g, " ")}
                  </strong>
                  {result.unavailable_reason && (
                    <span style={{ ...mutedText, marginLeft: 8 }}>{result.unavailable_reason}</span>
                  )}
                </p>
                <div style={{ display: "flex", gap: 28, flexWrap: "wrap" }}>
                  <SummaryStat label="MAO">{formatDecimalString(result.mao)}</SummaryStat>
                  <SummaryStat label="All-in basis">{formatDecimalString(result.all_in_basis)}</SummaryStat>
                  <SummaryStat label="Profit">{formatDecimalString(result.profit)}</SummaryStat>
                  <SummaryStat label="ROI">{formatPercentString(result.roi)}</SummaryStat>
                  <SummaryStat label="Margin of safety">{formatPercentString(result.margin_of_safety)}</SummaryStat>
                </div>
                {result.notices.length > 0 && (
                  <ul style={{ margin: "10px 0 0", paddingLeft: 18, fontSize: 13, color: palette.warn }}>
                    {result.notices.map((notice, index) => (
                      <li key={index}>{notice}</li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
        </section>
      )}

      {/* 5. Offer simulator */}
      {offers && (
        <section style={card}>
          <h3 style={cardTitle}>Offer simulator</h3>
          <OfferSimulator grid={offers} scenario={scenario} propertyId={propertyId} />
        </section>
      )}

      {/* 6. Distress timeline */}
      {payload.timeline.length > 0 && (
        <section style={card}>
          <h3 style={cardTitle}>Timeline</h3>
          <ul style={{ margin: 0, paddingLeft: 0, listStyle: "none" }}>
            {payload.timeline.map((event, index) => (
              <li
                key={index}
                style={{ display: "flex", gap: 12, padding: "6px 0", borderBottom: `1px solid ${palette.subtle}` }}
              >
                <span style={{ width: 96, color: palette.muted, fontSize: 13, flexShrink: 0 }}>
                  {event.date ?? "—"}
                </span>
                <span style={{ flex: 1 }}>
                  <strong style={{ fontSize: 13 }}>{event.kind.replace(/_/g, " ")}</strong> — {event.label}
                </span>
                <span style={{ fontSize: 13 }}>
                  <MoneyText money={event.amount} />
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* 7. Scores with sub-components */}
      {scores && (
        <section style={card}>
          <h3 style={cardTitle}>Scores</h3>
          <div style={{ display: "flex", gap: 28, flexWrap: "wrap", marginBottom: 10 }}>
            <SummaryStat label="Overall">
              <ScoreBar value={parseScore(scores.overall) ?? 0} />
            </SummaryStat>
            <SummaryStat label="Financial opportunity">
              <ScoreBar value={parseScore(scores.fos) ?? 0} />
            </SummaryStat>
            <SummaryStat label="Distress">
              <ScoreBar value={parseScore(scores.distress) ?? 0} />
            </SummaryStat>
            <SummaryStat label="Data confidence">
              <ScoreBar value={parseScore(scores.data_confidence) ?? 0} />
            </SummaryStat>
            <SummaryStat label="Risk">
              <ScoreBar value={parseScore(scores.risk) ?? 0} />
            </SummaryStat>
          </div>
          {Object.keys(scores.components).length > 0 && (
            <details>
              <summary style={{ cursor: "pointer", fontSize: 13, color: palette.muted }}>Sub-components</summary>
              <table style={{ ...table, marginTop: 8 }}>
                <tbody>
                  {Object.entries(scores.components).map(([name, value]) => (
                    <tr key={name}>
                      <td style={td}>{name.replace(/_/g, " ")}</td>
                      <td style={{ ...td, fontVariantNumeric: "tabular-nums" }}>{value}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          )}
          {scores.gates_applied.length > 0 && (
            <p style={{ fontSize: 13, color: palette.bad, marginBottom: 0 }}>
              Gates applied: {scores.gates_applied.join(", ")}
            </p>
          )}
        </section>
      )}

      {/* 8. Flags */}
      {payload.flags.length > 0 && (
        <section style={card}>
          <h3 style={cardTitle}>Flags</h3>
          <ul style={{ margin: 0, paddingLeft: 0, listStyle: "none" }}>
            {[...payload.flags]
              .sort((a, b) => Number(b.financial_impact ?? 0) - Number(a.financial_impact ?? 0))
              .map((flag, index) => (
                <li key={index} style={{ padding: "6px 0", borderBottom: `1px solid ${palette.subtle}`, fontSize: 14 }}>
                  <strong style={{ color: severityColor(flag.severity) }}>{flag.type.replace(/_/g, " ")}</strong>
                  {flag.is_gating && <span style={{ marginLeft: 8, fontSize: 12, color: palette.bad }}>gating</span>}
                  {flag.financial_impact !== null && flag.financial_impact !== undefined && (
                    <span style={{ marginLeft: 8, fontSize: 12, color: palette.muted }}>
                      impact {formatDecimalString(flag.financial_impact)}
                    </span>
                  )}
                </li>
              ))}
          </ul>
        </section>
      )}

      {/* 7 (drawer). Evidence */}
      <EvidenceDrawer propertyId={propertyId} fieldPath={evidenceField} onClose={() => setEvidenceField(null)} />
    </section>
  );
}
