"""Offline tests for classification/: signature loading, per-page match-rate
classification, calibrated token estimation, and sectioning."""

import math
import re
from pathlib import Path

import pytest

from classification import (
    Classification,
    SectionedUnit,
    Signature,
    classify,
    estimate_tokens,
    load_signatures,
    section_match_rate,
    section_pages,
)
from classification.service import DEFAULT_SIGNATURES

# ---------------------------------------------------------------------------
# Reference token counter: a cl100k-style approximation used as the ±10%
# yardstick for the cheap calibrated estimator in classification/tokens.py.
# ---------------------------------------------------------------------------

_REF_RE = re.compile(r"\s?[A-Za-z]+(?:'[a-z]+)?|\s?\d+|\s?[^\sA-Za-z\d]|\s+")


def reference_token_count(text: str) -> int:
    count = 0
    for chunk in _REF_RE.findall(text):
        if "\n" in chunk:
            count += 1  # newline runs tokenize separately
        core = chunk.strip()
        if not core:
            continue
        if core.replace("'", "").isalpha():
            n = len(core)
            count += 1 if n <= 8 else 1 + math.ceil((n - 8) / 4)
        elif core.isdigit():
            count += math.ceil(len(core) / 3)
        else:
            count += 1
    return max(1, count)


PROSE = """This deed of trust is made this 14th day of March, 2019, between the borrower,
Jonathan M. Appleseed, residing at 1422 West Elm Street, Springfield, and the lender,
First National Bank of America. The borrower acknowledges receipt of the principal sum
of three hundred forty two thousand dollars, payable in monthly installments beginning
on the first day of May, 2019, with interest accruing at the fixed rate described below.
The property securing this obligation is improved with a single family residence built
in 1987 containing approximately 2,140 square feet of living area on a 7,500 square foot lot."""

TABLE = """Address                Sale Date    Sale Price   SqFt   Beds  Baths  $/SqFt
1248 Oak Hollow Ln     03/12/2024   $485,000     1,980  3     2      $244.95
8821 Redwood Ct        01/28/2024   $512,500     2,210  4     2.5    $231.90
330 Meadowbrook Dr     12/05/2023   $449,900     1,760  3     2      $255.63
77 Birchwood Ave       11/19/2023   $529,000     2,305  4     3      $229.50
1510 Canyon View Rd    10/02/2023   $468,750     1,925  3     2.5    $243.51
942 Sunset Terrace     09/14/2023   $501,200     2,040  4     2      $245.69
2205 Lilac Way         08/30/2023   $437,000     1,700  3     1.5    $257.06"""

TAX = """TAX INFORMATION
Parcel Number: 042-118-003-000
Tax Year: 2024
Assessed Land Value: $182,400
Assessed Improvement Value: $296,700
Total Assessed Value: $479,100
Tax Rate Area: 07-021
1st Installment: $2,845.12  Due 11/01/2024  Status: Paid
2nd Installment: $2,845.12  Due 02/01/2025  Status: Due
Delinquent Amount: $0.00
Exemptions: Homeowner $7,000"""

HEADERY = """MORTGAGE / TRANSFER HISTORY
Recording Date: 06/21/2021
Document Number: 2021-0488273
Loan Amount: $410,000
Lender: WELLS FARGO BANK NA
Loan Type: Conventional
Term: 360 Months"""

SAMPLES = {
    "prose": PROSE,
    "table": TABLE,
    "tax": TAX,
    "headery": HEADERY,
    "mixed": PROSE + "\n\n" + TABLE + "\n\n" + TAX,
    "short": "FORECLOSURE DETAIL",
    "long": "\n\n".join([PROSE, TABLE, TAX, HEADERY] * 6),
    "unicode": "Señor García resides at 88 Crépe Myrtle Lane — façade renovated, naïve estimate €450,000 ±5%.",
    "numbers": " ".join(str(100000 + i * 137) for i in range(300)),
    "punct_heavy": " | ".join(f"${i},{i % 10}00.00" for i in range(100, 200)),
    "linebreaks": "\n".join(f"Line {i}: value {i * 7 % 997}" for i in range(120)),
    "blank_lines": "\n\n\n".join(["Section text with several words here."] * 40),
}

