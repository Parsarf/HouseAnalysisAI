"""Offline tests for WP-10 pipeline orchestration.

All DB access is faked: FakeStore mirrors the SqlStore method surface over an
in-memory state with copy-on-write transactions (commit swaps the state, an
exception discards it), which is what makes the crash-resume test meaningful.
"""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from common.errors import AcqError, ErrorCode
from contracts import AssumptionSet, EntityType, ExtractedFactDraft, SourceKind
from normalization import resolve_facts
from pipeline import Pipeline, recompute_property
from pipeline import batch as batch_machine
from pipeline.store import DEFAULT_SCORING_CONFIG_ID, UnitOutcome
from pipeline.worker import Worker, default_handlers

NOW = datetime(2026, 1, 1, tzinfo=UTC)
AS_OF = date(2026, 1, 1)
REPO_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ fake store

class FakeState:
    def __init__(self):
        self.properties = {}      # id -> dict
        self.facts = {}           # property_id -> [ExtractedFactDraft]
        self.units = {}           # id -> dict
        self.batches = {}         # id -> dict
        self.assumptions = None   # AssumptionSet
        self.scoring_config = None
        self.deal_scenarios = []
        self.offer_scenarios = []
        self.scores = []
        self.flags = []
        self.change_events = []
        self.settings = {}
        self.rankings = []
        self.locks_acquired = []  # log, excluded from state comparisons
        self.recomputed_marks = []


class FakeStoreFactory:
    """Copy-on-write transactions serialized by one lock (the fake analogue of
    the per-property row lock / advisory lock the SqlStore relies on)."""

    def __init__(self, state: FakeState | None = None):
        self.state = state or FakeState()
        self.lock = threading.RLock()
        self.crash_on = None  # method tag that raises once, simulating a worker crash

    def maybe_crash(self, tag: str) -> None:
        if self.crash_on == tag:
            self.crash_on = None
            raise RuntimeError(f"simulated crash at {tag}")

    @contextmanager
    def __call__(self):
        with self.lock:
            working = copy.deepcopy(self.state)
            store = FakeStore(working, self)
            yield store  # exception discards the working copy: rollback
            vars(self.state).update(vars(working))


