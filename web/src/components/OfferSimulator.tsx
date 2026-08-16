/**
 * Offer simulator (spec §9.3). The slider snaps to the 9 precomputed grid
 * points and linearly interpolates between them — exact, because every
 * quantity is linear in the offer price (WP-7's linearity guarantee). The
 * only arithmetic here is that interpolation; off-grid exact entry posts to
 * the API for an authoritative answer.
 */
import { useEffect, useMemo, useState } from "react";
import { postOffer, type OfferGrid, type OfferPoint, type Scenario } from "../api";
import { formatPercentString, MoneyText } from "./Money";
import { ProceedsFigure } from "./ProceedsFigure";
import { button, mutedText, palette } from "./ui";

const MONEY_FIELDS = [
  "confirmed_payoffs",
  "potential_payoffs",
  "closing_costs",
  "proceeds_low",
  "proceeds_expected",
  "proceeds_high",
  "buyer_basis",
  "profit",
] as const;

function toCents(value: number): string {
  return (Math.round(value * 100) / 100).toFixed(2);
}

function lerp(a: string, b: string, t: number): string {
  return toCents(Number(a) + (Number(b) - Number(a)) * t);
}

/**
 * Interpolate the grid at `offerPrice`. Clamps to the end points outside the
 * grid; between two adjacent points it lerps every money field. Returns null
 * when the scenario has no points.
 */
export function interpolatePoint(points: OfferPoint[], offerPrice: number): OfferPoint | null {
  if (points.length === 0) return null;
  const sorted = [...points].sort((a, b) => Number(a.offer_price) - Number(b.offer_price));
  if (offerPrice <= Number(sorted[0].offer_price)) return { ...sorted[0], offer_price: toCents(offerPrice) };
  const last = sorted[sorted.length - 1];
  if (offerPrice >= Number(last.offer_price)) return { ...last, offer_price: toCents(offerPrice) };
  let lo = sorted[0];
  let hi = sorted[1];
  for (let i = 0; i < sorted.length - 1; i += 1) {
    if (Number(sorted[i].offer_price) <= offerPrice && offerPrice <= Number(sorted[i + 1].offer_price)) {
      lo = sorted[i];
      hi = sorted[i + 1];
      break;
    }
  }
  const x0 = Number(lo.offer_price);
  const x1 = Number(hi.offer_price);
  const t = x1 === x0 ? 0 : (offerPrice - x0) / (x1 - x0);
  const interpolated: OfferPoint = {
    ...lo,
    offer_price: toCents(offerPrice),
    label: null,
  };
  for (const field of MONEY_FIELDS) interpolated[field] = lerp(lo[field], hi[field], t);
  interpolated.roi =
    lo.roi !== null && lo.roi !== undefined && hi.roi !== null && hi.roi !== undefined
      ? String(Number(lo.roi) + (Number(hi.roi) - Number(lo.roi)) * t)
      : null;
  interpolated.is_short_sale = offerPrice < Number(interpolated.confirmed_payoffs);
  return interpolated;
}

