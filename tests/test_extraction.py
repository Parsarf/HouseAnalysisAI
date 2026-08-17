"""Offline tests for WP-4 extraction. No network, no live DB:
providers are fake transports; persistence runs against SQLite in memory."""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from common.db import Base
from common.errors import AcqError, ErrorCode
from contracts import RecordedResponse
from db.models import Batch, ExtractedFact, ExtractionUnit
from extraction import (
    UNIT_SCHEMAS,
    ExtractionService,
    ProviderClient,
    UnitInput,
    compute_cost,
    estimate_cost,
    flatten_payload,
    parse_numeric,
    replay_response,
    route_model,
    run_gauntlet,
    schema_for,
)
from extraction.client import ENV_API_KEY
from extraction.eval import evaluate
from extraction.eval import main as eval_main
from extraction.prompts import load_prompt
from ops import reserve_budget
from tests.recorded import load_response

PAGE = (
    "INVOLUNTARY LIENS\n"
    "Lien Type: Federal Tax\n"
    "Creditor: INTERNAL REVENUE SERVICE\n"
    "Debtor: JOHN A SMITH\n"
    "Amount: $48,210.50\n"
    "Recording Date: 2022-03-14\n"
    "Status: Active\n"
    "Recorded against APN 4821-003-011, Lot 12, Tract 4455.\n"
)
PAGES = {1: PAGE}

GOOD_LIEN = {
    "lien_type": "federal_tax",
    "creditor_raw": "INTERNAL REVENUE SERVICE",
    "debtor_name_raw": "JOHN A SMITH",
    "amount_raw": "$48,210.50",
    "amount_parsed": 48210.50,
    "recording_date": "2022-03-14",
    "recording_doc_number": "2022-0088123",
    "status": "active",
    "attachment_basis": "recorded_against_property",
    "attachment_evidence": "Recorded against APN 4821-003-011",
    "attachment_confidence": 0.95,
    "page_number": 1,
    "snippet": "Recorded against APN 4821-003-011, Lot 12, Tract 4455",
    "extraction_confidence": 0.97,
    "null_reason": None,
}

USAGE = {"prompt_tokens": 10_000, "completion_tokens": 2_000}


def make_transport(payloads, calls, usage=None):
    """Fake transport: pops one payload per call; payloads of int are HTTP statuses."""
    queue = list(payloads)

    def transport(method, url, headers, body, timeout):
        calls.append(json.loads(body))
        response = queue.pop(0)
        if isinstance(response, int):
            return response, {"error": {"message": "boom"}}
        return 200, {"choices": [{"message": {"content": json.dumps(response)}}],
                     "usage": usage or {"prompt_tokens": 100, "completion_tokens": 25}}

    return transport


def make_provider(payloads, calls, **kwargs):
    return ProviderClient(api_key="test-key", transport=make_transport(payloads, calls),
                          sleep=lambda _: None, **kwargs)


def make_unit(unit_type="liens", text=PAGE, **kwargs):
    kwargs.setdefault("id", uuid4())
    kwargs.setdefault("report_id", uuid4())
    kwargs.setdefault("token_estimate", 200)
    return UnitInput(unit_type=unit_type, text=text, page_start=1, page_end=1, **kwargs)


@pytest.fixture
def session():
    # reserve_budget issues raw text() SQL; teach sqlite to bind Decimal/UUID
    # the same way SQLAlchemy's column types store them.
    import sqlite3
    sqlite3.register_adapter(Decimal, float)
    sqlite3.register_adapter(UUID, lambda value: value.hex)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Batch.__table__, ExtractionUnit.__table__, ExtractedFact.__table__])
    with Session(engine) as session:
        yield session


# --- Schemas and routing ------------------------------------------------------

def test_twelve_unit_schemas():
    assert len(UNIT_SCHEMAS) == 12
    for schema in UNIT_SCHEMAS.values():
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        (key,) = schema["required"]
        item = schema["properties"][key]["items"]
        assert {"page_number", "snippet", "extraction_confidence"} <= set(item["required"])
        assert item["properties"]["snippet"]["maxLength"] == 200


