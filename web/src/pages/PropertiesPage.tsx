import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import {
  ApiError,
  createSavedView,
  deleteSavedView,
  exportCsvUrl,
  ingestPaste,
  listProperties,
  listSavedViews,
  quickAddProperty,
  updateProperty,
  validateFilter,
  type FilterClause,
  type PropertyListItem,
  type PropertyPatch,
  type QuickAddRequest,
  type SavedView,
} from "../api";
import { useAuth } from "../auth";
import { FILTERABLE_FIELDS, FilterBar } from "../components/FilterBar";
import { parseScore, ScoreBar } from "../components/ScoreBar";
import { Link, navigate, replaceUrl } from "../router";

const SERVER_SORTS = [
  ["updated_at", "Recently updated"], ["created_at", "Recently added"], ["address", "Address"], ["city", "City"],
  ["pipeline_status", "Pipeline status"], ["next_action_date", "Next action"], ["gut_rating", "Gut rating"], ["apn", "APN"],
] as const;
const CLIENT_SORTS = [["page-score", "Score — loaded results"], ["page-rank", "Rank — loaded results"]] as const;
const STATUSES = ["new", "reviewing", "pursue", "offer_made", "under_contract", "dead"];
const EXPORT_COLUMNS = ["id", "address_line1", "city", "state", "zip5", "pipeline_status", "tags", "next_action", "next_action_date", "gut_rating", "is_watchlisted"];

function addressOf(item: PropertyListItem) { return item.address_line1 ?? item.address ?? "Unknown address"; }
function statusOf(item: PropertyListItem) { return item.pipeline_status ?? item.status ?? "new"; }
function readFilters(): FilterClause[] {
  try {
    const value: unknown = JSON.parse(new URLSearchParams(window.location.search).get("filters") ?? "[]");
    const allowed = new Set<string>(FILTERABLE_FIELDS.map((field) => field.field));
    return Array.isArray(value) ? value.filter((item): item is FilterClause => Boolean(item && typeof item === "object" && "field" in item && allowed.has(String(item.field)))) : [];
  } catch { return []; }
}
function readSort() { const params = new URLSearchParams(window.location.search); return { sort: params.get("sort") ?? "updated_at", order: params.get("order") === "asc" ? "asc" as const : "desc" as const }; }

function Modal(props: { title: string; description?: string; onClose: () => void; children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { const close = (event: globalThis.KeyboardEvent) => event.key === "Escape" && props.onClose(); window.addEventListener("keydown", close); ref.current?.querySelector<HTMLElement>("input,textarea,select,button")?.focus(); return () => window.removeEventListener("keydown", close); }, [props]);
  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && props.onClose()}><div ref={ref} className="modal" role="dialog" aria-modal="true" aria-label={props.title}><button className="modal-close" onClick={props.onClose} aria-label="Close">×</button><span className="eyebrow">Portfolio action</span><h2>{props.title}</h2>{props.description && <p>{props.description}</p>}{props.children}</div></div>;
}

