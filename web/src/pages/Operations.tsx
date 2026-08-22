import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  confirmOwnerProfileLink,
  createAssumptionSet,
  createRealizedDeal,
  estimateBatch,
  getBatch,
  getChanges,
  getDashboard,
  getProblems,
  getRankings,
  ingestPaste,
  listAssumptionSets,
  listFlags,
  listUnlinkedOwnerProfiles,
  listProperties,
  previewAssumptionSet,
  resolveFlag,
  startBatch,
  uploadReports,
  type AssumptionSetRecord,
  type BatchEstimate,
  type BatchStatus,
  type ChangeEvent,
  type DashboardResponse,
  type FlagRecord,
  type ProblemsResponse,
  type PropertyListItem,
  type RankingsResponse,
  type UnderwritingResult,
} from "../api";
import { useAuth } from "../auth";
import { MoneyText } from "../components/Money";
import { parseScore, ScoreBar } from "../components/ScoreBar";
import { Link } from "../router";

function money(value: string | null | undefined, estimated = false) {
  return value == null ? null : { value, confidence: 1, source_kind: estimated ? "derived" as const : "report" as const, is_estimated: estimated };
}

function useLoad<T>(load: () => Promise<T>, deps: readonly unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    load().then((value) => live && setData(value)).catch((reason: ApiError | Error) => live && setError(reason)).finally(() => live && setLoading(false));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, version]);
  return { data, error, loading, refresh: () => setVersion((value) => value + 1) };
}

function ErrorBlock(props: { error: ApiError | Error; retry?: () => void }) {
  const apiError = props.error instanceof ApiError ? props.error : null;
  return <div className="state-card state-error" role="alert">
    <div className="state-icon">!</div><div><strong>{props.error.message}</strong>
    {apiError && Object.keys(apiError.details).length > 0 && <details><summary>Technical details</summary><pre>{JSON.stringify(apiError.details, null, 2)}</pre></details>}
    {props.retry && <button className="btn btn-secondary" onClick={props.retry}>Try again</button>}</div>
  </div>;
}

function PageHeader(props: { eyebrow: string; title: string; description: string; actions?: React.ReactNode }) {
  return <div className="page-header"><div><div className="eyebrow">{props.eyebrow}</div><h1>{props.title}</h1><p>{props.description}</p></div>{props.actions && <div className="page-actions">{props.actions}</div>}</div>;
}

function LoadingGrid() { return <div className="skeleton-grid" aria-label="Loading"><span /><span /><span /><span /></div>; }

function batchResultLabel(result: NonNullable<BatchStatus["results"]>[number]) {
  const locality = [result.city, result.state, result.zip5].filter(Boolean).join(", ");
  return [result.address_line1, locality].filter(Boolean).join(", ") || `Property ${result.property_id.slice(0, 8)}`;
}

function batchStage(status: string) {
  if (["created", "uploading", "uploaded"].includes(status)) return "Uploading report…";
  if (["ingesting", "analyzing", "running"].includes(status)) return "Analyzing document…";
  if (status === "computing") return "Calculating property…";
  if (["complete", "completed"].includes(status)) return "Analysis complete";
  if (status === "unresolved_identity") return "Analysis extracted — identity unresolved";
  if (status.startsWith("failed")) return "Analysis failed";
  return status.replace(/_/g, " ");
}