def test_model_routing():
    cheap, frontier = "cheap-x", "frontier-x"
    for unit_type in ("comparables", "listings", "tax", "rental", "valuation", "property_core"):
        assert route_model(unit_type, cheap_model=cheap, frontier_model=frontier) == cheap
    for unit_type in ("liens", "mortgages", "foreclosure", "bankruptcy", "combined", "unknown"):
        assert route_model(unit_type, cheap_model=cheap, frontier_model=frontier) == frontier


def test_schema_aliases():
    assert schema_for("lien") is schema_for("liens")
    assert schema_for("mortgage") is schema_for("mortgages")
    assert schema_for("owner_report") is schema_for("ownership")


# --- Provider client -----------------------------------------------------------

def test_provider_request_uses_tool_mode_and_temperature_zero_when_supported():
    calls = []
    provider = make_provider([{"liens": []}], calls, frontier_model="gpt-4o")
    provider.complete("liens", PAGE, subject="Subject: 1 Main St", system_prompt=load_prompt())
    body = calls[0]
    assert body["model"] == "gpt-4o"
    assert body["temperature"] == 0
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == schema_for("liens")
    assert body["messages"][0]["role"] == "system"
    assert "Subject: 1 Main St" in body["messages"][1]["content"]


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5-mini", "openai/gpt-5-mini", "o3"])
def test_provider_omits_temperature_for_default_only_models(model):
    calls = []
    provider = make_provider([{"liens": []}], calls, frontier_model=model)

    provider.complete("liens", PAGE, system_prompt=load_prompt())

    assert calls[0]["model"] == model
    assert "temperature" not in calls[0]


def test_provider_retries_alias_without_temperature_when_provider_rejects_it():
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append(json.loads(body))
        if len(calls) == 1:
            return 400, {"error": {"message": (
                "Unsupported value: 'temperature' does not support 0 with this model. "
                "Only the default (1) value is supported."
            )}}
        return 200, {
            "choices": [{"message": {"content": json.dumps({"liens": []})}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 25},
        }

    provider = ProviderClient(
        api_key="test-key", frontier_model="provider/latest",
        transport=transport, sleep=lambda _: None,
    )

    response = provider.complete("liens", PAGE, system_prompt=load_prompt())

    assert calls[0]["temperature"] == 0
    assert "temperature" not in calls[1]
    assert response.attempts == 2


def test_provider_env_configuration(monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, "env-key")
    monkeypatch.setenv("ACQ_EXTRACTION_BASE_URL", "https://example.test/v1/")
    monkeypatch.setenv("ACQ_EXTRACTION_CHEAP_MODEL", "cheap-env")
    provider = ProviderClient()
    assert provider.api_key == "env-key"
    assert provider.base_url == "https://example.test/v1"
    assert provider.cheap_model == "cheap-env"


def test_provider_requires_api_key(monkeypatch):
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    provider = ProviderClient(transport=make_transport([{"liens": []}], []))
    with pytest.raises(AcqError) as error:
        provider.complete("liens", PAGE, system_prompt=load_prompt())
    assert error.value.code == ErrorCode.EXTRACTION_FAILED


def test_retry_backoff_then_success():
    calls, delays = [], []
    provider = ProviderClient(api_key="k", transport=make_transport([500, 429, {"liens": []}], calls),
                              base_delay=0.5, sleep=delays.append)
    response = provider.complete("liens", PAGE, system_prompt=load_prompt())
    assert response.attempts == 3
    assert delays == [0.5, 1.0]  # exponential backoff
    assert len(calls) == 3


def test_retry_exhaustion():
    provider = ProviderClient(api_key="k", transport=make_transport([500] * 10, []),
                              base_delay=0.5, max_retries=5, sleep=lambda _: None)
    with pytest.raises(AcqError) as error:
        provider.complete("liens", PAGE, system_prompt=load_prompt())
    assert error.value.code == ErrorCode.RETRY_EXHAUSTED


def test_client_error_not_retried():
    calls = []
    provider = ProviderClient(api_key="k", transport=make_transport([400], calls), sleep=lambda _: None)
    with pytest.raises(AcqError) as error:
        provider.complete("liens", PAGE, system_prompt=load_prompt())
    assert error.value.code == ErrorCode.EXTRACTION_FAILED
    assert len(calls) == 1


def test_schema_repair_retry_once():
    calls = []
    provider = make_provider([{"wrong_key": []}, {"liens": []}], calls)
    response = provider.complete("liens", PAGE, system_prompt=load_prompt())
    assert response.payload == {"liens": []}
    assert len(calls) == 2  # exactly one repair retry
    repair = calls[1]["messages"][-1]["content"]
    assert "liens" in repair  # the schema error is appended to the repair prompt


def test_schema_repair_second_failure_raises():
    provider = make_provider([{"wrong": []}, {"still_wrong": []}], [])
    with pytest.raises(AcqError) as error:
        provider.complete("liens", PAGE, system_prompt=load_prompt())
    assert error.value.code == ErrorCode.INVALID_INPUT


def test_cost_accounting_matches_usage():
    calls = []
    provider = make_provider([{"liens": []}], calls, frontier_model="gpt-4o")
    response = provider.complete("liens", PAGE, system_prompt=load_prompt())
    # compute_cost is called with usage 100/25 from make_transport default
    assert response.cost_usd == compute_cost("gpt-4o", {"prompt_tokens": 100, "completion_tokens": 25})
    expected = (Decimal(100) * Decimal("2.50") + Decimal(25) * Decimal("10.00")) / Decimal(1_000_000)
    assert response.cost_usd == expected.quantize(Decimal("0.000001"))


# --- Parse consistency ---------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("$48,210.50", Decimal("48210.50")),
    ("(1,200)", Decimal(-1200)),
    ("1.2M", Decimal(1200000)),
    ("500k", Decimal(500000)),
    ("6.5%", Decimal("6.5")),
    ("- $2,000.00", Decimal("-2000.00")),
])
def test_parse_numeric(raw, expected):
    assert parse_numeric(raw) == expected


