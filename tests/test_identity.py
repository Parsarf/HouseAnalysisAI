import threading
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList
from sqlalchemy.sql.selectable import Select

from contracts import FlagType
from db.models import Property, Report
from identity import (
    attach_report,
    merge,
    normalize_address,
    normalize_apn,
    resolve_property,
    trigram_similarity,
    unmerge,
)
from identity.models import MergeReportMove

# --- Offline store -------------------------------------------------------------
# Property.tags is a Postgres ARRAY, so the mapped tables cannot be created on
# SQLite. This fake evaluates the narrow set of selects identity/service.py
# issues against an in-memory store, and enforces the partial unique indexes
# from db/schema.sql (properties_apn_active_uq / properties_address_active_uq)
# on flush — the same backstop Postgres provides.


def _matches(row, criterion) -> bool:
    if isinstance(criterion, BooleanClauseList):
        outcomes = [_matches(row, clause) for clause in criterion.clauses]
        return any(outcomes) if criterion.operator is operators.or_ else all(outcomes)
    if isinstance(criterion, BinaryExpression):
        actual = getattr(row, criterion.left.name)
        if criterion.operator is operators.is_:
            return actual is None
        if criterion.operator is operators.is_not:
            return actual is not None
        value = criterion.right.value
        if criterion.operator is operators.eq:
            return actual == value
        if criterion.operator is operators.ge:
            return actual is not None and actual >= value
    raise AssertionError(f"unsupported criterion: {criterion!r}")


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _FakeStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = {Property: [], Report: [], MergeReportMove: []}

    def flush(self, pending):
        with self.lock:
            new = [row for row in pending if isinstance(row, Property) and row.merged_into_id is None]
            active = [row for row in self.rows[Property] if row.merged_into_id is None]
            for row in new:
                for other in active + [candidate for candidate in new if candidate is not row]:
                    if row.apn_key and row.apn_key == other.apn_key:
                        raise IntegrityError("INSERT INTO properties", {}, Exception("properties_apn_active_uq"))
                    if row.address_hash and row.address_hash == other.address_hash:
                        raise IntegrityError("INSERT INTO properties", {}, Exception("properties_address_active_uq"))
            for row in pending:
                row.id = row.id or __import__("uuid").uuid4()
                self.rows[type(row)].append(row)
            pending.clear()


class _FakeSession:
    def __init__(self, store):
        self._store = store
        self._pending = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="fake"))

    def add(self, row):
        self._pending.append(row)

    def flush(self):
        self._store.flush(self._pending)

    def rollback(self):
        self._pending.clear()

    def get(self, model, pk):
        for row in self._pending + self._store.rows[model]:
            if getattr(row, "id", None) == pk:
                return row
        return None

    def execute(self, clause, params=None):
        if isinstance(clause, Select):
            model = clause.column_descriptions[0]["entity"]
            candidates = [row for row in self._pending + self._store.rows[model] if isinstance(row, model)]
            rows = [row for row in candidates if all(_matches(row, c) for c in clause._where_criteria)]
            return _FakeResult(rows)
        raise AssertionError(f"unexpected execute: {clause!r}")


def _property(store, **kwargs) -> Property:
    session = _FakeSession(store)
    row = Property(**kwargs)
    session.add(row)
    session.flush()
    return row


# --- Normalization -------------------------------------------------------------


