"""Per-unit JSON schemas (spec §18) and model routing (spec §5.2).

One structured-output schema per extraction unit type. All schemas share the
same shape: a single top-level array of typed objects, each object carrying
``page_number``, ``snippet``, ``extraction_confidence`` and ``null_reason``.
Numeric leaves follow the ``<name>_raw`` / ``<name>_parsed`` pair convention so
the model never does arithmetic; ``flatten.py`` turns these objects into
``ExtractedFactDraft`` rows.
"""

# Shared leaf fragments -------------------------------------------------------

_NULL_REASON = {"enum": ["not_present", "illegible", "redacted", "conflicting_in_source", None]}

_PROVENANCE = {
    "page_number": {"type": "integer", "minimum": 1},
    "snippet": {"type": "string", "maxLength": 200},
    "extraction_confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "null_reason": _NULL_REASON,
}
_REQUIRED_PROVENANCE = ["page_number", "snippet", "extraction_confidence"]


def _money(name: str) -> dict[str, dict]:
    return {f"{name}_raw": {"type": ["string", "null"]}, f"{name}_parsed": {"type": ["number", "null"]}}


def _number(name: str) -> dict[str, dict]:
    return _money(name)


def _text(name: str) -> dict[str, dict]:
    return {name: {"type": ["string", "null"]}}


def _date(name: str) -> dict[str, dict]:
    return {name: {"type": ["string", "null"], "format": "date"}}


def _bool(name: str) -> dict[str, dict]:
    return {name: {"type": ["boolean", "null"]}}


def _enum(name: str, values: list[str]) -> dict[str, dict]:
    return {name: {"enum": values + [None]}}


def _unit(key: str, properties: dict, required: list[str]) -> dict:
    item = {
        "type": "object",
        "additionalProperties": False,
        "required": required + _REQUIRED_PROVENANCE,
        "properties": {**properties, **_PROVENANCE},
    }
    return {
        "type": "object",
        "required": [key],
        "additionalProperties": False,
        "properties": {key: {"type": "array", "items": item}},
    }


# The 12 per-unit schemas (spec §18) ------------------------------------------

LIENS = _unit(
    "liens",
    {
        **_enum("lien_type", ["federal_tax", "state_tax", "judgment", "hoa", "mechanics", "property_tax", "child_support", "ucc", "other", "unknown"]),
        **_text("creditor_raw"),
        **_text("debtor_name_raw"),
        **_money("amount"),
        **_date("recording_date"),
        **_text("recording_doc_number"),
        **_enum("status", ["active", "released", "satisfied", "expired", "unknown"]),
        **_enum("attachment_basis", ["recorded_against_property", "owner_named_only", "unknown"]),
        **_text("attachment_evidence"),
        "attachment_confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
    },
    ["lien_type", "attachment_basis"],
)

PROPERTY_CORE = _unit(
    "property",
    {
        **_text("address_line1"), **_text("unit"), **_text("city"), **_text("state"),
        **_text("zip5"), **_text("county"), **_text("apn"),
        **_enum("property_type", ["sfr", "condo", "townhome", "multi", "land", "other", "unknown"]),
        **_number("beds"), **_number("baths"), **_number("sqft"), **_number("lot_sqft"),
        **_number("year_built"), **_number("units"),
    },
    [],
)

OWNERSHIP = _unit(
    "owners",
    {
        **_text("owner_name"), **_text("mailing_address"),
        **_enum("entity_type", ["individual", "trust", "llc", "corporation", "estate", "other", "unknown"]),
        **_bool("is_owner_occupied"), **_bool("is_absentee"),
        **_date("ownership_start_date"), **_money("purchase_price"),
    },
    [],
)

MORTGAGES = _unit(
    "mortgages",
    {
        **_enum("position", ["first", "second", "third", "heloc", "other", "unknown"]),
        **_text("lender_raw"),
        **_money("original_amount"), **_money("balance"),
        **_number("rate"), **_number("term_months"),
        **_date("origination_date"), **_date("recording_date"), **_date("balance_as_of"),
        **_text("recording_doc_number"), **_bool("is_open"),
    },
    ["position"],
)

