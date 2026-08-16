"""Offline tests for ingestion/: dedupe, failure codes, OCR fallback, identity wiring.

Uses SQLite in-memory (Report/Batch tables only — no Postgres) and generates
PDF fixtures at runtime with PyMuPDF. No network, no live services.
"""

import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from common.errors import AcqError, ErrorCode
from db.models import Batch, Report
from ingestion import get_page_text, ingest_paste, register_pdf, scan_inbox, worker
from ingestion.ocr import NullBackend, OcrResult
from ingestion.worker import ingest_document, is_scanned, run_ingest

PAGE_TEXT = "123 Main Street parcel report. " * 30  # ~900 chars, clearly digital


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Report.__table__.create(engine)
    Batch.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session


def make_digital_pdf(path: Path, pages: tuple[str, ...] = (PAGE_TEXT, PAGE_TEXT)) -> Path:
    import fitz

    document = fitz.open()
    for text in pages:
        page = document.new_page()
        page.insert_textbox(fitz.Rect(36, 36, 560, 760), text)
    document.save(path)
    document.close()
    return path


def make_scanned_pdf(path: Path, text: str = PAGE_TEXT) -> Path:
    """A PDF whose page is a rendered image, so text extraction yields nothing."""
    import fitz

    source = fitz.open()
    page = source.new_page()
    page.insert_textbox(fitz.Rect(36, 36, 560, 760), text)
    pixmap = page.get_pixmap(dpi=150)
    document = fitz.open()
    image_page = document.new_page(width=page.rect.width, height=page.rect.height)
    image_page.insert_image(image_page.rect, pixmap=pixmap)
    document.save(path)
    document.close()
    source.close()
    return path


def make_encrypted_pdf(path: Path) -> Path:
    import fitz

    document = fitz.open()
    document.new_page().insert_textbox(fitz.Rect(36, 36, 560, 760), PAGE_TEXT)
    document.save(
        path,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        user_pw="secret",
        owner_pw="owner",
    )
    document.close()
    return path


def add_report(session, pdf_path: Path, **overrides) -> Report:
    report = Report(
        id=uuid4(),
        file_path=str(pdf_path),
        sha256=f"{uuid4().hex}{uuid4().hex}"[:64],
        status="uploaded",
        **overrides,
    )
    session.add(report)
    session.flush()
    return report


def run_job(monkeypatch, session, payload: dict) -> None:
    @contextmanager
    def fake_db_session():
        yield session

    monkeypatch.setattr(worker, "db_session", fake_db_session)
    ingest_document(payload)


# --- sha256 dedupe pre-check -------------------------------------------------


def test_register_pdf_dedupe_returns_existing(session, tmp_path):
    source = make_digital_pdf(tmp_path / "a.pdf")
    first, created = register_pdf(session, source, tmp_path / "docs")
    assert created
    again, created = register_pdf(session, source, tmp_path / "docs")
    assert not created
    assert again.id == first.id
    assert session.scalar(select(Report).where(Report.sha256 == first.sha256)).id == first.id
    rows = session.scalars(select(Report)).all()
    assert len(rows) == 1  # no second row, no IntegrityError
    assert len(list((tmp_path / "docs").iterdir())) == 1  # zero new documents on disk


def test_failed_duplicate_reupload_restores_storage_and_requeues(session, tmp_path):
    source = make_digital_pdf(tmp_path / "a.pdf")
    first, created = register_pdf(session, source, tmp_path / "docs", batch_id=uuid4())
    assert created
    Path(first.file_path).unlink()
    first.status = "failed"
    first.failure_reason = ErrorCode.EXTRACTION_FAILED.value
    replacement_batch = uuid4()

    again, created = register_pdf(
        session, source, tmp_path / "docs", batch_id=replacement_batch)

    assert created
    assert again.id == first.id
    assert again.batch_id == replacement_batch
    assert again.status == "uploaded"
    assert again.failure_reason is None
    assert Path(again.file_path).exists()


