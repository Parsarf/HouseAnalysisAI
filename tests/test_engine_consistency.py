"""Cross-engine invariants for the golden normalized records."""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from contracts import Scenario
from finance import underwrite
from finance.engine import _is_first
from scoring.engine import DEFAULT_CONFIG, _dcs
from strategies.engine import (
    _first_mortgage,
    _payoff_fees,
    all_strategies,
    foreclosure,
    offer_point,
)

ROOT = Path(__file__).parents[1]
AS_OF = date(2026, 8, 18)
ASSUMPTIONS = {
    path.stem: __import__("contracts").AssumptionSet.model_validate(json.loads(path.read_text()))
    for path in (ROOT / "fixtures/assumptions").glob("*.json")
}


def records():
    for path in sorted((ROOT / "fixtures/normalized").glob("*.json")):
        yield path.stem, __import__("contracts").NormalizedProperty.model_validate(json.loads(path.read_text()))


def test_first_mortgage_detection_agrees():
    for slug, record in records():
        finance_positions = {m.position for m in record.mortgages if m.is_open and _is_first(m)}
        strategy = _first_mortgage(record)
        assert bool(finance_positions) == (strategy is not None), slug


def test_foreclosure_resolves_original_only_first_mortgage():
    record = next(record for slug, record in records() if slug == "04_active_nts_postponements")
    mortgage = record.mortgages[0].model_copy(update={"estimated_balance": None})
    state = record.foreclosure.model_copy(update={"published_bid": None})
    record = record.model_copy(update={"mortgages": [mortgage], "foreclosure": state})
    underwriting = underwrite(record, ASSUMPTIONS["default"], as_of=AS_OF)
    result = foreclosure(record, underwriting, ASSUMPTIONS["default"], Decimal("200000"), Scenario.EXPECTED, AS_OF)
    assert result.metrics["total_obligations"] > 0


def test_expected_offer_proceeds_reconcile_with_adjusted_equity():
    for slug, record in records():
        underwriting = underwrite(record, ASSUMPTIONS["default"], as_of=AS_OF)
        value = underwriting.value.v_expected
        if value is None or underwriting.status != "ok":
            continue
        point = offer_point(underwriting, ASSUMPTIONS["default"], value, Scenario.EXPECTED)
        adjusted = underwriting.equity[Scenario.EXPECTED].adjusted
        assert adjusted is not None
        expected_from_equity = adjusted - _payoff_fees(underwriting) - point.closing_costs
        assert point.proceeds_expected == expected_from_equity, slug


def test_wholesale_dcs_matches_scoring_dcs():
    for slug, record in records():
        underwriting = underwrite(record, ASSUMPTIONS["default"], as_of=AS_OF)
        dcs, _ = _dcs(record, DEFAULT_CONFIG, AS_OF)
        strategy_results = all_strategies(record, underwriting, ASSUMPTIONS["default"], Decimal(0),
                                           as_of=AS_OF, data_confidence_value=dcs)
        wholesale = [item for item in strategy_results
                     if item.strategy.value == "wholesale" and item.metrics.get("dcs") is not None]
        if wholesale:
            assert all(item.metrics["dcs"] == dcs for item in wholesale), slug
