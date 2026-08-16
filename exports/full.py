"""Full data export (spec §20): properties.csv, liens.csv, mortgages.csv,
scores.csv, facts.jsonl, plus the documents folder. Streams row-by-row so the
export never materializes the whole dataset in memory."""
import csv
import json
import shutil
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from common.serializers import json_safe

CSV_TABLES = ("properties", "liens", "mortgages", "scores")
FACTS_TABLE = "extracted_facts"


def _export_csv(connection: Connection, table: str, path: Path) -> None:
    result = connection.execute(text(f"SELECT * FROM {table}"))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(result.keys())
        for row in result:
            writer.writerow([json_safe(value) for value in row])


def _export_facts(connection: Connection, path: Path) -> None:
    result = connection.execute(text(f"SELECT * FROM {FACTS_TABLE}"))
    columns = list(result.keys())
    with path.open("w", encoding="utf-8") as handle:
        for row in result:
            record = {column: json_safe(value) for column, value in zip(columns, row)}
            handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")


def full_export(connection: Connection, destination: Path,
                documents_root: Path | None = None) -> list[Path]:
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
    if documents_root is not None and Path(documents_root).is_dir():
        target = destination / "documents"
        shutil.copytree(documents_root, target, dirs_exist_ok=True)
        written.append(target)
    return written