export function DashboardPage() {
  const state = useLoad<DashboardResponse>(getDashboard);
  if (state.loading) return <><PageHeader eyebrow="Overview" title="Dashboard" description="Portfolio health at a glance." /><LoadingGrid /></>;
  if (state.error) return <ErrorBlock error={state.error} retry={state.refresh} />;
  if (!state.data) return null;
  const data = state.data;
  const maxStatus = Math.max(1, ...Object.values(data.by_status));
  return <section>
    <PageHeader eyebrow="Overview" title="Dashboard" description="A focused view of portfolio momentum, data quality, and work requiring attention." actions={<Link to="/batches" className="btn btn-primary">Add reports</Link>} />
    <div className="metric-grid">
      <Link to="/properties" className="metric-card"><span className="metric-label">Total properties</span><strong>{data.total_properties}</strong><span>Active portfolio records</span></Link>
      <Link to="/flags" className="metric-card"><span className="metric-label">Open flags</span><strong>{data.open_flags}</strong><span>Review by financial impact</span></Link>
      <Link to="/problems" className="metric-card"><span className="metric-label">Failed reports</span><strong>{data.failed_reports}</strong><span>Documents needing attention</span></Link>
      <Link to="/properties?filters=%5B%7B%22field%22%3A%22is_watchlisted%22%2C%22op%22%3A%22eq%22%2C%22value%22%3Atrue%7D%5D" className="metric-card"><span className="metric-label">Watchlisted</span><strong>{data.watchlisted}</strong><span>Priority opportunities</span></Link>
    </div>
    <div className="dashboard-grid">
      <section className="panel"><div className="panel-heading"><div><span className="eyebrow">Pipeline</span><h2>Current distribution</h2></div><Link to="/properties">View portfolio →</Link></div>
        {Object.keys(data.by_status).length === 0 ? <div className="empty-state">No properties are in the pipeline yet.</div> : <div className="status-bars">{Object.entries(data.by_status).map(([status, count]) => <Link key={status} to={`/properties?filters=${encodeURIComponent(JSON.stringify([{ field: "pipeline_status", op: "eq", value: status }]))}`} className="status-row"><span>{status.replace(/_/g, " ")}</span><span className="status-track"><i style={{ width: `${Math.max(6, count / maxStatus * 100)}%` }} /></span><strong>{count}</strong></Link>)}</div>}
      </section>
      <section className="panel warning-panel"><div className="warning-glyph">!</div><div><span className="eyebrow">Analysis exclusion</span><h2>{data.missing_valuation_count} {data.missing_valuation_count === 1 ? "property has" : "properties have"} no valuation</h2><p>These records are excluded from analysis until a supported valuation source is available.</p><Link to="/properties">Review portfolio →</Link></div></section>
    </div>
  </section>;
}

