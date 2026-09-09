"""Explicit, resumable backfill. Running the module without --execute is read-only."""
import argparse
import json
from uuid import uuid4

from sqlalchemy import func

from common.settings import settings
from db import models as dbm

from .document_schemas import VERSION
from .read_model import source_report_id


def schedule(session, queue, report, *, reanalyze=False, backfill=False, budget_batch_id=None):
    original = session.get(dbm.Report, source_report_id(session, report))
    session.query(dbm.Report).filter_by(id=original.id).with_for_update().one()
    latest = session.query(dbm.DocumentAnalysisRun).filter_by(report_id=original.id, version=VERSION).order_by(
        dbm.DocumentAnalysisRun.generation.desc(),
    ).first()
    if latest is None or (reanalyze and latest.status not in {"queued", "analyzing", "computing"}):
        generation = session.query(func.max(dbm.DocumentAnalysisRun.generation)).filter_by(report_id=original.id).scalar() or 0
        latest = dbm.DocumentAnalysisRun(id=uuid4(), report_id=original.id, generation=generation + 1,
                                         version=VERSION, status="queued", budget_batch_id=budget_batch_id)
        session.add(latest)
        session.flush()
    if latest.status not in {"complete", "no_properties"}:
        queue.enqueue(session, "analyze_report", json.dumps({
            "report_id": str(original.id), "run_id": str(latest.id), "backfill": backfill,
        }), f"document:{latest.id}")
        report.status = "analyzing"
    return latest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    from common.db import db_session
    from jobs.postgres import PostgresJobQueue
    with db_session() as session:
        reports = session.query(dbm.Report).filter(dbm.Report.duplicate_of.is_(None),
                                                 dbm.Report.vendor.is_distinct_from("pasted")).all()
        pending = [report for report in reports if not session.query(dbm.DocumentAnalysisRun).filter_by(
            report_id=report.id, version=VERSION,
        ).filter(dbm.DocumentAnalysisRun.status.in_(["complete", "no_properties"])).first()]
        print(f"{len(pending)} unique PDFs need {VERSION} analysis; execute={args.execute}")
        if not args.execute:
            return
        batch = session.query(dbm.Batch).filter_by(tag=f"backfill:{VERSION}").first()
        if batch is None:
            batch = dbm.Batch(id=uuid4(), name="PDF property backfill", tag=f"backfill:{VERSION}",
                              budget_limit_usd=settings.pdf_backfill_budget_usd)
            session.add(batch)
            session.flush()
        for report in pending:
            schedule(session, PostgresJobQueue(), report, backfill=True, budget_batch_id=batch.id)
        print(f"Queued {len(pending)} PDFs; shared budget batch {batch.id}")


if __name__ == "__main__":
    main()
