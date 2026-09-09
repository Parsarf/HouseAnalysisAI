"""Behavioral regressions for real PDF containers with deterministic provider responses."""
import copy
import json
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import fitz
import pytest

from common.settings import settings
from db import models as dbm
from pipeline.worker import _mark_terminal_failure
from report_analysis.document_identity import resolve_identity
from report_analysis.document_schemas import Discovery
from report_analysis.documents import _validate_discovery, analyze_document
from report_analysis.provider import (
    PermanentProviderError,
    ProviderAnalysis,
    ProviderError,
    ProviderIncompleteError,
)
from report_analysis.read_model import load_record, merge_sources
from tests.test_whole_pdf_analysis import canonical_payload
from tests.test_whole_pdf_analysis import whole_pdf_harness as _whole_pdf_harness

whole_pdf_harness = _whole_pdf_harness


def pdf_bytes(pages):
    with fitz.open() as document:
        for text in pages:
            page = document.new_page()
            page.insert_text((50, 50), text)
        return document.tobytes()


def upload(harness, pages):
    response = harness.client.post("/api/uploads", files=[
        ("files", ("properties.pdf", pdf_bytes(pages), "application/pdf")),
    ])
    assert response.status_code == 200
    return response.json()


def property_payload(key):
    payload = canonical_payload()
    payload["property_identity"].update(address_line1=f"{100 if key == 'A' else 200} Main St",
                                         full_address=None, apn=f"parcel-{key}")
    payload["valuation"]["estimated_value"] = 500000 if key == "A" else 900000
    payload["loans"][0]["estimated_balance"] = 100000 if key == "A" else 300000
    return payload


class DocumentProvider:
    def __init__(self, *, fail=None, unreadable=None):
        self.calls = []
        self.fail = fail
        self.unreadable = unreadable

    def analyze_pdf(self, path, *, schema, instruction, log_context):
        self.calls.append(log_context["chunk_key"])
        with fitz.open(path) as doc:
            texts = [page.get_text() for page in doc]
        if not any(texts):
            raise ProviderIncompleteError("Simulated unreadable scan")
        if "entities" in schema["properties"]:
            keys = sorted({key for text in texts for key in ("A", "B") if key in text.split()})
            payload = {"entities": [], "pages": []}
            for key in keys:
                source = property_payload(key)["property_identity"]
                payload["entities"].append({
                    "key": key, "kind": "property", "role": "subject" if key == "A" else "comparable",
                    "address": source["address_line1"], "city": source["city"], "state": source["state"],
                    "zip5": source["zip5"], "apn": source["apn"], "jurisdiction": "Orange CA",
                    "name": None, "pages": [i for i, text in enumerate(texts, 1) if key in text.split()],
                    "evidence": f"Property row {key}",
                })
            for i, text in enumerate(texts, 1):
                page_keys = [key for key in keys if key in text.split()]
                status = "unreadable" if self.unreadable and self.unreadable in text else "entities" if page_keys else "irrelevant"
                payload["pages"].append({"page": i, "status": status, "entity_keys": page_keys, "reason": None})
        else:
            entity = json.loads(instruction.rsplit("\n", 1)[1])
            if entity["key"] == self.fail:
                raise ValueError("Simulated invalid entity extraction")
            payload = property_payload(entity["key"])
            payload["source_references"][0]["source_page"] = entity["pages"][0]
            payload["source_references"][0]["evidence"] = entity["address"]
        return ProviderAnalysis(payload, "test", 100, 100, Decimal("0.001"), 1, 1)


def analyze(harness, uploaded, provider, computed, **kwargs):
    return analyze_document(UUID(uploaded["report_ids"][0]), provider=provider,
                            session_factory=harness.transaction,
                            identity_resolver=harness.resolve_identity,
                            compute=lambda record, **kw: computed.append(record), **kwargs)


@pytest.mark.parametrize("pages", [["A B"], ["A", "A", "B"], ["A", "B", "A"], ["A"]])
def test_each_property_has_its_own_evidence_and_analysis(whole_pdf_harness, pages):
    h = whole_pdf_harness
    uploaded = upload(h, pages)
    provider, computed = DocumentProvider(), []
    run_id = analyze(h, uploaded, provider, computed)
    expected = len({key for page in pages for key in page.split()})
    with h.transaction() as session:
        run = session.get(dbm.DocumentAnalysisRun, run_id)
        assert run.status == "complete", run.issues
        assert [page["page"] for page in run.coverage] == list(range(1, len(pages) + 1))
        entities = session.query(dbm.ReportEntityExtraction).filter_by(run_id=run_id).all()
        assert len(entities) == expected
        assert all(entity.active for entity in entities), [(e.status, e.issues) for e in entities]
        for entity in entities:
            identity = entity.raw_json["property_identity"]
            record = load_record(session, entity.property_id)
            assert record.address.line1 == identity["address_line1"]
            facts = session.query(dbm.ExtractedFact).filter_by(property_id=entity.property_id, is_active=True).all()
            assert facts and all(fact.entity_local_id.startswith(str(entity.id)) for fact in facts)
    assert len(computed) == expected
    batch = h.client.get(f"/api/batches/{uploaded['batch_id']}").json()
    assert len(batch["results"]) == expected
    assert batch["property_count"] == expected
    assert batch["documents"][0]["page_count"] == len(pages)
    for result in batch["results"]:
        assert len(h.client.get(f"/api/properties/{result['property_id']}/reports").json()["items"]) == 1


