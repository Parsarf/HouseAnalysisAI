import csv
from collections.abc import Iterable
from io import StringIO


def stream_properties(rows: Iterable[dict], columns: list[str]) -> Iterable[str]:
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    yield output.getvalue()
    for row in rows:
        for key, value in list(row.items()):
            if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
                row[key] = "'" + value
        output.seek(0)
        output.truncate(0)
        writer.writerow(row)
        yield output.getvalue()
