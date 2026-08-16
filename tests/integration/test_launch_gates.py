import pytest


@pytest.mark.integration
def test_gate_a_requires_human_fixtures():
    pytest.skip("HUMAN_FIXTURE_INPUT_REQUIRED: owner must provide the 20 anonymized PDFs and expected units")


@pytest.mark.integration
def test_gate_b_requires_recorded_responses():
    pytest.skip("HUMAN_FIXTURE_INPUT_REQUIRED: owner must provide reviewed recorded model responses")