@pytest.mark.parametrize("address,zip5,expected_key", [
    ("1420 San Bruno Ave", "94066", "1420|SAN BRUNO|AVE||94066"),
    ("1420 san bruno avenue", "94066", "1420|SAN BRUNO|AVE||94066"),
    ("  1420  SAN   BRUNO   AVE. ", "94066", "1420|SAN BRUNO|AVE||94066"),
    ("1420 San Bruno Ave", None, "1420|SAN BRUNO|AVE||"),
    ("1420 San Bruno Ave", "94066-1234", "1420|SAN BRUNO|AVE||94066"),
    ("2200 Westborough Blvd Ste 200", "94080", "2200|WESTBOROUGH|BLVD|STE 200|94080"),
    ("2200 Westborough Boulevard Suite 200", "94080", "2200|WESTBOROUGH|BLVD|STE 200|94080"),
    ("2200 Westborough Blvd #200", "94080", "2200|WESTBOROUGH|BLVD|UNIT 200|94080"),
    ("2200 Westborough Blvd, Apt 200", "94080", "2200|WESTBOROUGH|BLVD|APT 200|94080"),
    ("2200 Westborough Blvd Apartment 200", "94080", "2200|WESTBOROUGH|BLVD|APT 200|94080"),
    ("55 Van Ness Ave Unit B", "94102", "55|VAN NESS|AVE|UNIT B|94102"),
    ("350 5th Ave Fl 9", "10118", "350|5TH|AVE|FL 9|10118"),
    ("500 North First Street", "95112", "500|N FIRST|ST||95112"),
    ("500 N First St", "95112", "500|N FIRST|ST||95112"),
    ("1 Northeast 2nd Ave", None, "1|NE 2ND|AVE||"),
    ("PO Box 123", None, "123|PO BOX|||"),
    ("P.O. Box 123", None, "123|PO BOX|||"),
    ("PO BOX 123", "94066", "123|PO BOX|||94066"),
    ("123 Main Rd", None, "123|MAIN|RD||"),
    ("123 Main Road", None, "123|MAIN|RD||"),
    ("742 Evergreen Terrace", "12345", "742|EVERGREEN|TER||12345"),
    ("10 Downing Cir", None, "10|DOWNING|CIR||"),
    ("10 Downing Circle", None, "10|DOWNING|CIR||"),
    ("12A Oak Ln", None, "12A|OAK|LN||"),
    ("999 El Camino Real", "94025", "999|EL CAMINO REAL|||94025"),
    ("1600 Amphitheatre Parkway", "94043", "1600|AMPHITHEATRE|PKWY||94043"),
    ("300 Highway 101", None, "300|HWY 101|||"),
    ("88 Colin P Kelly Jr St", "94107", "88|COLIN P KELLY JR|ST||94107"),
])
def test_normalize_address_table(address, zip5, expected_key):
    identity = normalize_address(address, zip5)
    assert identity.address_key == expected_key
    assert identity.address_hash
    assert identity.zip5 == (expected_key.rsplit("|", 1)[1] or None)


def test_normalize_address_house_number():
    assert normalize_address("1420 San Bruno Ave").house_number == "1420"
    assert normalize_address("PO Box 123").house_number == "123"
    assert normalize_address("12A Oak Ln").house_number == "12A"


def test_normalize_apn():
    assert normalize_apn(None) is None
    assert normalize_apn("") is None
    assert normalize_apn("0600-123-400") == "0600123400"
    assert normalize_apn("0600-123-400", "06075") == "060750600123400"


# --- Trigram similarity --------------------------------------------------------


def test_trigram_similarity_pure_function():
    assert trigram_similarity("1420 SAN BRUNO AVE", "1420 SAN BRUNO AVE") == 1.0
    assert trigram_similarity("", "1420 SAN BRUNO AVE") == 0.0
    assert trigram_similarity("1420 SAN BRUNO AVE", "789 OAK LN") == 0.0
    merge_pair = trigram_similarity("1234567 N AVENIDA DE LAS PALMAS BLVD", "1234567 N AVENIDA DE LA PALMAS BLVD")
    assert merge_pair >= 0.92
    band_pair = trigram_similarity("1420 SAN BRUNO AVE", "1421 SAN BRUNO AVE")
    assert 0.80 <= band_pair < 0.92


# --- Resolution tiers ----------------------------------------------------------


def test_apn_less_resolution_does_not_match_unrelated_apn_less_property():
    # Regression: or_(Property.apn_key == None, ...) compiled to `apn_key IS NULL`
    # and matched any APN-less property.
    store = _FakeStore()
    session = _FakeSession(store)
    first = resolve_property(session, "789 Oak Ave", zip5="94066")
    second = resolve_property(session, "1420 San Bruno Ave", zip5="94110")
    assert second.id != first.id
    assert len(store.rows[Property]) == 2