@pytest.mark.parametrize("raw", ["N/A", "one million", "", None, "$abc"])
def test_parse_numeric_rejects_non_numeric(raw):
    assert parse_numeric(raw) is None


# --- The validation gauntlet ---------------------------------------------------

def flatten(payload, unit_type="liens"):
    return flatten_payload(unit_type, payload, report_id=uuid4(), extraction_unit_id=uuid4())


def test_flatten_good_lien_all_active():
    drafts, dropped = flatten({"liens": [GOOD_LIEN]})
    assert dropped == 0
    outcome = run_gauntlet(drafts, PAGES)
    assert outcome.dropped == 0 and outcome.inactive == []
    paths = {fact.field_path for fact in outcome.active}
    assert {"liens[0].amount", "liens[0].attachment_basis", "liens[0].recording_date"} <= paths
    amount = next(f for f in outcome.active if f.field_path == "liens[0].amount")
    assert amount.value_parsed == Decimal("48210.5") and amount.value_raw == "$48,210.50"
    date_fact = next(f for f in outcome.active if f.field_path == "liens[0].recording_date")
    assert date_fact.value_date == date(2022, 3, 14)


def test_poisoned_response_zero_facts_right_counters():
    poisoned = {"liens": [
        {**GOOD_LIEN, "snippet": "THIS SNIPPET IS FABRICATED"},  # grounding failure
        {**GOOD_LIEN, "amount_raw": "$99,999,999,999", "amount_parsed": 99_999_999_999},  # range
        {  # null without reason
            "lien_type": None, "creditor_raw": None, "debtor_name_raw": None,
            "amount_raw": None, "amount_parsed": None, "recording_date": None,
            "recording_doc_number": None, "status": None, "attachment_basis": None,
            "attachment_evidence": None, "attachment_confidence": None,
            "page_number": 1, "snippet": "Status: Active",
            "extraction_confidence": 0.5, "null_reason": None,
        },
    ]}
    drafts, dropped = flatten(poisoned)
    outcome = run_gauntlet(drafts, PAGES, dropped=dropped)
    # Fabricated snippet: the whole item is dropped; grounding failures are never retried.
    assert [f for f in outcome.active if f.entity_local_id == "liens[0]"] == []
    # Null without a reason: dropped.
    assert [f for f in outcome.active if f.entity_local_id == "liens[2]"] == []
    # Out-of-range value: stored but inactive.
    assert len(outcome.inactive) == 1
    assert outcome.inactive[0].field_path == "liens[1].amount"
    assert outcome.counters["grounding_failed"] > 0
    assert outcome.counters["range_violation"] > 0
    assert outcome.counters["null_reason_missing"] > 0