export function PropertiesPage() {
  const { user } = useAuth();
  const [clauses, setClauses] = useState<FilterClause[]>(readFilters);
  const [{ sort, order }, setSorting] = useState(readSort);
  const [items, setItems] = useState<PropertyListItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [filterError, setFilterError] = useState<string | null>(null);
  const [savedViews, setSavedViews] = useState<SavedView[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [focused, setFocused] = useState(0);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [modal, setModal] = useState<"quick" | "paste" | "save" | "export" | "help" | "tag" | null>(null);
  const [bulkAction, setBulkAction] = useState("pursue");
  const [bulkProgress, setBulkProgress] = useState<string | null>(null);
  const [bulkErrors, setBulkErrors] = useState<string[]>([]);
  const [clientSort, setClientSort] = useState<"page-score" | "page-rank" | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try { const response = await listProperties({ sort, order, filters: clauses, limit: 50 }); setItems(response.items); setNextCursor(response.next_cursor); setSelected(new Set()); }
    catch (reason) { setError(reason instanceof Error ? reason : new Error("Failed to load portfolio")); }
    finally { setLoading(false); }
  }, [clauses, order, sort]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { listSavedViews().then((response) => setSavedViews(response.items)).catch(() => setSavedViews([])); }, []);
  useEffect(() => { const params = new URLSearchParams(); if (clauses.length) params.set("filters", JSON.stringify(clauses)); params.set("sort", sort); params.set("order", order); replaceUrl(`/?${params.toString()}`); }, [clauses, sort, order]);

  const displayItems = useMemo(() => {
    if (!clientSort) return items;
    return [...items].sort((a,b) => clientSort === "page-rank" ? (a.rank ?? 99999) - (b.rank ?? 99999) : Number(b.overall_score ?? -1) - Number(a.overall_score ?? -1));
  }, [clientSort, items]);

  const applyFilters = async (next: FilterClause[]) => {
    setFilterError(null);
    try { await validateFilter(next); } catch (reason) { const message = reason instanceof Error ? reason.message : "Invalid filter"; setFilterError(message); throw reason; }
  };

  const optimisticPatch = useCallback(async (id: string, patch: PropertyPatch) => {
    const before = items.find((item) => item.id === id); if (!before) return;
    const optimistic: PropertyListItem = { ...before, ...(patch.pipeline_status !== undefined ? { pipeline_status: patch.pipeline_status, status: patch.pipeline_status } : {}), ...(patch.tags !== undefined ? { tags: patch.tags } : {}), ...(patch.gut_rating !== undefined ? { gut_rating: patch.gut_rating } : {}), ...(patch.is_watchlisted !== undefined ? { is_watchlisted: patch.is_watchlisted } : {}), ...(patch.next_action !== undefined ? { next_action: patch.next_action } : {}), ...(patch.next_action_date !== undefined ? { next_action_date: patch.next_action_date } : {}) };
    setItems((current) => current.map((item) => item.id === id ? optimistic : item));
    try { const saved = await updateProperty(id, patch); setItems((current) => current.map((item) => item.id === id ? { ...optimistic, ...saved } : item)); }
    catch (reason) { setItems((current) => current.map((item) => item.id === id ? before : item)); setError(reason instanceof Error ? reason : new Error("Update failed")); throw reason; }
  }, [items]);

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (modal || user.read_only || ["INPUT","TEXTAREA","SELECT"].includes((event.target as HTMLElement)?.tagName)) return;
      const current = displayItems[focused];
      if (event.key === "?") { event.preventDefault(); setModal("help"); return; }
      if (!current) return;
      if (event.key === "j" || event.key === "k") { event.preventDefault(); setFocused((index) => Math.max(0, Math.min(displayItems.length - 1, index + (event.key === "j" ? 1 : -1)))); }
      if (event.key === " ") { event.preventDefault(); setExpanded((set) => { const next = new Set(set); next.has(current.id) ? next.delete(current.id) : next.add(current.id); return next; }); }
      if (event.key === "Enter") navigate(`/properties/${current.id}`);
      if (event.key === "p") void optimisticPatch(current.id, { pipeline_status: "pursue" });
      if (event.key === "x") void optimisticPatch(current.id, { pipeline_status: "dead" });
      if (event.key === "w") void optimisticPatch(current.id, { is_watchlisted: !current.is_watchlisted });
      if (/^[1-5]$/.test(event.key)) void optimisticPatch(current.id, { gut_rating: Number(event.key) });
      if (event.key === "t") setModal("tag");
    };
    window.addEventListener("keydown", onKey); return () => window.removeEventListener("keydown", onKey);
  }, [displayItems, focused, modal, optimisticPatch, user.read_only]);

  const loadMore = async () => { if (!nextCursor) return; setLoadingMore(true); try { const response = await listProperties({ sort, order, filters: clauses, cursor: nextCursor, limit: 50 }); setItems((value) => [...value, ...response.items]); setNextCursor(response.next_cursor); } catch (reason) { setError(reason instanceof Error ? reason : new Error("Unable to load more")); } finally { setLoadingMore(false); } };
  const runBulk = async () => { const ids = [...selected]; if (!ids.length) return; setBulkErrors([]); for (let index = 0; index < ids.length; index += 1) { setBulkProgress(`Updating ${index + 1} of ${ids.length}`); try { if (bulkAction === "watch") await optimisticPatch(ids[index], { is_watchlisted: true }); else if (bulkAction.startsWith("tag:")) { const item = items.find((row) => row.id === ids[index]); await optimisticPatch(ids[index], { tags: [...new Set([...(item?.tags ?? []), bulkAction.slice(4)])] }); } else await optimisticPatch(ids[index], { pipeline_status: bulkAction }); } catch { setBulkErrors((value) => [...value, ids[index]]); } } setBulkProgress(null); };
  const toggleAll = () => setSelected(selected.size === displayItems.length ? new Set() : new Set(displayItems.map((item) => item.id)));

  return <section>
    <div className="page-header portfolio-header"><div><div className="eyebrow">Acquisition pipeline</div><h1>Portfolio</h1><p>Review, prioritize, and move opportunities through a source-traced underwriting workflow.</p></div><div className="page-actions"><button className="btn btn-secondary" onClick={() => setModal("export")}>Export CSV</button><button className="btn btn-secondary" disabled={user.read_only} onClick={() => setModal("paste")}>Paste report</button><button className="btn btn-primary" disabled={user.read_only} onClick={() => setModal("quick")}>＋ Quick add</button></div></div>
    <section className="portfolio-toolbar panel">
      <div className="toolbar-top"><FilterBar clauses={clauses} onChange={setClauses} onApply={applyFilters} /><div className="sort-group"><label>Sort</label><select className="select-input" value={clientSort ?? sort} onChange={(event) => { const value = event.target.value; if (value.startsWith("page-")) setClientSort(value as typeof clientSort); else { setClientSort(null); setSorting({ sort:value, order }); } }}>{SERVER_SORTS.map(([value,label]) => <option key={value} value={value}>{label}</option>)}{CLIENT_SORTS.map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select><button className="sort-direction" aria-label="Reverse sort" onClick={() => setSorting({ sort, order: order === "asc" ? "desc" : "asc" })}>{order === "asc" ? "↑" : "↓"}</button></div></div>
      <div className="saved-view-bar"><span>Saved views</span>{savedViews.length === 0 ? <small>No views saved</small> : savedViews.map((view) => <span className="saved-view" key={view.id}><button onClick={() => setClauses(view.filters)}>{view.name}</button><button disabled={user.read_only} aria-label={`Delete ${view.name}`} onClick={async () => { await deleteSavedView(view.id); setSavedViews((items) => items.filter((item) => item.id !== view.id)); }}>×</button></span>)}<button className="save-view" disabled={user.read_only} onClick={() => setModal("save")}>＋ Save current view</button></div>
    </section>
    {filterError && <div className="inline-error">{filterError}</div>}
    {error && <div className="state-card state-error"><div className="state-icon">!</div><div><strong>{error.message}</strong>{error instanceof ApiError && Object.keys(error.details).length > 0 && <details><summary>Details</summary><pre>{JSON.stringify(error.details,null,2)}</pre></details>}<button className="btn btn-secondary btn-small" onClick={load}>Try again</button></div></div>}
    {selected.size > 0 && <div className="bulk-bar"><strong>{selected.size} selected</strong><select className="select-input" disabled={user.read_only} value={bulkAction} onChange={(event) => setBulkAction(event.target.value)}>{STATUSES.map((status) => <option key={status} value={status}>Set status: {status.replace(/_/g," ")}</option>)}<option value="watch">Add to watchlist</option><option value="tag:priority">Add tag: priority</option></select><button className="btn btn-primary btn-small" disabled={Boolean(bulkProgress) || user.read_only} onClick={runBulk}>{bulkProgress ?? "Apply sequentially"}</button><button className="btn btn-ghost btn-small" onClick={() => setSelected(new Set())}>Clear</button>{bulkErrors.length > 0 && <span className="bulk-error">{bulkErrors.length} failed</span>}</div>}
    <div className="portfolio-table panel flush">
      {loading ? <div className="portfolio-skeleton">{Array.from({length:7},(_,index) => <span key={index} />)}</div> : displayItems.length === 0 ? <div className="empty-state"><strong>No matching properties</strong><span>Adjust the filters or add a property to begin underwriting.</span><button className="btn btn-primary" disabled={user.read_only} onClick={() => setModal("quick")}>Add property</button></div> : <div className="table-wrap"><table className="data-table"><thead><tr><th><input type="checkbox" aria-label="Select all" checked={selected.size === displayItems.length} onChange={toggleAll} /></th><th>Property</th><th>Pipeline</th><th>Tags</th><th>Rank</th><th>Score</th><th>Flags</th><th>Gut</th><th>Next action</th></tr></thead><tbody>{displayItems.map((item,index) => <PropertyRows key={item.id} item={item} focused={index === focused} expanded={expanded.has(item.id)} selected={selected.has(item.id)} onFocus={() => setFocused(index)} onSelect={() => setSelected((set) => { const next = new Set(set); next.has(item.id) ? next.delete(item.id) : next.add(item.id); return next; })} onToggle={() => setExpanded((set) => { const next = new Set(set); next.has(item.id) ? next.delete(item.id) : next.add(item.id); return next; })} />)}</tbody></table></div>}
      {nextCursor && <div className="load-more"><button className="btn btn-secondary" disabled={loadingMore} onClick={loadMore}>{loadingMore ? "Loading…" : "Load more properties"}</button><span>Cursor pagination · {items.length} loaded</span></div>}
    </div>
    {modal === "quick" && <QuickAddModal onClose={() => setModal(null)} onCreated={(item) => { setItems((value) => [item,...value]); setModal(null); }} />}
    {modal === "paste" && <PasteModal onClose={() => setModal(null)} />}
    {modal === "save" && <SaveViewModal clauses={clauses} onClose={() => setModal(null)} onSaved={(view) => { setSavedViews((items) => [view,...items]); setModal(null); }} />}
    {modal === "export" && <ExportModal clauses={clauses} onClose={() => setModal(null)} />}
    {modal === "help" && <Modal title="Keyboard triage" description="Move through the loaded page without leaving the keyboard." onClose={() => setModal(null)}><div className="shortcut-grid">{[["j / k","Move selection"],["space","Expand summary"],["enter","Open deal"],["p","Mark pursue"],["x","Mark dead"],["w","Toggle watchlist"],["1–5","Set gut rating"],["t","Edit tags"],["?","Show this guide"]].map(([key,label]) => <div key={key}><kbd>{key}</kbd><span>{label}</span></div>)}</div></Modal>}
    {modal === "tag" && displayItems[focused] && <TagModal item={displayItems[focused]} onClose={() => setModal(null)} onSave={async (tags) => { await optimisticPatch(displayItems[focused].id,{tags}); setModal(null); }} />}
  </section>;
}