def test_exact_address_match_returns_existing():
    store = _FakeStore()
    session = _FakeSession(store)
    first = resolve_property(session, "1420 San Bruno Ave", zip5="94066")
    second = resolve_property(session, "1420 San Bruno Avenue", zip5="94066")
    assert second.id == first.id
    assert len(store.rows[Property]) == 1
    assert second.identity_flags == []


def test_apn_match_returns_existing_without_conflict():
    store = _FakeStore()
    session = _FakeSession(store)
    first = resolve_property(session, "1420 San Bruno Ave", apn="0600-123-400")  # no zip recorded yet
    second = resolve_property(session, "1420 San Bruno Ave", apn="0600-123-400", zip5="94066")
    assert second.id == first.id
    assert second.identity_flags == []


def test_apn_zip_conflict_emits_flag_and_does_not_merge():
    store = _FakeStore()
    session = _FakeSession(store)
    existing = resolve_property(session, "1420 San Bruno Ave", apn="0600-123-400", zip5="94066")
    row = resolve_property(session, "500 Oak Ave", apn="0600-123-400", zip5="94110")
    assert row.id != existing.id
    assert row.apn_key is None  # raw APN kept, but never an auto-merge path
    assert row.apn == "0600-123-400"
    assert len(store.rows[Property]) == 2
    by_property = {flag.property_id: flag for flag in row.identity_flags}
    assert set(by_property) == {row.id, existing.id}
    for flag in row.identity_flags:
        assert flag.flag_type == FlagType.IDENTITY_CONFLICT
        assert flag.raised_by == "identity"
    assert by_property[row.id].payload["other_property_id"] == str(existing.id)
    assert by_property[row.id].payload["existing_zip5"] == "94066"
    assert by_property[row.id].payload["incoming_zip5"] == "94110"


def test_apn_house_number_conflict_emits_flag_and_does_not_merge():
    store = _FakeStore()
    session = _FakeSession(store)
    resolve_property(session, "1420 San Bruno Ave", apn="0600-123-400", zip5="94066")
    row = resolve_property(session, "9999 San Bruno Ave", apn="0600-123-400", zip5="94066")
    assert [flag.flag_type for flag in row.identity_flags] == [FlagType.IDENTITY_CONFLICT] * 2
    assert row.identity_flags[0].payload["existing_house_number"] == "1420"
    assert row.identity_flags[0].payload["incoming_house_number"] == "9999"


def test_fuzzy_high_similarity_same_house_number_merges():
    store = _FakeStore()
    session = _FakeSession(store)
    existing = resolve_property(session, "1234567 N Avenida De Las Palmas Blvd", zip5="91910")
    row = resolve_property(session, "1234567 N Avenida De La Palmas Blvd", zip5="91910")
    assert row.id == existing.id
    assert len(store.rows[Property]) == 1


def test_fuzzy_band_creates_separate_property_and_flags_possible_duplicate():
    store = _FakeStore()
    session = _FakeSession(store)
    existing = resolve_property(session, "12345 Monterey Bay Blvd", zip5="93940")
    row = resolve_property(session, "12345 Monterey Bay Blvd Apt A", zip5="93940")
    assert row.id != existing.id
    assert len(store.rows[Property]) == 2
    assert len(row.identity_flags) == 1
    flag = row.identity_flags[0]
    assert flag.flag_type == FlagType.POSSIBLE_DUPLICATE
    assert flag.property_id == row.id
    assert flag.payload["other_property_id"] == str(existing.id)
    assert 0.80 <= flag.payload["similarity"] < 0.92


def test_fuzzy_band_different_house_number_never_merges():
    store = _FakeStore()
    session = _FakeSession(store)
    existing = resolve_property(session, "1420 San Bruno Ave", zip5="94066")
    row = resolve_property(session, "1421 San Bruno Ave", zip5="94066")  # similarity ~0.81
    assert row.id != existing.id
    assert [flag.flag_type for flag in row.identity_flags] == [FlagType.POSSIBLE_DUPLICATE]


