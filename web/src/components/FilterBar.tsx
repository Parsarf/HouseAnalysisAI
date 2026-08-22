import { useState } from "react";
import { FILTER_OPS, type FilterClause, type FilterOp } from "../api";

export type FilterableField = "apn" | "address" | "city" | "state" | "zip5" | "pipeline_status" | "tags" | "next_action" | "next_action_date" | "gut_rating" | "is_watchlisted" | "lat" | "lng" | "created_at" | "updated_at";

interface FilterFieldDef {
  field: FilterableField;
  label: string;
  kind: "text" | "number" | "select" | "date" | "boolean";
  options?: string[];
  defaultOp: FilterOp;
}

export const FILTERABLE_FIELDS: readonly FilterFieldDef[] = [
  { field: "address", label: "Address", kind: "text", defaultOp: "contains" },
  { field: "apn", label: "APN", kind: "text", defaultOp: "eq" },
  { field: "city", label: "City", kind: "text", defaultOp: "eq" },
  { field: "state", label: "State", kind: "text", defaultOp: "eq" },
  { field: "zip5", label: "ZIP", kind: "text", defaultOp: "eq" },
  { field: "pipeline_status", label: "Pipeline status", kind: "select", options: ["new", "reviewing", "pursue", "offer_made", "under_contract", "dead"], defaultOp: "eq" },
  { field: "tags", label: "Tag", kind: "text", defaultOp: "contains" },
  { field: "next_action", label: "Next action", kind: "text", defaultOp: "contains" },
  { field: "next_action_date", label: "Next action date", kind: "date", defaultOp: "eq" },
  { field: "gut_rating", label: "Gut rating", kind: "number", defaultOp: "eq" },
  { field: "is_watchlisted", label: "Watchlisted", kind: "boolean", defaultOp: "eq" },
  { field: "lat", label: "Latitude", kind: "number", defaultOp: "between" },
  { field: "lng", label: "Longitude", kind: "number", defaultOp: "between" },
  { field: "created_at", label: "Created", kind: "date", defaultOp: "gte" },
  { field: "updated_at", label: "Updated", kind: "date", defaultOp: "gte" },
] as const;

const VALUE_OPS: readonly FilterOp[] = ["eq", "neq", "gt", "gte", "lt", "lte", "in", "between", "contains"];

export function describeClause(clause: FilterClause): string {
  const label = FILTERABLE_FIELDS.find((field) => field.field === clause.field)?.label ?? clause.field;
  if (clause.op === "is_null") return `${label} is empty`;
  if (clause.op === "between" && Array.isArray(clause.value)) return `${label} ${clause.value[0]} – ${clause.value[1]}`;
  if (clause.op === "in" && Array.isArray(clause.value)) return `${label}: ${clause.value.join(", ")}`;
  return `${label} ${clause.op.replace("eq", "is")} ${String(clause.value ?? "")}`;
}

function parseValue(raw: string, op: FilterOp, kind: FilterFieldDef["kind"]): unknown {
  if (op === "is_null") return true;
  if (op === "in") return raw.split(",").map((part) => part.trim()).filter(Boolean);
  if (op === "between") return raw.split(",").slice(0, 2).map((part) => kind === "number" ? Number(part.trim()) : part.trim());
  if (kind === "number") return Number(raw);
  if (kind === "boolean") return raw === "true";
  return raw;
}

export function FilterBar(props: { clauses: FilterClause[]; onChange: (clauses: FilterClause[]) => void; onApply?: (clauses: FilterClause[]) => Promise<void> | void; showArchived?: boolean; onShowArchived?: (value: boolean) => void }) {
  const [field, setField] = useState<FilterableField>("address");
  const def = FILTERABLE_FIELDS.find((item) => item.field === field) ?? FILTERABLE_FIELDS[0];
  const [op, setOp] = useState<FilterOp>(def.defaultOp);
  const [raw, setRaw] = useState("");
  const add = async () => {
    if (VALUE_OPS.includes(op) && !raw.trim()) return;
    const next = [...props.clauses, { field, op, value: parseValue(raw, op, def.kind) }];
    await props.onApply?.(next);
    props.onChange(next);
    setRaw("");
  };
  return <div className="filter-builder">
    <div className="filter-controls">
      <select className="select-input" aria-label="Filter field" value={field} onChange={(event) => { const next = FILTERABLE_FIELDS.find((item) => item.field === event.target.value) ?? FILTERABLE_FIELDS[0]; setField(next.field); setOp(next.defaultOp); setRaw(""); }}>
        {FILTERABLE_FIELDS.map((item) => <option key={item.field} value={item.field}>{item.label}</option>)}
      </select>
      <select className="select-input" aria-label="Filter operator" value={op} onChange={(event) => setOp(event.target.value as FilterOp)}>
        {FILTER_OPS.map((candidate) => <option key={candidate} value={candidate}>{candidate.replace("is_null", "is empty")}</option>)}
      </select>
      {op !== "is_null" && (def.kind === "select" ? <select className="select-input" aria-label="Filter value" value={raw} onChange={(event) => setRaw(event.target.value)}><option value="">Choose…</option>{def.options?.map((value) => <option key={value} value={value}>{value.replace(/_/g," ")}</option>)}</select> : def.kind === "boolean" ? <select className="select-input" aria-label="Filter value" value={raw} onChange={(event) => setRaw(event.target.value)}><option value="">Choose…</option><option value="true">Yes</option><option value="false">No</option></select> : <input className="text-input" aria-label="Filter value" type={def.kind === "date" ? "date" : def.kind === "number" && op !== "between" ? "number" : "text"} value={raw} onChange={(event) => setRaw(event.target.value)} onKeyDown={(event) => event.key === "Enter" && add()} placeholder={op === "between" ? "low, high" : op === "in" ? "one, two" : "Value"} />)}
      <button className="btn btn-secondary" onClick={add}>Add filter</button>
    </div>
    <label className="check-row archive-toggle"><input type="checkbox" checked={Boolean(props.showArchived)} onChange={(event) => props.onShowArchived?.(event.target.checked)} /><span><strong>Show archived</strong></span></label>
    {props.clauses.length > 0 && <div className="active-filters">{props.clauses.map((clause, index) => <span key={`${clause.field}-${index}`}>{describeClause(clause)}<button aria-label={`Remove ${describeClause(clause)}`} onClick={() => props.onChange(props.clauses.filter((_, itemIndex) => itemIndex !== index))}>×</button></span>)}<button className="clear-filters" onClick={() => props.onChange([])}>Clear all</button></div>}
  </div>;
}