FIXTURE_PAGE_TEXT = Path(__file__).parents[1] / "fixtures" / "page_text"


def _fixture_samples() -> dict[str, str]:
    if not FIXTURE_PAGE_TEXT.is_dir():
        return {}
    return {path.name: path.read_text() for path in sorted(FIXTURE_PAGE_TEXT.glob("*.txt"))}


# ---------------------------------------------------------------------------
# Token estimator calibration (WP-2: within ±10% of a reference count)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_estimate_tokens_within_10_pct_of_reference(name):
    text = SAMPLES[name]
    estimate = estimate_tokens(text)
    reference = reference_token_count(text)
    assert abs(estimate - reference) / reference <= 0.10, f"{name}: estimate={estimate} reference={reference}"


def test_estimate_tokens_on_page_text_fixtures():
    for name, text in _fixture_samples().items():
        estimate = estimate_tokens(text)
        reference = reference_token_count(text)
        assert abs(estimate - reference) / reference <= 0.10, f"{name}: estimate={estimate} reference={reference}"


def test_estimate_tokens_edge_cases():
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello") == 1
    assert estimate_tokens("a" * 100) >= 1


# ---------------------------------------------------------------------------
# Signature loading (document_signatures is primary; built-ins are fallback)
# ---------------------------------------------------------------------------

def test_load_signatures_from_injected_source():
    rows = [{"pattern": r"zestimate|zillow", "report_type": "valuation", "vendor": "zillow", "priority": 5, "is_active": True}]
    signatures = load_signatures(rows)
    assert signatures == [Signature(r"zestimate|zillow", "valuation", "zillow", 5)]


def test_load_signatures_falls_back_on_empty_table():
    assert load_signatures([]) == list(DEFAULT_SIGNATURES)


def test_load_signatures_falls_back_when_source_raises():
    def broken():
        raise ConnectionError("no database")

    assert load_signatures(broken) == list(DEFAULT_SIGNATURES)


def test_load_signatures_skips_inactive_and_invalid_rows():
    rows = [
        {"pattern": r"lien", "report_type": "lien", "is_active": False},
        {"pattern": r"(unclosed", "report_type": "lien"},
        {"pattern": r"comparables", "report_type": "comparables"},
    ]
    signatures = load_signatures(rows)
    assert signatures == [Signature(r"comparables", "comparables", None, 0)]


def test_new_signature_row_changes_classification_without_code_change():
    text = "Home valuation report\nEstimated market value: $512,000"
    assert classify(text, signature_source=[]).report_type == "unknown"
    rows = [{"pattern": r"home valuation report", "report_type": "valuation", "vendor": "valueco"}]
    result = classify(text, signature_source=rows)
    assert result.report_type == "valuation"
    assert result.vendor == "valueco"


# ---------------------------------------------------------------------------
# Per-page classification with match rate (spec §4.4)
# ---------------------------------------------------------------------------

def _pages(first: str, rest: str, count: int) -> list[str]:
    return [first] + [f"{rest} page {i}" for i in range(2, count + 1)]


def test_classify_unknown_when_nothing_matches():
    result = classify("lorem ipsum dolor sit amet", signature_source=[])
    assert result == Classification("unknown", None, 0.0, 0.0)


def test_classify_uses_builtin_rules_without_database():
    # No signature_source and no reachable database -> DEFAULT_SIGNATURES.
    result = classify("DEED OF TRUST\nLoan Amount: $410,000")
    assert result.report_type == "mortgage"
    assert result.confidence > 0.0


def test_classify_matches_per_page_and_reports_match_rate():
    pages = _pages("DEED OF TRUST\nLoan Amount: $410,000", "boilerplate", 10)
    result = classify("", pages=pages, signature_source=[])
    assert result.report_type == "mortgage"
    assert result.match_rate == pytest.approx(0.1)
    assert 0.0 < result.confidence < 0.9