def test_fuzzy_requires_same_zip():
    store = _FakeStore()
    session = _FakeSession(store)
    resolve_property(session, "1234567 N Avenida De Las Palmas Blvd", zip5="91910")
    row = resolve_property(session, "1234567 N Avenida De La Palmas Blvd", zip5="91911")
    assert len(store.rows[Property]) == 2
    assert row.identity_flags == []


def test_dissimilar_address_creates_property_without_flags():
    store = _FakeStore()
    session = _FakeSession(store)
    resolve_property(session, "1420 San Bruno Ave", zip5="94066")
    row = resolve_property(session, "789 Oak Ave", zip5="94066")
    assert len(store.rows[Property]) == 2
    assert row.identity_flags == []


# --- Concurrency ---------------------------------------------------------------


def test_fifty_concurrent_resolutions_create_exactly_one_property():
    store = _FakeStore()
    results, errors = [], []

    def worker():
        try:
            session = _FakeSession(store)
            results.append(resolve_property(session, "1420 San Bruno Ave", zip5="94066").id)
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(store.rows[Property]) == 1
    assert len(set(results)) == 1


# --- attach_report -------------------------------------------------------------


def test_attach_report_sets_property_id():
    store = _FakeStore()
    session = _FakeSession(store)
    report = Report(file_path="documents/r1/original.pdf", sha256="a" * 64)
    property_row = attach_report(session, report, "1420 San Bruno Ave", zip5="94066")
    assert report.property_id == property_row.id
    assert property_row.identity_flags == []
    other = Report(file_path="documents/r2/original.pdf", sha256="b" * 64)
    assert attach_report(session, other, "1420 San Bruno Avenue", zip5="94066").id == property_row.id
    assert other.property_id == property_row.id


# --- merge / unmerge -----------------------------------------------------------


def _property_with_report(store, address, zip5, sha256):
    session = _FakeSession(store)
    property_row = resolve_property(session, address, zip5=zip5)
    report = Report(file_path=f"documents/{sha256[:8]}/original.pdf", sha256=sha256, property_id=property_row.id)
    session.add(report)
    session.flush()
    return property_row, report


def test_merge_unmerge_merge_round_trip_restores_identical_state():
    store = _FakeStore()
    session = _FakeSession(store)
    source, source_report = _property_with_report(store, "1420 San Bruno Ave", "94066", "c" * 64)
    target, target_report = _property_with_report(store, "789 Oak Ave", "94066", "d" * 64)

    merge(session, source, target)
    assert source.merged_into_id == target.id
    assert source_report.property_id == target.id
    assert target_report.property_id == target.id
    merged_state = (source.merged_into_id, source_report.property_id, target_report.property_id)

    enqueued = []
    restored = unmerge(session, source, enqueue=lambda name, payload: enqueued.append((name, payload)))
    assert restored.id == source.id
    assert source.merged_into_id is None
    assert source_report.property_id == source.id
    assert target_report.property_id == target.id
    assert {payload["property_id"] for _, payload in enqueued} == {str(source.id), str(target.id)}
    assert all(name == "recompute_property" for name, _ in enqueued)

    merge(session, source, target)
    assert (source.merged_into_id, source_report.property_id, target_report.property_id) == merged_state
    moves = store.rows[MergeReportMove]
    assert len([move for move in moves if move.restored_at is not None]) == 1
    assert len([move for move in moves if move.restored_at is None]) == 1


def test_merge_rejects_self_merge_and_double_merge():
    store = _FakeStore()
    session = _FakeSession(store)
    source = resolve_property(session, "1420 San Bruno Ave", zip5="94066")
    target = resolve_property(session, "789 Oak Ave", zip5="94066")
    with pytest.raises(ValueError, match="itself"):
        merge(session, source, source)
    merge(session, source, target)
    with pytest.raises(ValueError, match="already merged"):
        merge(session, source, target)


def test_unmerge_rejects_unmerged_property():
    store = _FakeStore()
    session = _FakeSession(store)
    row = resolve_property(session, "1420 San Bruno Ave", zip5="94066")
    with pytest.raises(ValueError, match="not merged"):
        unmerge(session, row)
