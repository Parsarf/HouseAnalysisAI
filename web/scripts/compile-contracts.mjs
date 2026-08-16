// Compiles the contract JSON Schemas (contracts/schema.py export) to TypeScript
// using json-schema-to-typescript. Invoked by scripts/generate_types.py.
//
// All models are compiled in a single pass with every pydantic $def hoisted to a
// shared $defs map, so shared sub-schemas (TrackedValue, enums, ...) emit exactly
// one declaration instead of colliding duplicates.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { compile } from "json-schema-to-typescript";

const [schemasPath, outPath] = process.argv.slice(2);
if (!schemasPath || !outPath) {
  console.error("usage: node compile-contracts.mjs <schemas.json> <out.ts>");
  process.exit(2);
}

const schemas = JSON.parse(readFileSync(schemasPath, "utf8"));
const defs = {};
const properties = {};
for (const [name, schema] of Object.entries(schemas)) {
  const { $defs: localDefs, ...rest } = schema;
  for (const [defName, defSchema] of Object.entries(localDefs ?? {})) {
    const existing = defs[defName];
    if (existing !== undefined && JSON.stringify(existing) !== JSON.stringify(defSchema)) {
      console.error(`conflicting $defs for ${defName}`);
      process.exit(1);
    }
    defs[defName] = defSchema;
  }
  defs[name] = rest;
  properties[name] = { $ref: `#/$defs/${name}` };
}
const combined = { title: "Contracts", type: "object", additionalProperties: false, properties, $defs: defs };

const compiled = await compile(combined, "Contracts", {
  bannerComment: "",
  format: false,
  strictIndexSignatures: true,
});
// Drop the wrapper interface; only the per-model declarations are wanted.
const body = compiled.replace(/export interface Contracts \{[^]*?\n\}\n/, "");
const header = [
  "// Generated from contracts.schema via json-schema-to-typescript; do not edit.",
  "// Money values are decimal strings on the wire (never floats).",
  "",
].join("\n");
mkdirSync(dirname(outPath), { recursive: true });
writeFileSync(outPath, header + body.trim() + "\n");
console.log(`wrote ${outPath} (${Object.keys(schemas).length} models)`);