class FakeStore:
    def __init__(self, state: FakeState, factory: FakeStoreFactory):
        self.state = state
        self.factory = factory

    # locking / reads
    def acquire_property_lock(self, property_id):
        self.state.locks_acquired.append(UUID(str(property_id)))

    def get_property(self, property_id):
        row = self.state.properties.get(UUID(str(property_id)))
        return dict(row) if row else None

    def load_facts(self, property_id):
        return list(self.state.facts.get(UUID(str(property_id)), []))

    def reports_ocr_applied(self, property_id):
        return False

    def load_assumptions(self, assumption_set_id=None):
        if self.state.assumptions is None:
            raise AcqError(ErrorCode.NOT_FOUND, "no assumption set configured")
        return self.state.assumptions

    def active_scoring_config(self):
        return self.state.scoring_config or (DEFAULT_SCORING_CONFIG_ID, None)

    # recompute persistence
    def replace_results(self, property_id, computation, *, purchase_price):
        property_id = UUID(str(property_id))
        uw = computation.underwriting
        self.state.deal_scenarios = [r for r in self.state.deal_scenarios
                                     if r["property_id"] != property_id]
        for result in computation.strategies:
            costs = uw.costs.get(result.scenario)
            self.state.deal_scenarios.append({
                "id": uuid4(), "property_id": property_id,
                "strategy": result.strategy.value, "scenario": result.scenario.value,
                "assumption_set_id": uw.assumption_set_id, "purchase_price": purchase_price,
                "arv": uw.value.arv_by_scenario.get(result.scenario),
                "repairs": costs.repairs if costs else None,
                "profit": result.profit, "mao": result.mao, "status": result.status,
                "computed_at": NOW})
        self.state.offer_scenarios = [r for r in self.state.offer_scenarios
                                      if r["property_id"] != property_id]
        for point in computation.grid.points:
            self.state.offer_scenarios.append({
                "id": uuid4(), "property_id": property_id, "offer_price": point.offer_price,
                "scenario": point.scenario.value, "proceeds_low": point.proceeds_low,
                "is_short_sale": point.is_short_sale})
        self.factory.maybe_crash("replace_results.score")
        score = computation.score
        self.state.scores = [r for r in self.state.scores
                             if not (r["property_id"] == property_id
                                     and r["scoring_config_id"] == score.scoring_config_id)]
        self.state.scores.append({
            "id": uuid4(), "property_id": property_id,
            "scoring_config_id": score.scoring_config_id, "fos": score.fos,
            "distress": score.distress, "data_confidence": score.data_confidence,
            "risk": score.risk, "overall": score.overall,
            "gates_applied": list(score.gates_applied), "computed_at": NOW})

    def persist_flags(self, property_id, requests):
        existing = {flag["dedupe_key"] for flag in self.state.flags}
        created = 0
        for request in requests:
            if request.dedupe_key in existing:
                continue
            existing.add(request.dedupe_key)
            self.state.flags.append({
                "id": uuid4(), "property_id": request.property_id,
                "flag_type": request.flag_type.value, "dedupe_key": request.dedupe_key,
                "financial_impact_usd": request.financial_impact_usd, "status": "open"})
            created += 1
        return created

    def mark_recomputed(self, property_id, underwriting_status):
        self.state.recomputed_marks.append((UUID(str(property_id)), underwriting_status))

    # extraction fan-in
    def get_unit(self, unit_id):
        row = self.state.units.get(UUID(str(unit_id)))
        return dict(row) if row else None

    def finish_unit(self, unit_id, facts, *, cost_usd=None, model=None, prompt_version=None):
        unit_id = UUID(str(unit_id))
        row = self.state.units.get(unit_id)
        if row is None:
            raise AcqError(ErrorCode.NOT_FOUND, f"extraction unit {unit_id} not found")
        property_id = row["property_id"]
        transitioned = row["status"] in ("queued", "running")
        if transitioned:
            row["status"] = "extracted"
            row["cost_usd"] = cost_usd
            if property_id is not None:
                self.state.facts.setdefault(property_id, []).extend(facts)
        outstanding = None
        if property_id is not None:
            outstanding = sum(1 for unit in self.state.units.values()
                              if unit["property_id"] == property_id
                              and unit["status"] in ("queued", "running"))
        return UnitOutcome(unit_id=unit_id, property_id=property_id,
                           batch_id=row.get("batch_id"), outstanding=outstanding,
                           transitioned=transitioned)

    def fail_unit(self, unit_id, reason):
        self.state.units[UUID(str(unit_id))]["status"] = "failed"

    # batches
    def get_batch(self, batch_id):
        row = self.state.batches.get(UUID(str(batch_id)))
        return dict(row) if row else None

    def update_batch(self, batch_id, **fields):
        self.state.batches[UUID(str(batch_id))].update(fields)

    def batch_is_paused(self, batch_id):
        batch = self.state.batches[UUID(str(batch_id))]
        return batch["status"] == "paused_budget" or bool(batch.get("awaiting_confirmation"))

    def reserve_batch_budget(self, batch_id, amount):
        batch = self.state.batches[UUID(str(batch_id))]
        limit = batch.get("budget_limit_usd")
        spent = batch.get("spent_usd", Decimal(0))
        if limit is not None and spent + amount > limit:
            return False
        batch["spent_usd"] = spent + amount
        return True

    def increment_batch_finished(self, batch_id, *, failed=False):
        batch = self.state.batches[UUID(str(batch_id))]
        batch["failed_count" if failed else "completed_count"] += 1
        return dict(batch)

    # rankings / bulk / changes
    def rank_scope(self, scope_type, scope_id=None):
        from scoring import compute_ranks
        latest = {}
        for row in self.state.scores:
            latest[row["property_id"]] = row["overall"]
        previous = {row["property_id"]: row["rank"] for row in self.state.rankings
                    if row["scope_type"] == scope_type}
        rows = compute_ranks(latest, previous)
        self.state.rankings = [row for row in self.state.rankings
                               if row["scope_type"] != scope_type]
        for row in rows:
            self.state.rankings.append({
                "scope_type": scope_type, "scope_id": scope_id,
                "property_id": row.property_id, "rank": row.rank,
                "prev_rank": row.prev_rank, "score": row.score})
        return len(rows)

    def count_properties(self):
        return len(self.state.properties)

    def list_property_ids(self, *, limit, offset):
        ids = sorted(self.state.properties, key=lambda item: item.int)
        return ids[offset:offset + limit]

    def put_settings(self, key, value):
        self.state.settings[key] = dict(value)

    def get_settings(self, key):
        value = self.state.settings.get(key)
        return dict(value) if value else None

    def persist_change_events(self, property_id, events, *, source_report_id=None):
        for event in events:
            self.state.change_events.append({
                "id": uuid4(), "property_id": UUID(str(property_id)),
                "change_type": str(event.change_type), "field_path": event.field_path,
                "old_value": event.old_value, "new_value": event.new_value,
                "source_report_id": source_report_id, "detected_at": NOW})
        return len(events)


