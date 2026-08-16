/**
 * Portfolio list view (WP-12 subset): a sortable, filterable table emitting
 * the shared filter grammar. Sort/filter state is URL-encoded so views are
 * shareable. Rank/score/value/equity columns render once the API serves those
 * rollups; until then they show "—".
 */
import { useEffect, useMemo, useState } from "react";
import {
  listProperties,
  type FilterClause,
  type PropertyListItem,
} from "../api";
import { FilterBar } from "../components/FilterBar";
import { MoneyText } from "../components/Money";
import { parseScore, ScoreBar } from "../components/ScoreBar";
import { button, mutedText, palette, table, td, th } from "../components/ui";
import { Link, replaceUrl } from "../router";

const SORTABLE_COLUMNS: readonly { key: string; label: string }[] = [
  { key: "rank", label: "Rank" },
  { key: "address", label: "Address" },
  { key: "address.city", label: "City" },
  { key: "pipeline_status", label: "Status" },
  { key: "scores.overall", label: "Score" },
  { key: "underwriting.value.v_expected", label: "Est. value" },
  { key: "underwriting.equity.adjusted", label: "Equity" },
];

function readFiltersFromUrl(): FilterClause[] {
  const raw = new URLSearchParams(window.location.search).get("filters");
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (clause): clause is FilterClause =>
        typeof clause === "object" && clause !== null && "field" in clause && "op" in clause,
    );
  } catch {
    return [];
  }
}

function readSortFromUrl(): { sort: string; order: "asc" | "desc" } {
  const params = new URLSearchParams(window.location.search);
  const sort = params.get("sort") ?? "scores.overall";
  const order = params.get("order") === "asc" ? "asc" : "desc";
  return { sort, order };
}

export function PropertiesPage() {
  const [clauses, setClauses] = useState<FilterClause[]>(readFiltersFromUrl);
  const [{ sort, order }, setSorting] = useState(readSortFromUrl);
  const [items, setItems] = useState<PropertyListItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Shareable URL: reflect filter/sort state without triggering navigation.
  useEffect(() => {
    const params = new URLSearchParams();
    if (clauses.length > 0) params.set("filters", JSON.stringify(clauses));
    params.set("sort", sort);
    params.set("order", order);
    replaceUrl(`/properties?${params.toString()}`);
  }, [clauses, sort, order]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    listProperties({ sort, order, filters: clauses })
      .then((response) => {
        if (cancelled) return;
        setItems(response.items);
        setNextCursor(response.next_cursor);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "failed to load properties");
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [clauses, sort, order]);

  const loadMore = () => {
    if (!nextCursor) return;
    listProperties({ sort, order, filters: clauses, cursor: nextCursor })
      .then((response) => {
        setItems((prev) => [...prev, ...response.items]);
        setNextCursor(response.next_cursor);
      })
      .catch(() => setNextCursor(null));
  };

  const toggleSort = (key: string) => {
    setSorting((prev) =>
      prev.sort === key ? { sort: key, order: prev.order === "asc" ? "desc" : "asc" } : { sort: key, order: "desc" },
    );
  };

  const header = useMemo(
    () => (
      <tr>
        {SORTABLE_COLUMNS.map((column) => (
          <th key={column.key} style={{ ...th, cursor: "pointer" }} onClick={() => toggleSort(column.key)}>
            {column.label}
            {sort === column.key ? (order === "asc" ? " ▲" : " ▼") : ""}
          </th>
        ))}
      </tr>
    ),
    [sort, order],
  );

  return (
    <section>
      <h2 style={{ margin: "0 0 12px", fontSize: 18 }}>Portfolio</h2>
      <FilterBar clauses={clauses} onChange={setClauses} />
      {error && <p style={{ color: palette.bad }}>{error}</p>}
      {loading ? (
        <p style={mutedText}>Loading…</p>
      ) : items.length === 0 ? (
        <p style={mutedText}>No properties match these filters.</p>
      ) : (
        <>
          <table style={table}>
            <thead>{header}</thead>
            <tbody>
              {items.map((item) => {
                const score = parseScore(item.overall_score);
                return (
                  <tr key={item.id}>
                    <td style={{ ...td, fontVariantNumeric: "tabular-nums" }}>
                      {item.rank !== null && item.rank !== undefined
                        ? `#${item.rank}${item.rank_total ? ` of ${item.rank_total}` : ""}`
                        : "—"}
                    </td>
                    <td style={td}>
                      <Link to={`/properties/${item.id}`} style={{ color: palette.accent }}>
                        {item.address || "Unknown address"}
                      </Link>
                    </td>
                    <td style={td}>{item.city ?? "—"}</td>
                    <td style={td}>{item.status.replace(/_/g, " ")}</td>
                    <td style={td}>{score === null ? "—" : <ScoreBar value={score} />}</td>
                    <td style={td}>
                      <MoneyText money={item.value} />
                    </td>
                    <td style={td}>
                      <MoneyText money={item.equity} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {nextCursor && (
            <div style={{ marginTop: 12 }}>
              <button style={button} onClick={loadMore}>
                Load more
              </button>
            </div>
          )}
        </>
      )}
    </section>
  );
}
