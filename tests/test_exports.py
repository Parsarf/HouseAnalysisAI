import csv
import json
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from contracts import (
    AddressBlock,
    AttachmentBasis,
    EquityBlock,
    ForeclosureState,
    LiabilityBlock,
    LienRecord,
    NormalizedProperty,
    OfferPoint,
    Scenario,
    ScoreSet,
    SourceKind,
    StrategyResult,
    StrategyType,
    TrackedValue,
    UnderwritingResult,
    ValueBlock,
)
from exports import (
    deal_sheet_html,
    estimated_figures,
    full_export,
    net_sheet_html,
    stream_properties,
    write_export,
)


def money(value, estimated=False):
    return TrackedValue(value=Decimal(str(value)), confidence=0.9,
                        source_kind=SourceKind.REPORT, is_estimated=estimated)


def sample_property(estimated_sqft=False):
    return NormalizedProperty(
        property_id=uuid4(),
        address=AddressBlock(line1="1 Main St", city="Testville", state="CA", zip5="90001"),
        liens=[LienRecord(lien_type="federal_tax", amount=money("12800"),
                          attachment_basis=AttachmentBasis.OWNER_NAMED_ONLY,
                          attachment_confidence=0.8)],
        foreclosure=ForeclosureState(stage="nts", nod_date=date(2025, 1, 10),
                                     nts_date=date(2025, 4, 1),
                                     current_sale_date=date(2025, 7, 1),
                                     postponement_count=2, is_active=True),
        resolution_version="test",
    )


def sample_underwriting(prop):
    return UnderwritingResult(
        property_id=prop.property_id, assumption_set_id=uuid4(), engine_version="test",
        status="ok",
        value=ValueBlock(v_low=Decimal(360000), v_expected=Decimal(400000),
                         v_high=Decimal(440000)),
        liabilities=LiabilityBlock(confirmed=Decimal(150000), potential=Decimal(12800),
                                   maximum=Decimal(162800)),
        equity={Scenario.EXPECTED: EquityBlock(gross=Decimal(250000))},
    )


def sample_scores(prop):
    return ScoreSet(property_id=prop.property_id, scoring_config_id=uuid4(),
                    fos=Decimal(62), distress=Decimal(48), data_confidence=Decimal(71),
                    risk=Decimal(30), overall=Decimal(55), components={}, gates_applied=[],
                    is_rankable=True)


def offer_point(potential=Decimal(12800), short_sale=False):
    return OfferPoint(
        offer_price=Decimal(240000), scenario=Scenario.EXPECTED,
        confirmed_payoffs=Decimal(151200), potential_payoffs=potential,
        closing_costs=Decimal(3600),
        proceeds_low=Decimal("72400" if not short_sale else "-5000"),
        proceeds_expected=Decimal(85200), proceeds_high=Decimal(98000),
        buyer_basis=Decimal(260000), profit=Decimal(140000), roi=Decimal("0.53"),
        is_short_sale=short_sale,
    )


def test_stream_properties_header_and_rows():
    rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y", "ignored": 9}]
    chunks = list(stream_properties(iter(rows), ["a", "b"]))
    assert chunks[0] == "a,b\r\n"
    assert chunks[1] == "1,x\r\n"
    assert chunks[2] == "2,y\r\n"


def test_stream_properties_streams_100k_rows():
    rows = ({"a": i, "b": "x"} for i in range(100_000))
    stream = stream_properties(rows, ["a", "b"])
    count = sum(1 for _ in stream)
    assert count == 100_001  # header + 100k rows, yielded chunk by chunk


def test_deal_sheet_renders_full_payload():
    prop = sample_property()
    sheet = deal_sheet_html(prop, sample_underwriting(prop),
                            strategies=[StrategyResult(strategy=StrategyType.CASH,
                                                       scenario=Scenario.EXPECTED,
                                                       status="viable", mao=Decimal(210000),
                                                       profit=Decimal(90000))],
                            scores=sample_scores(prop))
    assert "1 Main St" in sheet
    assert "$400,000" in sheet  # expected value
    assert "$210,000" in sheet  # MAO
    assert "NTS" in sheet and "2025-07-01" in sheet  # distress timeline
    assert "71 / 100" in sheet  # data confidence
    assert "No estimated figures" in sheet


def test_deal_sheet_renders_for_missing_data_property():
    prop = NormalizedProperty(property_id=uuid4(), address=AddressBlock(line1="9 Nowhere Rd"),
                              resolution_version="test")
    sheet = deal_sheet_html(prop, None, [], None)
    assert "9 Nowhere Rd" in sheet
    assert "unavailable" in sheet
    assert "No viable strategies" in sheet


def test_deal_sheet_footer_matches_is_estimated_flags():
    prop = sample_property()
    prop.valuation_candidates = []
    prop.liens[0].amount = money("12800", estimated=True)
    sheet = deal_sheet_html(prop, sample_underwriting(prop))
    assert "federal_tax lien amount" in sheet
    assert estimated_figures(prop) == ["federal_tax lien amount"]


def test_net_sheet_shows_range_when_potential_liabilities_exist():
    prop = sample_property()
    sheet = net_sheet_html(prop, offer_point())
    assert "$72,400" in sheet and "$98,000" in sheet  # range, not a single number
    assert "most likely $85,200" in sheet
    assert "Unverified obligations" in sheet
    assert "federal_tax lien" in sheet