# ------------------------------------------------------------------ fixtures

def load_assumptions() -> AssumptionSet:
    raw = json.loads((REPO_ROOT / "fixtures" / "assumptions" / "default.json").read_text())
    return AssumptionSet.model_validate(raw)


def make_fact(property_id, unit_id, report_id, entity_type, local_id, field_path, *,
              value_parsed=None, value_text=None, value_date=None, confidence=0.9):
    return ExtractedFactDraft(
        report_id=report_id, extraction_unit_id=unit_id, entity_type=entity_type,
        entity_local_id=local_id, field_path=field_path,
        value_raw=str(value_parsed if value_parsed is not None else value_text),
        value_parsed=value_parsed, value_text=value_text, value_date=value_date,
        page_number=1, snippet="verbatim snippet", extraction_confidence=confidence,
        source_kind=SourceKind.REPORT, as_of_date=AS_OF)


def property_facts(property_id, unit_id, report_id):
    """A fully computable property: address, sqft, two valuations, a mortgage,
    one attached lien, one owner-only lien (fires a flag), rent, taxes."""
    return [
        make_fact(property_id, unit_id, report_id, EntityType.PROPERTY, "p", "property.address", value_text="1 Main St"),
        make_fact(property_id, unit_id, report_id, EntityType.PROPERTY, "p", "property.apn", value_text="123-456"),
        make_fact(property_id, unit_id, report_id, EntityType.PROPERTY, "p", "property.sqft", value_parsed=Decimal(1800)),
        make_fact(property_id, unit_id, report_id, EntityType.VALUATION, "avm1", "valuation.type", value_text="avm"),
        make_fact(property_id, unit_id, report_id, EntityType.VALUATION, "avm1", "valuation.value", value_parsed=Decimal(300000)),
        make_fact(property_id, unit_id, report_id, EntityType.VALUATION, "comp1", "valuation.type", value_text="comp"),
        make_fact(property_id, unit_id, report_id, EntityType.VALUATION, "comp1", "valuation.value", value_parsed=Decimal(320000)),
        make_fact(property_id, unit_id, report_id, EntityType.MORTGAGE, "m1", "mortgage.position", value_text="first"),
        make_fact(property_id, unit_id, report_id, EntityType.MORTGAGE, "m1", "mortgage.original_amount", value_parsed=Decimal(200000)),
        make_fact(property_id, unit_id, report_id, EntityType.MORTGAGE, "m1", "mortgage.balance", value_parsed=Decimal(150000)),
        make_fact(property_id, unit_id, report_id, EntityType.MORTGAGE, "m1", "mortgage.rate", value_parsed=Decimal("0.05")),
        make_fact(property_id, unit_id, report_id, EntityType.MORTGAGE, "m1", "mortgage.origination_date", value_date=date(2020, 6, 1)),
        make_fact(property_id, unit_id, report_id, EntityType.LIEN, "l1", "lien.type", value_text="judgment"),
        make_fact(property_id, unit_id, report_id, EntityType.LIEN, "l1", "lien.amount", value_parsed=Decimal(25000)),
        make_fact(property_id, unit_id, report_id, EntityType.LIEN, "l1", "lien.attachment_basis", value_text="recorded_against_property"),
        make_fact(property_id, unit_id, report_id, EntityType.LIEN, "l2", "lien.type", value_text="judgment"),
        make_fact(property_id, unit_id, report_id, EntityType.LIEN, "l2", "lien.amount", value_parsed=Decimal(40000)),
        make_fact(property_id, unit_id, report_id, EntityType.LIEN, "l2", "lien.attachment_basis", value_text="owner_named_only"),
        make_fact(property_id, unit_id, report_id, EntityType.RENTAL, "r1", "rental.rent_estimate", value_parsed=Decimal(2000)),
        make_fact(property_id, unit_id, report_id, EntityType.TAX, "t1", "tax.annual_taxes", value_parsed=Decimal(3000)),
    ]


