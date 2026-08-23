"""Owner intelligence, grounded chat, archive, and outreach invariants."""

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from chat.service import (
    ChatProviderClient,
    ChatTurn,
    answer_chat,
    ungrounded_numbers,
    validate_grounded_numbers,
)
from contracts import AssumptionSet, NormalizedProperty, Scenario
from db import models as dbm
from finance import underwrite
from identity.service import normalize_mailing_address, normalize_owner_name
from ops.chat_budget import (
    cache_document_text,
    cached_document_text,
    chat_session_key,
    reconcile_daily_chat_budget,
    reserve_chat_session_tokens,
    reserve_daily_chat_budget,
)
from outreach.service import Draft, generate_draft, validate_draft
from report_analysis.classification import classify_document_text
from scoring import score


def test_structural_document_classification_ignores_filename():
    owner_text = "MARLENE C LEWIS Person Type Individual Ownership Role Owner"
    property_text = "APN 931-762-13 Property Details Tax Assessment"
    assert classify_document_text(owner_text)[0] == "owner_profile"
    assert classify_document_text(property_text)[0] == "property_profile"


def test_owner_identity_normalizes_reversed_name_and_mailing_address():
    assert normalize_owner_name("LEWIS,MARLENE C") == normalize_owner_name("MARLENE C LEWIS")
    assert normalize_mailing_address("2549 Eastbluff Dr #279, Newport Beach CA 92660") == (
        normalize_mailing_address("2549 EASTBLUFF DR 279 NEWPORT BEACH, CA 92660")
    )


def test_chat_rejects_numbers_absent_from_structured_or_tool_data():
    context = {"equity": "431824"}
    assert validate_grounded_numbers("Equity is $431,824 (equity.expected).", context, {})
    assert not validate_grounded_numbers("Equity is $500,000.", context, {})


class FakeChatProvider:
    def complete(self, messages, structured_context, tool_results):
        return ChatTurn("Expected equity is $431,824 (equity.expected).", 10, 8,
                        Decimal("0.001"), "fake")


def test_chat_answer_uses_grounded_provider_numbers():
    turn = answer_chat(FakeChatProvider(), [{"role": "user", "content": "equity?"}],
                       {"equity": "431824"}, {})
    assert "$431,824" in turn.text


def test_grounding_allows_human_percent_rounding_of_context_ratios():
    context = {"confidence": 0.9167}
    assert validate_grounded_numbers("Confidence is about 92%.", context, {})
    assert not validate_grounded_numbers("Confidence is about 97%.", context, {})


class UngroundedThenGroundedProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, structured_context, tool_results, *,
                 tool_definitions=None, execute_tool=None):
        self.calls += 1
        if any("ONLY numbers copied verbatim" in str(m.get("content")) for m in messages):
            return ChatTurn("The equity is $431,824 per equity.expected.", 5, 6,
                            Decimal("0.001"), "fake")
        return ChatTurn("The equity is roughly $999,999 by my estimate.", 10, 8,
                        Decimal("0.001"), "fake")


def test_grounding_allows_k_and_m_shorthand_and_page_ranges():
    context = {"value": 725000, "arv": 1200000}
    assert validate_grounded_numbers("Value is about $725K.", context, {})
    assert validate_grounded_numbers("ARV is roughly $1.2M.", context, {})
    # page ranges extract a negative component ("7-9" -> -9)
    assert validate_grounded_numbers("See pages 7-9 of the report.", {"page": "7"}, {})
    assert not validate_grounded_numbers("Value is about $800K.", context, {})


def test_ungrounded_numbers_reports_offenders():
    offenders = ungrounded_numbers("Worth $999,888 or 2.5x.", {"value": 725000}, {})
    assert Decimal("999888") in offenders
    assert any(value == Decimal("2.5") for value in offenders)


def test_salvage_keeps_grounded_sentences_and_drops_offenders():
    from chat.service import _salvage_grounded_sentences

    context = {"equity": "431824", "value": 725000}
    text = ("The equity is $431,824 per equity.expected. "
            "That is roughly $999,999 above the market. "
            "The expected value is $725,000.")
    salvaged, dropped = _salvage_grounded_sentences(text, context, {})
    assert dropped == 1
    assert "$431,824" in salvaged and "$725,000" in salvaged
    assert "999,999" not in salvaged
    assert "omitted" in salvaged

    nothing_kept, dropped = _salvage_grounded_sentences("All bogus $1,000,000.", context, {})
    assert nothing_kept is None and dropped == 1


def test_ungrounded_reply_retries_then_never_raises():
    provider = UngroundedThenGroundedProvider()
    turn = answer_chat(provider, [{"role": "user", "content": "equity?"}],
                       {"equity": "431824"}, {})
    assert "$431,824" in turn.text
    assert provider.calls == 2
    # retry costs are billed to the same turn
    assert turn.input_tokens == 15 and turn.output_tokens == 14


class AlwaysUngroundedProvider:
    def complete(self, messages, structured_context, tool_results, *,
                 tool_definitions=None, execute_tool=None):
        return ChatTurn("Definitely $999,999.", 10, 8, Decimal("0.001"), "fake")