def test_stray_mention_does_not_beat_document_wide_match():
    # Regression: a mortgage report mentioning "lien" once must classify as mortgage.
    pages = [f"Deed of trust details continued {i}" for i in range(1, 11)]
    pages[3] += "\nOne lien was released in 2011."
    result = classify("", pages=pages, signature_source=[])
    assert result.report_type == "mortgage"
    assert result.match_rate == pytest.approx(1.0)
    assert result.confidence == pytest.approx(0.95)


def test_classify_splits_form_feed_pages_from_text():
    text = "\f".join(_pages("NOTICE OF TRUSTEE'S SALE", "filler", 4))
    result = classify(text, signature_source=[])
    assert result.report_type == "foreclosure"
    assert result.match_rate == pytest.approx(0.25)


def test_classify_counts_filename_as_page_one():
    result = classify("", filename="2024_bankruptcy_chapter7.pdf", signature_source=[])
    assert result.report_type == "bankruptcy"
    assert result.match_rate == pytest.approx(1.0)


def test_classify_priority_breaks_ties():
    rows = [
        Signature(r"report", "property_profile", None, 0),
        Signature(r"report", "combined", None, 10),
    ]
    result = classify("report", signature_source=rows)
    assert result.report_type == "combined"


# ---------------------------------------------------------------------------
# Sectioning (spec §5.1)
# ---------------------------------------------------------------------------

def _header_pages() -> list[str]:
    pages = [f"plain page {i}" for i in range(1, 11)]
    pages[0] = "MORTGAGE / TRANSFER HISTORY\n" + pages[0]
    pages[4] = "TAX INFORMATION\n" + pages[4]
    pages[7] = "COMPARABLE SALES\n" + pages[7]
    return pages


def test_section_pages_splits_on_headers_with_exact_boundaries():
    pages = _header_pages()
    units = section_pages(pages)
    assert [(u.unit_type, u.page_start, u.page_end) for u in units] == [("mortgage", 1, 4), ("tax", 5, 7), ("comparables", 8, 10)]
    assert units[0].text == "\n".join(pages[0:4])
    assert all(u.matched_header for u in units)
    for unit in units:
        assert unit.token_estimate == estimate_tokens(unit.text)


def test_section_pages_closes_unit_when_next_header_opens():
    # Regression: consecutive headers must not swallow the open unit.
    pages = ["MORTGAGE page one", "TAX INFORMATION page two", "plain page three"]
    units = section_pages(pages)
    assert [(u.unit_type, u.page_start, u.page_end) for u in units] == [("mortgage", 1, 1), ("tax", 2, 3)]


def test_section_pages_fallback_windows_have_one_page_overlap():
    pages = [f"page {i}" for i in range(1, 8)]
    units = section_pages(pages)
    assert [(u.page_start, u.page_end) for u in units] == [(1, 3), (3, 5), (5, 7)]
    assert all(u.unit_type == "combined" and not u.matched_header for u in units)


def test_section_pages_fallback_windows_without_overlap():
    pages = [f"page {i}" for i in range(1, 8)]
    units = section_pages(pages, overlap=0)
    assert [(u.page_start, u.page_end) for u in units] == [(1, 3), (4, 6), (7, 7)]


def test_section_pages_fallback_smaller_than_window():
    units = section_pages(["only", "two pages"])
    assert [(u.page_start, u.page_end) for u in units] == [(1, 2)]


def test_section_pages_empty_input():
    assert section_pages([]) == []


def test_section_match_rate_metric():
    assert section_match_rate(section_pages(_header_pages())) == 1.0
    assert section_match_rate(section_pages([f"page {i}" for i in range(5)])) == 0.0
    assert section_match_rate([]) == 0.0


def test_sectioned_unit_defaults_keep_three_arg_construction():
    unit = SectionedUnit("combined", 1, 3, "text", 7)
    assert unit.matched_header is True