def test_register_pdf_rejects_non_pdf(session, tmp_path):
    source = tmp_path / "junk.pdf"
    source.write_bytes(b"not a pdf at all")
    with pytest.raises(AcqError) as excinfo:
        register_pdf(session, source, tmp_path / "docs")
    assert excinfo.value.code == ErrorCode.NOT_PDF
    assert session.scalars(select(Report)).all() == []


# --- scanned classification ---------------------------------------------------


def test_is_scanned_thresholds():
    assert not is_scanned([PAGE_TEXT, PAGE_TEXT])
    assert is_scanned(["", "", ""])  # all pages empty
    assert is_scanned(["x" * 50, "y" * 60])  # median < 100
    assert not is_scanned([])


# --- digital ingest -----------------------------------------------------------


def test_ingest_digital_pdf(session, tmp_path, monkeypatch):
    pdf = make_digital_pdf(tmp_path / "doc.pdf")
    report = add_report(session, pdf)
    run_job(monkeypatch, session, {"report_id": str(report.id)})
    assert report.status == "text_extracted"
    assert report.page_count == 2
    assert report.is_scanned is False
    assert report.ocr_applied is False
    text = get_page_text(report.file_path, 1)
    assert "123 Main Street" in text


def test_ingest_accepts_json_string_payload(session, tmp_path, monkeypatch):
    pdf = make_digital_pdf(tmp_path / "doc.pdf")
    report = add_report(session, pdf)
    run_job(monkeypatch, session, json.dumps({"report_id": str(report.id)}))
    assert report.status == "text_extracted"


def test_ingest_missing_report_raises(session, monkeypatch):
    with pytest.raises(ValueError, match="report not found"):
        run_job(monkeypatch, session, {"report_id": str(uuid4())})


def test_pymupdf_document_is_closed(session, tmp_path, monkeypatch):
    import fitz

    pdf = make_digital_pdf(tmp_path / "doc.pdf")
    report = add_report(session, pdf)
    closed = []
    real_open = fitz.open

    class SpyDoc:
        def __init__(self, document):
            self._document = document

        def __getattr__(self, name):
            return getattr(self._document, name)

        def __iter__(self):
            return iter(self._document)

        def __len__(self):
            return len(self._document)

        def close(self):
            closed.append(True)
            self._document.close()

    monkeypatch.setattr(fitz, "open", lambda *args, **kwargs: SpyDoc(real_open(*args, **kwargs)))
    run_ingest(session, report)
    assert closed == [True]


# --- failure codes ------------------------------------------------------------


def test_encrypted_pdf_fails_with_code(session, tmp_path, monkeypatch):
    pdf = make_encrypted_pdf(tmp_path / "locked.pdf")
    report = add_report(session, pdf)
    report.failure_reason = None  # stand-in for the concurrently-added column
    run_job(monkeypatch, session, {"report_id": str(report.id)})
    assert report.status == "failed"
    assert report.failure_reason == ErrorCode.ENCRYPTED.value


def test_corrupt_pdf_fails_without_raising(session, tmp_path, monkeypatch):
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"%PDF-1.7\n" + b"garbage" * 500)
    report = add_report(session, pdf)
    run_job(monkeypatch, session, {"report_id": str(report.id)})  # must not raise
    assert report.status == "failed"
    assert report.failure_reason == ErrorCode.CORRUPT.value


def test_missing_stored_pdf_raises_instead_of_being_mislabeled_corrupt(session, tmp_path):
    missing = tmp_path / "missing.pdf"
    report = add_report(session, missing)

    with pytest.raises(AcqError) as excinfo:
        run_ingest(session, report)

    assert excinfo.value.code == ErrorCode.EXTRACTION_FAILED
    assert excinfo.value.details["document_path"] == str(missing)
    assert report.status == "uploaded"


# --- OCR path -----------------------------------------------------------------


def test_scanned_pdf_without_ocr_backend_goes_ocr_pending(session, tmp_path):
    pdf = make_scanned_pdf(tmp_path / "scan.pdf")
    report = add_report(session, pdf)
    run_ingest(session, report, ocr_backend=NullBackend())
    assert report.is_scanned is True
    assert report.status == "ocr_pending"
    assert report.ocr_applied is False
    assert (pdf.parent / "pages" / "1.txt").exists()  # whatever text existed is still written


