/**
 * Seller-proceeds figure. Per WP-13 the component refuses to collapse a range:
 * when potential (unverified) liabilities exist, the props *require* low and
 * high — the single-value variant is impossible to construct in that case.
 */
import { formatDecimalString } from "./Money";
import { palette } from "./ui";

export type ProceedsFigureProps =
  | { hasPotentialLiabilities: false; expected: string }
  | { hasPotentialLiabilities: true; low: string; expected: string; high: string };

export function ProceedsFigure(props: ProceedsFigureProps) {
  if (!props.hasPotentialLiabilities) {
    return (
      <span style={{ fontVariantNumeric: "tabular-nums" }} title="All payoffs are confirmed">
        {formatDecimalString(props.expected, 2)}
      </span>
    );
  }
  return (
    <span style={{ fontVariantNumeric: "tabular-nums" }}>
      <span style={{ color: palette.bad }}>{formatDecimalString(props.low, 2)}</span>
      {" – "}
      <span style={{ color: palette.good }}>{formatDecimalString(props.high, 2)}</span>
      <span style={{ color: palette.muted, fontSize: 12 }}>
        {" "}
        (expected {formatDecimalString(props.expected, 2)}; includes unverified obligations)
      </span>
    </span>
  );
}