FORECLOSURE = _unit(
    "foreclosure_events",
    {
        **_enum("stage", ["nod", "nts", "sale_scheduled", "postponed", "rescinded", "sold", "cancelled", "unknown"]),
        **_date("nod_date"), **_date("nts_date"), **_date("original_sale_date"), **_date("current_sale_date"),
        **_money("published_bid"), **_money("default_amount"), **_date("default_as_of"),
        **_text("trustee"), **_text("trustee_sale_number"),
        **_number("postponement_count"), **_number("rescission_count"),
        **_bool("is_active"),
    },
    ["stage"],
)

BANKRUPTCY = _unit(
    "bankruptcies",
    {
        **_enum("chapter", ["7", "11", "12", "13", "unknown"]),
        **_enum("status", ["filed", "active", "discharged", "dismissed", "unknown"]),
        **_date("filing_date"), **_date("discharge_date"),
        **_text("case_number"), **_text("court"), **_text("debtor_name_raw"),
    },
    ["chapter", "status"],
)

VALUATION = _unit(
    "valuations",
    {
        **_enum("valuation_type", ["avm", "appraisal", "tax_assessment", "list_price", "sale_price", "other", "unknown"]),
        **_money("value"), **_money("value_low"), **_money("value_high"),
        **_date("as_of_date"),
        "reported_confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
    },
    ["valuation_type"],
)

COMPARABLES = _unit(
    "comparables",
    {
        **_text("address"), **_date("sale_date"), **_money("price"),
        **_number("sqft"), **_number("beds"), **_number("baths"), **_number("distance_miles"),
    },
    [],
)

LISTINGS = _unit(
    "listings",
    {
        **_date("list_date"), **_date("delist_date"), **_money("price"),
        **_enum("status", ["active", "pending", "sold", "withdrawn", "expired", "cancelled", "unknown"]),
        **_number("dom"), **_text("mls_number"),
    },
    ["status"],
)

TAX = _unit(
    "tax_records",
    {
        **_number("tax_year"), **_money("annual_taxes"), **_money("assessed_value"),
        **_money("delinquent_amount"), **_number("delinquent_years"),
    },
    [],
)

RENTAL = _unit(
    "rentals",
    {**_money("rent_estimate"), **_text("source"), **_date("as_of_date")},
    [],
)

CONDITION_SIGNALS = _unit(
    "condition_signals",
    {**_enum("condition", ["pristine", "cosmetic", "moderate", "heavy", "gut"]), **_text("evidence")},
    ["condition"],
)

UNIT_SCHEMAS: dict[str, dict] = {
    "property_core": PROPERTY_CORE,
    "ownership": OWNERSHIP,
    "mortgages": MORTGAGES,
    "foreclosure": FORECLOSURE,
    "bankruptcy": BANKRUPTCY,
    "valuation": VALUATION,
    "comparables": COMPARABLES,
    "listings": LISTINGS,
    "tax": TAX,
    "rental": RENTAL,
    "condition_signals": CONDITION_SIGNALS,
    "liens": LIENS,
}

# Aliases from classification's unit types (classification/sectioning.py) to schema names.
UNIT_TYPE_ALIASES = {
    "mortgage": "mortgages",
    "lien": "liens",
    "owner_report": "ownership",
    "combined": "property_core",
}


def canonical_unit_type(unit_type: str) -> str:
    return UNIT_TYPE_ALIASES.get(unit_type, unit_type)


def schema_for(unit_type: str) -> dict:
    return UNIT_SCHEMAS[canonical_unit_type(unit_type)]


def top_level_key(unit_type: str) -> str:
    schema = schema_for(unit_type)
    return next(iter(schema["properties"]))


# Model routing (spec §5.2): frontier where ambiguity costs money.
FRONTIER_UNIT_TYPES = {"liens", "mortgages", "foreclosure", "bankruptcy"}


def route_model(unit_type: str, *, cheap_model: str, frontier_model: str) -> str:
    # "combined" fallback windows can contain liens/mortgages, so they route frontier.
    if unit_type == "combined":
        return frontier_model
    canonical = canonical_unit_type(unit_type)
    if canonical in FRONTIER_UNIT_TYPES or canonical not in UNIT_SCHEMAS:
        return frontier_model
    return cheap_model