def seed_property(state: FakeState, property_id=None, *, with_facts=True):
    property_id = property_id or uuid4()
    report_id, unit_id = uuid4(), uuid4()
    state.properties[property_id] = {"id": property_id, "pipeline_status": "new"}
    if with_facts:
        state.facts[property_id] = property_facts(property_id, unit_id, report_id)
    state.assumptions = load_assumptions()
    return property_id


def make_pipeline(state: FakeState | None = None, **kwargs):
    factory = FakeStoreFactory(state)
    return Pipeline(factory, **kwargs), factory


def snapshot(state: FakeState) -> dict:
    """Derived state, excluding logs and generated row ids."""
    def rows(items):
        return sorted(({k: v for k, v in row.items() if k != "id"} for row in items),
                      key=lambda row: json.dumps(row, default=str, sort_keys=True))
    return {
        "deal_scenarios": rows(state.deal_scenarios),
        "offer_scenarios": rows(state.offer_scenarios),
        "scores": rows(state.scores),
        "flags": rows(state.flags),
    }


# ------------------------------------------------------------------ recompute

def test_recompute_persists_deal_offer_and_score_rows():
    pipeline, factory = make_pipeline()
    property_id = seed_property(factory.state)

    computation = pipeline.recompute(property_id, reason="test")

    state = factory.state
    assert computation.underwriting.status == "ok"
    assert len(state.deal_scenarios) == len(computation.strategies) == 18  # 6 strategies x 3 scenarios
    assert {row["property_id"] for row in state.deal_scenarios} == {property_id}
    assert len(state.offer_scenarios) == len(computation.grid.points) > 0
    assert len(state.scores) == 1
    assert state.scores[0]["property_id"] == property_id
    assert state.scores[0]["scoring_config_id"] == DEFAULT_SCORING_CONFIG_ID
    # per-property advisory lock was taken, and recompute markers written
    assert UUID(str(property_id)) in state.locks_acquired
    assert state.recomputed_marks == [(property_id, "ok")]
    # the owner-only lien over $10k fires a lien_attachment flag via flags.collect_flags
    assert any(flag["flag_type"] == "lien_attachment" for flag in state.flags)


def test_recompute_is_idempotent():
    pipeline, factory = make_pipeline()
    property_id = seed_property(factory.state)

    pipeline.recompute(property_id)
    first = snapshot(factory.state)
    pipeline.recompute(property_id)
    pipeline.recompute(property_id, reason="again")
    assert snapshot(factory.state) == first
    assert len(factory.state.recomputed_marks) == 3  # log grows; derived state does not


