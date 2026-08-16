"""Offline test for scripts/generate_types.py.

Requires node and web/node_modules (json-schema-to-typescript); skipped otherwise.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (WEB / "node_modules" / "json-schema-to-typescript").is_dir(),
    reason="requires node and web/node_modules (run `npm ci` in web/)",
)


def test_generate_types_produces_real_types():
    result = subprocess.run(
        [sys.executable, "scripts/generate_types.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    out = (WEB / "src" / "types" / "index.ts").read_text()
    for name in ("TrackedValue", "NormalizedProperty", "UnderwritingResult", "OfferGrid", "ScoreSet"):
        assert f"export interface {name} " in out
    # No model field is left as a bare unknown; dict[str, Any] index signatures are allowed.
    assert not re.search(r"^\w+\?: unknown$", out, flags=re.MULTILINE)
    assert not re.search(r"^\w+: unknown$", out, flags=re.MULTILINE)
    # StrEnums become string-union types.
    assert '"report" | "derived" | "human" | "api" | "pasted"' in out
    # No duplicate declarations (single combined compile pass).
    names = re.findall(r"^export (?:interface|type) (\w+)", out, flags=re.MULTILINE)
    assert len(names) == len(set(names))

    schemas = json.loads((WEB / "src" / "types" / "schemas.json").read_text())
    assert set(schemas) >= {"TrackedValue", "NormalizedProperty", "ScoreSet"}