function PropertyRows(props: { item: PropertyListItem; focused: boolean; expanded: boolean; selected: boolean; onFocus: () => void; onSelect: () => void; onToggle: () => void }) {
  const item = props.item; const score = parseScore(item.overall_score); const status = statusOf(item);
  return <><tr className={`${props.focused ? "keyboard-focus" : ""} ${props.selected ? "selected" : ""}`} onMouseEnter={props.onFocus}><td><input type="checkbox" aria-label={`Select ${addressOf(item)}`} checked={props.selected} onChange={props.onSelect} /></td><td><div className="property-cell"><button className="expand-row" aria-label="Expand summary" onClick={props.onToggle}>{props.expanded ? "−" : "+"}</button><div><Link to={`/properties/${item.id}`} className="property-link">{addressOf(item)}</Link><small>{[item.city,item.state,item.zip5].filter(Boolean).join(", ")} {item.apn ? `· APN ${item.apn}` : ""}</small></div>{item.is_watchlisted && <span className="watch-star" title="Watchlisted">★</span>}</div></td><td><span className={`pipeline-chip pipeline-${status}`}>{status.replace(/_/g," ")}</span></td><td><div className="tag-list">{(item.tags ?? []).slice(0,2).map((tag) => <span key={tag}>{tag}</span>)}{(item.tags?.length ?? 0) > 2 && <i>+{item.tags!.length-2}</i>}</div></td><td>{item.rank ? <span className="rank-badge">#{item.rank}</span> : "—"}</td><td>{score == null ? "—" : <ScoreBar value={score} />}</td><td><span className={(item.open_flags ?? 0) > 0 ? "flag-count has-flags" : "flag-count"}>{item.open_flags ?? 0}</span></td><td><span className="gut-rating">{item.gut_rating ? `${item.gut_rating}/5` : "—"}</span></td><td><div className="next-action"><strong>{item.next_action ?? "—"}</strong><small>{item.next_action_date ? new Date(`${item.next_action_date}T00:00:00`).toLocaleDateString() : "No date"}</small></div></td></tr>{props.expanded && <tr className="inline-summary"><td /><td colSpan={8}><div><span><small>Status</small><strong>{status.replace(/_/g," ")}</strong></span><span><small>Overall score</small><strong>{score == null ? "Not scored" : Math.round(score)}</strong></span><span><small>Open flags</small><strong>{item.open_flags ?? 0}</strong></span><span><small>Watchlist</small><strong>{item.is_watchlisted ? "Yes" : "No"}</strong></span><Link to={`/properties/${item.id}`} className="btn btn-secondary btn-small">Open full analysis →</Link></div></td></tr>}</>;
}

function QuickAddModal(props: { onClose: () => void; onCreated: (item: PropertyListItem) => void }) { const [form,setForm] = useState<QuickAddRequest>({address_line1:"",city:"",state:"CA",zip5:"",apn:""}); const [error,setError]=useState<string|null>(null); const [busy,setBusy]=useState(false); const submit=async(event:FormEvent)=>{event.preventDefault();setBusy(true);try{props.onCreated(await quickAddProperty(form));}catch(reason){setError(reason instanceof Error?reason.message:"Unable to add property");}finally{setBusy(false);}}; return <Modal title="Quick add property" description="Create a lightweight record now; upload reports when they arrive." onClose={props.onClose}><form onSubmit={submit} className="modal-form"><label>Street address<input className="text-input" required value={form.address_line1} onChange={(e)=>setForm({...form,address_line1:e.target.value})}/></label><div className="form-row"><label>City<input className="text-input" value={form.city ?? ""} onChange={(e)=>setForm({...form,city:e.target.value})}/></label><label>State<input className="text-input" value={form.state ?? ""} onChange={(e)=>setForm({...form,state:e.target.value})}/></label><label>ZIP<input className="text-input" value={form.zip5 ?? ""} onChange={(e)=>setForm({...form,zip5:e.target.value})}/></label></div><label>APN <span>optional</span><input className="text-input" value={form.apn ?? ""} onChange={(e)=>setForm({...form,apn:e.target.value})}/></label>{error&&<div className="inline-error">{error}</div>}<div className="modal-actions"><button className="btn btn-ghost" type="button" onClick={props.onClose}>Cancel</button><button className="btn btn-primary" disabled={busy}>{busy?"Adding…":"Add property"}</button></div></form></Modal>; }
function PasteModal(props:{onClose:()=>void}) { const [text,setText]=useState("");const [name,setName]=useState("");const [error,setError]=useState<string|null>(null);const [done,setDone]=useState<string|null>(null);const submit=async()=>{try{const result=await ingestPaste(text,name||undefined);setDone(result.batch_id);}catch(reason){setError(reason instanceof Error?reason.message:"Unable to ingest text");}};return <Modal title="Paste source report" description="Create an ingestion batch from copied report text." onClose={props.onClose}>{done?<div className="success-state"><strong>Batch created</strong><code>{done}</code><Link className="btn btn-primary" to={`/batches?batch=${done}`}>Review spend gate</Link></div>:<><label className="field-label">Batch name <span>optional</span></label><input className="text-input" value={name} onChange={(e)=>setName(e.target.value)}/><label className="field-label">Report text</label><textarea className="text-area" rows={10} value={text} onChange={(e)=>setText(e.target.value)} />{error&&<div className="inline-error">{error}</div>}<div className="modal-actions"><button className="btn btn-ghost" onClick={props.onClose}>Cancel</button><button className="btn btn-primary" disabled={!text.trim()} onClick={submit}>Create batch</button></div></>}</Modal>;}
function SaveViewModal(props:{clauses:FilterClause[];onClose:()=>void;onSaved:(view:SavedView)=>void}){const[name,setName]=useState("");const[error,setError]=useState<string|null>(null);return <Modal title="Save current view" description={`${props.clauses.length} filter${props.clauses.length===1?"":"s"} will be saved.`} onClose={props.onClose}><label className="field-label">View name</label><input className="text-input" value={name} onChange={(e)=>setName(e.target.value)}/>{error&&<div className="inline-error">{error}</div>}<div className="modal-actions"><button className="btn btn-ghost" onClick={props.onClose}>Cancel</button><button className="btn btn-primary" disabled={!name.trim()} onClick={()=>createSavedView(name,props.clauses).then(props.onSaved).catch((reason:Error)=>setError(reason.message))}>Save view</button></div></Modal>}
function ExportModal(props:{clauses:FilterClause[];onClose:()=>void}){const[columns,setColumns]=useState(new Set(EXPORT_COLUMNS.slice(0,7)));return <Modal title="Export portfolio CSV" description="The export uses the current server-side filters." onClose={props.onClose}><div className="column-picker">{EXPORT_COLUMNS.map((column)=><label key={column}><input type="checkbox" checked={columns.has(column)} onChange={()=>setColumns((value)=>{const next=new Set(value);next.has(column)?next.delete(column):next.add(column);return next;})}/><span>{column.replace(/_/g," ")}</span></label>)}</div><div className="modal-actions"><button className="btn btn-ghost" onClick={props.onClose}>Cancel</button><a className="btn btn-primary" href={exportCsvUrl(props.clauses,[...columns])}>Download CSV</a></div></Modal>}
function TagModal(props:{item:PropertyListItem;onClose:()=>void;onSave:(tags:string[])=>Promise<void>}){const[value,setValue]=useState((props.item.tags??[]).join(", "));return <Modal title="Edit property tags" description={addressOf(props.item)} onClose={props.onClose}><label className="field-label">Comma-separated tags</label><input className="text-input" value={value} onChange={(e)=>setValue(e.target.value)}/><div className="modal-actions"><button className="btn btn-ghost" onClick={props.onClose}>Cancel</button><button className="btn btn-primary" onClick={()=>props.onSave(value.split(",").map((tag)=>tag.trim()).filter(Boolean))}>Save tags</button></div></Modal>}