def test_lien_naming_only_a_person_never_recorded_against_property():
    person_only = {**GOOD_LIEN,
                   "snippet": "Federal Tax Lien against JOHN A SMITH",
                   "attachment_evidence": "names the owner only"}
    page = {1: "INVOLUNTARY LIENS\nFederal Tax Lien against JOHN A SMITH\nAmount: $48,210.50"}
    drafts, _ = flatten({"liens": [person_only]})
    outcome = run_gauntlet(drafts, page)
    basis = next(f for f in outcome.active if f.field_path.endswith("attachment_basis"))
    assert basis.value_text == "unknown"
    assert outcome.counters["attachment_downgraded"] == 1


def test_lien_with_parcel_anchor_keeps_recorded_basis():
    drafts, _ = flatten({"liens": [GOOD_LIEN]})
    outcome = run_gauntlet(drafts, PAGES)
    basis = next(f for f in outcome.active if f.field_path.endswith("attachment_basis"))
    assert basis.value_text == "recorded_against_property"
    assert "attachment_downgraded" not in outcome.counters


def test_parse_inconsistency_drops_fact():
    bad = {**GOOD_LIEN, "amount_raw": "$48,210.50", "amount_parsed": 99_999.0}
    drafts, _ = flatten({"liens": [bad]})
    outcome = run_gauntlet(drafts, PAGES)
    assert outcome.counters["parse_inconsistency"] == 1
    assert all(f.field_path != "liens[0].amount" for f in outcome.active + outcome.inactive)


def test_raw_without_parse_requires_null_reason():
    illegible = {**GOOD_LIEN, "amount_raw": "$4?,210", "amount_parsed": None, "null_reason": None}
    drafts, _ = flatten({"liens": [illegible]})
    outcome = run_gauntlet(drafts, PAGES)
    # existing rule: value_raw without a parse and without a reason is invalid
    assert outcome.counters.get("invalid_input", 0) == 1
    assert all(f.field_path != "liens[0].amount" for f in outcome.active)


def test_cross_field_mortgage_rules():
    page = {2: "MORTGAGE HISTORY\nFirst: $400,000 at 6.5%\nSecond: $50,000\nBalance: $900,000"}
    payload = {"mortgages": [
        {"position": "second", "lender_raw": "BANK B", "original_amount_raw": "$50,000",
         "original_amount_parsed": 50_000, "balance_raw": None, "balance_parsed": None,
         "rate_raw": None, "rate_parsed": None, "term_months_raw": None, "term_months_parsed": None,
         "origination_date": None, "recording_date": None, "balance_as_of": None,
         "recording_doc_number": None, "is_open": True,
         "page_number": 2, "snippet": "Second: $50,000", "extraction_confidence": 0.9,
         "null_reason": None},
        {"position": "first", "lender_raw": "BANK A", "original_amount_raw": "$400,000",
         "original_amount_parsed": 400_000, "balance_raw": "$900,000", "balance_parsed": 900_000,
         "rate_raw": "6.5%", "rate_parsed": 6.5, "term_months_raw": None, "term_months_parsed": None,
         "origination_date": None, "recording_date": None, "balance_as_of": None,
         "recording_doc_number": None, "is_open": True,
         "page_number": 2, "snippet": "Balance: $900,000", "extraction_confidence": 0.9,
         "null_reason": None},
    ]}
    drafts, _ = flatten(payload, "mortgages")
    outcome = run_gauntlet(drafts, page)
    # balance 900k > 1.5x original 400k -> balance fact inactive
    inactive_paths = {f.field_path for f in outcome.inactive}
    assert "mortgages[1].balance" in inactive_paths
    # a first exists here, so the second mortgage is fine
    assert all(not f.field_path.startswith("mortgages[0]") for f in outcome.inactive)

    payload["mortgages"] = payload["mortgages"][:1]  # second without a first
    drafts, _ = flatten(payload, "mortgages")
    outcome = run_gauntlet(drafts, page)
    assert outcome.active == [] and outcome.counters["cross_field_violation"] > 0