def test_recompute_missing_property_raises_not_found():
    pipeline, factory = make_pipeline()
    factory.state.assumptions = load_assumptions()
    with pytest.raises(AcqError) as excinfo:
        pipeline.recompute(uuid4())
    assert excinfo.value.code == ErrorCode.NOT_FOUND


def test_recompute_crash_mid_persist_resumes_to_identical_state():
    # Clean reference run.
    clean_pipeline, clean_factory = make_pipeline()
    property_id = seed_property(clean_factory.state)
    clean_pipeline.recompute(property_id)
    reference = snapshot(clean_factory.state)

    # Same seed, killed between deal/offer rows and the score insert.
    crashed_pipeline, crashed_factory = make_pipeline()
    property_id_2 = seed_property(crashed_factory.state)
    crashed_factory.crash_on = "replace_results.score"
    with pytest.raises(RuntimeError, match="simulated crash"):
        crashed_pipeline.recompute(property_id_2)
    # Rollback left no partial rows behind.
    assert crashed_factory.state.deal_scenarios == []
    assert crashed_factory.state.offer_scenarios == []
    assert crashed_factory.state.scores == []

    crashed_pipeline.recompute(property_id_2)
    resumed = snapshot(crashed_factory.state)
    # Identical end state modulo the property id embedded in every row.
    def normalize(rows, pid):
        return json.loads(json.dumps(rows, default=str).replace(str(pid), "<pid>"))
    assert normalize(resumed, property_id_2) == normalize(reference, property_id)


# ------------------------------------------------------------------ extract_unit + fan-in

def _seed_units(state: FakeState, property_id, count, *, batch=None):
    report_id = uuid4()
    unit_ids = []
    for _ in range(count):
        unit_id = uuid4()
        state.units[unit_id] = {"id": unit_id, "report_id": report_id,
                                "property_id": property_id, "batch_id": batch,
                                "status": "queued", "unit_type": "liens",
                                "token_estimate": 100}
        unit_ids.append(unit_id)
    return unit_ids


def test_fan_in_triggers_exactly_one_recompute_for_concurrent_units():
    pipeline, factory = make_pipeline(extractor=lambda unit: [])
    property_id = seed_property(factory.state, with_facts=False)
    batch_id = uuid4()
    factory.state.batches[batch_id] = {"id": batch_id, "status": "extracting",
                                       "total_count": 20, "completed_count": 0,
                                       "failed_count": 0, "awaiting_confirmation": False,
                                       "spent_usd": Decimal(0), "budget_limit_usd": None}
    unit_ids = _seed_units(factory.state, property_id, 20, batch=batch_id)

    recompute_calls = []
    counter_lock = threading.Lock()
    original = pipeline.recompute

    def counting_recompute(pid, **kwargs):
        with counter_lock:
            recompute_calls.append(pid)
        return original(pid, **kwargs)

    pipeline.recompute = counting_recompute
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(pipeline.extract_unit, unit_ids))

    assert recompute_calls == [property_id]  # exactly one recompute for 20 units
    assert all(outcome.transitioned for outcome in outcomes)
    assert all(unit["status"] == "extracted" for unit in factory.state.units.values())
    assert factory.state.batches[batch_id]["completed_count"] == 20
    assert factory.state.batches[batch_id]["status"] == "computing"
    assert len(factory.state.scores) == 1


def test_extract_unit_persists_facts_and_recomputes_when_complete():
    pipeline, factory = make_pipeline()
    property_id = seed_property(factory.state, with_facts=False)
    report_id = uuid4()
    unit_ids = _seed_units(factory.state, property_id, 2)

    def extractor(unit):
        return property_facts(property_id, unit["id"], report_id)

    pipeline.extract_unit(unit_ids[0], extractor=extractor)
    assert factory.state.deal_scenarios == []  # no recompute while units outstanding
    pipeline.extract_unit(unit_ids[1], extractor=extractor)
    assert len(factory.state.scores) == 1
    assert len(factory.state.facts[property_id]) == 2 * len(property_facts(property_id, uuid4(), report_id))