def test_overlap_and_duplicate_upload_reuse_all_properties(whole_pdf_harness, monkeypatch):
    monkeypatch.setattr(settings, "pdf_chunk_pages", 3)
    h = whole_pdf_harness
    uploaded = upload(h, ["A", "B", "A", "B", "A"])
    provider, computed = DocumentProvider(), []
    run_id = analyze(h, uploaded, provider, computed)
    assert len(computed) == 2
    calls = len(provider.calls)
    assert analyze(h, uploaded, provider, computed) == run_id
    assert len(provider.calls) == calls
    with h.transaction() as session:
        original = session.get(dbm.Report, UUID(uploaded["report_ids"][0]))
        data = Path(original.file_path).read_bytes()
    response = h.client.post("/api/uploads", files=[("files", ("same.pdf", data, "application/pdf"))]).json()
    analyze(h, response, provider, computed)
    batch = h.client.get(f"/api/batches/{response['batch_id']}").json()
    assert len(batch["results"]) == 2
    assert len(provider.calls) == calls


def test_partial_failure_keeps_other_property_and_retry_reuses_work(whole_pdf_harness):
    h = whole_pdf_harness
    uploaded = upload(h, ["A B"])
    provider, computed = DocumentProvider(fail="B"), []
    run_id = analyze(h, uploaded, provider, computed)
    with h.transaction() as session:
        assert session.get(dbm.DocumentAnalysisRun, run_id).status == "partial"
    assert len(computed) == 1
    first_calls = list(provider.calls)
    provider.fail = None
    analyze(h, uploaded, provider, computed)
    assert len(computed) == 2
    assert provider.calls[len(first_calls):] == [key for key in first_calls if key.endswith(":B")]
    with h.transaction() as session:
        assert session.get(dbm.DocumentAnalysisRun, run_id).status == "complete"


def test_unreadable_page_never_counts_as_complete(whole_pdf_harness):
    h = whole_pdf_harness
    uploaded = upload(h, ["A", "UNREADABLE"])
    run_id = analyze(h, uploaded, DocumentProvider(unreadable="UNREADABLE"), [])
    with h.transaction() as session:
        run = session.get(dbm.DocumentAnalysisRun, run_id)
        assert run.status == "partial"
        assert any(page["status"] == "unreadable" for page in run.coverage)


def test_no_properties_and_corrupt_pdf_are_explicit(whole_pdf_harness):
    h = whole_pdf_harness
    uploaded = upload(h, ["Nothing relevant"])
    run_id = analyze(h, uploaded, DocumentProvider(), [])
    with h.transaction() as session:
        assert session.get(dbm.DocumentAnalysisRun, run_id).status == "no_properties"
    response = h.client.post("/api/uploads", files=[("files", ("bad.pdf", b"%PDF-corrupt", "application/pdf"))]).json()
    run_id = analyze(h, response, DocumentProvider(), [])
    with h.transaction() as session:
        assert session.get(dbm.DocumentAnalysisRun, run_id).status == "failed"


def test_budget_pause_makes_no_provider_request(whole_pdf_harness):
    h = whole_pdf_harness
    uploaded = upload(h, ["A B"])
    with h.transaction() as session:
        session.get(dbm.Batch, UUID(uploaded["batch_id"])).budget_limit_usd = Decimal(0)
    provider = DocumentProvider()
    run_id = analyze(h, uploaded, provider, [])
    assert provider.calls == []
    with h.transaction() as session:
        assert session.get(dbm.DocumentAnalysisRun, run_id).status == "paused_budget"


def test_permanent_provider_failure_stops_without_recursive_splitting(whole_pdf_harness):
    class RejectedProvider:
        def __init__(self):
            self.calls = 0

        def analyze_pdf(self, path, *, schema, instruction, log_context):
            self.calls += 1
            raise PermanentProviderError("request rejected")

    h = whole_pdf_harness
    uploaded = upload(h, ["A", "B"])
    provider = RejectedProvider()
    run_id = analyze(h, uploaded, provider, [])
    assert provider.calls == 1
    with h.transaction() as session:
        assert session.get(dbm.DocumentAnalysisRun, run_id).status == "failed"