class FakePartialBackend:
    name = "fake"

    def available(self) -> bool:
        return True

    def ocr_pdf(self, pdf_path, work_dir, timeout=600):
        return OcrResult(["ocr text one", "ocr text two"], None, partial=True)


def test_scanned_pdf_partial_ocr_marks_reason_but_completes(session, tmp_path):
    pdf = make_scanned_pdf(tmp_path / "scan.pdf")
    report = add_report(session, pdf)
    report.failure_reason = None
    run_ingest(session, report, ocr_backend=FakePartialBackend())
    assert report.status == "text_extracted"
    assert report.ocr_applied is True
    assert report.failure_reason == ErrorCode.PARTIAL_OCR.value
    assert get_page_text(report.file_path, 1) == "ocr text one"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_scanned_pdf_ocr_with_tesseract(session, tmp_path):
    pdf = make_scanned_pdf(tmp_path / "scan.pdf")
    report = add_report(session, pdf)
    run_ingest(session, report)  # default backend: ocrmypdf if present, else tesseract
    assert report.ocr_applied is True
    assert report.status == "text_extracted"
    assert "Main Street" in get_page_text(report.file_path, 1)


# --- identity wiring ----------------------------------------------------------


def fake_attach(calls, property_id):
    def attach(session, report, address, apn=None, fips=None, zip5=None):
        calls.append({"address": address, "apn": apn, "zip5": zip5})
        report.property_id = property_id

    return attach


def test_identity_attached_when_address_hint_present(session, tmp_path, monkeypatch):
    pdf = make_digital_pdf(tmp_path / "doc.pdf")
    report = add_report(session, pdf)
    property_id = uuid4()
    calls = []
    monkeypatch.setattr(worker, "attach_report", fake_attach(calls, property_id))
    run_job(
        monkeypatch,
        session,
        {"report_id": str(report.id), "address": "123 Main St", "zip5": "90001"},
    )
    assert calls == [{"address": "123 Main St", "apn": None, "zip5": "90001"}]
    assert report.property_id == property_id
    assert report.status == "text_extracted"


def test_identity_skipped_without_address(session, tmp_path, monkeypatch):
    pdf = make_digital_pdf(tmp_path / "doc.pdf")
    report = add_report(session, pdf)
    calls = []
    monkeypatch.setattr(worker, "attach_report", fake_attach(calls, uuid4()))
    run_job(monkeypatch, session, {"report_id": str(report.id)})
    assert calls == []
    assert report.property_id is None


def test_identity_conflict_does_not_fail_ingest(session, tmp_path, monkeypatch):
    pdf = make_digital_pdf(tmp_path / "doc.pdf")
    report = add_report(session, pdf)

    def conflict(session, report, address, apn=None, fips=None, zip5=None):
        raise ValueError("identity_conflict")

    monkeypatch.setattr(worker, "attach_report", conflict)
    run_job(monkeypatch, session, {"report_id": str(report.id), "address": "123 Main St"})
    assert report.status == "text_extracted"  # ingestion succeeds; property left unattached
    assert report.property_id is None


# --- paste ingestion and watched folder ---------------------------------------


def test_ingest_paste_creates_single_page_pseudo_report(session, tmp_path):
    text = "Seller email: 45 Oak Rd, asking $210k, probate."
    report, created = ingest_paste(session, text, tmp_path / "docs")
    assert created
    assert report.vendor == "pasted"
    assert report.page_count == 1
    assert report.status == "text_extracted"
    assert get_page_text(report.file_path, 1) == text
    again, created = ingest_paste(session, text, tmp_path / "docs")
    assert not created
    assert again.id == report.id


def test_scan_inbox_ingests_pdfs_and_quarantines_junk(session, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    make_digital_pdf(inbox / "good.pdf")
    (inbox / "bad.pdf").write_bytes(b"junk")
    reports = scan_inbox(session, inbox, tmp_path / "docs")
    assert len(reports) == 1
    assert (inbox / "processed" / "good.pdf").exists()
    assert (inbox / "failed" / "bad.pdf").exists()
    assert sorted(path.name for path in inbox.glob("*.pdf")) == []