def test_extract_unit_retry_does_not_duplicate_facts():
    pipeline, factory = make_pipeline(extractor=lambda unit: [])
    property_id = seed_property(factory.state, with_facts=False)
    (unit_id,) = _seed_units(factory.state, property_id, 1)

    first = pipeline.extract_unit(unit_id)
    assert first.transitioned and first.outstanding == 0
    # Crash-after-finish resume: the retry must not re-extract, and the fan-in
    # still fires the (idempotent) recompute.
    second = pipeline.extract_unit(unit_id)
    assert not second.transitioned
    assert len(factory.state.scores) == 1


def test_extract_unit_budget_pause_and_resume():
    state = FakeState()
    factory = FakeStoreFactory(state)
    pipeline = Pipeline(factory)
    property_id = seed_property(state, with_facts=False)
    batch_id = uuid4()
    state.batches[batch_id] = {"id": batch_id, "status": "extracting", "total_count": 1,
                               "completed_count": 0, "failed_count": 0,
                               "awaiting_confirmation": False, "spent_usd": Decimal(0),
                               "budget_limit_usd": Decimal(100)}
    (unit_id,) = _seed_units(state, property_id, 1, batch=batch_id)

    class CostlyResult:
        facts = []
        cost_usd = Decimal(200)

    with pytest.raises(AcqError) as excinfo:
        pipeline.extract_unit(unit_id, extractor=lambda unit: CostlyResult())
    assert excinfo.value.code == ErrorCode.BUDGET_PAUSED
    assert state.batches[batch_id]["status"] == "paused_budget"
    assert state.units[unit_id]["status"] == "queued"  # no work lost

    # Paused batch blocks further extraction until the budget is raised.
    with pytest.raises(AcqError):
        pipeline.extract_unit(unit_id, extractor=lambda unit: [])

    batch_machine.resume_batch(_store(factory), batch_id, new_budget_limit_usd=Decimal(1000))
    assert state.batches[batch_id]["status"] == "extracting"

    outcome = pipeline.extract_unit(unit_id, extractor=lambda unit: CostlyResult())
    assert outcome.transitioned
    assert state.units[unit_id]["status"] == "extracted"
    assert state.batches[batch_id]["spent_usd"] == Decimal(200)


def _store(factory: FakeStoreFactory) -> FakeStore:
    """Direct (untransactional) store handle for driving the batch machine."""
    return FakeStore(factory.state, factory)


def _seed_batch(state: FakeState, **overrides):
    batch_id = uuid4()
    state.batches[batch_id] = {"id": batch_id, "status": "uploading", "file_count": 0,
                               "total_count": 0, "completed_count": 0, "failed_count": 0,
                               "awaiting_confirmation": False, "spent_usd": Decimal(0),
                               "budget_limit_usd": None, **overrides}
    return batch_id


# ------------------------------------------------------------------ batch state machine

def test_batch_estimate_over_budget_awaits_confirmation():
    state = FakeState()
    batch_id = _seed_batch(state, status="estimating", budget_limit_usd=Decimal(1000))
    store = _store(FakeStoreFactory(state))

    assert batch_machine.estimation_ready(store, batch_id, Decimal(5000)) == "awaiting_confirmation"
    assert state.batches[batch_id]["awaiting_confirmation"] is True
    assert state.batches[batch_id]["estimated_cost_usd"] == Decimal(5000)

    assert batch_machine.confirm_estimate(store, batch_id) == "extracting"
    assert state.batches[batch_id]["awaiting_confirmation"] is False


