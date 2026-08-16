from pathlib import Path

from common.storage import get_document_storage


def get_page_text(report_path: str | Path, page: int) -> str:
    reference = str(report_path)
    storage = get_document_storage()
    return storage.read_text(storage.child(reference, f"pages/{page}.txt"))


def get_all_page_text(report_path: str | Path) -> list[str]:
    storage = get_document_storage()
    references = storage.list_children(str(report_path), "pages")
    return [storage.read_text(reference) for reference in references]