def test_net_sheet_short_sale_banner():
    sheet = net_sheet_html(sample_property(), offer_point(short_sale=True))
    assert "short sale" in sheet.lower()


def test_net_sheet_single_proceeds_when_no_potential_obligations():
    prop = sample_property()
    prop.liens = []
    sheet = net_sheet_html(prop, offer_point(potential=Decimal(0)))
    assert "$85,200" in sheet
    assert "Unverified obligations" not in sheet


def test_write_export_stores_under_property_exports(tmp_path):
    path = write_export(tmp_path, uuid4(), "deal-sheet.html", "<html></html>")
    assert path.parent.name == "exports"
    assert path.read_text() == "<html></html>"


@pytest.fixture()
def sqlite_connection():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE properties (id text, apn text, sqft numeric)"))
        connection.execute(text("CREATE TABLE liens (id text, property_id text, amount numeric)"))
        connection.execute(text("CREATE TABLE mortgages (id text, property_id text, balance numeric)"))
        connection.execute(text("CREATE TABLE scores (id text, property_id text, overall numeric)"))
        connection.execute(text(
            "CREATE TABLE extracted_facts (id text, property_id text, field_path text, value_parsed numeric)"))
        connection.execute(text("INSERT INTO properties VALUES ('p1', 'APN-1', 1800)"))
        connection.execute(text("INSERT INTO properties VALUES ('p2', 'APN-2', 950)"))
        connection.execute(text("INSERT INTO liens VALUES ('l1', 'p1', 12800.50)"))
        connection.execute(text("INSERT INTO mortgages VALUES ('m1', 'p1', 150000)"))
        connection.execute(text("INSERT INTO scores VALUES ('s1', 'p1', 55.5)"))
        connection.execute(text("INSERT INTO extracted_facts VALUES ('f1', 'p1', 'liens[0].amount', 12800.5)"))
        yield connection


def test_full_export_round_trip(sqlite_connection, tmp_path):
    written = full_export(sqlite_connection, tmp_path)
    names = {path.name for path in written}
    assert names == {"properties.csv", "liens.csv", "mortgages.csv", "scores.csv", "facts.jsonl"}

    with (tmp_path / "properties.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert [row["apn"] for row in rows] == ["APN-1", "APN-2"]

    with (tmp_path / "liens.csv").open() as handle:
        (lien_row,) = list(csv.DictReader(handle))
    assert Decimal(lien_row["amount"]) == Decimal("12800.5")

    facts = [json.loads(line) for line in (tmp_path / "facts.jsonl").read_text().splitlines()]
    assert facts == [{"id": "f1", "property_id": "p1", "field_path": "liens[0].amount",
                      "value_parsed": 12800.5}]


def test_full_export_copies_documents(sqlite_connection, tmp_path):
    docs = tmp_path / "docs"
    (docs / "p1").mkdir(parents=True)
    (docs / "p1" / "page.txt").write_text("page text")
    full_export(sqlite_connection, tmp_path / "out", documents_root=docs)
    assert (tmp_path / "out" / "documents" / "p1" / "page.txt").read_text() == "page text"


def test_full_export_excludes_archived_documents_and_contacts_by_default(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    documents = tmp_path / "documents"
    active_doc = documents / "active" / "original.pdf"
    archived_doc = documents / "archived" / "original.pdf"
    owner_doc = documents / "owner" / "original.pdf"
    for path in (active_doc, archived_doc, owner_doc):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.parent.name)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE properties (id text, archived_at datetime)"))
        connection.execute(text("CREATE TABLE liens (id text, property_id text)"))
        connection.execute(text("CREATE TABLE mortgages (id text, property_id text)"))
        connection.execute(text("CREATE TABLE scores (id text, property_id text)"))
        connection.execute(text("CREATE TABLE extracted_facts (id text, property_id text)"))
        connection.execute(text(
            "CREATE TABLE reports (id text, property_id text, file_path text, ocr_path text)",
        ))
        connection.execute(text(
            "CREATE TABLE owner_contacts (id text, owner_id text, kind text, value text)",
        ))
        connection.execute(text("CREATE TABLE property_owners (property_id text, owner_id text)"))
        connection.execute(text("INSERT INTO properties VALUES ('active', NULL), ('archived', '2026-08-01')"))
        connection.execute(text(
            "INSERT INTO reports VALUES "
            "('r1', 'active', :active, NULL), ('r2', 'archived', :archived, NULL), "
            "('r3', NULL, :owner, NULL)",
        ), {"active": str(active_doc), "archived": str(archived_doc), "owner": str(owner_doc)})
        connection.execute(text("INSERT INTO property_owners VALUES ('active', 'o1')"))
        connection.execute(text("INSERT INTO owner_contacts VALUES ('c1', 'o1', 'email', 'owner@example.com')"))

        default_paths = full_export(connection, tmp_path / "default", documents)
        assert "owner_contacts.csv" not in {path.name for path in default_paths}
        assert (tmp_path / "default/documents/active/original.pdf").is_file()
        assert not (tmp_path / "default/documents/archived/original.pdf").exists()
        assert not (tmp_path / "default/documents/owner/original.pdf").exists()

        full_export(connection, tmp_path / "opt-in", documents, include_owner_contacts=True)
        contacts = list(csv.DictReader((tmp_path / "opt-in/owner_contacts.csv").open()))
        assert contacts[0]["value"] == "owner@example.com"