def test_batch_estimate_within_budget_goes_straight_to_extracting():
    state = FakeState()
    batch_id = _seed_batch(state, status="estimating", budget_limit_usd=Decimal(1000))
    store = _store(FakeStoreFactory(state))
    assert batch_machine.estimation_ready(store, batch_id, Decimal(500)) == "extracting"
    assert state.batches[batch_id]["awaiting_confirmation"] is False


def test_batch_confirm_requires_awaiting_confirmation():
    state = FakeState()
    batch_id = _seed_batch(state, status="extracting")
    with pytest.raises(AcqError) as excinfo:
        batch_machine.confirm_estimate(_store(FakeStoreFactory(state)), batch_id)
    assert excinfo.value.code == ErrorCode.CONFLICT


def test_batch_unit_completion_drives_computing_then_complete():
    state = FakeState()
    batch_id = _seed_batch(state, status="extracting", total_count=2)
    store = _store(FakeStoreFactory(state))

    assert batch_machine.start_ingestion(store, batch_id, file_count=2) == "ingesting"
    store.update_batch(batch_id, status="extracting")
    assert batch_machine.unit_finished(store, batch_id) == "extracting"
    assert batch_machine.unit_finished(store, batch_id) == "computing"
    assert batch_machine.mark_complete(store, batch_id) == "complete"


def test_batch_pause_and_resume_budget():
    state = FakeState()
    batch_id = _seed_batch(state, status="extracting", budget_limit_usd=Decimal(100))
    store = _store(FakeStoreFactory(state))

    assert batch_machine.pause_budget(store, batch_id) == "paused_budget"
    assert batch_machine.resume_batch(store, batch_id,
                                      new_budget_limit_usd=Decimal(500)) == "extracting"
    assert state.batches[batch_id]["budget_limit_usd"] == Decimal(500)


# ------------------------------------------------------------------ bulk recompute

def test_bulk_recompute_enqueues_in_chunks_with_progress():
    state = FakeState()
    factory = FakeStoreFactory(state)
    for _ in range(5):
        state.properties[uuid4()] = {"id": uuid4()}
    state.properties = {pid: {"id": pid} for pid in state.properties}
    enqueued = []
    pipeline = Pipeline(factory, enqueue=lambda name, payload, key: enqueued.append((name, payload, key)))

    result = pipeline.bulk_recompute(reason="assumption_change", chunk_size=2)

    assert result["total"] == 5 and result["enqueued"] == 5
    assert len(enqueued) == 5
    assert all(name == "recompute_property" for name, _, _ in enqueued)
    assert all(payload["reason"] == "assumption_change" for _, payload, _ in enqueued)
    assert all(key == f"recompute_property:{payload['property_id']}"
               for _, payload, key in enqueued)
    progress = state.settings[f"bulk_recompute:{result['run_id']}"]
    assert progress == {"status": "complete", "total": 5, "enqueued": 5,
                        "reason": "assumption_change"}


# ------------------------------------------------------------------ rank_scope / nightly

def test_rank_scope_writes_snapshot_and_carries_prev_rank():
    pipeline, factory = make_pipeline()
    first = seed_property(factory.state)
    second = seed_property(factory.state)
    pipeline.recompute(first)
    pipeline.recompute(second)

    assert pipeline.rank_scope("portfolio") == 2
    rows = factory.state.rankings
    assert {row["rank"] for row in rows} == {1, 2}
    assert all(row["prev_rank"] is None for row in rows)
    by_property = {row["property_id"]: row["rank"] for row in rows}

    assert pipeline.nightly()["ranked"] == 2
    for row in factory.state.rankings:
        assert row["prev_rank"] == by_property[row["property_id"]]


# ------------------------------------------------------------------ detect_changes

