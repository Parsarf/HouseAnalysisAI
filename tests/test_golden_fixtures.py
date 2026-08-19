"""Golden-fixture reproduction tests for the numeric core (WP-6/7/8).

Loads ``fixtures/normalized/*.json`` + ``fixtures/assumptions/*.json``, runs the
real engines (``finance.underwrite``, ``strategies.all_strategies`` /
``strategies.offer_grid``, ``scoring.score``), and asserts byte-exact
reproduction of the hand-computed goldens in ``fixtures/underwriting/``,
``fixtures/strategies/`` and ``fixtures/scores/``.

The goldens are computed from the specification formulas (spec S7/S8/S9/S10) by
``fixtures/generate_goldens.py``, with every calculation step recorded in the
``worksheet.csv`` next to each golden directory. Interpretation decisions are
documented in that module's docstring ("GOLDEN FORMULA SET v2").

Only ``engine_version`` is excluded from comparison: it is metadata the engine
packages bump on rewrite, not a computed number. Numeric strings are compared
by value (``"0"`` vs ``"0.00"`` is serialization noise, not a numeric
difference). Everything else — every number, status, reason, flag and
ordering — must match exactly.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from contracts import AssumptionSet, NormalizedProperty
from finance import underwrite
from scoring import data_confidence, score
from strategies import all_strategies, offer_grid

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SCORING_CONFIG_ID = UUID("20000000-0000-0000-0000-000000000001")
GOLDEN_AS_OF = date(2026, 8, 18)
IGNORED_KEYS = {"engine_version"}


def load_normalized() -> dict[str, NormalizedProperty]:
    return {
        p.stem: NormalizedProperty.model_validate(json.loads(p.read_text()))
        for p in sorted((FIXTURES / "normalized").glob("*.json"))
    }


def load_assumptions() -> dict[str, AssumptionSet]:
    return {
        p.stem: AssumptionSet.model_validate(json.loads(p.read_text()))
        for p in sorted((FIXTURES / "assumptions").glob("*.json"))
    }


def load_json(path: Path):
    return json.loads(path.read_text())


def strip(value, ignored=IGNORED_KEYS):
    if isinstance(value, dict):
        return {k: strip(v, ignored) for k, v in value.items() if k not in ignored}
    if isinstance(value, list):
        return [strip(v, ignored) for v in value]
    return value


def diff(expected, actual, path: str = "") -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        problems = []
        for key in sorted(expected.keys() | actual.keys()):
            if key not in expected:
                problems.append(f"{path}.{key}: unexpected key (got {actual[key]!r})")
            elif key not in actual:
                problems.append(f"{path}.{key}: missing (expected {expected[key]!r})")
            else:
                problems.extend(diff(expected[key], actual[key], f"{path}.{key}"))
        return problems
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [f"{path}: length {len(actual)} != expected {len(expected)}"]
        problems = []
        for index, (e, a) in enumerate(zip(expected, actual)):
            problems.extend(diff(e, a, f"{path}[{index}]"))
        return problems
    if expected != actual:
        try:
            if Decimal(str(expected)) == Decimal(str(actual)):
                return []
        except Exception:
            pass
        return [f"{path}: expected {expected!r}, got {actual!r}"]
    return []


NORMALIZED = load_normalized()
ASSUMPTIONS = load_assumptions()


@pytest.mark.parametrize("slug", sorted(NORMALIZED))
@pytest.mark.parametrize("assumption_name", sorted(ASSUMPTIONS))
def test_underwriting_reproduces_golden(slug: str, assumption_name: str):
    golden_path = FIXTURES / "underwriting" / f"{slug}.{assumption_name}.json"
    expected = strip(load_json(golden_path))
    record = NORMALIZED[slug]
    result = underwrite(record, ASSUMPTIONS[assumption_name])
    actual = strip(result.model_dump(mode="json"))
    problems = diff(expected, actual)
    assert not problems, f"{slug} x {assumption_name}:\n" + "\n".join(problems[:25])


@pytest.mark.parametrize("slug", sorted(NORMALIZED))
def test_strategies_and_offer_grid_reproduce_golden(slug: str):
    golden = load_json(FIXTURES / "strategies" / f"{slug}.json")
    record = NORMALIZED[slug]
    assumptions = ASSUMPTIONS[golden["assumption_set"]]
    price = Decimal(golden["purchase_price"]) if golden["purchase_price"] is not None else Decimal(0)
    underwriting = underwrite(record, assumptions)
    results = all_strategies(record, underwriting, assumptions, price, as_of=GOLDEN_AS_OF,
                             data_confidence_value=data_confidence(record, as_of=GOLDEN_AS_OF))
    grid = offer_grid(underwriting, record.property_id, assumptions, price)
    actual = {"strategies": [r.model_dump(mode="json") for r in results],
              "offer_grid": grid.model_dump(mode="json")}
    expected = {"strategies": golden["strategies"], "offer_grid": golden["offer_grid"]}
    problems = diff(expected, actual)
    assert not problems, f"{slug}:\n" + "\n".join(problems[:25])


@pytest.mark.parametrize("slug", sorted(NORMALIZED))
def test_scores_reproduce_golden(slug: str):
    golden = load_json(FIXTURES / "scores" / f"{slug}.json")
    strategy_golden = load_json(FIXTURES / "strategies" / f"{slug}.json")
    record = NORMALIZED[slug]
    assumptions = ASSUMPTIONS[strategy_golden["assumption_set"]]
    price = (Decimal(strategy_golden["purchase_price"])
             if strategy_golden["purchase_price"] is not None else Decimal(0))
    underwriting = underwrite(record, assumptions)
    results = all_strategies(record, underwriting, assumptions, price, as_of=GOLDEN_AS_OF,
                             data_confidence_value=data_confidence(record, as_of=GOLDEN_AS_OF))
    actual = score(
        record, underwriting, SCORING_CONFIG_ID, results, as_of=GOLDEN_AS_OF,
    ).model_dump(mode="json")
    problems = diff(golden, actual)
    assert not problems, f"{slug}:\n" + "\n".join(problems[:25])


def test_engines_are_deterministic_over_golden_fixtures():
    for record in NORMALIZED.values():
        assumptions = ASSUMPTIONS["default"]
        first = underwrite(record, assumptions).model_dump(mode="json")
        second = underwrite(record, assumptions).model_dump(mode="json")
        assert first == second


def test_fixture_01_remains_the_reproducible_worked_example():
    """Fixture #1 is the worked-example anchor: single comp candidate at
    $500,000 on 1,800 sqft, no debt, and a fact ledger that produces it."""
    record = NORMALIZED["01_clean_high_equity"]
    assert record.valuation_candidates[0].value.value == Decimal(500000)
    assert record.attributes.sqft.value == Decimal(1800)
    assert not record.mortgages and not record.liens
    facts_path = FIXTURES / "facts" / "01_clean_high_equity.jsonl"
    facts = [json.loads(line) for line in facts_path.read_text().splitlines() if line.strip()]
    by_path = {f["field_path"]: f for f in facts}
    assert by_path["valuation.comp.value"]["value_parsed"] == "500000"
    assert by_path["attributes.sqft"]["value_parsed"] == "1800"


def test_golden_files_cover_every_fixture_and_assumption_set():
    for slug in NORMALIZED:
        for assumption_name in ASSUMPTIONS:
            assert (FIXTURES / "underwriting" / f"{slug}.{assumption_name}.json").exists()
        assert (FIXTURES / "strategies" / f"{slug}.json").exists()
        assert (FIXTURES / "scores" / f"{slug}.json").exists()