def test_cross_field_foreclosure_dates():
    page = {3: "FORECLOSURE DETAIL\nNOD recorded 2023-05-01\nNTS recorded 2023-02-01"}
    event = {"stage": "nts", "nod_date": "2023-05-01", "nts_date": "2023-02-01",
             "original_sale_date": None, "current_sale_date": None,
             "published_bid_raw": None, "published_bid_parsed": None,
             "default_amount_raw": None, "default_amount_parsed": None, "default_as_of": None,
             "trustee": None, "trustee_sale_number": None,
             "postponement_count_raw": None, "postponement_count_parsed": None,
             "rescission_count_raw": None, "rescission_count_parsed": None,
             "is_active": True, "page_number": 3, "snippet": "NTS recorded 2023-02-01",
             "extraction_confidence": 0.9, "null_reason": None}
    drafts, _ = flatten({"foreclosure_events": [event]}, "foreclosure")
    outcome = run_gauntlet(drafts, page)
    inactive_paths = {f.field_path for f in outcome.inactive}
    assert {"foreclosure_events[0].nod_date", "foreclosure_events[0].nts_date"} <= inactive_paths


def test_missing_snippet_dropped_at_schema_stage():
    drafts, dropped = flatten({"liens": [{**GOOD_LIEN, "snippet": ""}]})
    assert dropped == 1 and drafts == []


def test_determinism_same_payload_same_facts():
    recorded = load_response("lien_unit_sample")
    first = replay_response(recorded, PAGES)
    second = replay_response(recorded, PAGES)
    assert [f.model_dump(mode="json") for f in first.facts] == [f.model_dump(mode="json") for f in second.facts]
    assert first.dropped == second.dropped == 0


# --- Service: budget gate, persistence, reextract ------------------------------

def seed_unit(session, unit, text_path=None):
    session.add(ExtractionUnit(id=unit.id, report_id=unit.report_id, unit_type=unit.unit_type,
                               page_start=unit.page_start, page_end=unit.page_end,
                               text_path=str(text_path) if text_path else None,
                               token_estimate=unit.token_estimate))
    session.flush()


def test_extract_unit_persists_facts_and_unit_cost(session):
    unit = make_unit()
    seed_unit(session, unit)
    calls = []
    service = ExtractionService(make_provider([{"liens": [GOOD_LIEN]}], calls), reserve=reserve_budget)
    result = service.extract_unit(unit, session=session, page_text_by_number=PAGES)
    session.commit()
    facts = session.scalars(select(ExtractedFact)).all()
    assert len(facts) == len(result.facts)
    assert all(f.is_active for f in facts)
    amount = next(f for f in facts if f.field_path == "liens[0].amount")
    assert amount.value_parsed == Decimal("48210.5") and amount.report_id == unit.report_id
    row = session.get(ExtractionUnit, unit.id)
    assert row.status == "extracted" and row.model == "gpt-4o"
    # extracted_facts/extraction_units cost columns are numeric(14,2): sub-cent
    # costs round on write, in SQLite and Postgres alike.
    assert row.cost_usd == result.cost_usd.quantize(Decimal("0.01"))
    assert row.prompt_version == result.prompt_version


def test_budget_gate_pauses_before_provider_call(session):
    batch = Batch(id=uuid4(), budget_limit_usd=Decimal("0.00"), spent_usd=Decimal("0.00"))
    session.add(batch)
    unit = make_unit(batch_id=batch.id)
    seed_unit(session, unit)
    calls = []
    service = ExtractionService(make_provider([{"liens": [GOOD_LIEN]}], calls), reserve=reserve_budget)
    with pytest.raises(AcqError) as error:
        service.extract_unit(unit, session=session, page_text_by_number=PAGES)
    assert error.value.code == ErrorCode.BUDGET_PAUSED
    assert calls == []  # provider never called once the budget trips
    assert session.scalars(select(ExtractedFact)).all() == []


