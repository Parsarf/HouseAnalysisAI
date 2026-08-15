import json
from pathlib import Path

from contracts.schema import export_schemas


def main() -> None:
    out = Path("contracts/generated")
    out.mkdir(parents=True, exist_ok=True)
    schemas = export_schemas()
    (out / "schemas.json").write_text(json.dumps(schemas, indent=2, default=str) + "\n")
    lines = ["// Generated from contracts.schema; do not edit.", ""]
    for name, schema in schemas.items():
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        lines.append(f"export interface {name} {{")
        for field, definition in props.items():
            typ = "string" if definition.get("type") in ("string", "number", "integer") else "unknown"
            optional = "" if field in required else "?"
            lines.append(f"  {field}{optional}: {typ};")
        lines.extend(["}", ""])
    (out / "index.ts").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
