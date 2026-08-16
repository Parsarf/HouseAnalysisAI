"""Offline tests for analyst/ (WP-14): deterministic ScoreSet comparison,
phrasing number-validation, and the §11.5 comp-set table."""
from decimal import Decimal
from uuid import uuid4

import pytest

from analyst import (
    CompSetEntry,
    build_comp_set,
    compare_scores,
    explain_comparison,
    extract_numbers,
    phrasing_payload,
    template_explanation,
    validate_phrasing,
    why_above,
)
from contracts import (
    CostBlock,
    EquityBlock,
    LiabilityBlock,
    Scenario,
    ScoreSet,
    StrategyResult,
    StrategyType,
    UnderwritingResult,
    ValueBlock,
)


def make_scores(overall, fos, distress, dcs, risk, components, recommended=StrategyType.FLIP):
    return ScoreSet(property_id=uuid4(), scoring_config_id=uuid4(), fos=Decimal(str(fos)),
                    distress=Decimal(str(distress)), data_confidence=Decimal(str(dcs)),
                    risk=Decimal(str(risk)), overall=Decimal(str(overall)),
                    components={key: Decimal(str(value)) for key, value in components.items()},
                    gates_applied=[], is_rankable=True, recommended_strategy=recommended)


def fixture_pair():
    """57 Cottage above 42 Main, per the spec §13 example shape."""
    a = make_scores(overall=78, fos=70, distress=60, dcs=80, risk=20, components={
        "profit": Decimal(296000), "roi": Decimal("0.31"), "equity_pct": Decimal("0.52"),
        "discount_to_value": Decimal("0.20"), "margin_of_safety": Decimal("0.25"),
        "fos_profit": Decimal(30), "fos_roi": Decimal("15.5"), "fos_equity_pct": Decimal("17.3"),
        "fos_discount_to_value": Decimal("8.6"), "fos_margin_of_safety": Decimal("7.1"),
        "distress_foreclosure": Decimal(30), "distress_absentee": Decimal(5),
        "dcs_coverage": Decimal(27), "risk_lien_count": Decimal(6),
    })
    b = make_scores(overall=59, fos=52, distress=51, dcs=78, risk=38, components={
        "profit": Decimal(71000), "roi": Decimal("0.12"), "equity_pct": Decimal("0.30"),
        "discount_to_value": Decimal("0.10"), "margin_of_safety": Decimal("0.10"),
        "fos_profit": Decimal("14.2"), "fos_roi": Decimal(6), "fos_equity_pct": Decimal(10),
        "fos_discount_to_value": Decimal("4.3"), "fos_margin_of_safety": Decimal("2.9"),
        "distress_foreclosure": Decimal(24), "distress_absentee": Decimal(0),
        "dcs_coverage": Decimal(26), "risk_lien_count": Decimal(24),
    })
    return a, b


def test_terms_ranked_by_absolute_delta_with_risk_negated():
    a, b = fixture_pair()
    comparison = compare_scores(a, b, a_label="57 Cottage", b_label="42 Main")
    assert comparison.overall_delta == Decimal(19)
    assert comparison.pillar_deltas == {
        "fos": Decimal(18), "distress": Decimal(9),
        "dcs": Decimal(2), "risk": Decimal(18),  # risk is negated: 20 vs 38 favors A
    }
    deltas = [abs(term.point_delta) for term in comparison.terms]
    assert deltas == sorted(deltas, reverse=True)
    top = comparison.terms[0]
    assert top.name == "risk_lien_count"
    assert top.point_delta == Decimal(18)  # A has fewer risk points -> positive driver
    assert len(comparison.terms) <= 5


def test_raw_driver_values_ride_along():
    a, b = fixture_pair()
    comparison = compare_scores(a, b)
    profit_term = next(term for term in comparison.all_terms if term.name == "fos_profit")
    assert profit_term.a_raw == Decimal(296000)
    assert profit_term.b_raw == Decimal(71000)
    assert profit_term.raw_format == "money"
    assert profit_term.point_delta == Decimal("15.8")


def test_template_explanation_uses_only_payload_numbers():
    a, b = fixture_pair()
    comparison = compare_scores(a, b, a_label="57 Cottage", b_label="42 Main")
    text = template_explanation(comparison)
    assert "57 Cottage is 19.0 points above 42 Main" in text
    assert validate_phrasing(text, phrasing_payload(comparison))


def test_explanation_is_deterministic_across_runs():
    a, b = fixture_pair()
    outputs = {why_above(a, b, a_label="57 Cottage", b_label="42 Main")[1] for _ in range(20)}
    assert len(outputs) == 1


