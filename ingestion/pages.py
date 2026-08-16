from pathlib import Path


def get_page_text(report_path: str | Path, page: int) -> str:
    return (Path(report_path).parent / "pages" / f"{page}.txt").read_text()


def get_all_page_text(report_path: str | Path) -> list[str]:
    directory = Path(report_path).parent / "pages"
    return [path.read_text() for path in sorted(directory.glob("[0-9]*.txt"), key=lambda item: int(item.stem))]
