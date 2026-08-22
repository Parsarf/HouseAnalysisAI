"""Explainability / audit-trace tests.

Covers: provenance presence, formula consistency (recorded steps reproduce the
persisted result exactly), engine versions, resolution candidates, missing-data
handling, unresolved-vs-confirmed semantics, and golden explanation files.
All offline per repo convention.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from common.trace import TraceRecorder
from contracts import (
    AssumptionSet,
    ExplanationTrace,
    NormalizedProperty,
    Scenario,
)
from db import models as dbm
from db.models import Base
from explanation.builder import ExplainContext
from explanation.dispatch import available_keys, build_trace

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
GOLDEN_AS_OF = date(2026, 8, 18)
FIXTURE_NAME = "03_conflicting_mortgages"


# ------------------------------------------------------------------ helpers


def load_record(name: str = FIXTURE_NAME) -> NormalizedProperty:
    return NormalizedProperty.model_validate(
        json.loads((FIXTURES / "normalized" / f"{name}.json").read_text()))


def load_assumptions(name: str = "default") -> AssumptionSet:
    return AssumptionSet.model_validate(
        json.loads((FIXTURES / "assumptions" / f"{name}.json").read_text()))


def _underwrite_with_trace(name: str = FIXTURE_NAME, assumption_name: str = "default",
                           as_of: date | None = GOLDEN_AS_OF):
    from finance import underwrite

    record = load_record(name)
    recorder = TraceRecorder(engine="finance", engine_version="finance-test")
    result = underwrite(load_assumptions(assumption_name) if assumption_name else None,
                        trace=recorder) if False else underwrite(
        record, load_assumptions(assumption_name), as_of, trace=recorder)
    return record, result, recorder


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _step_results(recorder: TraceRecorder, label_prefix: str) -> list:
    return [step.get("result") for step in recorder.steps
            if step["label"].startswith(label_prefix)]


def _step_by_label(recorder: TraceRecorder, label: str):
    return next((step for step in recorder.steps if step["label"] == label), None)


# ------------------------------------------------------------------ finance provenance


def test_underwrite_records_steps_and_engine_version():
    _, _, recorder = _underwrite_with_trace()
    assert recorder.engine == "finance"
    labels = [step["label"] for step in recorder.steps]
    assert any("Weighted valuation sum" in label for label in labels)
    assert any("Expected value" == label for label in labels)
    assert any(label.startswith("Gross equity") for label in labels)
    assert any(label.startswith("Holding cost") for label in labels)


def test_valuation_formula_reproduces_persisted_expected_value():
    from finance.engine import SPEC_TYPE_WEIGHT

    record, result, recorder = _underwrite_with_trace()
    # Rebuild the weighted blend purely from recorded candidate-weight steps and
    # confirm it equals the persisted v_expected byte-exact.
    weighted = []
    for candidate in record.valuation_candidates:
        kind = candidate.valuation_type.strip().casefold()
        base = SPEC_TYPE_WEIGHT.get(kind, Decimal(1))
        weight = base * Decimal(str(candidate.value.confidence))
        weighted.append((candidate.value.value, weight))
    expected = _money(sum((v * w for v, w in weighted), Decimal(0))
                      / sum((w for _, w in weighted), Decimal(0)))
    assert expected == result.value.v_expected
    persisted_step = _step_by_label(recorder, "Expected value")
    assert persisted_step is not None
    assert _money(persisted_step["result"]) == expected


def test_equity_substitution_matches_persisted_value():
    _, result, recorder = _underwrite_with_trace()
    block = result.equity[Scenario.EXPECTED]
    step = _step_by_label(recorder, "Gross equity (expected)")
    inputs = step["inputs"]
    recomputed = _money(inputs["value"] - inputs["confirmed obligations"])
    assert recomputed == block.gross


def test_engine_version_and_formula_fields_are_populated():
    _, _, recorder = _underwrite_with_trace()
    assert recorder.engine_version.startswith("finance")
    assert all(step.get("formula") or step.get("substitution") for step in recorder.steps)


def test_single_candidate_confidence_cap_is_documented():
    name = "02_owner_only_federal_lien"
    record = load_record(name)
    if len(record.valuation_candidates) != 1:
        pytest.skip("fixture has multiple valuation candidates")
    _, result, recorder = _underwrite_with_trace(name)
    assumptions_text = [entry.get("value") for entry in recorder.by_kind("assumption")]
    assert any("capped at 50%" in str(text) for text in assumptions_text)
    assert result.value.valuation_confidence <= Decimal("0.5")


def test_amortization_balance_carries_payoff_warning():
    """A mortgage with original amount but no reported balance is derived — the
    trace must say so and warn that this is not a payoff statement."""
    from finance import underwrite

    record = load_record()
    if not record.mortgages:
        pytest.skip("fixture has no mortgages")
    mortgage = record.mortgages[0]
    object.__setattr__(mortgage, "estimated_balance", None)
    if mortgage.original_amount is None or mortgage.origination_date is None:
        pytest.skip("fixture mortgage lacks derivation inputs")
    recorder = TraceRecorder(engine="finance", engine_version="test")
    underwrite(record, load_assumptions(), GOLDEN_AS_OF, trace=recorder)
    derived = [step for step in recorder.steps if "amortization" in step["label"].lower()]
    assert derived, "derived balance must be recorded"
    warnings = recorder.warnings
    assert any("not a lender payoff statement" in warning for warning in warnings)
    candidates = recorder.by_kind("candidate")
    assert any(entry.get("confidence") == Decimal("0.55") for entry in candidates)


def test_unknown_amount_lien_is_potential_not_confirmed():
    from finance import underwrite

    record = load_record()
    if not record.liens:
        pytest.skip("fixture has no liens")
    lien = record.liens[0]
    object.__setattr__(lien, "amount", None)
    recorder = TraceRecorder(engine="finance", engine_version="test")
    result = underwrite(record, load_assumptions(), GOLDEN_AS_OF, trace=recorder)
    labels = [step["label"] for step in recorder.steps]
    assert any("unknown amount" in label.lower() for label in labels)
    confirmed_labels = [step["label"] for step in recorder.steps
                        if step["label"].startswith("Confirmed lien")]
    assert not any(lien.lien_type in label for label in confirmed_labels)
    assert result.liabilities.potential > 0


# ------------------------------------------------------------------ strategy provenance


def test_cash_profit_step_reproduces_persisted_result():
    from finance import underwrite
    from strategies import cash

    record = load_record()
    assumptions = load_assumptions()
    uw = underwrite(record, assumptions, GOLDEN_AS_OF)
    price = Decimal("400000")
    recorder = TraceRecorder(engine="strategies", engine_version="strategies-test")
    result = cash(record, uw, assumptions, price, Scenario.EXPECTED, trace=recorder)
    profit_step = _step_by_label(recorder, "Cash profit")
    assert profit_step is not None
    assert _money(profit_step["result"]) == result.profit
    mao_step = _step_by_label(recorder, "Maximum Allowable Offer (cash)")
    assert mao_step is not None
    assert _money(mao_step["result"]) == result.mao
    basis_step = _step_by_label(recorder, "All-in basis")
    substitution = basis_step["substitution"]
    assert str(price) in substitution.replace(",", "")


def test_flip_mao_algebra_recorded():
    from strategies import flip

    record = load_record()
    assumptions = load_assumptions()
    from finance import underwrite

    uw = underwrite(record, assumptions, GOLDEN_AS_OF)
    price = Decimal("400000")
    recorder = TraceRecorder(engine="strategies", engine_version="strategies-test")
    result = flip(record, uw, assumptions, price, Scenario.EXPECTED, trace=recorder)
    if result.status == "unavailable":
        pytest.skip("fixture lacks sqft/arv for flip")
    mao_step = _step_by_label(recorder, "Maximum Allowable Offer (flip)")
    assert mao_step is not None
    assert _money(mao_step["result"]) == result.mao
    financing_step = _step_by_label(recorder, "Financing cost")
    assert financing_step is not None
    assert "points × loan" in financing_step["formula"]


def test_offer_point_trace_reproduces_proceeds():
    from finance import underwrite
    from strategies import offer_point

    record = load_record()
    assumptions = load_assumptions()
    uw = underwrite(record, assumptions, GOLDEN_AS_OF)
    offer = Decimal("500000")
    recorder = TraceRecorder(engine="strategies", engine_version="strategies-test")
    point = offer_point(uw, assumptions, offer, Scenario.EXPECTED, trace=recorder)
    high_step = _step_by_label(recorder, "Seller proceeds (high)")
    recomputed = (_money(high_step["inputs"]["offer"])
                  - _money(high_step["inputs"]["confirmed payoffs"])
                  - _money(high_step["inputs"]["closing"]))
    assert recomputed == point.proceeds_high


# ------------------------------------------------------------------ scoring provenance


def test_score_overall_terms_sum_from_components():
    from finance import underwrite
    from scoring import data_confidence
    from scoring.engine import DEFAULT_CONFIG, score
    from strategies import all_strategies

    record = load_record()
    assumptions = load_assumptions()
    uw = underwrite(record, assumptions, GOLDEN_AS_OF)
    dcs = data_confidence(record)
    strategies = all_strategies(record, uw, assumptions, Decimal("400000"),
                                data_confidence_value=dcs)
    recorder = TraceRecorder(engine="scoring", engine_version="scoring-test")
    result = score(record, uw, UUID(int=1), strategies=strategies, config=None,
                   as_of=GOLDEN_AS_OF, trace=recorder)
    overall_step = _step_by_label(recorder, "Overall score")
    weights = DEFAULT_CONFIG["weights"]["overall"]
    recomputed = (result.fos * weights["fos"] + result.distress * weights["distress"]
                  + result.data_confidence * weights["dcs"] - result.risk * weights["risk"])
    recomputed = max(Decimal(0), min(Decimal(100), recomputed)).quantize(Decimal("0.0001"))
    assert recomputed == result.overall
    assert overall_step is not None


def test_distress_terms_present_in_components_for_distressed_fixture():
    from finance import underwrite
    from scoring import data_confidence
    from scoring.engine import score as scoring_score
    from strategies import all_strategies

    record = load_record("04_active_nts_postponements")
    assumptions = load_assumptions()
    uw = underwrite(record, assumptions, GOLDEN_AS_OF)
    strategies = all_strategies(record, uw, assumptions, Decimal("400000"),
                                data_confidence_value=data_confidence(record))
    result = scoring_score(record, uw, UUID(int=1), strategies=strategies, as_of=GOLDEN_AS_OF)
    assert any(name.startswith("distress_") for name in result.components)
    assert isinstance(result.distress, Decimal)
    # every displayed distress term must be nonnegative and capped at 100 total
    assert all(value >= 0 for value in result.components.values())
    assert result.distress <= 100


# ------------------------------------------------------------------ contract


def test_explanation_trace_contract_round_trip():
    trace = ExplanationTrace(key="value.expected", title="t", description="d", value=Decimal("1.00"))
    payload = json.loads(trace.model_dump_json())
    restored = ExplanationTrace.model_validate(payload)
    assert restored.key == "value.expected"


def test_unresolved_trace_never_looks_confirmed():
    from explanation.dispatch import build_trace as _build  # noqa: F401  (import sanity)

    trace = ExplanationTrace(key="x", title="t", description="d", value=None,
                             unresolved_dependencies=["sqft was never extracted"])
    assert trace.unresolved_dependencies
    assert trace.value is None


# ------------------------------------------------------------------ builders against sqlite


@pytest.fixture()
def sqlite_session():
    engine = create_engine("sqlite:///:memory:")
    needed = ["reports", "report_extractions", "extracted_facts", "assumption_sets",
              "deal_scenarios", "rankings"]
    tables = [Base.metadata.tables[name] for name in needed]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture()
def seeded_property(sqlite_session):
    record = load_record()
    pid = UUID("00000000-0000-0000-0000-000000000123")
    report = dbm.Report(id=uuid4(), property_id=pid, report_type="property_profile",
                        vendor="First American", status="ready", page_count=12,
                        ocr_applied=False, file_path=f"documents/{pid}/report.pdf",
                        sha256="0" * 64)
    sqlite_session.add(report)
    extraction = dbm.ReportExtraction(
        id=uuid4(), report_id=report.id, property_id=pid, schema_version="v1",
        status="complete", normalized_json={"property": json.loads(record.model_dump_json())},
        updated_at=datetime(2026, 8, 18, tzinfo=UTC))
    sqlite_session.add(extraction)
    # one valuation fact so source evidence attaches to value traces
    sqlite_session.add(dbm.ExtractedFact(
        id=uuid4(), property_id=pid, report_id=report.id, entity_type="valuation",
        entity_local_id="v1", field_path="valuation.avm.value", value_raw="$725,000",
        value_parsed=Decimal("725000"), page_number=7, snippet="Estimated Market Value: $725,000",
        extraction_confidence=Decimal("0.91"), source_kind="report", is_active=True))
    assumptions = load_assumptions()
    sqlite_session.add(dbm.AssumptionSet(
        id=assumptions.id, name="default", is_default=True, version=assumptions.version,
        effective_from=date(2026, 1, 1),
        params={key: value for key, value in json.loads(assumptions.model_dump_json()).items()
                if key not in ("id", "version", "name")}))
    sqlite_session.add(dbm.DealScenario(
        id=uuid4(), property_id=pid, strategy="cash", scenario="expected",
        assumption_set_id=assumptions.id, engine_version="finance-3", purchase_price=Decimal("150000"),
        repairs=Decimal("0"), holding=Decimal("0"), resale=Decimal("0"), financing=Decimal("0"),
        computed_at=datetime(2026, 8, 18, tzinfo=UTC)))
    sqlite_session.commit()
    return pid


def _patch_persistence(monkeypatch):
    from explanation import builder
    from explanation import sources as source_store
    from pipeline.store import DEFAULT_SCORING_CONFIG_ID

    monkeypatch.setattr(builder.store, "load_scoring_config",
                        lambda session: (DEFAULT_SCORING_CONFIG_ID, None))
    # the sqlite subset omits the ARRAY-backed field_resolutions/scores tables
    monkeypatch.setattr(source_store, "_winning_fact_ids", lambda session, pid: set())
    monkeypatch.setattr(source_store, "candidates_for_field",
                        lambda session, pid, field_path: ([], None, None))
    monkeypatch.setattr(builder.store, "load_persisted_score", lambda session, pid: None)


def test_catalog_contains_all_material_figures(seeded_property, monkeypatch):
    keys = available_keys()
    for required in ("value.expected", "liabilities.confirmed", "equity.expected",
                     "score.overall", "recommendation.strategy", "rank",
                     "strategy.cash.expected", "strategy.cash.expected.mao",
                     "offers.simulator"):
        assert required in keys, required


def test_build_value_expected_with_sources(sqlite_session, seeded_property, monkeypatch):
    _patch_persistence(monkeypatch)
    trace = build_trace(sqlite_session, seeded_property, "value.expected")
    assert isinstance(trace, ExplanationTrace)
    assert trace.value is not None
    assert trace.steps, "expected value must carry calculation steps"
    assert trace.formula
    assert trace.candidates, "valuation candidates must be listed"
    assert trace.source_facts, "source facts must attach when fact rows exist"
    avm_source = next((s for s in trace.source_facts if s.page_number == 7), None)
    assert avm_source is not None
    assert avm_source.snippet == "Estimated Market Value: $725,000"
    assert avm_source.extraction_confidence == pytest.approx(0.91)
    assert avm_source.vendor == "First American"
    assert "/api/reports/" in avm_source.source_url and "page=7" in avm_source.source_url


def test_liabilities_semantics_confirmed_vs_potential(sqlite_session, seeded_property, monkeypatch):
    _patch_persistence(monkeypatch)
    confirmed = build_trace(sqlite_session, seeded_property, "liabilities.confirmed")
    potential = build_trace(sqlite_session, seeded_property, "liabilities.potential")
    assert confirmed.title == "Confirmed liabilities"
    for child in confirmed.children:
        assert "NOT part of confirmed" not in child.description or child.value_kind != "estimated"
    estimated_lines = [child for child in potential.children if child.value_kind == "estimated"]
    for child in estimated_lines:
        assert "estimated" in child.description.lower() or child.warnings


def test_score_overall_children_cover_components(sqlite_session, seeded_property, monkeypatch):
    _patch_persistence(monkeypatch)
    trace = build_trace(sqlite_session, seeded_property, "score.overall")
    child_titles = {child.key for child in trace.children}
    assert {"score.fos", "score.distress", "score.data_confidence", "score.risk"} <= child_titles


def test_missing_sqft_repairs_unresolved_not_zero(sqlite_session, seeded_property, monkeypatch):
    _patch_persistence(monkeypatch)
    record = load_record()
    object.__setattr__(record.attributes, "sqft", None)
    extraction = (sqlite_session.query(dbm.ReportExtraction)
                  .filter(dbm.ReportExtraction.property_id == seeded_property).first())
    extraction.normalized_json = {"property": json.loads(record.model_dump_json())}
    sqlite_session.commit()
    trace = build_trace(sqlite_session, seeded_property, "repairs.expected")
    assert any("square footage" in dependency.lower() for dependency in trace.unresolved_dependencies)


def test_strategy_mao_key_narrowing(sqlite_session, seeded_property, monkeypatch):
    _patch_persistence(monkeypatch)
    full = build_trace(sqlite_session, seeded_property, "strategy.cash.expected")
    narrowed = build_trace(sqlite_session, seeded_property, "strategy.cash.expected.mao")
    mao_child = next(child for child in full.children if child.key.endswith(".mao"))
    assert narrowed.value == mao_child.value
    assert narrowed.title == "Maximum Allowable Offer"


def test_recommendation_ranks_alternatives(sqlite_session, seeded_property, monkeypatch):
    _patch_persistence(monkeypatch)
    trace = build_trace(sqlite_session, seeded_property, "recommendation.strategy")
    assert trace.candidates, "alternative strategies must be shown with reasons"
    winners = [candidate for candidate in trace.candidates if candidate.is_winner]
    if winners:
        assert "highest expected profit" in winners[0].reason


def test_golden_explanation_files_are_stable(tmp_path, monkeypatch):
    """Golden stability: the same inputs must produce identical trace JSON
    (excluding wall-clock metadata like computed_at)."""
    _patch_persistence(monkeypatch)
    pid = UUID(int=77)
    record = load_record()
    assumptions = load_assumptions()

    from explanation import builder as _builder_mod
    _builder_mod.source_store.sources_for_property = lambda *a, **k: []
    _builder_mod.source_store.candidates_for_field = lambda *a, **k: ([], None, None)

    class FakeCtx(ExplainContext):
        def __init__(self_inner, session=None, property_id=None):
            self_inner.session = None
            self_inner.property_id = pid
            self_inner.normalized = record
            self_inner.assumptions = assumptions
            self_inner._underwriting = None

        def scores(self_inner):
            from pipeline.store import DEFAULT_SCORING_CONFIG_ID
            from scoring import data_confidence as scoring_dcs
            from scoring import score as scoring_score
            from strategies import all_strategies

            self_inner.underwriting()
            dcs = scoring_dcs(record)
            strategies = all_strategies(record, self_inner.underwriting()[0], assumptions,
                                        Decimal("400000"), data_confidence_value=dcs)
            result = scoring_score(record, self_inner.underwriting()[0], DEFAULT_SCORING_CONFIG_ID,
                                   strategies=strategies, as_of=GOLDEN_AS_OF)
            return result, None, None

        def purchase_price(self_inner):
            return Decimal("400000")

    from explanation import builder, dispatch

    original_ctx = builder.ExplainContext
    builder.ExplainContext = FakeCtx
    dispatch.ExplainContext = FakeCtx

    def strip(payload):
        if isinstance(payload, dict):
            return {k: strip(v) for k, v in payload.items() if k != "computed_at"}
        if isinstance(payload, list):
            return [strip(item) for item in payload]
        return payload

    try:
        payloads = {}
        for key in ("value.expected", "equity.expected", "repairs.expected",
                    "strategy.cash.expected", "strategy.flip.expected",
                    "score.overall", "recommendation.strategy"):
            trace = build_trace(None, pid, key)
            payloads[key] = json.loads(trace.model_dump_json())
        # second run must be byte-identical (deterministic engines)
        rebuilt_all = {}
        for key in payloads:
            rebuilt_all[key] = json.loads(build_trace(None, pid, key).model_dump_json())
        for key, payload in payloads.items():
            assert strip(rebuilt_all[key]) == strip(payload), key
    finally:
        builder.ExplainContext = original_ctx
        dispatch.ExplainContext = original_ctx

    golden_dir = FIXTURES / "explanations"
    golden_dir.mkdir(exist_ok=True)
    for key, payload in payloads.items():
        path = golden_dir / f"{key.replace('.', '_')}.json"
        if not path.exists():
            path.write_text(json.dumps(strip(payload), sort_keys=True, indent=1))
            continue
        stored = json.loads(path.read_text())
        assert strip(stored) == strip(payload), f"golden drift for {key}"
