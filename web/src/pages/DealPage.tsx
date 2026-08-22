/**
 * Property deal page (spec §11.7, WP-13). One `/analysis` call loads the full
 * payload; the scenario toggle and the offer slider then work locally with
 * zero further requests. Material numbers carry an evidence button that opens
 * the drawer for that field path.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  createNote,
  createOutreachDraft,
  getAnalysis,
  getProperty,
  listNotes,
  listReports,
  mergeProperties,
  openDealSheet,
  openNetSheet,
  recomputeProperty,
  restoreProperty,
  updateOutreachDraft,
  resolveFlag,
  SCENARIOS,
  submitFact,
  unmergeProperties,
  updateProperty,
  type AnalysisPayload,
  type FactSubmission,
  type NoteRecord,
  type OutreachDraft,
  type PropertyListItem,
  type ReportRecord,
  type Scenario,
  type StrategyResult,
} from "../api";
import { useAuth } from "../auth";
import { EvidenceDrawer } from "../components/EvidenceDrawer";
import { formatPercentString, MoneyText } from "../components/Money";
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

function money(value: string | null | undefined, estimated = false) {
  return value == null ? null : { value, confidence: 1, source_kind: estimated ? "derived" as const : "report" as const, is_estimated: estimated };
}

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
  const { user } = useAuth();
  const { propertyId } = props;
  const [payload, setPayload] = useState<AnalysisPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scenario, setScenario] = useState<Scenario>("expected");
  const [evidenceField, setEvidenceField] = useState<string | null>(null);
  const [activeStrategy, setActiveStrategy] = useState<string | null>(null);
  const [property, setProperty] = useState<PropertyListItem | null>(null);
  const [notes, setNotes] = useState<NoteRecord[]>([]);
  const [reports, setReports] = useState<ReportRecord[]>([]);
  const [noteBody, setNoteBody] = useState("");
  const [operation, setOperation] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionPanel, setActionPanel] = useState<"override" | "merge" | "unmerge" | "net" | null>(null);
  const [draft, setDraft] = useState<OutreachDraft | null>(null);
  const [drafting, setDrafting] = useState(false);
  const [selectedEmail, setSelectedEmail] = useState("");

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

  useEffect(() => {
    let live = true;
    Promise.all([getProperty(propertyId), listNotes(propertyId), listReports(propertyId)])
      .then(([propertyData, noteData, reportData]) => { if (live) { setProperty(propertyData); setNotes(noteData.items); setReports(reportData.items); } })
      .catch((reason: unknown) => live && setActionError(reason instanceof Error ? reason.message : "Unable to load property operations"));
    return () => { live = false; };
  }, [propertyId]);

  const strategiesForScenario = useMemo(
    () => (payload?.strategies ?? []).filter((s) => s.scenario === scenario),
    [payload, scenario],
  );

  useEffect(() => {
    if (activeStrategy && !strategiesForScenario.some((result) => result.strategy === activeStrategy)) setActiveStrategy(null);
  }, [activeStrategy, strategiesForScenario]);

  const saveProperty = async (changes: Parameters<typeof updateProperty>[1]) => {
    if (!property) return;
    const before = property;
    const optimistic = { ...property, ...changes, ...(changes.pipeline_status ? { pipeline_status: changes.pipeline_status, status: changes.pipeline_status } : {}) };
    setProperty(optimistic);
    setActionError(null);
    try { setProperty({ ...optimistic, ...(await updateProperty(propertyId, changes)) }); setOperation("Property updated"); }
    catch (reason) { setProperty(before); setActionError(reason instanceof Error ? reason.message : "Update failed"); }
  };

  const addNote = async () => {
    if (!noteBody.trim()) return;
    setActionError(null);
    try { const note = await createNote(propertyId, noteBody.trim()); setNotes((items) => [note, ...items]); setNoteBody(""); }
    catch (reason) { setActionError(reason instanceof Error ? reason.message : "Unable to save note"); }
  };
  const draftOutreach = async (instruction?: string) => {
    setDrafting(true); setActionError(null);
    try { setDraft(await createOutreachDraft(propertyId, draft ? { offer_price: draft.offer_price, prior_draft: { subject: draft.subject, body: draft.body }, instruction } : {})); setSelectedEmail(""); }
    catch (reason) { setActionError(reason instanceof Error ? reason.message : "Unable to create draft"); }
    finally { setDrafting(false); }
  };
  const saveDraft = async (status: "draft" | "sent" = "draft") => {
    if (!draft) return;
    await updateOutreachDraft(propertyId, draft.draft_id, {
      subject: draft.subject, body: draft.body,
      recipient: selectedEmail || null, status,
    });
    setOperation(status === "sent" ? "Sent copy recorded" : "Draft changes saved");
  };
  const openGmail = async () => {
    if (!draft || !selectedEmail) return;
    const compose = window.open("about:blank", "_blank");
    try {
      await saveDraft("draft");
      const url = `https://mail.google.com/mail/?view=cm&fs=1&to=${encodeURIComponent(selectedEmail)}&su=${encodeURIComponent(draft.subject)}&body=${encodeURIComponent(draft.body)}`;
      if (compose) compose.location.href = url; else window.open(url, "_blank");
    } catch (reason) {
      compose?.close();
      setActionError(reason instanceof Error ? reason.message : "Unable to save outreach draft");
    }
  };

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
  const ownerProfile = payload.owner_profile as undefined | {
    owners: Array<{full_name:string;mailing_address?:string|null}>;
    contacts: Array<{id:string;kind:string;value:string;source:string;confidence?:string|null;association_warning?:string|null}>;
    liens: Array<{id:string;type?:string;amount?:string;recording_date?:string;status?:string}>;
    bankruptcies: Array<{id:string;chapter?:string;case_number?:string;filing_date?:string;status?:string;filing_sequence?:number}>;
    serial_filing: {dismissed_count:number;near_scheduled_sale:boolean;window_days:number};
    timeline: Array<{kind:string;date?:string;label:string;status?:string;sale_date?:string}>;
    equity_if_owner_liens_attach?:string|null; owner_lien_total?:string;
  };

  return (
    <section>
      <p style={{ margin: "0 0 8px" }}>
        <Link to="/" style={{ color: palette.accent }}>
          ← Portfolio
        </Link>
      </p>
      <div className="deal-heading">
        <div>
          <div className="eyebrow">Deal workspace</div>
          <h1>{addressLine || "Unknown address"}</h1>
        </div>
        <div className="deal-heading-actions">
          {property?.rank && <span className="deal-rank"><small>Portfolio rank</small><strong>#{property.rank}</strong></span>}
          <Link className="btn btn-secondary" to={`/chat?property=${propertyId}`}>Ask about this property</Link>
          <button className="btn btn-secondary" disabled={user.read_only || drafting} onClick={() => draftOutreach()}>{drafting ? "Drafting…" : "Draft cash-offer email"}</button>
          <button className="btn btn-secondary" onClick={() => openDealSheet(propertyId).catch((reason: Error) => setActionError(reason.message))}>Open deal sheet</button>
          <button className="btn btn-primary" disabled={user.read_only} onClick={() => { setOperation("Queueing recompute…"); recomputeProperty(propertyId).then(() => setOperation("Recompute queued")).catch((reason: Error) => setActionError(reason.message)); }}>Recompute</button>
        </div>
      </div>
      {property?.archived_at && <div className="archived-banner"><strong>Archived property</strong><span>This record remains available by direct URL.</span><button className="btn btn-secondary btn-small" disabled={user.read_only} onClick={() => restoreProperty(propertyId).then(() => setProperty({...property,archived_at:null}))}>Restore</button></div>}
      <p style={{ ...mutedText, marginTop: 0 }}>
        {normalized?.apn ? `APN ${normalized.apn}` : "APN missing"}
        {payload.flags.length > 0 && ` · ${payload.flags.length} open flag${payload.flags.length === 1 ? "" : "s"}`}
      </p>
      {(operation || actionError) && <div className={actionError ? "inline-error" : "operation-toast"}>{actionError ?? operation}</div>}

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
              <MoneyText money={money(underwriting.value.v_expected, true)} />
              <EvidenceButton onEvidence={setEvidenceField} fieldPath="valuation.v_expected" />
            </SummaryStat>
            <SummaryStat label="Confirmed debt">
              <MoneyText money={money(underwriting.liabilities.confirmed)} />
              <EvidenceButton onEvidence={setEvidenceField} fieldPath="liabilities.confirmed" />
            </SummaryStat>
            <SummaryStat label="Potential obligations">
              <MoneyText money={money(underwriting.liabilities.potential, true)} />
            </SummaryStat>
            <SummaryStat label={`Equity (${scenario})`}>
              <MoneyText money={money(equity?.adjusted, true)} />
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

      {normalized && <section className="panel owner-intelligence">
        <div className="panel-heading"><div><span className="eyebrow">Reference-only owner data</span><h2>Owner intelligence</h2></div><span className={normalized.ownership?.is_owner_occupied ? "status-pill status-warning" : "status-pill status-complete"}>Owner occupied: {normalized.ownership?.is_owner_occupied == null ? "Unknown" : normalized.ownership.is_owner_occupied ? "Yes" : "No"}</span></div>
        {ownerProfile?.serial_filing.dismissed_count ? <div className="owner-alert"><strong>Serial-filing indicator</strong><span>{ownerProfile.serial_filing.dismissed_count} dismissed filing{ownerProfile.serial_filing.dismissed_count === 1 ? "" : "s"}{ownerProfile.serial_filing.near_scheduled_sale ? ` · filing within ${ownerProfile.serial_filing.window_days} days of a scheduled sale` : ""}. Reference only; scoring and underwriting are unchanged.</span></div> : null}
        {ownerProfile?.owner_lien_total && Number(ownerProfile.owner_lien_total) > 0 && <div className="owner-alert"><strong>Owner-level lien not included in underwriting</strong><span>Equity if attached: <MoneyText money={money(ownerProfile.equity_if_owner_liens_attach, true)} />. Owner-level lien total: <MoneyText money={money(ownerProfile.owner_lien_total, false)} />.</span></div>}
        {ownerProfile && ownerProfile.owners.length > 0 && <div className="owner-grid"><div><h3>Owners</h3>{ownerProfile.owners.map((owner) => <p key={owner.full_name}><strong>{owner.full_name}</strong><small>{owner.mailing_address ?? "No mailing address"}</small></p>)}</div><div><h3>Contact candidates</h3>{ownerProfile.contacts.length === 0 ? <p className="muted">No contact candidates.</p> : ownerProfile.contacts.map((contact) => <p key={contact.id}><strong>{contact.value}</strong><small>{contact.source} · confidence {contact.confidence ?? "unknown"}{contact.association_warning ? ` · ${contact.association_warning}` : ""}</small></p>)}</div></div>}
        {ownerProfile?.timeline.length ? <details><summary>Interleaved owner/property timeline</summary><div className="owner-timeline">{ownerProfile.timeline.map((event,index) => <p key={`${event.kind}-${index}`}><time>{event.date ?? "Unknown date"}</time><span><strong>{event.label}</strong>{event.status ? ` · ${event.status}` : ""}{event.sale_date ? ` · sale ${event.sale_date}` : ""}</span></p>)}</div></details> : null}
        <p className="compliance-note">Owner records are informational only. Have counsel review outreach under California Civil Code §§1695 and 2945 before first send.</p>
      </section>}

      {draft && <section className="panel outreach-editor">
        <div className="panel-heading"><div><span className="eyebrow">Cash-offer outreach</span><h2>Email draft</h2></div><button className="btn btn-ghost btn-small" onClick={() => setDraft(null)}>Close</button></div>
        <label><span>Recipient — select before opening Gmail</span><select className="select-input" value={selectedEmail} onChange={(event) => setSelectedEmail(event.target.value)}><option value="">Choose an email…</option>{draft.recipients.map((recipient) => <option key={recipient.id} value={recipient.value}>{recipient.value} · {recipient.source}{recipient.association_warning ? " · may be relative/stale" : ""}</option>)}</select></label>
        {draft.recipients.length === 0 && <p className="muted">No email is available. Copy the draft or use the mailing address: {draft.mailing_addresses.join("; ") || "not available"}.</p>}
        <label><span>Subject</span><input className="text-input" value={draft.subject} onChange={(event) => setDraft({...draft,subject:event.target.value})} /></label>
        <label><span>Body</span><textarea className="text-area" rows={12} value={draft.body} onChange={(event) => setDraft({...draft,body:event.target.value})} /></label>
        <div className="action-buttons"><button className="btn btn-secondary" onClick={() => navigator.clipboard.writeText(`${draft.subject}\n\n${draft.body}`)}>Copy</button><button className="btn btn-secondary" onClick={() => saveDraft().catch((reason: Error) => setActionError(reason.message))}>Save changes</button><button className="btn btn-secondary" disabled={drafting} onClick={() => draftOutreach("Make it shorter while preserving the exact offer figure.")}>Make shorter</button><button className="btn btn-primary" disabled={!selectedEmail} onClick={openGmail}>Open Gmail</button><button className="btn btn-secondary" disabled={!selectedEmail} onClick={() => saveDraft("sent").catch((reason: Error) => setActionError(reason.message))}>Mark sent</button><a className="btn btn-ghost" href={`mailto:${encodeURIComponent(selectedEmail)}?subject=${encodeURIComponent(draft.subject)}&body=${encodeURIComponent(draft.body)}`}>Mail app fallback</a></div>
      </section>}

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
              <MoneyText money={money(underwriting.value.v_low, true)} /> – <MoneyText money={money(underwriting.value.v_high, true)} />
            </SummaryStat>
            <SummaryStat label={`Value (${scenario})`}>
              <MoneyText money={money(underwriting.value.arv_by_scenario?.[scenario], true)} />
            </SummaryStat>
            <SummaryStat label="Net realizable equity"><MoneyText money={money(equity?.net_realizable, true)} /></SummaryStat>
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
                  <td style={td}><MoneyText money={money(costs.acquisition, true)} /></td>
                  <td style={td}><MoneyText money={money(costs.repairs, true)} /></td>
                  <td style={td}><MoneyText money={money(costs.holding, true)} /></td>
                  <td style={td}><MoneyText money={money(costs.resale, true)} /></td>
                  <td style={td}><MoneyText money={money(costs.financing, true)} /></td>
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
          {underwriting && (
            <div className="liability-summary">
              <div><span>Confirmed</span><strong><MoneyText money={money(underwriting.liabilities.confirmed)} /></strong></div>
              <div><span>Potential</span><strong><MoneyText money={money(underwriting.liabilities.potential, true)} /></strong></div>
              <div><span>Maximum exposure</span><strong><MoneyText money={money(underwriting.liabilities.maximum, true)} /></strong></div>
            </div>
          )}
          {underwriting && underwriting.liabilities.breakdown.length > 0 && (
            <table style={{ ...table, marginBottom: 16 }}>
              <thead><tr><th style={th}>Liability</th><th style={th}>Amount</th><th style={th}>Bucket</th><th style={th}>Attachment basis</th></tr></thead>
              <tbody>{underwriting.liabilities.breakdown.map((entry, index) => {
                const label = String(entry.label ?? entry.type ?? entry.lien_type ?? `Liability ${index + 1}`);
                const amount = entry.amount == null ? null : String(entry.amount);
                const bucket = String(entry.bucket ?? entry.category ?? "—");
                const basis = String(entry.attachment_basis ?? "unknown");
                return <tr key={`${label}-${index}`}><td style={td}>{label.replace(/_/g," ")}</td><td style={td}><MoneyText money={money(amount, true)} /></td><td style={td}>{bucket.replace(/_/g," ")}</td><td style={td}><span className={`attachment-badge attachment-${basis}`}>{basis.replace(/_/g," ")}</span></td></tr>;
              })}</tbody>
            </table>
          )}
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
                          ? <><MoneyText money={money(candidate.value_low, true)} /> – <MoneyText money={money(candidate.value_high, true)} /></>
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
                  <SummaryStat label="MAO"><MoneyText money={money(result.mao, true)} /></SummaryStat>
                  <SummaryStat label="All-in basis"><MoneyText money={money(result.all_in_basis, true)} /></SummaryStat>
                  <SummaryStat label="Profit"><MoneyText money={money(result.profit, true)} /></SummaryStat>
                  <SummaryStat label="ROI">{formatPercentString(result.roi)}</SummaryStat>
                  <SummaryStat label="Margin of safety">{formatPercentString(result.margin_of_safety)}</SummaryStat>
                </div>
                {(Object.keys(result.metrics).length > 0 || Object.keys(result.inputs_echo).length > 0) && (
                  <div className="calculation-grid">
                    {Object.keys(result.metrics).length > 0 && <div><h4>Strategy metrics</h4>{Object.entries(result.metrics).map(([name,value]) => <p key={name}><span>{name.replace(/_/g," ")}</span><strong>{value ?? "—"}</strong></p>)}</div>}
                    {Object.keys(result.inputs_echo).length > 0 && <div><h4>Calculation inputs</h4>{Object.entries(result.inputs_echo).map(([name,value]) => <p key={name}><span>{name.replace(/_/g," ")}</span><strong>{value}</strong></p>)}</div>}
                  </div>
                )}
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
                  <strong style={{ fontSize: 13 }}>{(event.kind ?? event.event_type ?? "event").replace(/_/g, " ")}</strong> — {event.label}
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
              .sort((a, b) => Number(b.financial_impact_usd ?? 0) - Number(a.financial_impact_usd ?? 0))
              .map((flag, index) => (
                <li key={flag.id ?? index} className="deal-flag-row" style={{ padding: "8px 0", borderBottom: `1px solid ${palette.subtle}`, fontSize: 14 }}>
                  <div><strong style={{ color: severityColor(flag.severity ?? "warning") }}>{flag.label ?? flag.flag_type.replace(/_/g, " ")}</strong>
                  {flag.status === "open" && <span style={{ marginLeft: 8, fontSize: 12, color: palette.bad }}>open</span>}
                  {flag.financial_impact_usd !== null && flag.financial_impact_usd !== undefined && (
                    <span style={{ marginLeft: 8, fontSize: 12, color: palette.muted }}>
                      impact <MoneyText money={money(flag.financial_impact_usd, true)} />
                    </span>
                  )}</div>
                  {flag.summary && <div style={{ color: palette.muted, fontSize: 12, marginTop: 3 }}>{flag.summary}</div>}
                  {flag.review_guidance && <div style={{ color: palette.muted, fontSize: 12, marginTop: 3 }}>Review: {flag.review_guidance}</div>}
                  {flag.status === "open" && <InlineFlagResolution flagId={flag.id} disabled={user.read_only} onResolved={() => setPayload({...payload,flags:payload.flags.map((item)=>item.id===flag.id?{...item,status:"resolved"}:item)})} onError={setActionError} />}
                </li>
              ))}
          </ul>
        </section>
      )}

      <div className="deal-operations-grid">
        <section className="panel deal-edit-panel">
          <div className="panel-heading"><div><span className="eyebrow">Triage</span><h2>Property workflow</h2></div>{property?.is_watchlisted && <span className="watch-status">★ Watchlisted</span>}</div>
          {!property ? <p className="muted">Loading property controls…</p> : <div className="deal-edit-grid">
            <label><span>Pipeline status</span><select className="select-input" disabled={user.read_only} value={property.pipeline_status ?? property.status ?? "new"} onChange={(event) => saveProperty({pipeline_status:event.target.value})}>{["new","reviewing","pursue","offer_made","under_contract","dead"].map((status) => <option key={status} value={status}>{status.replace(/_/g," ")}</option>)}</select></label>
            <label><span>Gut rating</span><select className="select-input" disabled={user.read_only} value={property.gut_rating ?? ""} onChange={(event) => saveProperty({gut_rating:event.target.value ? Number(event.target.value) : null})}><option value="">Not rated</option>{[1,2,3,4,5].map((rating)=><option key={rating} value={rating}>{rating} / 5</option>)}</select></label>
            <label><span>Next action</span><input className="text-input" disabled={user.read_only} value={property.next_action ?? ""} onChange={(event)=>setProperty({...property,next_action:event.target.value})} onBlur={(event)=>saveProperty({next_action:event.target.value||null})}/></label>
            <label><span>Action date</span><input className="text-input" type="date" disabled={user.read_only} value={property.next_action_date ?? ""} onChange={(event)=>saveProperty({next_action_date:event.target.value||null})}/></label>
            <label className="full"><span>Tags</span><input className="text-input" disabled={user.read_only} value={(property.tags??[]).join(", ")} onChange={(event)=>setProperty({...property,tags:event.target.value.split(",").map((tag)=>tag.trim()).filter(Boolean)})} onBlur={(event)=>saveProperty({tags:event.target.value.split(",").map((tag)=>tag.trim()).filter(Boolean)})}/></label>
            <label className="check-row full"><input type="checkbox" disabled={user.read_only} checked={Boolean(property.is_watchlisted)} onChange={(event)=>saveProperty({is_watchlisted:event.target.checked})}/><span><strong>Add to watchlist</strong><small>Keep this opportunity visible during triage.</small></span></label>
          </div>}
        </section>

        <section className="panel notes-panel">
          <div className="panel-heading"><div><span className="eyebrow">Collaboration</span><h2>Notes</h2></div><span className="count-chip">{notes.length}</span></div>
          <div className="note-compose"><textarea className="text-area" rows={3} value={noteBody} onChange={(event)=>setNoteBody(event.target.value)} placeholder={user.read_only?"Notes are disabled in read-only mode":"Add underwriting context or a next step…"} disabled={user.read_only}/><button className="btn btn-primary btn-small" disabled={user.read_only||!noteBody.trim()} onClick={addNote}>Add note</button></div>
          <div className="note-list">{notes.length===0?<div className="empty-state compact">No notes yet.</div>:notes.map((note)=><article key={note.id}><p>{note.body}</p><time>{note.created_at?new Date(note.created_at).toLocaleString():"Just now"}</time></article>)}</div>
        </section>
      </div>

      <section className="panel">
        <div className="panel-heading"><div><span className="eyebrow">Source ledger</span><h2>Reports</h2></div><Link to="/batches">Add reports →</Link></div>
        {reports.length===0?<div className="empty-state compact"><strong>No reports attached</strong><span>Upload a source document to begin extraction.</span></div>:<div className="table-wrap"><table className="data-table"><thead><tr><th>Type</th><th>Vendor</th><th>Status</th><th>Failure</th><th>Pages</th><th>OCR</th><th>Created</th></tr></thead><tbody>{reports.map((report)=><tr key={report.id}><td>{report.report_type?.replace(/_/g," ")??"Unclassified"}</td><td>{report.vendor??"—"}</td><td><span className={`status-pill status-${report.status}`}>{report.status.replace(/_/g," ")}</span></td><td>{report.failure_reason?.replace(/_/g," ")??"—"}</td><td>{report.page_count??"—"}</td><td>{report.ocr_applied?"Applied":"Digital text"}</td><td>{report.created_at?new Date(report.created_at).toLocaleDateString():"—"}</td></tr>)}</tbody></table></div>}
      </section>

      <section className="panel action-center">
        <div><span className="eyebrow">Record operations</span><h2>Actions</h2><p>Advanced actions preserve source history and queue deterministic recomputation.</p></div>
        <div className="action-buttons"><button className="btn btn-secondary" disabled={user.read_only} onClick={()=>setActionPanel("override")}>Add override fact</button><button className="btn btn-secondary" disabled={user.read_only} onClick={()=>setActionPanel("merge")}>Merge record</button><button className="btn btn-secondary" disabled={user.read_only} onClick={()=>setActionPanel("unmerge")}>Unmerge source</button><button className="btn btn-secondary" disabled={!underwriting} onClick={()=>setActionPanel("net")}>Open net sheet</button></div>
      </section>
      {actionPanel && <DealActionModal mode={actionPanel} propertyId={propertyId} scenario={scenario} reports={reports} onClose={()=>setActionPanel(null)} onMessage={(message)=>{setOperation(message);setActionPanel(null);}} onError={setActionError} />}

      {/* 7 (drawer). Evidence */}
      <EvidenceDrawer propertyId={propertyId} fieldPath={evidenceField} onClose={() => setEvidenceField(null)} />
    </section>
  );
}

