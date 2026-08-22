"""Full data export (spec §20): properties.csv, liens.csv, mortgages.csv,
scores.csv, facts.jsonl, plus the documents folder. Streams row-by-row so the
export never materializes the whole dataset in memory."""
import csv
import json
import shutil
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from common.serializers import json_safe

CSV_TABLES = ("properties", "liens", "mortgages", "scores")
FACTS_TABLE = "extracted_facts"


def _has_column(connection: Connection, table: str, column: str) -> bool:
    return column in set(connection.execute(text(f"SELECT * FROM {table} LIMIT 0")).keys())


def _export_csv(connection: Connection, table: str, path: Path) -> None:
    if not _has_column(connection, "properties", "archived_at"):
        sql = f"SELECT * FROM {table}"
    elif table == "properties":
        sql = "SELECT * FROM properties WHERE archived_at IS NULL"
    else:
        sql = (f"SELECT item.* FROM {table} item JOIN properties p ON p.id = item.property_id "
               "WHERE p.archived_at IS NULL")
    result = connection.execute(text(sql))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(result.keys())
        for row in result:
            writer.writerow([json_safe(value) for value in row])


def _export_facts(connection: Connection, path: Path) -> None:
    sql = f"SELECT * FROM {FACTS_TABLE}"
    if _has_column(connection, "properties", "archived_at"):
        sql = (f"SELECT fact.* FROM {FACTS_TABLE} fact JOIN properties p ON p.id = fact.property_id "
               "WHERE p.archived_at IS NULL")
    result = connection.execute(text(sql))
    columns = list(result.keys())
    with path.open("w", encoding="utf-8") as handle:
        for row in result:
            record = {column: json_safe(value) for column, value in zip(columns, row)}
            handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")


def _copy_active_documents(connection: Connection, source_root: Path, target: Path) -> bool:
    """Copy only report directories attached to active properties on current schemas."""
    if not inspect(connection).has_table("reports") or not _has_column(
        connection, "properties", "archived_at",
    ):
        shutil.copytree(source_root, target, dirs_exist_ok=True)
        return True
    references = connection.execute(text("""
        SELECT report.file_path, report.ocr_path
        FROM reports report
        JOIN properties property ON property.id = report.property_id
        WHERE property.archived_at IS NULL
    """)).all()
    copied = False
    root = source_root.resolve()
    for file_path, ocr_path in references:
        for reference in (file_path, ocr_path):
            if not reference or str(reference).startswith("s3://"):
                continue
            source = Path(str(reference)).resolve()
            if not source.is_file() or not source.is_relative_to(root):
                continue
            relative = source.relative_to(root)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied = True
    return copied


def full_export(connection: Connection, destination: Path,
                documents_root: Path | None = None,
                include_owner_contacts: bool = False) -> list[Path]:
    """Run the full export into `destination`; returns the paths written."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for table in CSV_TABLES:
        path = destination / f"{table}.csv"
        _export_csv(connection, table, path)
        written.append(path)
    facts_path = destination / "facts.jsonl"
    _export_facts(connection, facts_path)
    written.append(facts_path)
    if include_owner_contacts:
        contacts_path = destination / "owner_contacts.csv"
        result = connection.execute(text("""
            SELECT DISTINCT contact.* FROM owner_contacts contact
            JOIN property_owners po ON po.owner_id = contact.owner_id
            JOIN properties p ON p.id = po.property_id
            WHERE p.archived_at IS NULL
        """))
        with contacts_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(result.keys())
            for row in result:
                writer.writerow([json_safe(value) for value in row])
        written.append(contacts_path)
    if documents_root is not None and Path(documents_root).is_dir():
        target = destination / "documents"
        if _copy_active_documents(connection, Path(documents_root), target):
            written.append(target)
    return written