def test_budget_reservation_spends_estimate(session):
    batch = Batch(id=uuid4(), budget_limit_usd=Decimal("10.00"), spent_usd=Decimal("0.00"))
    session.add(batch)
    unit = make_unit(batch_id=batch.id, token_estimate=1_000_000)  # estimate: $5.00
    seed_unit(session, unit)
    service = ExtractionService(make_provider([{"liens": [GOOD_LIEN]}], []), reserve=reserve_budget)
    service.extract_unit(unit, session=session, page_text_by_number=PAGES)
    session.flush()
    session.expire(batch)  # the UPDATE bypasses the ORM identity map
    assert session.get(Batch, batch.id).spent_usd == estimate_cost(unit)


def test_reextract_supersedes_never_deletes(session, tmp_path):
    report_id = uuid4()
    text_file = tmp_path / "unit.txt"
    text_file.write_text(PAGE)
    unit = make_unit(report_id=report_id)
    seed_unit(session, unit, text_path=text_file)

    calls = []
    provider = make_provider([{"liens": [GOOD_LIEN]}], calls)
    ExtractionService(provider, reserve=reserve_budget).extract_unit(
        unit, session=session, page_text_by_number=PAGES)
    session.commit()
    original = session.scalars(select(ExtractedFact)).all()
    assert original and all(f.is_active for f in original)

    updated = {**GOOD_LIEN, "amount_raw": "$48,211.00", "amount_parsed": 48_211.00}
    provider2 = make_provider([{"liens": [updated]}], [])
    service = ExtractionService(provider2, reserve=reserve_budget)
    results = service.reextract(session, [report_id])
    session.commit()

    assert len(results) == 1
    all_facts = session.scalars(select(ExtractedFact)).all()
    assert len(all_facts) == 2 * len(original)  # nothing deleted
    old = [f for f in all_facts if f.id in {o.id for o in original}]
    assert all(not f.is_active for f in old)
    new_amount = next(f for f in all_facts if f.is_active and f.field_path == "liens[0].amount")
    assert new_amount.value_parsed == Decimal(48211)
    old_amount = next(f for f in old if f.field_path == "liens[0].amount")
    assert old_amount.superseded_by == new_amount.id


# --- Replay path and eval harness ----------------------------------------------

def test_replay_recorded_response_from_fixtures():
    recorded = load_response("lien_unit_sample")
    assert isinstance(recorded, RecordedResponse)
    result = replay_response(recorded, PAGES)
    assert result.dropped == 0
    basis = next(f for f in result.facts if f.field_path == "liens[0].attachment_basis")
    assert basis.value_text == "recorded_against_property"
    assert result.model == "gpt-4o"


def test_replay_poisoned_recorded_response_drops_everything():
    poisoned = RecordedResponse(response_id="p", model="gpt-4o", prompt_version="x", input_hash="h",
                                response={"liens": [{**GOOD_LIEN, "snippet": "NOT IN THE PAGE"}]})
    result = replay_response(poisoned, PAGES)
    assert result.facts == [] and result.counters["grounding_failed"] > 0


def test_eval_harness_scores_fixtures(capsys):
    fixtures_dir = Path(__file__).parents[1] / "fixtures" / "recorded_responses"
    report = evaluate(fixtures_dir)
    assert report.documents >= 1
    assert report.grounding_failures == 0 and report.grounding_failure_rate == 0.0
    assert report.field_scores["liens[0].attachment_basis"].accuracy == 1.0
    assert report.field_scores["liens[0].amount"].accuracy == 1.0
    assert report.overall_accuracy == 1.0
    eval_main(fixtures_dir)
    out = capsys.readouterr().out
    assert "field accuracy" in out and "grounding failures" in out


def test_eval_harness_empty_dir(tmp_path, capsys):
    report = evaluate(tmp_path)
    assert report.documents == 0
    eval_main(tmp_path)
    assert "No recorded responses" in capsys.readouterr().out
