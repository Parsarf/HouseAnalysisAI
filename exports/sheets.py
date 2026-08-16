"""Deal sheet and seller net sheet rendering (spec §9.4).

Both sheets are HTML rendered from the same contract objects the API payload
uses, so there is one source of truth for what a property "is". PDF conversion
is handled by the caller (WeasyPrint) — this module produces the HTML.
"""
import html
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from contracts import (
    AttachmentBasis,
    NormalizedProperty,
    OfferPoint,
    Scenario,
    ScoreSet,
    StrategyResult,
    StrategyType,
    TrackedValue,
    UnderwritingResult,
)

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
body {{ font-family: Helvetica, Arial, sans-serif; margin: 32px; color: #222; font-size: 13px; }}
h1 {{ font-size: 20px; margin-bottom: 4px; }}
h2 {{ font-size: 14px; text-transform: uppercase; color: #555; border-bottom: 1px solid #ccc; padding-bottom: 4px; }}
table {{ border-collapse: collapse; width: 100%; margin: 8px 0 16px; }}
td, th {{ text-align: left; padding: 4px 8px; border-bottom: 1px solid #eee; }}
.photo {{ width: 100%; height: 120px; background: #eee; border: 1px dashed #bbb;
         display: flex; align-items: center; justify-content: center; color: #999; }}
.range {{ font-weight: bold; }}
.banner {{ background: #fdecea; border: 1px solid #e6a19a; padding: 8px; margin: 8px 0; }}
.footer {{ margin-top: 24px; font-size: 11px; color: #666; }}
</style></head><body>
{body}
</body></html>
"""


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _usd(value: Decimal | None) -> str:
    if value is None:
        return "&mdash;"
    return f"${value:,.0f}"


def _tracked(value: TrackedValue | None) -> Decimal | None:
    return None if value is None else value.value


def _address_line(prop: NormalizedProperty) -> str:
    parts = [prop.address.line1, prop.address.city, prop.address.state, prop.address.zip5]
    return ", ".join(_esc(part) for part in parts if part)


def estimated_figures(prop: NormalizedProperty) -> list[str]:
    """Labels of every figure backed by an estimated TrackedValue — the deal
    sheet footer is generated from this, never hand-maintained."""
    labels: list[str] = []

    def check(label: str, tracked: TrackedValue | None) -> None:
        if tracked is not None and tracked.value is not None and tracked.is_estimated:
            labels.append(label)

    for name in ("property_type", "beds", "baths", "sqft", "lot_sqft", "year_built", "units"):
        check(name, getattr(prop.attributes, name))
    check("purchase price", prop.ownership.purchase_price)
    for candidate in prop.valuation_candidates:
        check(f"valuation ({candidate.valuation_type})", candidate.value)
    for mortgage in prop.mortgages:
        check(f"{mortgage.position} mortgage balance", mortgage.estimated_balance)
    for lien in prop.liens:
        check(f"{lien.lien_type} lien amount", lien.amount)
    if prop.foreclosure is not None:
        check("published bid", prop.foreclosure.published_bid)
        check("default amount", prop.foreclosure.default_amount)
    check("annual taxes", prop.taxes.annual_taxes)
    check("assessed value", prop.taxes.assessed_value)
    check("delinquent taxes", prop.taxes.delinquent_amount)
    check("HOA monthly dues", prop.hoa.monthly_dues)
    check("HOA arrears", prop.hoa.arrears)
    check("rent estimate", prop.rental.rent_estimate)
    for listing in prop.listings:
        check(f"list price ({listing.list_date})", listing.price)
    return labels


def _strategy_rank(result: StrategyResult) -> Decimal:
    return result.mao if result.mao is not None else Decimal("-1")


def deal_sheet_html(prop: NormalizedProperty,
                    underwriting: UnderwritingResult | None = None,
                    strategies: list[StrategyResult] | None = None,
                    scores: ScoreSet | None = None) -> str:
    """One-page deal sheet (spec §9.4). Renders for missing-data properties."""
    strategies = strategies or []
    top = sorted((s for s in strategies if s.mao is not None), key=_strategy_rank, reverse=True)[:2]

    rows = []
    if underwriting is not None and underwriting.status == "ok":
        value = underwriting.value
        equity = underwriting.equity.get(Scenario.EXPECTED) or next(iter(underwriting.equity.values()), None)
        rows.append(("Value (low / expected / high)",
                     f"{_usd(value.v_low)} / {_usd(value.v_expected)} / {_usd(value.v_high)}"))
        rows.append(("Confirmed debt", _usd(underwriting.liabilities.confirmed)))
        rows.append(("Potential debt", _usd(underwriting.liabilities.potential)))
        if equity is not None:
            rows.append(("Equity (expected)", _usd(equity.gross)))
    else:
        reason = None if underwriting is None else underwriting.unavailable_reason
        rows.append(("Underwriting", f"unavailable{': ' + _esc(reason) if reason else ''}"))
    summary_rows = "".join(f"<tr><th>{label}</th><td>{value}</td></tr>" for label, value in rows)

    strategy_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            _esc(s.strategy.value if isinstance(s.strategy, StrategyType) else s.strategy),
            _esc(s.scenario), _usd(s.mao), _usd(s.profit))
        for s in top
    ) or "<tr><td colspan=\"4\">No viable strategies computed.</td></tr>"

    timeline = []
    foreclosure = prop.foreclosure
    if foreclosure is not None and foreclosure.is_active:
        for label, when in (("NOD", foreclosure.nod_date), ("NTS", foreclosure.nts_date),
                            ("Sale", foreclosure.current_sale_date)):
            if when is not None:
                timeline.append(f"<tr><th>{label}</th><td>{_esc(when)}</td></tr>")
        if foreclosure.postponement_count:
            timeline.append(f"<tr><th>Postponements</th><td>{foreclosure.postponement_count}</td></tr>")
    timeline_html = "".join(timeline) or "<tr><td>No active foreclosure timeline.</td></tr>"

    confidence = "&mdash;" if scores is None else f"{scores.data_confidence:.0f} / 100"
    overall = "&mdash;" if scores is None else f"{scores.overall:.0f} / 100"

    estimated = estimated_figures(prop)
    footer = ("Estimated figures: " + ", ".join(_esc(label) for label in estimated)) if estimated \
        else "No estimated figures; all values are as reported."

    body = f"""
<h1>{_address_line(prop)}</h1>
<div class="photo">photo placeholder</div>
<h2>Executive summary</h2>
<table>{summary_rows}
<tr><th>Data confidence</th><td>{confidence}</td></tr>
<tr><th>Overall score</th><td>{overall}</td></tr></table>
<h2>Top strategies</h2>
<table><tr><th>Strategy</th><th>Scenario</th><th>MAO</th><th>Profit</th></tr>{strategy_rows}</table>
<h2>Distress timeline</h2>
<table>{timeline_html}</table>
<div class="footer">{footer}</div>
"""
    return _PAGE.format(body=body)


def _unverified_obligations(prop: NormalizedProperty) -> list[str]:
    obligations = []
    for lien in prop.liens:
        if lien.status == "released":
            continue
        if lien.attachment_basis != AttachmentBasis.RECORDED_AGAINST_PROPERTY:
            basis = "owner-named only" if lien.attachment_basis == AttachmentBasis.OWNER_NAMED_ONLY else "unknown attachment"
            obligations.append(f"{_esc(lien.lien_type)} lien ({basis}): {_usd(_tracked(lien.amount))}")
    return obligations


def net_sheet_html(prop: NormalizedProperty, offer: OfferPoint,
                   underwriting: UnderwritingResult | None = None) -> str:
    """Seller net sheet (spec §9.2/§9.4). When potential obligations exist the
    proceeds render as a range — never a single number."""
    deduction_rows = [
        ("Confirmed payoffs (mortgages, property-attached liens)", offer.confirmed_payoffs),
        ("Potential obligations (unverified)", offer.potential_payoffs),
        ("Seller closing costs (title, escrow, transfer tax, recording)", offer.closing_costs),
    ]
    deductions = "".join(
        f"<tr><td>{label}</td><td>{_usd(amount)}</td></tr>" for label, amount in deduction_rows)

    if offer.potential_payoffs > 0:
        proceeds = (
            f"<tr><th>Estimated proceeds</th><td class=\"range\">"
            f"{_usd(offer.proceeds_low)} &ndash; {_usd(offer.proceeds_high)} "
            f"(most likely {_usd(offer.proceeds_expected)})</td></tr>"
            "<tr><td colspan=\"2\">Proceeds are shown as a range because some obligations "
            "are unverified.</td></tr>"
        )
    else:
        proceeds = f"<tr><th>Estimated proceeds</th><td class=\"range\">{_usd(offer.proceeds_expected)}</td></tr>"

    banner = ""
    if offer.is_short_sale:
        banner = ("<div class=\"banner\">At this offer the proceeds do not cover the confirmed "
                  "obligations. This sale would require lender approval of a short sale.</div>")

    unverified = _unverified_obligations(prop)
    unverified_html = (
        "<h2>Unverified obligations</h2><p>The following obligations are not verified against "
        "the property and may not be owed at closing:</p><ul>"
        + "".join(f"<li>{item}</li>" for item in unverified) + "</ul>"
    ) if unverified else ""

    body = f"""
<h1>Seller net sheet &mdash; {_address_line(prop)}</h1>
{banner}
<table>
<tr><th>Offer price</th><td>{_usd(offer.offer_price)}</td></tr>
{deductions}
{proceeds}
</table>
{unverified_html}
<div class="footer">These figures are estimates prepared for discussion purposes and are not a
commitment. Actual closing figures are determined by the title company.</div>
"""
    return _PAGE.format(body=body)


def write_export(document_root: Path, property_id: UUID, filename: str, content: str) -> Path:
    """Store a rendered export under documents/{property_id}/exports/ (spec §9.4)."""
    directory = Path(document_root) / str(property_id) / "exports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    return path
