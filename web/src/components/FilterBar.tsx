/**
 * Filter bar emitting the shared filter grammar: `{field, op, value}[]` with
 * the closed operator set from contracts.FilterClause. The allowlist below is
 * the client-side view of the API's allowlist of filterable fields (spec
 * §11.1); the server remains authoritative.
 */
import { useState } from "react";
import { FILTER_OPS, type FilterClause, type FilterOp } from "../api";
import { button, mutedText, palette } from "./ui";

interface FilterFieldDef {
  field: string;
  label: string;
  kind: "text" | "number" | "select";
  options?: string[];
  defaultOp: FilterOp;
}

export const FILTERABLE_FIELDS: readonly FilterFieldDef[] = [
  { field: "address.city", label: "City", kind: "text", defaultOp: "eq" },
  { field: "address.zip5", label: "ZIP", kind: "text", defaultOp: "eq" },
  { field: "address.county", label: "County", kind: "text", defaultOp: "eq" },
  { field: "scores.overall", label: "Overall score", kind: "number", defaultOp: "gte" },
  { field: "scores.distress", label: "Distress", kind: "number", defaultOp: "gte" },
  { field: "underwriting.value.v_expected", label: "Est. value", kind: "number", defaultOp: "gte" },
  { field: "underwriting.equity.adjusted", label: "Equity $", kind: "number", defaultOp: "gte" },
  { field: "underwriting.equity.equity_pct", label: "Equity %", kind: "number", defaultOp: "gte" },
  { field: "foreclosure.stage", label: "Foreclosure stage", kind: "text", defaultOp: "eq" },
  { field: "foreclosure.current_sale_date", label: "Auction date", kind: "text", defaultOp: "gte" },
  { field: "pipeline_status", label: "Status", kind: "select", options: ["new", "reviewing", "pursue", "offer_made", "under_contract", "dead"], defaultOp: "eq" },
  { field: "tags", label: "Tag", kind: "text", defaultOp: "contains" },
];

const OPS_REQUIRING_VALUE: readonly FilterOp[] = ["eq", "neq", "gt", "gte", "lt", "lte", "in", "between", "contains"];

export function describeClause(clause: FilterClause): string {
  const label = FILTERABLE_FIELDS.find((f) => f.field === clause.field)?.label ?? clause.field;
  if (clause.op === "is_null") return `${label} is empty`;
  if (clause.op === "between" && Array.isArray(clause.value)) return `${label} between ${clause.value[0]} and ${clause.value[1]}`;
  if (clause.op === "in" && Array.isArray(clause.value)) return `${label} in [${clause.value.join(", ")}]`;
  return `${label} ${clause.op} ${String(clause.value ?? "")}`;
}

function parseValue(raw: string, op: FilterOp, kind: FilterFieldDef["kind"]): unknown {
  if (op === "is_null") return true;
  if (op === "in") return raw.split(",").map((part) => part.trim()).filter(Boolean);
  if (op === "between") {
    const [lo, hi] = raw.split(",").map((part) => part.trim());
    return [kind === "number" ? Number(lo) : lo, kind === "number" ? Number(hi) : hi];
  }
  return kind === "number" ? Number(raw) : raw;
}

export function FilterBar(props: { clauses: FilterClause[]; onChange: (clauses: FilterClause[]) => void }) {
  const [field, setField] = useState<string>(FILTERABLE_FIELDS[0].field);
  const def = FILTERABLE_FIELDS.find((f) => f.field === field) ?? FILTERABLE_FIELDS[0];
  const [op, setOp] = useState<FilterOp>(def.defaultOp);
  const [rawValue, setRawValue] = useState("");

  const addClause = () => {
    if (OPS_REQUIRING_VALUE.includes(op) && rawValue.trim() === "") return;
    props.onChange([...props.clauses, { field, op, value: parseValue(rawValue, op, def.kind) }]);
    setRawValue("");
  };

  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <select
          value={field}
          onChange={(e) => {
            const next = FILTERABLE_FIELDS.find((f) => f.field === e.target.value) ?? FILTERABLE_FIELDS[0];
            setField(next.field);
            setOp(next.defaultOp);
          }}
          style={{ padding: "5px 8px" }}
        >
          {FILTERABLE_FIELDS.map((f) => (
            <option key={f.field} value={f.field}>
              {f.label}
            </option>
          ))}
        </select>
        <select value={op} onChange={(e) => setOp(e.target.value as FilterOp)} style={{ padding: "5px 8px" }}>
          {FILTER_OPS.map((candidate) => (
            <option key={candidate} value={candidate}>
              {candidate}
            </option>
          ))}
        </select>
        {op === "is_null" ? null : def.kind === "select" ? (
          <select value={rawValue} onChange={(e) => setRawValue(e.target.value)} style={{ padding: "5px 8px" }}>
            <option value="">—</option>
            {(def.options ?? []).map((option) => (
              <option key={option} value={option}>
                {option.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        ) : (
          <input
            value={rawValue}
            onChange={(e) => setRawValue(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && addClause()}
            placeholder={op === "between" ? "low, high" : op === "in" ? "a, b, c" : "value"}
            type={def.kind === "number" && (op === "eq" || op === "neq" || op.startsWith("g") || op.startsWith("l")) ? "number" : "text"}
            style={{ padding: "5px 8px", width: 160 }}
          />
        )}
        <button style={button} onClick={addClause}>
          Add filter
        </button>
      </div>
      {props.clauses.length > 0 && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8 }}>
          {props.clauses.map((clause, index) => (
            <span
              key={index}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                border: `1px solid ${palette.border}`,
                borderRadius: 12,
                padding: "2px 10px",
                fontSize: 12,
                background: palette.subtle,
              }}
            >
              {describeClause(clause)}
              <button
                style={{ ...button, border: "none", background: "transparent", padding: 0, color: palette.muted }}
                onClick={() => props.onChange(props.clauses.filter((_, i) => i !== index))}
                aria-label={`Remove filter ${describeClause(clause)}`}
              >
                ×
              </button>
            </span>
          ))}
          <button style={{ ...button, fontSize: 12 }} onClick={() => props.onChange([])}>
            Clear all
          </button>
          <span style={mutedText}>filters are applied server-side</span>
        </div>
      )}
    </div>
  );
}