def test_transient_discovery_failure_is_left_for_queue_retry(whole_pdf_harness):
    class UnavailableProvider:
        def analyze_pdf(self, path, *, schema, instruction, log_context):
            raise ProviderError("rate limited")

    h = whole_pdf_harness
    uploaded = upload(h, ["A"])
    with pytest.raises(ProviderError, match="rate limited"):
        analyze(h, uploaded, UnavailableProvider(), [])
    with h.transaction() as session:
        run = session.query(dbm.DocumentAnalysisRun).one()
        assert run.status == "analyzing"
        chunk = session.query(dbm.DocumentAnalysisChunk).one()
        assert chunk.status == "failed"


def test_transient_entity_failure_is_left_for_queue_retry(whole_pdf_harness):
    class EntityUnavailableProvider(DocumentProvider):
        def analyze_pdf(self, path, *, schema, instruction, log_context):
            if "entities" not in schema["properties"]:
                raise ProviderError("rate limited")
            return super().analyze_pdf(
                path, schema=schema, instruction=instruction, log_context=log_context
            )

    h = whole_pdf_harness
    uploaded = upload(h, ["A"])
    with pytest.raises(ProviderError, match="rate limited"):
        analyze(h, uploaded, EntityUnavailableProvider(), [])
    with h.transaction() as session:
        run = session.query(dbm.DocumentAnalysisRun).one()
        assert run.status == "analyzing"
        chunks = session.query(dbm.DocumentAnalysisChunk).all()
        assert [chunk.status for chunk in chunks] == ["complete", "failed"]


def test_exhausted_provider_retries_mark_analysis_failed(whole_pdf_harness):
    h = whole_pdf_harness
    uploaded = upload(h, ["A"])
    with pytest.raises(ProviderError):
        analyze(h, uploaded, type("UnavailableProvider", (), {
            "analyze_pdf": lambda *args, **kwargs: (_ for _ in ()).throw(
                ProviderError("provider failed after 3 attempts (429): rate limit reached")
            ),
        })(), [])
    with h.transaction() as session:
        run = session.query(dbm.DocumentAnalysisRun).one()
        _mark_terminal_failure(session, {
            "id": UUID("00000000-0000-0000-0000-000000000001"),
            "name": "analyze_report",
            "payload": {
                "report_id": uploaded["report_ids"][0],
                "run_id": str(run.id),
            },
        }, ProviderError("rate limit reached"))
    with h.transaction() as session:
        run = session.query(dbm.DocumentAnalysisRun).one()
        assert run.status == "failed"
        assert run.issues[-1]["code"] == "provider_retry_exhausted"
        report = session.get(dbm.Report, UUID(uploaded["report_ids"][0]))
        batch = session.get(dbm.Batch, UUID(uploaded["batch_id"]))
        assert report.status == "failed"
        assert batch.status == "needs_review"
        assert batch.failed_count == 1


def test_discovery_requires_all_pages_and_valid_entity_links():
    with pytest.raises(ValueError, match="every page"):
        _validate_discovery({"pages": [], "entities": []}, 2)
    with pytest.raises(ValueError, match="unknown entity"):
        _validate_discovery({"pages": [{"page": 1, "status": "entities", "entity_keys": ["missing"], "reason": None}], "entities": []}, 1)
    assert Discovery.model_json_schema()["additionalProperties"] is False


def test_merge_preserves_missing_details_and_deduplicates_debt():
    old = property_payload("A")
    new = copy.deepcopy(old)
    new["tax"]["annual_taxes"] = None
    new["loans"][0]["source_page"] = 2
    source, _ = merge_sources([new, old])
    assert source["tax"]["annual_taxes"] == old["tax"]["annual_taxes"]
    assert len(source["loans"]) == 1


def test_reanalysis_preserves_old_results_until_success(whole_pdf_harness):
    h = whole_pdf_harness
    uploaded = upload(h, ["A B"])
    old_run = analyze(h, uploaded, DocumentProvider(), [])
    response = h.client.post(f"/api/reports/{uploaded['report_ids'][0]}/reanalyze", json={})
    assert response.status_code == 200
    run_id = UUID(response.json()["run_id"])
    assert run_id != old_run
    analyze(h, uploaded, DocumentProvider(fail="B"), [], run_id=run_id)
    batch = h.client.get(f"/api/batches/{uploaded['batch_id']}").json()
    assert batch["property_count"] == 2
    with h.transaction() as session:
        active = session.query(dbm.ReportEntityExtraction).filter_by(active=True).all()
        assert len(active) == 2
        assert {entity.run_id for entity in active} == {old_run, run_id}


def test_strict_identity_requires_locality_and_never_fuzzy_merges():
    with pytest.raises(ValueError, match="ZIP or city/state"):
        resolve_identity(None, None, "100 Main St")