export function RankingsPage() {
  const state = useLoad<RankingsResponse>(() => getRankings("portfolio"));
  const properties = useLoad(() => listProperties({ limit: 500 }));
  const names = useMemo(() => new Map((properties.data?.items ?? []).map((item) => [item.id, item])), [properties.data]);
  return <section><PageHeader eyebrow="Portfolio snapshot" title="Rankings" description="The latest deterministic ordering across the full portfolio." />
    {state.loading ? <LoadingGrid /> : state.error ? <ErrorBlock error={state.error} retry={state.refresh} /> : !state.data?.items.length ? <div className="empty-state panel"><strong>No ranking snapshot yet</strong><span>Run a recompute to create the first portfolio ranking.</span></div> : <div className="panel flush"><div className="snapshot-bar"><span>Scope <strong>Portfolio</strong></span><span>Ranked <strong>{state.data.ranked_at ? new Date(state.data.ranked_at).toLocaleString() : "—"}</strong></span></div><div className="table-wrap"><table className="data-table"><thead><tr><th>Rank</th><th>Property</th><th>Score</th><th>Movement</th></tr></thead><tbody>{state.data.items.map((entry) => { const item = names.get(entry.property_id); const delta = entry.prev_rank == null ? null : entry.prev_rank - entry.rank; return <tr key={entry.property_id}><td><span className="rank-badge">#{entry.rank}</span></td><td><Link to={`/properties/${entry.property_id}`} className="property-link">{item?.address_line1 ?? item?.address ?? entry.property_id}</Link><small>{[item?.city, item?.state].filter(Boolean).join(", ")}</small></td><td>{entry.score == null ? "—" : <ScoreBar value={parseScore(entry.score) ?? 0} />}</td><td><span className={delta == null || delta === 0 ? "delta neutral" : delta > 0 ? "delta positive" : "delta negative"}>{delta == null ? "New" : delta > 0 ? `↑ ${delta}` : delta < 0 ? `↓ ${Math.abs(delta)}` : "—"}</span></td></tr>; })}</tbody></table></div></div>}
  </section>;
}

function FlagResolver(props: { flag: FlagRecord; onResolved: () => void }) {
  const { user } = useAuth();
  const [open, setOpen] = useState(false);
  const [resolution, setResolution] = useState<"approve" | "reject" | "replace" | "dismiss">("approve");
  const [replacement, setReplacement] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true); setMessage(null);
    try {
      await resolveFlag(props.flag.id, { resolution, note: note || null, resolved_value: resolution === "replace" ? { value: replacement } : null });
      setMessage("Resolved. Recompute has been queued.");
      window.setTimeout(props.onResolved, 700);
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "Resolution failed"); } finally { setBusy(false); }
  };
  return <div className="resolve-wrap">{!open ? <button className="btn btn-secondary btn-small" disabled={user.read_only} onClick={() => setOpen(true)}>Resolve</button> : <div className="resolve-panel"><select className="select-input" value={resolution} onChange={(e) => setResolution(e.target.value as typeof resolution)}><option value="approve">Approve</option><option value="reject">Reject</option><option value="replace">Replace</option><option value="dismiss">Dismiss</option></select>{resolution === "replace" && <input className="text-input" value={replacement} onChange={(e) => setReplacement(e.target.value)} placeholder="Replacement value" />}<input className="text-input" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Resolution note" /><button className="btn btn-primary btn-small" disabled={busy || (resolution === "replace" && !replacement)} onClick={submit}>{busy ? "Saving…" : "Confirm"}</button><button className="btn btn-ghost btn-small" onClick={() => setOpen(false)}>Cancel</button>{message && <small>{message}</small>}</div>}</div>;
}

function FlagDetails({ flag }: { flag: FlagRecord }) {
  const payload = flag.payload ?? {};
  const keyLabels: Record<string, string> = {
    affected_offer_points: "Affected offers",
    affected_scenarios: "Scenario count",
    underwriting_scenario_count: "Underwriting scenarios",
    offer_price_min: "Offer minimum",
    offer_price_max: "Offer maximum",
    proceeds_low_min: "Lowest seller proceeds",
    proceeds_low_max: "Highest seller proceeds",
    scenarios: "Scenarios",
    reason: "Why it was flagged",
    review_guidance: "Recommended review",
  };
  const entries = Object.entries(payload).filter(([key]) => key !== "logical_key" && key !== "fingerprint");
  return <div className="flag-details"><span>{flag.summary ?? flag.label ?? flag.flag_type.replace(/_/g, " ")}</span>{flag.review_guidance && <small>{flag.review_guidance}</small>}<details><summary>View supporting details</summary><dl>{entries.map(([key, value]) => <div key={key}><dt>{keyLabels[key] ?? key.replace(/_/g, " ")}</dt><dd>{Array.isArray(value) ? value.map(String).join(" + ") : String(value)}</dd></div>)}</dl></details></div>;
}

export function FlagsPage() {
  const [status, setStatus] = useState<"open" | "resolved">("open");
  const state = useLoad(() => listFlags(status), [status]);
  return <section><PageHeader eyebrow="Risk operations" title="Flags queue" description="Resolve uncertainty in order of financial impact." actions={<div className="segmented"><button className={status === "open" ? "active" : ""} onClick={() => setStatus("open")}>Open</button><button className={status === "resolved" ? "active" : ""} onClick={() => setStatus("resolved")}>Resolved</button></div>} />
    {state.loading ? <LoadingGrid /> : state.error ? <ErrorBlock error={state.error} retry={state.refresh} /> : !state.data?.items.length ? <div className="empty-state panel"><strong>No {status} flags</strong><span>{status === "open" ? "The review queue is clear." : "Resolved decisions will appear here."}</span></div> : <div className="panel flush"><div className="table-wrap"><table className="data-table"><thead><tr><th>Impact</th><th>Flag</th><th>Property</th><th>Details</th><th>Status</th><th>Action</th></tr></thead><tbody>{[...state.data.items].sort((a,b) => Number(b.financial_impact_usd ?? 0) - Number(a.financial_impact_usd ?? 0)).map((flag) => <tr key={flag.id}><td className="impact"><MoneyText money={money(flag.financial_impact_usd, true)} /></td><td><span className={`type-chip severity-${flag.severity ?? "warning"}`}>{flag.label ?? flag.flag_type.replace(/_/g, " ")}</span>{flag.is_gating && <small className="flag-blocking">Blocks ranking</small>}</td><td><Link className="property-link" to={`/properties/${flag.property_id}`}>{flag.property_label ?? "View property"}</Link></td><td><FlagDetails flag={flag} /></td><td>{flag.status}</td><td>{status === "open" && <FlagResolver flag={flag} onResolved={state.refresh} />}</td></tr>)}</tbody></table></div></div>}
  </section>;
}

export function ProblemsPage() {
  const state = useLoad<ProblemsResponse>(getProblems);
  const ownerReviews = useLoad(listUnlinkedOwnerProfiles);
  const { user } = useAuth();
  const groups = useMemo(() => { const map = new Map<string, ProblemsResponse["failed_reports"]>(); for (const report of state.data?.failed_reports ?? []) { const key = report.failure_reason ?? "unknown"; map.set(key, [...(map.get(key) ?? []), report]); } return map; }, [state.data]);
  return <section><PageHeader eyebrow="System health" title="Problems" description="Gating decisions, owner-link reviews, and failed source documents that prevent complete analysis." />{state.loading ? <LoadingGrid /> : state.error ? <ErrorBlock error={state.error} retry={state.refresh} /> : <><div className="two-column"><section className="panel"><div className="panel-heading"><div><span className="eyebrow">Decision blockers</span><h2>Gating flags</h2></div><span className="count-chip">{state.data?.gating_flags.length ?? 0}</span></div>{!state.data?.gating_flags.length ? <div className="empty-state compact">No gating flags are open.</div> : state.data.gating_flags.map((flag) => <div className="problem-row" key={flag.id}><div><strong>{flag.flag_type.replace(/_/g, " ")}</strong><small><MoneyText money={money(flag.financial_impact_usd, true)} /></small></div><Link to={`/properties/${flag.property_id}`}>Review →</Link></div>)}</section><section className="panel"><div className="panel-heading"><div><span className="eyebrow">Ingestion</span><h2>Failed reports</h2></div><span className="count-chip">{state.data?.failed_reports.length ?? 0}</span></div>{groups.size === 0 ? <div className="empty-state compact">No report failures.</div> : [...groups.entries()].map(([reason, reports]) => <div className="failure-group" key={reason}><strong>{reason.replace(/_/g, " ")} <span>{reports.length}</span></strong>{reports.map((report) => <div key={report.id}><code>{report.id.slice(0,8)}</code><span>{report.file_path ?? "No file path"}</span>{report.batch_id && <Link to={`/batches?batch=${report.batch_id}`}>Batch</Link>}</div>)}</div>)}</section></div><section className="panel owner-review"><div className="panel-heading"><div><span className="eyebrow">Owner identity</span><h2>Owner-document link review</h2></div><span className="count-chip">{ownerReviews.data?.items.length ?? 0}</span></div>{ownerReviews.error ? <ErrorBlock error={ownerReviews.error} retry={ownerReviews.refresh} /> : ownerReviews.loading ? <LoadingGrid /> : !ownerReviews.data?.items.length ? <div className="empty-state compact">No owner profiles are awaiting review.</div> : ownerReviews.data.items.map((profile) => <article className="owner-review-row" key={profile.report_id}><div><strong>{profile.owner_name ?? "Unidentified owner"}</strong><small>{profile.file_name}</small></div>{profile.link_candidates.length === 0 ? <span className="status-pill status-warning">No identity candidate</span> : <div>{profile.link_candidates.map((candidate) => <button key={candidate.owner_id} className="btn btn-secondary btn-small" disabled={user.read_only} onClick={() => confirmOwnerProfileLink(profile.report_id, candidate.owner_id).then(ownerReviews.refresh)}>{candidate.owner_name ?? candidate.owner_id.slice(0,8)} · {candidate.confidence} confidence</button>)}</div>}</article>)}</section></>}
  </section>;
}

export function ChangesPage() {
  const state = useLoad(() => getChanges(200));
  const [filter, setFilter] = useState("all");
  const types = useMemo(() => [...new Set((state.data?.items ?? []).map((item) => item.change_type))], [state.data]);
  const items = (state.data?.items ?? []).filter((item) => filter === "all" || item.change_type === filter);
  return <section><PageHeader eyebrow="Activity" title="Changes" description="A reverse-chronological feed of meaningful property and score changes." />{state.loading ? <LoadingGrid /> : state.error ? <ErrorBlock error={state.error} retry={state.refresh} /> : <><div className="chip-row"><button className={`filter-chip ${filter === "all" ? "active" : ""}`} onClick={() => setFilter("all")}>All</button>{types.map((type) => <button className={`filter-chip ${filter === type ? "active" : ""}`} onClick={() => setFilter(type)} key={type}>{type.replace(/_/g, " ")}</button>)}</div>{items.length === 0 ? <div className="empty-state panel">No changes in this view.</div> : <div className="timeline-feed">{items.map((item: ChangeEvent) => <article key={item.id}><span className="feed-dot" /><div className="feed-main"><div><span className="type-chip">{item.change_type.replace(/_/g, " ")}</span><Link to={`/properties/${item.property_id}`} className="property-link">Property {item.property_id.slice(0,8)}</Link></div><h3>{item.field_path?.replace(/_/g, " ") ?? "Property updated"}</h3><p><del>{String(item.old_value ?? "—")}</del><span>→</span><ins>{String(item.new_value ?? "—")}</ins></p></div><div className="feed-meta"><span className={Number(item.score_delta ?? 0) > 0 ? "delta positive" : Number(item.score_delta ?? 0) < 0 ? "delta negative" : "delta neutral"}>{item.score_delta ? `${Number(item.score_delta) > 0 ? "+" : ""}${item.score_delta} score` : "No score change"}</span><time>{item.detected_at ? new Date(item.detected_at).toLocaleString() : "—"}</time></div></article>)}</div>}</>}
  </section>;
}

export function BatchesPage() {
  const { user } = useAuth();
  const initial = new URLSearchParams(window.location.search).get("batch");
  const [batchId, setBatchId] = useState<string | null>(initial);
  const [batch, setBatch] = useState<BatchStatus | null>(null);
  const [estimate, setEstimate] = useState<BatchEstimate | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [name, setName] = useState("");
  const [paste, setPaste] = useState("");
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  useEffect(() => { if (!batchId) return; let live = true; const poll = () => getBatch(batchId).then((value) => live && setBatch(value)).catch((reason) => live && setError(reason instanceof Error ? reason.message : "Unable to load batch")); poll(); const id = window.setInterval(poll, 2000); return () => { live = false; window.clearInterval(id); }; }, [batchId]);
  const acceptFiles = (incoming: File[]) => setFiles(incoming.filter((file) => file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")));
  const upload = async () => { setBusy(true); setError(null); try { const result = await uploadReports(files, name || undefined); setBatchId(result.batch_id); setFiles([]); } catch (reason) { setError(reason instanceof Error ? reason.message : "Upload failed"); } finally { setBusy(false); } };
  const ingest = async () => { setBusy(true); setError(null); try { const result = await ingestPaste(paste, name || undefined); setBatchId(result.batch_id); setPaste(""); } catch (reason) { setError(reason instanceof Error ? reason.message : "Paste ingestion failed"); } finally { setBusy(false); } };
  const estimateNow = async () => { if (!batchId) return; setBusy(true); try { setEstimate(await estimateBatch(batchId)); } catch (reason) { setError(reason instanceof Error ? reason.message : "Estimate failed"); } finally { setBusy(false); } };
  const start = async () => { if (!batchId) return; setBusy(true); try { setBatch(await startBatch(batchId)); setEstimate(null); } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to start batch"); } finally { setBusy(false); } };
  const progress = batch && batch.total ? Math.round((batch.completed + batch.failed) / batch.total * 100) : 0;
  return <section><PageHeader eyebrow="Analysis" title="Batches" description="Upload property reports for whole-document analysis and deterministic underwriting." />{error && <div className="inline-error">{error}</div>}<div className="batch-layout"><section className="panel upload-panel"><div className="panel-heading"><div><span className="eyebrow">Source documents</span><h2>Upload property reports</h2></div></div><input className="text-input" placeholder="Batch name (optional)" value={name} onChange={(e) => setName(e.target.value)} disabled={user.read_only} /><button type="button" className={`drop-zone ${dragging ? "dragging" : ""}`} disabled={user.read_only} onClick={() => fileRef.current?.click()} onDragOver={(e) => { e.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={(e) => { e.preventDefault(); setDragging(false); acceptFiles([...e.dataTransfer.files]); }}><span className="upload-icon">↑</span><strong>Drop PDF reports here</strong><small>or choose files from your computer</small></button><input ref={fileRef} hidden multiple type="file" accept="application/pdf,.pdf" onChange={(e) => acceptFiles([...(e.target.files ?? [])])} />{files.length > 0 && <div className="file-list">{files.map((file) => <div key={`${file.name}-${file.size}`}><span>{file.name}</span><small>{(file.size / 1_000_000).toFixed(1)} MB</small></div>)}</div>}<button className="btn btn-primary btn-block" disabled={!files.length || busy || user.read_only} onClick={upload}>{busy ? "Uploading…" : `Analyze ${files.length || ""} report${files.length === 1 ? "" : "s"}`}</button><div className="or-divider"><span>or paste text</span></div><textarea className="text-area" rows={6} value={paste} onChange={(e) => setPaste(e.target.value)} placeholder="Paste a property report or source text…" disabled={user.read_only} /><button className="btn btn-secondary btn-block" disabled={!paste.trim() || busy || user.read_only} onClick={ingest}>Create batch from text</button></section><section className="panel run-panel"><div className="panel-heading"><div><span className="eyebrow">Report analysis</span><h2>{batch ? `Batch ${batch.name ?? batch.id.slice(0,8)}` : "No active batch"}</h2></div>{batch && <span className={`status-pill status-${batch.status}`}>{batchStage(batch.status)}</span>}</div>{!batch ? <div className="empty-state"><strong>Upload reports to begin</strong><span>Each original PDF is analyzed as a complete document, then calculated in code.</span></div> : <><div className="progress-summary"><div><span>{batchStage(batch.status)}</span><strong>{batch.completed + batch.failed} / {batch.total}</strong></div><div className="progress-track"><span style={{ width: `${progress}%` }} /></div><div className="progress-legend"><span>{batch.completed} complete</span><span>{batch.failed} needs attention</span><span>{progress}%</span></div></div>{["analyzing","ingesting","running"].includes(batch.status) && <div className="callout"><strong>Analyzing document…</strong><p>Reading the complete PDF and preserving its visual context.</p></div>}{batch.status === "computing" && <div className="callout"><strong>Calculating property…</strong><p>Validating source facts and running deterministic underwriting.</p></div>}{batch.status === "uploaded" && !estimate && <div className="callout"><strong>Legacy report ready</strong><p>This rollback-mode batch still uses the extraction confirmation flow.</p><button className="btn btn-primary" disabled={busy} onClick={estimateNow}>Calculate estimate</button></div>}{estimate && <div className="estimate-card"><div><span>Reports</span><strong>{estimate.report_count}</strong></div><div><span>Estimated tokens</span><strong>{estimate.total_tokens.toLocaleString()}</strong></div><div><span>Estimated cost</span><strong><MoneyText money={money(estimate.estimated_cost_usd, true)} /></strong></div><button className="btn btn-primary btn-block" disabled={busy || user.read_only} onClick={start}>Start legacy extraction</button></div>}{batch.status === "awaiting_confirmation" && !estimate && <div className="callout warning"><strong>Awaiting confirmation</strong><button className="btn btn-primary" onClick={estimateNow}>Review estimate</button></div>}{batch.results && batch.results.length > 0 && <div className="callout batch-results"><strong>Analysis complete</strong><p>Your property analysis is ready.</p>{batch.results.map((result) => <div className="batch-result" key={result.property_id}><span>{batchResultLabel(result)}</span><Link className="btn btn-primary" to={`/properties/${result.property_id}`}>View analysis</Link></div>)}</div>}{batch.unresolved_reports && batch.unresolved_reports.length > 0 && <div className="callout danger"><strong>Analysis extracted, but property identity could not be resolved</strong>{batch.unresolved_reports.map((report) => <p key={report.report_id}>{report.identity?.full_address ?? report.identity?.address_line1 ?? "No grounded street address"}{report.identity?.apn ? ` · APN ${report.identity.apn}` : ""}</p>)}<p>The canonical extraction and original PDF were preserved. No property was fabricated.</p><Link to="/problems">Review unresolved reports →</Link></div>}{batch.status.startsWith("failed") && <div className="callout danger"><strong>Analysis could not be completed</strong><p>The original PDF and any provider output were preserved for review.</p><Link to="/problems">Review failure →</Link></div>}{["complete","completed","failed_provider","failed_validation","failed_computation","unresolved_identity"].includes(batch.status) && <div className="cost-row"><div><span>Actual provider cost</span><MoneyText money={money(batch.actual_cost_usd)} /></div>{batch.failed > 0 && <Link to="/problems">Review issues →</Link>}</div>}</>}</section></div></section>;
}

export function AssumptionsPage() {
  const { user } = useAuth();
  const state = useLoad(() => listAssumptionSets());
  const [selected, setSelected] = useState<AssumptionSetRecord | null>(null);
  const [name, setName] = useState("New underwriting assumptions");
  const [json, setJson] = useState("{}");
  const [propertyId, setPropertyId] = useState("");
  const [preview, setPreview] = useState<UnderwritingResult | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const parse = () => { const value: unknown = JSON.parse(json); if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("Parameters must be a JSON object"); return value as Record<string, unknown>; };
  const doPreview = async () => { setError(null); try { const result = await previewAssumptionSet(name, parse(), propertyId || undefined); setPreview(result.underwriting); } catch (reason) { setError(reason instanceof Error ? reason.message : "Preview failed"); } };
  const save = async () => { setError(null); try { await createAssumptionSet(name, parse(), false); setPreview(undefined); state.refresh(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Save failed"); } };
  return <section><PageHeader eyebrow="Underwriting controls" title="Assumption sets" description="Version, preview, and publish the parameters behind every financial outcome." />{state.error ? <ErrorBlock error={state.error} retry={state.refresh} /> : <div className="assumption-layout"><section className="panel flush"><div className="panel-pad"><span className="eyebrow">Versions</span><h2>Published sets</h2></div>{state.loading ? <LoadingGrid /> : !state.data?.items.length ? <div className="empty-state compact">No assumption sets yet.</div> : <div className="version-list">{state.data.items.map((item) => <button key={item.id} className={selected?.id === item.id ? "active" : ""} onClick={() => setSelected(item)}><span><strong>{item.name}</strong><small>v{item.version} · {item.effective_from ?? "No effective date"}</small></span>{item.is_default && <i>Default</i>}</button>)}</div>}</section><section className="panel"><span className="eyebrow">Inspect</span><h2>{selected?.name ?? "Select a version"}</h2>{selected ? <pre className="json-tree">{JSON.stringify(selected.params, null, 2)}</pre> : <div className="empty-state compact">Choose a published set to inspect its parameters.</div>}</section><section className="panel editor-panel"><span className="eyebrow">Create version</span><h2>Preview before publishing</h2><label className="field-label">Name</label><input className="text-input" value={name} onChange={(e) => { setName(e.target.value); setPreview(undefined); }} disabled={user.read_only} /><label className="field-label">Parameters JSON</label><textarea className="code-editor" value={json} onChange={(e) => { setJson(e.target.value); setPreview(undefined); }} disabled={user.read_only} spellCheck={false} /><label className="field-label">Property ID for impact preview <span>optional</span></label><input className="text-input" value={propertyId} onChange={(e) => { setPropertyId(e.target.value); setPreview(undefined); }} disabled={user.read_only} />{error && <div className="inline-error">{error}</div>}<div className="button-row"><button className="btn btn-secondary" onClick={doPreview}>Run preview</button><button className="btn btn-primary" disabled={preview === undefined || user.read_only} onClick={save}>Publish version</button></div>{preview !== undefined && <div className="preview-result"><strong>{preview ? "Preview completed" : "Parameters are valid"}</strong>{preview ? <><span>Expected value</span><MoneyText money={money(preview.value.v_expected, true)} /><span>Confirmed liabilities</span><MoneyText money={money(preview.liabilities.confirmed)} /></> : <p>Add a property ID to see a full underwriting comparison.</p>}</div>}</section></div>}</section>;
}

export function SettingsPage() {
  const { user, signOut } = useAuth();
  const [form, setForm] = useState({ property_id: "", outcome: "", purchase_price: "", sale_price: "", actual_repairs: "", actual_costs: "", actual_holding_days: "", notes: "" });
  const [message, setMessage] = useState<string | null>(null);
  const submit = async (event: FormEvent) => { event.preventDefault(); setMessage(null); try { const payload = Object.fromEntries(Object.entries(form).map(([key,value]) => [key, value === "" ? null : key === "actual_holding_days" ? Number(value) : value])); await createRealizedDeal(payload); setMessage("Realized outcome recorded for future calibration."); } catch (reason) { setMessage(reason instanceof Error ? reason.message : "Unable to save outcome"); } };
  return <section><PageHeader eyebrow="Workspace" title="Settings" description="Session details, portfolio exports, and realized outcome capture." /><div className="settings-grid"><section className="panel"><span className="eyebrow">Current session</span><h2>{user.id}</h2><div className="session-detail"><span>Access</span><strong>{user.read_only ? "Read only" : "Owner"}</strong></div><p className="muted">Logging out clears this browser's app state. The secure server cookie remains valid until it expires.</p><button className="btn btn-secondary" onClick={signOut}>Log out</button></section><section className="panel"><span className="eyebrow">Exports</span><h2>Portfolio data</h2><p className="muted">Download the current property ledger as CSV for spreadsheet analysis.</p><a className="btn btn-secondary" href="/api/exports/csv">Download portfolio CSV</a></section><section className="panel realized-panel"><span className="eyebrow">Calibration input</span><h2>Record a realized deal</h2><p className="muted">This endpoint is write-only. Results will appear in future calibration reporting once a read workflow is available.</p><form onSubmit={submit} className="form-grid"><label><span>Property ID</span><input className="text-input" required value={form.property_id} onChange={(e) => setForm({...form, property_id:e.target.value})} /></label><label><span>Outcome</span><input className="text-input" value={form.outcome} onChange={(e) => setForm({...form, outcome:e.target.value})} /></label><label><span>Purchase price</span><input className="text-input" inputMode="decimal" value={form.purchase_price} onChange={(e) => setForm({...form, purchase_price:e.target.value})} /></label><label><span>Sale price</span><input className="text-input" inputMode="decimal" value={form.sale_price} onChange={(e) => setForm({...form, sale_price:e.target.value})} /></label><label><span>Actual repairs</span><input className="text-input" inputMode="decimal" value={form.actual_repairs} onChange={(e) => setForm({...form, actual_repairs:e.target.value})} /></label><label><span>Actual costs</span><input className="text-input" inputMode="decimal" value={form.actual_costs} onChange={(e) => setForm({...form, actual_costs:e.target.value})} /></label><label><span>Holding days</span><input className="text-input" type="number" value={form.actual_holding_days} onChange={(e) => setForm({...form, actual_holding_days:e.target.value})} /></label><label className="full"><span>Notes</span><textarea className="text-area" value={form.notes} onChange={(e) => setForm({...form, notes:e.target.value})} /></label><div className="full"><button className="btn btn-primary" disabled={user.read_only}>Record outcome</button>{message && <span className="form-message">{message}</span>}</div></form></section></div></section>;
}