def test_clean_phraser_is_used_verbatim():
    a, b = fixture_pair()
    comparison = compare_scores(a, b, a_label="57 Cottage", b_label="42 Main")
    payload = phrasing_payload(comparison)

    def phraser(p):
        return ("57 Cottage beats 42 Main by 19.0 points: "
                "expected profit $296,000 vs $71,000 and a 18.0-point lien-count risk edge.")

    assert validate_phrasing(phraser(payload), payload)
    assert explain_comparison(comparison, phraser) == phraser(payload)


def test_poisoned_phraser_falls_back_to_template():
    a, b = fixture_pair()
    comparison = compare_scores(a, b)

    def poisoned(p):
        return "A wins because the ARV is $999,999 and three extra liens appeared."

    result = explain_comparison(comparison, poisoned)
    assert result == template_explanation(comparison)
    assert "999,999" not in result


def test_failing_phraser_falls_back_to_template():
    a, b = fixture_pair()
    comparison = compare_scores(a, b)

    def broken(p):
        raise RuntimeError("model unavailable")

    assert explain_comparison(comparison, broken) == template_explanation(comparison)


def test_extract_numbers_handles_money_percent_commas():
    assert extract_numbers("$1,200,000 and 25.0% and 0.25 and 18") == [
        Decimal(1200000), Decimal("25.0"), Decimal("0.25"), Decimal(18)]


def test_tied_scores_report_no_drivers():
    a, _ = fixture_pair()
    comparison = compare_scores(a, a, a_label="A", b_label="B")
    assert comparison.overall_delta == 0
    assert comparison.terms == []
    assert "tied" in template_explanation(comparison)


# --- comp set (spec §11.5) ---------------------------------------------------

def make_underwriting(v_expected, confirmed, equity):
    return UnderwritingResult(
        property_id=uuid4(), assumption_set_id=uuid4(), engine_version="test", status="ok",
        value=ValueBlock(v_expected=Decimal(str(v_expected))),
        liabilities=LiabilityBlock(confirmed=Decimal(str(confirmed))),
        equity={Scenario.EXPECTED: EquityBlock(adjusted=Decimal(str(equity)))},
        costs={Scenario.EXPECTED: CostBlock()}, debt_data_present=True)


def make_strategy(strategy, mao, profit, roi, status="viable"):
    return StrategyResult(strategy=strategy, scenario=Scenario.EXPECTED, status=status,
                          mao=Decimal(str(mao)) if mao is not None else None,
                          profit=Decimal(str(profit)) if profit is not None else None,
                          roi=Decimal(str(roi)) if roi is not None else None)


def fixture_entries():
    entries = []
    data = [
        ("57 Cottage", (500000, 200000, 300000), {"overall": 78, "fos": 70, "distress": 60, "dcs": 80, "risk": 20,
                                                  "components": {"risk_lien_count": Decimal(6)}}),
        ("42 Main", (400000, 250000, 150000), {"overall": 59, "fos": 52, "distress": 51, "dcs": 78, "risk": 38,
                                               "components": {"risk_lien_count": Decimal(24)}}),
        ("9 Elm", (350000, 100000, 250000), {"overall": 70, "fos": 66, "distress": 40, "dcs": 90, "risk": 10,
                                             "components": {}}),
    ]
    for label, (v, debt, eq), scores in data:
        entries.append(CompSetEntry(
            label=label,
            underwriting=make_underwriting(v, debt, eq),
            scores=make_scores(components=scores.pop("components"), recommended=StrategyType.FLIP, **scores),
            strategies=[make_strategy(StrategyType.FLIP, 300000, 90000, "0.3"),
                        make_strategy(StrategyType.CASH, 250000, 50000, "0.15")]))
    return entries


def test_comp_set_rows_and_winners():
    table = build_comp_set(fixture_entries())
    assert table.labels == ["57 Cottage", "42 Main", "9 Elm"]
    rows = {row.key: row for row in table.rows}
    assert rows["value"].winner_indices == [0]
    assert rows["confirmed_debt"].winner_indices == [2]          # lower is better
    assert rows["adjusted_equity"].winner_indices == [0]
    assert rows["overall"].winner_indices == [0]
    assert rows["risk"].winner_indices == [2]                    # lower is better
    assert rows["best_strategy"].winner_indices == []            # non-numeric row
    assert rows["best_strategy"].values == ["flip", "flip", "flip"]
    assert rows["mao"].values == [Decimal(300000)] * 3
    assert rows["key_risks"].values[1] == ["lien_count"]         # top risk term surfaced


def test_comp_set_rejects_out_of_range_sizes():
    entries = fixture_entries()
    with pytest.raises(ValueError):
        build_comp_set(entries[:1])
    with pytest.raises(ValueError):
        build_comp_set(entries + entries[:2])


def test_comp_set_tied_cells_all_win():
    entries = fixture_entries()
    for entry in entries:
        entry.underwriting.value.v_expected = Decimal(400000)
    table = build_comp_set(entries)
    row = next(row for row in table.rows if row.key == "value")
    assert row.winner_indices == [0, 1, 2]
