from pathlib import Path

from classification import classify, section_pages
from common.db import db_session
from db.models import ExtractionUnit, Report


def ingest_document(payload: dict) -> None:
    report_id = payload["report_id"]
    with db_session() as session:
        report = session.get(Report, report_id)
        if report is None:
            raise ValueError("report not found")
        pdf = Path(report.file_path)
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError("PyMuPDF is required for document ingestion") from exc
        document = fitz.open(pdf)
        pages = [page.get_text() for page in document]
        report.page_count = len(pages)
        report.is_scanned = sum(len(page.strip()) < 100 for page in pages) > len(pages) * .4
        classification = classify(pages[0] if pages else "", pdf.name)
        report.report_type = classification.report_type
        report.classification_confidence = classification.confidence
        page_dir = pdf.parent / "pages"
        page_dir.mkdir(parents=True, exist_ok=True)
        for number, page_text in enumerate(pages, 1):
            (page_dir / f"{number}.txt").write_text(page_text)
        for unit in section_pages(pages):
            unit_path = page_dir / f"unit-{unit.page_start}-{unit.page_end}.txt"
            unit_path.write_text(unit.text)
            session.add(ExtractionUnit(report_id=report.id, unit_type=unit.unit_type, page_start=unit.page_start, page_end=unit.page_end, text_path=str(unit_path), token_estimate=unit.token_estimate))
        report.status = "text_extracted"