export function OfferSimulator(props: { grid: OfferGrid; scenario: Scenario; propertyId: string }) {
  const points = useMemo(
    () =>
      props.grid.points
        .filter((point) => point.scenario === props.scenario)
        .sort((a, b) => Number(a.offer_price) - Number(b.offer_price)),
    [props.grid, props.scenario],
  );
  const min = points.length > 0 ? Number(points[0].offer_price) : 0;
  const max = points.length > 0 ? Number(points[points.length - 1].offer_price) : 0;
  const [offer, setOffer] = useState<number>(() => (points.length > 0 ? Number(points[Math.floor(points.length / 2)].offer_price) : 0));
  const [exactInput, setExactInput] = useState("");
  const [exactResult, setExactResult] = useState<OfferPoint | null>(null);
  const [exactError, setExactError] = useState<string | null>(null);

  const money = (value: string, estimated = false) => ({ value, confidence: 1, source_kind: estimated ? "derived" as const : "report" as const, is_estimated: estimated });

  useEffect(() => {
    if (points.length > 0) setOffer(Number(points[Math.floor(points.length / 2)].offer_price));
    setExactResult(null);
    setExactError(null);
  }, [points]);

  if (points.length === 0 || min === max) {
    return <p style={mutedText}>No offer grid is available for this scenario yet.</p>;
  }

  const current = interpolatePoint(points, offer);
  const onGrid = points.some((point) => Number(point.offer_price) === Number(current?.offer_price));

  const submitExact = async () => {
    setExactError(null);
    setExactResult(null);
    try {
      setExactResult(await postOffer(props.propertyId, exactInput.trim(), props.scenario));
    } catch (error) {
      setExactError(error instanceof Error ? error.message : "offer evaluation failed");
    }
  };

  const figure = (label: string, value: string, cents = true) => (
    <div>
      <div style={{ fontSize: 12, color: palette.muted }}>{label}</div>
      <div style={{ fontSize: 16, fontVariantNumeric: "tabular-nums" }}><MoneyText money={money(value, !cents)} cents={cents} /></div>
    </div>
  );

  const reconcile = async () => {
    if (!current) return;
    try {
      const server = await postOffer(props.propertyId, current.offer_price, props.scenario);
      const delta = Math.abs(Number(server.proceeds_expected) - Number(current.proceeds_expected));
      if (delta > 0.01) console.error("Offer interpolation mismatch", { client: current, server, delta });
      setExactResult(server);
    } catch (reason) {
      setExactError(reason instanceof Error ? reason.message : "Unable to reconcile offer");
    }
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 10 }}>
        {points.map((point) => (
          <button
            key={point.offer_price}
            style={{
              ...button,
              fontSize: 12,
              borderColor: Number(point.offer_price) === offer ? palette.accent : palette.border,
            }}
            onClick={() => setOffer(Number(point.offer_price))}
            title={point.label ?? undefined}
          >
            <MoneyText money={money(point.offer_price)} />
          </button>
        ))}
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={(max - min) / 1000}
        value={offer}
        onChange={(e) => setOffer(Number(e.target.value))}
        onMouseUp={reconcile}
        onTouchEnd={reconcile}
        style={{ width: "100%" }}
        aria-label="Offer price"
      />
      {current && (
        <>
          <div style={{ display: "flex", gap: 24, flexWrap: "wrap", margin: "12px 0" }}>
            {figure("Offer", current.offer_price)}
            <div>
              <div style={{ fontSize: 12, color: palette.muted }}>Seller proceeds</div>
              <div style={{ fontSize: 16 }}>
                {Number(current.potential_payoffs) > 0 ? (
                  <ProceedsFigure
                    hasPotentialLiabilities
                    low={current.proceeds_low}
                    expected={current.proceeds_expected}
                    high={current.proceeds_high}
                  />
                ) : (
                  <ProceedsFigure hasPotentialLiabilities={false} expected={current.proceeds_expected} />
                )}
              </div>
            </div>
            {figure("Confirmed payoffs", current.confirmed_payoffs)}
            {figure("Closing costs", current.closing_costs)}
            {figure("Buyer basis", current.buyer_basis)}
            {figure("Profit", current.profit)}
            <div>
              <div style={{ fontSize: 12, color: palette.muted }}>ROI</div>
              <div style={{ fontSize: 16 }}>{formatPercentString(current.roi)}</div>
            </div>
          </div>
          <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
            {!onGrid && <span style={mutedText}>interpolated between grid points (exact — linear)</span>}
            {current.is_short_sale && (
              <span style={{ color: palette.warn, fontSize: 13, fontWeight: 600 }}>
                short sale — offer does not cover confirmed payoffs
              </span>
            )}
          </div>
        </>
      )}
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 12 }}>
        <input
          value={exactInput}
          onChange={(e) => setExactInput(e.target.value)}
          placeholder="Exact offer, e.g. 780000"
          inputMode="decimal"
          style={{ padding: "5px 8px", width: 200 }}
        />
        <button style={button} onClick={submitExact} disabled={exactInput.trim() === ""}>
          Evaluate exactly
        </button>
        <span style={mutedText}>authoritative value from the server</span>
      </div>
      {exactError && <p style={{ color: palette.bad, fontSize: 13 }}>{exactError}</p>}
      {exactResult && (
        <p style={{ fontSize: 13, marginTop: 8 }}>
          Server: offer <MoneyText money={money(exactResult.offer_price)} cents /> → profit{" "}
          <MoneyText money={money(exactResult.profit, true)} cents /> · ROI {formatPercentString(exactResult.roi)} · proceeds{" "}
          <MoneyText money={money(exactResult.proceeds_expected, true)} cents />
        </p>
      )}
    </div>
  );
}
