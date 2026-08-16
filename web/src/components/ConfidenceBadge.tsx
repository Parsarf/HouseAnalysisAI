/**
 * Confidence indicator for a 0..1 confidence value (fact-level) — thin bar
 * plus a percentage, with tooltip naming the source.
 */
import type { CSSProperties } from "react";
import { palette } from "./ui";

export function ConfidenceBadge(props: { confidence: number; sourceKind?: string; style?: CSSProperties }) {
  const pct = Math.round(Math.max(0, Math.min(1, props.confidence)) * 100);
  const color = pct >= 80 ? palette.good : pct >= 50 ? palette.warn : palette.bad;
  const title = props.sourceKind ? `confidence ${pct}% · source: ${props.sourceKind}` : `confidence ${pct}%`;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 5, ...props.style }} title={title}>
      <span style={{ width: 36, height: 5, background: palette.subtle, borderRadius: 3, overflow: "hidden" }}>
        <span style={{ display: "block", width: `${pct}%`, height: "100%", background: color }} />
      </span>
      <span style={{ fontSize: 12, color: palette.muted, fontVariantNumeric: "tabular-nums" }}>{pct}%</span>
    </span>
  );
}