def test_persistently_ungrounded_reply_degrades_to_safe_fallback():
    turn = answer_chat(AlwaysUngroundedProvider(), [{"role": "user", "content": "equity?"}],
                       {"equity": "431824"}, {})
    assert "couldn't answer" in turn.text
    assert "999" not in turn.text


def test_chat_provider_executes_requested_tool_before_answering():
    requests = []

    def transport(method, url, headers, body, timeout):
        request = __import__("json").loads(body)
        requests.append(request)
        if len(requests) == 1:
            return 200, {
                "id": "resp_1", "model": "gpt-4o-mini",
                "output": [{
                    "type": "function_call", "call_id": "call_1",
                    "name": "lookup", "arguments": "{}",
                }],
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        return 200, {
            "id": "resp_2", "model": "gpt-4o-mini",
            "output": [{"type": "message", "content": [{
                "type": "output_text", "text": "Expected equity is $431,824 (lookup.equity).",
            }]}],
            "usage": {"input_tokens": 5, "output_tokens": 8},
        }

    client = ChatProviderClient(api_key="test", transport=transport)
    definitions = [{
        "type": "function", "name": "lookup", "description": "Look up equity.",
        "strict": True,
        "parameters": {"type": "object", "properties": {},
                       "additionalProperties": False, "required": []},
    }]
    turn = answer_chat(
        client, [{"role": "user", "content": "What is expected equity?"}], {}, {},
        tool_definitions=definitions, execute_tool=lambda name, arguments: {"equity": "431824"},
    )
    assert len(requests) == 2
    assert requests[1]["previous_response_id"] == "resp_1"
    assert requests[1]["input"][0]["type"] == "function_call_output"
    assert turn.input_tokens == 15
    assert turn.tool_results == {"lookup": {"equity": "431824"}}


def test_chat_session_tokens_and_actual_daily_cost_are_persistently_capped():
    engine = create_engine("sqlite:///:memory:")
    dbm.Setting.__table__.create(engine)
    with Session(engine) as session:
        key = chat_session_key("owner", uuid4())
        assert reserve_chat_session_tokens(session, key, 60, 100)
        assert not reserve_chat_session_tokens(session, key, 41, 100)
        assert reserve_daily_chat_budget(session, Decimal("0.10"), Decimal("0.20"))
        reconcile_daily_chat_budget(session, Decimal("0.10"), Decimal("0.15"))
        value = session.execute(text(
            "SELECT value FROM settings WHERE key LIKE 'chat_spend:%'",
        )).scalar_one()
        if isinstance(value, str):
            value = __import__("json").loads(value)
        assert Decimal(str(value["reserved"])) == Decimal("0.15")
        assert not reserve_daily_chat_budget(session, Decimal("0.06"), Decimal("0.20"))
        cache_document_text(session, key, "report:1:2", {"pages": [{"page": 1, "text": "x"}]})
        assert cached_document_text(session, key, "report:1:2") == {
            "pages": [{"page": 1, "text": "x"}],
        }


def test_owner_profile_events_are_reference_only_for_finance_and_scoring():
    record = NormalizedProperty.model_validate(__import__("json").loads(
        Path("fixtures/normalized/02_owner_only_federal_lien.json").read_text(),
    ))
    assumptions = AssumptionSet.model_validate(__import__("json").loads(
        Path("fixtures/assumptions/default.json").read_text(),
    ))
    before_underwriting = underwrite(record, assumptions)
    before_score = score(record, before_underwriting, uuid4())

    owner_id = uuid4()
    owner_events = [
        dbm.BankruptcyEvent(owner_id=owner_id, property_id=None, chapter="13",
                            status="dismissed", filing_sequence=5, is_repeat=True),
        dbm.Lien(owner_id=owner_id, property_id=None, lien_type="federal_tax",
                 amount=Decimal("140294"), status="open", attachment_basis="owner_named_only",
                 attachment_confidence=Decimal("0.95")),
    ]
    assert all(event.property_id is None for event in owner_events)

    after_underwriting = underwrite(record, assumptions)
    after_score = score(record, after_underwriting, before_score.scoring_config_id)
    assert after_underwriting.equity[Scenario.EXPECTED].adjusted == (
        before_underwriting.equity[Scenario.EXPECTED].adjusted
    )
    assert after_underwriting.liabilities == before_underwriting.liabilities
    assert after_score.model_dump() == before_score.model_dump()


class RegeneratingOutreachProvider:
    def __init__(self):
        self.calls = 0

    def generate(self, context, *, prior_draft=None, instruction=None):
        self.calls += 1
        if self.calls == 1:
            return Draft("Regarding foreclosure", "We know about the auction.")
        return Draft("Cash offer for 57 Cottage Ln", "We can offer $950,000 cash with flexible timing.")


def test_outreach_distress_vocabulary_regenerates_once():
    provider = RegeneratingOutreachProvider()
    draft = generate_draft(provider, {"offer_price": "950000", "address": "57 Cottage Ln"})
    assert provider.calls == 2
    assert validate_draft(draft)
    assert "$950,000" in draft.body


class AlwaysUnsafeProvider:
    def generate(self, context, *, prior_draft=None, instruction=None):
        return Draft("Auction", "Your bankruptcy prompted this offer.")


def test_outreach_rejects_second_policy_failure():
    with pytest.raises(ValueError, match="content policy"):
        generate_draft(AlwaysUnsafeProvider(), {"offer_price": "950000"})
