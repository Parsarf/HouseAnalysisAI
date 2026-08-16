"""Regenerate the TypeScript contract types in web/src/types/ from contracts.schema.

Dumps the pydantic JSON Schemas to web/src/types/schemas.json, then compiles them
with json-schema-to-typescript (a web/ devDependency) so the generated types are
real interfaces instead of ``unknown``. Requires ``npm ci`` to have been run in web/.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from contracts.schema import export_schemas

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
TYPES_DIR = WEB / "src" / "types"


def main() -> None:
    node = shutil.which("node")
    if node is None:
        sys.exit("node is required to generate types; install Node.js")
    if not (WEB / "node_modules" / "json-schema-to-typescript").is_dir():
        sys.exit("json-schema-to-typescript is missing; run `npm ci` in web/ first")

    schemas = export_schemas()
    TYPES_DIR.mkdir(parents=True, exist_ok=True)
    schemas_path = TYPES_DIR / "schemas.json"
    schemas_path.write_text(json.dumps(schemas, indent=2, default=str) + "\n")

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(schemas, tmp, default=str)
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(
            [node, str(WEB / "scripts" / "compile-contracts.mjs"), str(tmp_path), str(TYPES_DIR / "index.ts")],
            check=True,
            cwd=WEB,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
