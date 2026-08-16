/** Small 0–100 score readout with a bar, used in the table and score card. */
import type { CSSProperties } from "react";
import { palette } from "./ui";

export function scoreColor(score: number): string {
  if (score >= 70) return palette.good;
  if (score >= 40) return palette.warn;
  return palette.bad;
}

export function ScoreBar(props: { value: number; label?: string; style?: CSSProperties }) {
  const clamped = Math.max(0, Math.min(100, props.value));
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, ...props.style }} title={props.label}>
      {props.label !== undefined && <span style={{ color: palette.muted, fontSize: 12 }}>{props.label}</span>}
      <span style={{ width: 48, height: 6, background: palette.subtle, borderRadius: 3, overflow: "hidden" }}>
        <span
          style={{
            display: "block",
            width: `${clamped}%`,
            height: "100%",
            background: scoreColor(clamped),
          }}
        />
      </span>
      <span style={{ fontVariantNumeric: "tabular-nums" }}>{Math.round(clamped)}</span>
    </span>
  );
}

/** Parse a decimal-string score ("72.5") for ScoreBar; null-safe. */
export function parseScore(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}
