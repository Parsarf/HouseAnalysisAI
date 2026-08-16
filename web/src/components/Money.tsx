/**
 * Money display — the shared contract from WP-12: takes the
 * `{value, confidence, source_kind, is_estimated, null_reason}` envelope and
 * renders estimated values distinctly (italic + dotted underline + tooltip).
 * Nothing in the app renders a raw money number; everything goes through here.
 */
import type { CSSProperties } from "react";
import type { TrackedValue } from "../api";
import { palette } from "./ui";

export function formatDecimalString(value: string | null | undefined, fractionDigits = 0): string {
  if (value === null || value === undefined || value === "") return "—";
  const num = Number(value);
  if (!Number.isFinite(num)) return value;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(num);
}

/** Ratio-as-decimal-string ("0.123") -> "12.3%". */
export function formatPercentString(value: string | null | undefined, fractionDigits = 1): string {
  if (value === null || value === undefined || value === "") return "—";
  const num = Number(value);
  if (!Number.isFinite(num)) return value;
  return `${(num * 100).toFixed(fractionDigits)}%`;
}

function titleFor(money: TrackedValue): string {
  return `${money.is_estimated ? "Estimated" : "Recorded"} · source: ${money.source_kind} · confidence ${Math.round(
    money.confidence * 100,
  )}%`;
}

export function MoneyText(props: { money: TrackedValue | null | undefined; cents?: boolean; style?: CSSProperties }) {
  const { money, cents = false, style } = props;
  if (!money || money.value === null || money.value === undefined) {
    const reason = money?.null_reason ? `No value — ${money.null_reason.replace(/_/g, " ")}` : "No value";
    return (
      <span style={{ color: palette.muted, ...style }} title={reason}>
        —
      </span>
    );
  }
  const text = formatDecimalString(money.value, cents ? 2 : 0);
  if (money.is_estimated) {
    return (
      <span
        style={{
          fontStyle: "italic",
          textDecoration: "underline dotted",
          textUnderlineOffset: 3,
          ...style,
        }}
        title={titleFor(money)}
      >
        {text}
      </span>
    );
  }
  return (
    <span style={style} title={titleFor(money)}>
      {text}
    </span>
  );
}