def test_detect_changes_persists_change_events():
    pipeline, factory = make_pipeline()
    property_id = seed_property(factory.state)
    before = resolve_facts(property_id, factory.state.facts[property_id])

    # A new lien lands in the ledger after the snapshot was taken.
    factory.state.facts[property_id].extend([
        make_fact(property_id, uuid4(), uuid4(), EntityType.LIEN, "l9", "lien.type",
                  value_text="hoa"),
        make_fact(property_id, uuid4(), uuid4(), EntityType.LIEN, "l9", "lien.amount",
                  value_parsed=Decimal(9000)),
        make_fact(property_id, uuid4(), uuid4(), EntityType.LIEN, "l9", "lien.attachment_basis",
                  value_text="recorded_against_property"),
    ])

    events = pipeline.detect_changes(property_id, before=before)

    assert any("lien" in event.change_type for event in events)
    persisted = factory.state.change_events
    assert len(persisted) == len(events) > 0
    assert all(row["property_id"] == property_id for row in persisted)


def test_detect_changes_without_snapshot_is_a_noop():
    pipeline, factory = make_pipeline()
    property_id = seed_property(factory.state)
    assert pipeline.detect_changes(property_id) == []
    assert factory.state.change_events == []


# ------------------------------------------------------------------ worker

def test_worker_registers_all_pipeline_job_handlers():
    handlers = default_handlers()
    assert {"ingest_document", "extract_unit", "recompute_property",
            "rank_scope", "detect_changes", "nightly"} <= set(handlers)


class FakeQueue:
    """claim/complete/fail over a list, mirroring PostgresJobQueue's surface."""

    def __init__(self, jobs):
        self.jobs = jobs

    def claim(self, session):
        job = next((job for job in self.jobs if job["status"] == "queued"), None)
        if job is not None:
            job["status"] = "running"
            job["attempts"] += 1
        return job

    def complete(self, session, job_id):
        self._get(job_id)["status"] = "complete"

    def fail(self, session, job_id, attempts, max_attempts, error):
        job = self._get(job_id)
        job["status"] = "dead" if attempts >= max_attempts else "queued"
        job["error"] = error

    def _get(self, job_id):
        return next(job for job in self.jobs if job["id"] == job_id)


def _job(name, payload, max_attempts=2):
    return {"id": uuid4(), "name": name, "payload": payload, "status": "queued",
            "attempts": 0, "max_attempts": max_attempts}


def test_worker_runs_recompute_job_end_to_end():
    pipeline, factory = make_pipeline()
    property_id = seed_property(factory.state)
    queue = FakeQueue([_job("recompute_property",
                            {"property_id": str(property_id), "reason": "test"})])
    handlers = {"recompute_property": lambda payload: pipeline.recompute(
        payload["property_id"], reason=payload.get("reason", "manual"))}
    worker = Worker(handlers, queue=queue, session_factory=lambda: nullcontext(None))

    assert worker.run_once() is True
    assert queue.jobs[0]["status"] == "complete"
    assert len(factory.state.scores) == 1
    assert worker.run_once() is False  # queue drained


def test_worker_requeues_failed_job_then_dead_letters():
    queue = FakeQueue([_job("recompute_property", {"property_id": str(uuid4())})])

    def boom(payload):
        raise RuntimeError("boom")

    worker = Worker({"recompute_property": boom}, queue=queue,
                    session_factory=lambda: nullcontext(None))
    worker.run_once()
    assert queue.jobs[0]["status"] == "queued"  # retry with attempts < max
    assert queue.jobs[0]["error"] == "boom"
    worker.run_once()
    assert queue.jobs[0]["status"] == "dead"


# ------------------------------------------------------------------ pure compute core

def test_pure_recompute_property_signature_is_stable():
    from uuid import UUID as _UUID

    from contracts import AddressBlock, NormalizedProperty
    assumptions = load_assumptions()
    record = NormalizedProperty(property_id=uuid4(), address=AddressBlock(line1="1 Main St"),
                                resolution_version="test")
    computation = recompute_property(record, assumptions, _UUID(int=0), Decimal(0))
    assert computation.underwriting.status == "insufficient_data"
    assert computation.grid.points == []