function InlineFlagResolution(props: { flagId: string; disabled: boolean; onResolved: () => void; onError: (message: string) => void }) {
  const [resolution,setResolution]=useState<"approve"|"reject"|"replace"|"dismiss">("approve");
  const [value,setValue]=useState("");
  const [busy,setBusy]=useState(false);
  return <div className="inline-flag-controls"><select className="select-input" disabled={props.disabled} value={resolution} onChange={(event)=>setResolution(event.target.value as typeof resolution)}><option value="approve">Approve</option><option value="reject">Reject</option><option value="replace">Replace</option><option value="dismiss">Dismiss</option></select>{resolution==="replace"&&<input className="text-input" value={value} onChange={(event)=>setValue(event.target.value)} placeholder="Replacement"/>}<button className="btn btn-secondary btn-small" disabled={props.disabled||busy||(resolution==="replace"&&!value)} onClick={async()=>{setBusy(true);try{await resolveFlag(props.flagId,{resolution,resolved_value:resolution==="replace"?{value}:null});props.onResolved();}catch(reason){props.onError(reason instanceof Error?reason.message:"Resolution failed");}finally{setBusy(false);}}}>{busy?"Saving…":"Resolve"}</button></div>;
}

function DealActionModal(props: { mode: "override" | "merge" | "unmerge" | "net"; propertyId: string; scenario: Scenario; reports: ReportRecord[]; onClose: () => void; onMessage: (message: string) => void; onError: (message: string) => void }) {
  const [target, setTarget] = useState("");
  const [offer, setOffer] = useState("");
  const [busy, setBusy] = useState(false);
  const [fact, setFact] = useState({ report_id: props.reports[0]?.id ?? "", extraction_unit_id: "", field_path: "", value_text: "", snippet: "Human override" });
  useEffect(() => { const close = (event: globalThis.KeyboardEvent) => event.key === "Escape" && props.onClose(); window.addEventListener("keydown",close); return ()=>window.removeEventListener("keydown",close); },[props]);
  const submit = async () => {
    setBusy(true); props.onError("");
    try {
      if (props.mode === "merge") { await mergeProperties({source_id:props.propertyId,target_id:target}); props.onMessage("Property merged into the target record"); }
      if (props.mode === "unmerge") { await unmergeProperties({source_id:target,target_id:props.propertyId}); props.onMessage("Source property unmerged"); }
      if (props.mode === "net") { await openNetSheet(props.propertyId,offer,props.scenario); props.onMessage("Net sheet opened in a new tab"); }
      if (props.mode === "override") {
        const body: FactSubmission = { report_id:fact.report_id, extraction_unit_id:fact.extraction_unit_id, entity_type:"property", entity_local_id:"property", field_path:fact.field_path, value_text:fact.value_text||null, value_raw:fact.value_text||null, page_number:1, snippet:fact.snippet, extraction_confidence:1, source_kind:"human" };
        await submitFact(props.propertyId,body); props.onMessage("Override saved and recompute queued");
      }
    } catch (reason) { props.onError(reason instanceof Error?reason.message:"Action failed"); } finally { setBusy(false); }
  };
  const titles = {override:"Add override fact",merge:"Merge property record",unmerge:"Unmerge source record",net:"Open seller net sheet"};
  return <div className="modal-backdrop" onMouseDown={(event)=>event.target===event.currentTarget&&props.onClose()}><div className="modal" role="dialog" aria-modal="true"><button className="modal-close" onClick={props.onClose}>×</button><span className="eyebrow">Advanced action</span><h2>{titles[props.mode]}</h2>{props.mode==="merge"&&<><p>Move this record into a canonical target property.</p><label className="field-label">Target property ID</label><input className="text-input" value={target} onChange={(event)=>setTarget(event.target.value)}/></>}{props.mode==="unmerge"&&<><p>Restore a source record previously merged into this property.</p><label className="field-label">Source property ID</label><input className="text-input" value={target} onChange={(event)=>setTarget(event.target.value)}/></>}{props.mode==="net"&&<><p>Generate the server-rendered HTML net sheet using an authoritative offer calculation.</p><label className="field-label">Offer price</label><input className="text-input" inputMode="decimal" value={offer} onChange={(event)=>setOffer(event.target.value)}/></>}{props.mode==="override"&&<><p>The current API requires source-ledger identifiers for an override. Use the extraction unit that produced the field being replaced.</p><label className="field-label">Report</label><select className="select-input" value={fact.report_id} onChange={(event)=>setFact({...fact,report_id:event.target.value})}><option value="">Select report</option>{props.reports.map((report)=><option key={report.id} value={report.id}>{report.report_type??report.id}</option>)}</select><label className="field-label">Extraction unit ID</label><input className="text-input" value={fact.extraction_unit_id} onChange={(event)=>setFact({...fact,extraction_unit_id:event.target.value})}/><label className="field-label">Field path</label><input className="text-input" placeholder="valuation.value" value={fact.field_path} onChange={(event)=>setFact({...fact,field_path:event.target.value})}/><label className="field-label">Replacement value</label><input className="text-input" value={fact.value_text} onChange={(event)=>setFact({...fact,value_text:event.target.value})}/><label className="field-label">Source note</label><input className="text-input" maxLength={200} value={fact.snippet} onChange={(event)=>setFact({...fact,snippet:event.target.value})}/></>}<div className="modal-actions"><button className="btn btn-ghost" onClick={props.onClose}>Cancel</button><button className="btn btn-primary" disabled={busy||((props.mode==="merge"||props.mode==="unmerge")&&!target)||(props.mode==="net"&&!offer)||(props.mode==="override"&&(!fact.report_id||!fact.extraction_unit_id||!fact.field_path||!fact.value_text))} onClick={submit}>{busy?"Working…":"Confirm action"}</button></div></div></div>;
}
