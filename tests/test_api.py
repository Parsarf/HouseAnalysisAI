"""WP-11 API tests. Fully offline: FastAPI TestClient against the app with the
session/queue dependencies overridden by in-memory fakes. No Postgres, Redis,
network, or API keys.

The fake query layer evaluates the actual SQLAlchemy expressions produced by
``api.filters.translate_filters``/``cursor_condition`` against in-memory rows,
so filter/pagination tests exercise real translation, not echoes.
"""
import contextlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql import operators as sa_ops
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    UnaryExpression,
)

from api import deps as api_deps
from api import routes_portfolio
from api.app import app
from api.deps import get_queue, get_session
from api.filters import translate_filters
from auth.dependencies import make_session
from auth.service import hash_password
from common.settings import settings
from contracts import EntityType
from db import models as dbm

PID = UUID("00000000-0000-0000-0000-0000000000a1")
PID2 = UUID("00000000-0000-0000-0000-0000000000a2")
PID3 = UUID("00000000-0000-0000-0000-0000000000a3")
BATCH_ID = UUID("00000000-0000-0000-0000-0000000000b1")
NOW = datetime(2026, 2, 1, tzinfo=UTC)

DEFAULT_PARAMS = {key: value for key, value in
                  json.loads(Path("fixtures/assumptions/default.json").read_text()).items()
                  if key not in ("id", "version", "name")}


# --- fake session -------------------------------------------------------------

def _literal(node):
    if isinstance(node, BindParameter):
        return node.value
    if node is None:
        return None
    name = type(node).__name__
    if name == "True_":
        return True
    if name == "False_":
        return False
    if name == "Null":
        return None
    return node


def _eval(expr, row):
    """Evaluate the narrow SQLAlchemy expression subset the API generates."""
    if isinstance(expr, BooleanClauseList):
        values = [_eval(clause, row) for clause in expr.clauses]
        return all(values) if expr.operator is sa_ops.and_ else any(values)
    assert isinstance(expr, BinaryExpression), f"unsupported criterion: {expr!r}"
    left = getattr(row, expr.left.key)
    right = _literal(expr.right)
    op = expr.operator
    if op is sa_ops.eq:
        return left == right
    if op is sa_ops.ne:
        return left != right
    if op is sa_ops.gt:
        return left is not None and right is not None and left > right
    if op is sa_ops.ge:
        return left is not None and right is not None and left >= right
    if op is sa_ops.lt:
        return left is not None and right is not None and left < right
    if op is sa_ops.le:
        return left is not None and right is not None and left <= right
    if op is sa_ops.in_op:
        return left in right
    if op is sa_ops.is_:
        return (left is None) if right is None else (left == right)
    if op is sa_ops.is_not:
        return (left is not None) if right is None else (left != right)
    if op in (sa_ops.ilike_op, sa_ops.like_op):
        return str(right).strip("%").lower() in str(left or "").lower()
    if op is sa_ops.contains_op or getattr(op, "opstring", None) == "@>" or "contains" in getattr(op, "__name__", ""):
        if isinstance(left, list):
            wanted = right if isinstance(right, list) else [right]
            return all(item in left for item in wanted)
        return str(right) in str(left or "")
    raise AssertionError(f"unsupported operator {op}")


class FakeQuery:
    def __init__(self, session, model, criteria=(), ordering=(), limit=None):
        self.session, self.model = session, model
        self.criteria, self.ordering, self._limit = criteria, ordering, limit

    def filter(self, *criteria):
        return FakeQuery(self.session, self.model, self.criteria + criteria, self.ordering, self._limit)

    def order_by(self, *keys):
        return FakeQuery(self.session, self.model, self.criteria, self.ordering + keys, self._limit)

    def limit(self, n):
        return FakeQuery(self.session, self.model, self.criteria, self.ordering, n)

    def all(self):
        rows = [row for row in self.session.rows.get(self.model, [])
                if all(_eval(criterion, row) for criterion in self.criteria)]
        for key in reversed(self.ordering):
            if isinstance(key, UnaryExpression):
                column, descending = key.element.key, key.modifier is sa_ops.desc_op
            else:
                column, descending = key.key, False
            rows.sort(key=lambda row, name=column: (getattr(row, name) is None, getattr(row, name)),
                      reverse=descending)
        return rows[: self._limit] if self._limit is not None else rows

    def first(self):
        result = self.all()
        return result[0] if result else None

    def count(self):
        return len(self.all())


class FakeSession:
    def __init__(self, rows=None):
        self.rows = {model: list(items) for model, items in (rows or {}).items()}
        self.commits = 0

    def get(self, model, pk):
        return next((row for row in self.rows.get(model, []) if row.id == pk), None)

    def query(self, model):
        return FakeQuery(self, model)

    def add(self, obj):
        self.rows.setdefault(type(obj), []).append(obj)

    def delete(self, obj):
        self.rows.get(type(obj), []).remove(obj)

    def flush(self):
        pass

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass

    def begin_nested(self):
        return contextlib.nullcontext()

    def scalar(self, statement):  # ingestion.find_by_sha256 — no duplicates in tests
        return None


class FakeQueue:
    def __init__(self):
        self.jobs = []

    def enqueue(self, session, name, payload, dedupe_key, max_attempts=5):
        self.jobs.append({"name": name, "payload": json.loads(payload), "dedupe_key": dedupe_key})
        return uuid4()


# --- seed helpers ---------------------------------------------------------------

def prop(pid, address, city, state, zip5, *, status="new", tags=None, gut=None,
         watchlisted=False, created=None, next_action=None):
    return dbm.Property(id=pid, address_line1=address, city=city, state=state, zip5=zip5,
                        pipeline_status=status, tags=tags or [], gut_rating=gut,
                        is_watchlisted=watchlisted, next_action=next_action,
                        apn=f"APN-{zip5}", created_at=created or NOW, updated_at=NOW)


def fact_row(pid, entity_type, local_id, field_path, *, parsed=None, text=None, when=None):
    return dbm.ExtractedFact(id=uuid4(), property_id=pid, report_id=uuid4(), extraction_unit_id=uuid4(),
                             entity_type=entity_type.value, entity_local_id=local_id, field_path=field_path,
                             value_raw=text or (str(parsed) if parsed is not None else None),
                             value_parsed=parsed, value_text=text, value_date=when, page_number=1,
                             snippet="verbatim snippet", extraction_confidence=Decimal("0.9"),
                             source_kind="report", is_active=True)


def seed_rows():
    props = [
        prop(PID, "100 Main St", "Oakland", "CA", "94601", tags=["watch"], gut=4,
             created=datetime(2026, 1, 5, tzinfo=UTC)),
        prop(PID2, "200 Elm St", "Berkeley", "CA", "94702", gut=2,
             created=datetime(2026, 1, 3, tzinfo=UTC)),
        prop(PID3, "300 Oak Ave", "Austin", "TX", "78701", status="analyzed", watchlisted=True,
             created=datetime(2026, 1, 1, tzinfo=UTC)),
    ]
    facts = [
        fact_row(PID, EntityType.PROPERTY, "p1", "property.apn", text="APN-94601"),
        fact_row(PID, EntityType.PROPERTY, "p1", "property.address", text="100 Main St"),
        fact_row(PID, EntityType.PROPERTY, "p1", "property.address.city", text="Oakland"),
        fact_row(PID, EntityType.PROPERTY, "p1", "property.address.state", text="CA"),
        fact_row(PID, EntityType.PROPERTY, "p1", "property.address.zip5", text="94601"),
        fact_row(PID, EntityType.PROPERTY, "p1", "property.sqft", parsed=Decimal(1812)),
        fact_row(PID, EntityType.VALUATION, "v1", "valuation.type", text="comp"),
        fact_row(PID, EntityType.VALUATION, "v1", "valuation.value", parsed=Decimal(500000)),
        fact_row(PID, EntityType.MORTGAGE, "m1", "mortgage.position", text="first"),
        fact_row(PID, EntityType.MORTGAGE, "m1", "mortgage.estimated_balance", parsed=Decimal(320000)),
    ]
    scenarios = [
        dbm.DealScenario(id=uuid4(), property_id=PID, strategy="cash", scenario="expected",
                         status="viable", mao=Decimal("300000.00"), all_in_basis=Decimal("350000.00"),
                         profit=Decimal("50000.00"), roi=Decimal("0.14"), margin_of_safety=Decimal("0.10"),
                         arv=Decimal("450000.00"), computed_at=NOW),
        dbm.DealScenario(id=uuid4(), property_id=PID, strategy="flip", scenario="conservative",
                         status="not_viable", computed_at=NOW),
    ]
    offers = [
        dbm.OfferScenario(id=uuid4(), property_id=PID, offer_price=Decimal("300000.00"), scenario="expected",
                          confirmed_payoffs=Decimal("320000.00"), potential_payoffs=Decimal(0),
                          closing_costs=Decimal("3000.00"), proceeds_low=Decimal("-23000.00"),
                          proceeds_expected=Decimal("-23000.00"), proceeds_high=Decimal("-23000.00"),
                          buyer_basis=Decimal("330000.00"), profit=Decimal("20000.00"),
                          roi=Decimal("0.06"), is_short_sale=True),
        dbm.OfferScenario(id=uuid4(), property_id=PID, offer_price=Decimal("350000.00"), scenario="expected",
                          confirmed_payoffs=Decimal("320000.00"), potential_payoffs=Decimal(0),
                          closing_costs=Decimal("3500.00"), proceeds_low=Decimal("26500.00"),
                          proceeds_expected=Decimal("26500.00"), proceeds_high=Decimal("26500.00"),
                          buyer_basis=Decimal("380000.00"), profit=Decimal("-30000.00"),
                          roi=Decimal("-0.08"), is_short_sale=False),
    ]
    scores = [dbm.Score(id=uuid4(), property_id=PID, scoring_config_id=uuid4(), fos=Decimal("0.7"),
                        distress=Decimal("0.5"), data_confidence=Decimal("0.9"), risk=Decimal("0.2"),
                        overall=Decimal("0.83"), components={"base_equity": "0.4"},
                        gates_applied=[], computed_at=NOW)]
    flags = [dbm.Flag(id=uuid4(), property_id=PID, flag_type="lien_attachment",
                      payload={"index": 0}, financial_impact_usd=Decimal("12000.00"),
                      status="open", dedupe_key="lien-attachment:1")]
    assumption_sets = [dbm.AssumptionSet(id=uuid4(), name="default", version=1, is_default=True,
                                         params=DEFAULT_PARAMS, effective_from=date(2026, 1, 1))]
    valuations = [dbm.Valuation(id=uuid4(), property_id=PID, valuation_type="comp",
                                value=Decimal("500000.00"), confidence_reported=Decimal("0.9"),
                                as_of_date=date(2026, 1, 1), is_active=True)]
    events = [
        dbm.ForeclosureEvent(id=uuid4(), property_id=PID, event_type="nod",
                             event_date=date(2025, 11, 1), stage_after_event="nod"),
        dbm.Lien(id=uuid4(), property_id=PID, lien_type="hoa", creditor_raw="HOA",
                 amount=Decimal("4000.00"), recording_date=date(2025, 12, 15), status="active",
                 attachment_basis="recorded_against_property", attachment_confidence=Decimal("0.9")),
        dbm.ChangeEvent(id=uuid4(), property_id=PID, change_type="value_change", field_path="valuation.value",
                        old_value={}, new_value={}, detected_at=datetime(2026, 1, 20, tzinfo=UTC)),
    ]
    return {
        dbm.Property: props, dbm.ExtractedFact: facts, dbm.DealScenario: scenarios,
        dbm.OfferScenario: offers, dbm.Score: scores, dbm.Flag: flags,
        dbm.AssumptionSet: assumption_sets, dbm.Valuation: valuations,
        dbm.ForeclosureEvent: events[:1], dbm.Lien: events[1:2], dbm.ChangeEvent: events[2:],
    }


@pytest.fixture()
def session():
    return FakeSession(seed_rows())


@pytest.fixture()
def queue():
    return FakeQueue()


@pytest.fixture()
def client(session, queue):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_queue] = lambda: queue
    client = TestClient(app)
    client.cookies.set("session_cookie", make_session("owner", False, settings.session_secret))
    yield client
    app.dependency_overrides.clear()


@pytest.fixture()
def readonly_client(session, queue):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_queue] = lambda: queue
    client = TestClient(app)
    client.cookies.set("session_cookie", make_session("viewer", True, settings.session_secret))
    yield client
    app.dependency_overrides.clear()


def filters_param(*clauses):
    return json.dumps(list(clauses))


# --- basics / auth / errors -------------------------------------------------------

def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_unauthenticated_gets_structured_401(client):
    client.cookies.clear()
    response = client.get("/api/properties")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert set(body["error"]) == {"code", "message", "details"}


def test_login_sets_session_cookie(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_password_hash", hash_password("s3cret"))
    client.cookies.clear()
    bad = client.post("/api/auth/login", data={"password": "wrong"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_input"
    ok = client.post("/api/auth/login", data={"password": "s3cret"})
    assert ok.status_code == 200 and "session_cookie" in ok.cookies


def test_validation_error_is_envelope(client):
    response = client.get(f"/api/properties/{PID}/analysis?scenario=bogus")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"


def test_unhandled_exception_never_leaks_text(session, queue):
    def boom():
        raise RuntimeError("boom-sensitive-internal-detail")

    app.dependency_overrides[get_session] = boom
    app.dependency_overrides[get_queue] = lambda: queue
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("session_cookie", make_session("owner", False, settings.session_secret))
    try:
        response = client.get("/api/properties")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal", "message": "internal server error", "details": {}}}
    assert "boom-sensitive-internal-detail" not in response.text


def test_session_dependency_logs_commit(monkeypatch, session, caplog):
    monkeypatch.setattr(api_deps, "SessionLocal", lambda: session)
    request = SimpleNamespace(
        method="POST", url=SimpleNamespace(path=f"/api/batches/{BATCH_ID}/start")
    )
    dependency = api_deps.get_session(request)
    assert next(dependency) is session

    with caplog.at_level("INFO"), pytest.raises(StopIteration):
        next(dependency)

    record = next(record for record in caplog.records
                  if record.message == "database transaction committed")
    assert record.request_method == "POST"
    assert record.request_path.endswith("/start")
    assert record.transaction_status == "committed"


# --- filter grammar -----------------------------------------------------------------

def test_translate_filters_produces_real_criteria():
    from contracts import FilterClause

    criteria = translate_filters([FilterClause(**clause) for clause in [
        {"field": "zip5", "op": "eq", "value": "94601"},
        {"field": "gut_rating", "op": "between", "value": [3, 5]},
        {"field": "city", "op": "contains", "value": "oak"},
        {"field": "state", "op": "in", "value": ["CA", "TX"]},
        {"field": "next_action", "op": "is_null", "value": True},
    ]])
    assert len(criteria) == 5
    sql = " AND ".join(str(criterion.compile(compile_kwargs={"literal_binds": True})) for criterion in criteria)
    assert "properties.zip5 = '94601'" in sql
    assert "properties.gut_rating >= 3 AND properties.gut_rating <= 5" in sql
    assert "lower(properties.city) LIKE lower('%oak%')" in sql
    assert "properties.state IN ('CA', 'TX')" in sql
    assert "properties.next_action IS NULL" in sql


def test_translate_filters_rejects_unknown_field_and_operator():
    from contracts import FilterClause

    with pytest.raises(Exception):
        translate_filters([FilterClause(field="overall; DROP TABLE properties", op="eq", value=1)])
    with pytest.raises(Exception):
        FilterClause(field="zip5", op="regex", value="9.*")  # closed operator set in the contract


def test_properties_filter_returns_matching_subset(client):
    response = client.get("/api/properties", params={"filters": filters_param(
        {"field": "state", "op": "eq", "value": "CA"})})
    assert response.status_code == 200
    items = response.json()["items"]
    assert {item["id"] for item in items} == {str(PID), str(PID2)}

    response = client.get("/api/properties", params={"filters": filters_param(
        {"field": "gut_rating", "op": "gte", "value": 3})})
    assert [item["id"] for item in response.json()["items"]] == [str(PID)]

    response = client.get("/api/properties", params={"filters": filters_param(
        {"field": "city", "op": "contains", "value": "OAK"})})
    assert [item["id"] for item in response.json()["items"]] == [str(PID)]

    response = client.get("/api/properties", params={"filters": filters_param(
        {"field": "tags", "op": "contains", "value": "watch"})})
    assert [item["id"] for item in response.json()["items"]] == [str(PID)]

    response = client.get("/api/properties", params={"filters": filters_param(
        {"field": "is_watchlisted", "op": "eq", "value": True},
        {"field": "pipeline_status", "op": "in", "value": ["analyzed", "closed"]})})
    assert [item["id"] for item in response.json()["items"]] == [str(PID3)]


def test_properties_invalid_filter_is_400(client):
    response = client.get("/api/properties", params={"filters": filters_param(
        {"field": "nope", "op": "eq", "value": 1})})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"
    assert "allowed" in response.json()["error"]["details"]


def test_filter_validate_endpoint(client):
    ok = client.post("/api/filter/validate", json=[{"field": "zip5", "op": "eq", "value": "94601"}])
    assert ok.status_code == 200 and ok.json()["valid"] is True
    bad = client.post("/api/filter/validate", json=[{"field": "bad", "op": "eq", "value": 1}])
    assert bad.status_code == 400


# --- pagination & sorting -------------------------------------------------------------

def test_cursor_pagination_walks_all_rows(client, session):
    for index in range(4, 8):
        pid = UUID(f"00000000-0000-0000-0000-00000000000{index}")
        session.add(prop(pid, f"{index} Page St", "Oakland", "CA", "94601",
                         created=datetime(2026, 1, index, tzinfo=UTC)))
    seen, cursor = [], None
    for _ in range(10):
        params = {"limit": 2, "sort": "-created_at"}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/api/properties", params=params).json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 7 and len(set(seen)) == 7  # 3 seeded + 4 added, no repeats, no skips


def test_sorting(client):
    body = client.get("/api/properties", params={"sort": "gut_rating"}).json()
    guts = [item["gut_rating"] for item in body["items"]]
    assert guts == sorted(guts, key=lambda v: (v is None, v))
    body = client.get("/api/properties", params={"sort": "-gut_rating"}).json()
    guts = [item["gut_rating"] for item in body["items"]]
    assert guts == sorted(guts, key=lambda v: (v is None, v), reverse=True)
    bad = client.get("/api/properties", params={"sort": "-no_such_column"})
    assert bad.status_code == 400


def test_malformed_cursor_is_400(client):
    assert client.get("/api/properties", params={"cursor": "!!!not-base64!!!"}).status_code == 400


def test_property_detail_has_money_envelope(client):
    body = client.get(f"/api/properties/{PID}").json()
    assert body["id"] == str(PID)
    assert body["latest_valuation"]["value"] == "500000.00"
    assert set(body["latest_valuation"]) == {"value", "confidence", "source_kind", "is_estimated", "null_reason"}
    assert body["overall_score"] == "0.83"
    assert body["open_flags"] == 1
    assert client.get(f"/api/properties/{uuid4()}").status_code == 404


def test_patch_property(client, session):
    response = client.patch(f"/api/properties/{PID}", json={"pipeline_status": "offer_out", "gut_rating": 5})
    assert response.status_code == 200
    assert session.get(dbm.Property, PID).pipeline_status == "offer_out"
    bad = client.patch(f"/api/properties/{PID}", json={"apn": "hax"})
    assert bad.status_code == 400


# --- analysis payload -----------------------------------------------------------------

def _assert_no_float_money(node, path=""):
    """Money is Decimal and must serialize as a string, never a float."""
    if isinstance(node, dict):
        for key, value in node.items():
            _assert_no_float_money(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _assert_no_float_money(value, f"{path}[{index}]")
    elif isinstance(node, float):
        assert "confidence" in path, f"float leaked at {path}: {node}"


def test_analysis_returns_persisted_payload(client):
    body = client.get(f"/api/properties/{PID}/analysis").json()
    assert body["property_id"] == str(PID) and body["scenario"] == "expected"

    normalized = body["normalized"]
    assert normalized["address"]["line1"] == "100 Main St"
    assert normalized["valuation_candidates"][0]["value"]["value"] == "500000"

    assert body["underwriting"] is not None
    assert body["underwriting"]["value"]["v_expected"] is not None

    strategies = {(s["strategy"], s["scenario"]): s for s in body["strategies"]}
    assert strategies[("cash", "expected")]["mao"] == "300000.00"
    assert strategies[("flip", "conservative")]["status"] == "not_viable"

    points = body["offers"]["points"]
    assert [p["offer_price"] for p in points] == ["300000.00", "350000.00"]
    assert points[0]["is_short_sale"] is True
    assert isinstance(points[0]["proceeds_expected"], str)

    assert body["scores"]["overall"] == "0.83"
    assert body["scores"]["components"] == {"base_equity": "0.4"}

    assert body["flags"][0]["flag_type"] == "lien_attachment"
    assert body["flags"][0]["financial_impact_usd"] == "12000.00"

    timeline_dates = [event["event_date"] for event in body["timeline"]]
    assert timeline_dates == sorted(d for d in timeline_dates if d)
    assert {event["event_type"] for event in body["timeline"]} == {"foreclosure", "lien", "change"}

    _assert_no_float_money(body)


def test_analysis_404_for_unknown_property(client):
    assert client.get(f"/api/properties/{uuid4()}/analysis").status_code == 404


def test_analysis_empty_property_returns_null_sections(client, session):
    pid = UUID("00000000-0000-0000-0000-0000000000c9")
    session.add(prop(pid, "1 Empty Lot", "Oakland", "CA", "94601"))
    body = client.get(f"/api/properties/{pid}/analysis").json()
    assert body["normalized"] is None and body["underwriting"] is None
    assert body["strategies"] == [] and body["offers"] is None and body["scores"] is None
    assert body["flags"] == [] and body["timeline"] == []


def test_offer_endpoint_computes_authoritative_point(client):
    body = client.post(f"/api/properties/{PID}/offers",
                       json={"offer_price": "320000", "scenario": "expected"}).json()
    assert body["offer_price"] == "320000"
    assert isinstance(body["proceeds_expected"], str)
    assert isinstance(body["closing_costs"], str)
    _assert_no_float_money(body)


def test_offer_endpoint_without_underwriting_is_400(client):
    response = client.post(f"/api/properties/{PID2}/offers", json={"offer_price": "100000"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"


def test_evidence_endpoint(client, session):
    winning = next(f for f in session.rows[dbm.ExtractedFact]
                   if f.field_path == "property.sqft")
    session.add(dbm.FieldResolution(id=uuid4(), property_id=PID, field_path="property.sqft",
                                    winning_fact_id=winning.id, method="single_source",
                                    verification_state="unverified"))
    body = client.get(f"/api/properties/{PID}/evidence/property.sqft").json()
    assert body["field_path"] == "property.sqft"
    assert body["resolution"]["method"] == "single_source"
    assert body["candidates"][0]["is_winner"] is True
    assert body["candidates"][0]["snippet"] == "verbatim snippet"
    assert body["candidates"][0]["page_number"] == 1


def test_reports_endpoint(client, session):
    session.add(dbm.Report(id=uuid4(), property_id=PID, report_type="title", vendor="firstam",
                           file_path="/tmp/x.pdf", sha256="a" * 64, status="ready",
                           page_count=12, created_at=NOW))
    body = client.get(f"/api/properties/{PID}/reports").json()
    assert body["items"][0]["report_type"] == "title"
    assert body["items"][0]["status"] == "ready"


# --- flags ----------------------------------------------------------------------------

def test_flags_list_and_resolve(client, session, queue):
    body = client.get("/api/flags").json()
    assert len(body["items"]) == 1
    flag_id = body["items"][0]["id"]

    response = client.post(f"/api/flags/{flag_id}/resolve",
                           json={"resolution": "approve", "note": "verified against recorded doc"})
    assert response.status_code == 200
    assert response.json()["flag"]["status"] == "resolved"
    assert response.json()["flag"]["resolution"] == "approve"
    assert queue.jobs[-1]["name"] == "recompute_property"

    again = client.post(f"/api/flags/{flag_id}/resolve", json={"resolution": "dismiss"})
    assert again.status_code == 409

    open_after = client.get("/api/flags").json()
    assert open_after["items"] == []
    assert client.post(f"/api/flags/{uuid4()}/resolve", json={"resolution": "approve"}).status_code == 404


# --- notes, saved views -----------------------------------------------------------------

def test_notes_roundtrip(client):
    created = client.post(f"/api/properties/{PID}/notes", json={"body": "called agent, motivated"})
    assert created.status_code == 200
    body = client.get(f"/api/properties/{PID}/notes").json()
    assert body["items"][0]["body"] == "called agent, motivated"


def test_saved_views_roundtrip_and_validation(client):
    created = client.post("/api/saved-views", json={
        "name": "CA watchlist", "filters": [{"field": "state", "op": "eq", "value": "CA"}],
        "columns": {"order": ["address", "score"]}})
    assert created.status_code == 200
    view_id = created.json()["id"]

    listed = client.get("/api/saved-views").json()["items"]
    assert listed[0]["name"] == "CA watchlist"
    assert listed[0]["filters"][0]["field"] == "state"

    bad = client.post("/api/saved-views", json={"name": "bad", "filters": [{"field": "nope", "op": "eq"}]})
    assert bad.status_code == 400

    assert client.delete(f"/api/saved-views/{view_id}").json()["deleted"] == view_id
    assert client.get("/api/saved-views").json()["items"] == []


# --- batches ----------------------------------------------------------------------------

def test_batch_estimate_and_start(client, session, queue, caplog):
    session.add(dbm.Batch(id=BATCH_ID, name="jan", file_count=2, total_count=2, status="uploaded"))
    report1 = dbm.Report(id=uuid4(), batch_id=BATCH_ID, file_path="/a.pdf", sha256="1" * 64, status="uploaded")
    report2 = dbm.Report(id=uuid4(), batch_id=BATCH_ID, file_path="/b.pdf", sha256="2" * 64, status="uploaded")
    session.add(report1)
    session.add(report2)
    session.add(dbm.ExtractionUnit(id=uuid4(), report_id=report1.id, unit_type="lien",
                                   page_start=1, page_end=5, token_estimate=10000, status="queued"))
    session.add(dbm.ExtractionUnit(id=uuid4(), report_id=report2.id, unit_type="mortgages",
                                   page_start=6, page_end=9, token_estimate=4000, status="queued"))

    estimate = client.post(f"/api/batches/{BATCH_ID}/estimate").json()
    assert estimate["report_count"] == 2
    assert estimate["total_tokens"] == 14000
    assert estimate["estimated_cost_usd"] == "0.04"  # 14k tokens * $0.003/1k, quantized to cents
    assert estimate["awaiting_confirmation"] is True
    assert session.get(dbm.Batch, BATCH_ID).status == "awaiting_confirmation"

    with caplog.at_level("INFO"):
        started = client.post(f"/api/batches/{BATCH_ID}/start").json()
    assert started["status"] == "running"
    assert started["awaiting_confirmation"] is False
    assert [job["name"] for job in queue.jobs] == ["extract_unit", "extract_unit"]
    records = [record for record in caplog.records if record.name == "api.routes_portfolio"]
    assert "batch start received" in [record.message for record in records]
    eligible = next(record for record in records
                    if record.message == "batch eligible extraction units")
    assert eligible.batch_id == BATCH_ID
    assert eligible.eligible_units == 2
    inserted = [record for record in records
                if record.message == "batch extraction job inserted"]
    assert len(inserted) == 2
    assert all(record.job_name == "extract_unit" for record in inserted)
    assert all(record.job_status == "queued" for record in inserted)
    staged = next(record for record in records
                  if record.message == "batch start transaction staged")
    assert staged.queued_jobs == 2

    status = client.get(f"/api/batches/{BATCH_ID}").json()
    assert status["status"] == "running" and status["total"] == 2
    assert client.post(f"/api/batches/{uuid4()}/estimate").status_code == 404


def test_batch_start_rejects_zero_eligible_units(client, session, queue):
    session.add(dbm.Batch(
        id=BATCH_ID, name="empty", file_count=1, total_count=1,
        status="awaiting_confirmation", awaiting_confirmation=True,
    ))
    session.add(dbm.Report(
        id=uuid4(), batch_id=BATCH_ID, file_path="/classified.pdf",
        sha256="a" * 64, status="classified",
    ))

    response = client.post(f"/api/batches/{BATCH_ID}/start")

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {
        "report_count": 1,
        "report_statuses": {"classified": 1},
        "unit_count": 0,
        "unit_statuses": {},
    }
    assert queue.jobs == []
    batch = session.get(dbm.Batch, BATCH_ID)
    assert batch.status == "awaiting_confirmation"
    assert batch.awaiting_confirmation is True


def test_batch_start_logs_exact_nonqueued_statuses(client, session, queue, caplog):
    session.add(dbm.Batch(
        id=BATCH_ID, name="nonqueued", file_count=1, total_count=1,
        status="awaiting_confirmation", awaiting_confirmation=True,
    ))
    report = dbm.Report(
        id=uuid4(), batch_id=BATCH_ID, file_path="/classified.pdf",
        sha256="b" * 64, status="classified",
    )
    unit = dbm.ExtractionUnit(
        id=uuid4(), report_id=report.id, unit_type="combined",
        page_start=1, page_end=1, status="extracted",
    )
    session.add(report)
    session.add(unit)

    with caplog.at_level("INFO"):
        response = client.post(f"/api/batches/{BATCH_ID}/start")

    assert response.status_code == 409
    eligible = next(record for record in caplog.records
                    if getattr(record, "event", None) == "extraction_units_eligible")
    assert eligible.eligible_unit_count == 0
    assert eligible.unit_ids == []
    assert eligible.unit_statuses == {"extracted": 1}
    assert eligible.excluded_unit_statuses == {"extracted": 1}
    rejected = next(record for record in caplog.records
                    if getattr(record, "event", None) == "batch_start_rejected")
    assert rejected.unit_ids == [str(unit.id)]
    assert rejected.excluded_unit_statuses == {"extracted": 1}


def test_batch_get_serializes_persisted_post_ingestion_status(client, session, caplog):
    routes_portfolio._BATCH_STATUS_LOG_STATE.clear()
    session.add(dbm.Batch(
        id=BATCH_ID, name="ready", file_count=1, total_count=1,
        completed_count=0, failed_count=0, status="uploaded",
    ))

    with caplog.at_level("INFO"):
        response = client.get(f"/api/batches/{BATCH_ID}")
        repeated = client.get(f"/api/batches/{BATCH_ID}")

    assert response.status_code == 200
    assert repeated.status_code == 200
    assert response.json()["status"] == "uploaded"
    records = [record for record in caplog.records if record.message == "batch status read"]
    assert len(records) == 1
    record = records[0]
    assert record.event == "batch_status_returned"
    assert record.batch_id == BATCH_ID
    assert record.batch_status_after == "uploaded"
    assert record.report_count == 0


# --- merge / quick-add / recompute / facts ------------------------------------------------

def test_merge_unmerge_quick_add(client, session):
    merged = client.post("/api/properties/merge", json={"source_id": str(PID2), "target_id": str(PID)})
    assert merged.status_code == 200
    assert session.get(dbm.Property, PID2).merged_into_id == PID
    # Merged property disappears from the list.
    assert all(item["id"] != str(PID2) for item in client.get("/api/properties").json()["items"])
    unmerged = client.post("/api/properties/unmerge", json={"source_id": str(PID2)})
    assert unmerged.status_code == 200
    assert session.get(dbm.Property, PID2).merged_into_id is None

    added = client.post("/api/properties/quick-add",
                        json={"address_line1": "9 New Ct", "city": "Oakland", "state": "CA", "zip5": "94601"})
    assert added.status_code == 200
    assert session.get(dbm.Property, UUID(added.json()["id"])).address_line1 == "9 New Ct"
    assert client.post("/api/properties/quick-add", json={"city": "x"}).status_code == 400


def test_recompute_enqueues_job(client, queue):
    body = client.post(f"/api/properties/{PID}/recompute").json()
    assert body["enqueued"] is True
    assert queue.jobs[-1]["name"] == "recompute_property"
    assert queue.jobs[-1]["payload"]["property_id"] == str(PID)


def test_add_human_fact(client, session, queue):
    body = client.post(f"/api/properties/{PID}/facts", json={
        "report_id": str(uuid4()), "extraction_unit_id": str(uuid4()), "entity_type": "property",
        "entity_local_id": "p1", "field_path": "property.sqft", "value_raw": "1900",
        "value_parsed": "1900", "page_number": 1, "snippet": "agent said 1900 sqft",
        "extraction_confidence": 1.0}).json()
    fact = next(f for f in session.rows[dbm.ExtractedFact] if str(f.id) == body["id"])
    assert fact.source_kind == "human"
    assert queue.jobs[-1]["name"] == "recompute_property"


# --- exports ------------------------------------------------------------------------------

def test_csv_export_applies_filters(client):
    response = client.get("/api/exports/csv", params={
        "filters": filters_param({"field": "state", "op": "eq", "value": "CA"}),
        "columns": "id,address_line1,city,state"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    lines = [line for line in response.text.strip().splitlines()]
    assert lines[0] == "id,address_line1,city,state"
    assert len(lines) == 3  # header + two CA rows
    assert "Austin" not in response.text


def test_deal_sheet_and_net_sheet(client):
    response = client.post(f"/api/properties/{PID}/exports/deal-sheet")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "100 Main St" in response.text

    net = client.post(f"/api/properties/{PID}/exports/net-sheet",
                      json={"offer_price": "320000", "scenario": "expected"})
    assert net.status_code == 200 and net.headers["content-type"].startswith("text/html")

    missing = client.post(f"/api/properties/{PID2}/exports/net-sheet", json={"offer_price": "100000"})
    assert missing.status_code == 400


# --- portfolio views ------------------------------------------------------------------------

def test_rankings_dashboard_problems_changes(client, session):
    ranked_at = datetime(2026, 2, 1, tzinfo=UTC)
    session.add(dbm.Ranking(id=uuid4(), scope_type="portfolio", property_id=PID, rank=1, score=Decimal("0.83"), ranked_at=ranked_at))
    session.add(dbm.Ranking(id=uuid4(), scope_type="portfolio", property_id=PID2, rank=2, score=Decimal("0.5"), ranked_at=ranked_at))
    session.add(dbm.Ranking(id=uuid4(), scope_type="portfolio", property_id=PID2, rank=9, score=Decimal("0.4"),
                            ranked_at=datetime(2026, 1, 1, tzinfo=UTC)))
    body = client.get("/api/rankings").json()
    assert [item["property_id"] for item in body["items"]] == [str(PID), str(PID2)]
    assert body["items"][0]["score"] == "0.83"

    dash = client.get("/api/dashboard").json()
    assert dash["total_properties"] == 3
    assert dash["by_status"] == {"new": 2, "analyzed": 1}
    assert dash["open_flags"] == 1
    assert dash["missing_valuation_count"] == 2  # only PID has a valuation — exclusion is visible

    session.add(dbm.Flag(id=uuid4(), property_id=PID, flag_type="identity_conflict",
                         payload={}, status="open", dedupe_key="identity-conflict:x"))
    session.add(dbm.Report(id=uuid4(), batch_id=None, file_path="/bad.pdf", sha256="f" * 64,
                           status="failed", failure_reason="encrypted"))
    problems = client.get("/api/problems").json()
    assert [f["flag_type"] for f in problems["gating_flags"]] == ["identity_conflict"]
    assert problems["failed_reports"][0]["failure_reason"] == "encrypted"

    changes = client.get("/api/changes").json()
    assert changes["items"][0]["change_type"] == "value_change"


def test_assumption_sets(client):
    listed = client.get("/api/assumption-sets").json()["items"]
    assert listed[0]["name"] == "default"

    created = client.post("/api/assumption-sets", json={"name": "aggressive", "params": DEFAULT_PARAMS})
    assert created.status_code == 200
    bad = client.post("/api/assumption-sets", json={"name": "broken", "params": {"acquisition": {}}})
    assert bad.status_code == 400

    preview = client.post("/api/assumption-sets/preview",
                          json={"params": DEFAULT_PARAMS, "property_id": str(PID)})
    assert preview.status_code == 200
    assert preview.json()["valid"] is True
    assert preview.json()["underwriting"]["property_id"] == str(PID)


def test_realized_deal(client, session):
    body = client.post("/api/realized-deals", json={
        "property_id": str(PID), "purchase_price": "300000", "sale_price": "455000",
        "outcome": "sold", "notes": "smooth"}).json()
    assert body["purchase_price"] == "300000"
    assert client.post("/api/realized-deals", json={"property_id": str(uuid4())}).status_code == 404


# --- uploads / paste --------------------------------------------------------------------------

def test_upload_and_paste(client, session, queue, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "document_root", tmp_path)
    upload = client.post("/api/uploads",
                         files=[("files", ("a.pdf", b"%PDF-1.4 fake", "application/pdf"))],
                         data={"batch_name": "jan"})
    assert upload.status_code == 200
    assert upload.json()["count"] == 1
    assert queue.jobs[-1]["name"] == "ingest_document"
    assert (tmp_path / upload.json()["report_ids"][0] / "original.pdf").exists()

    bad = client.post("/api/uploads", files=[("files", ("x.pdf", b"not a pdf", "application/pdf"))])
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "not_pdf"

    pasted = client.post("/api/ingest/paste", json={"text": "Grant deed ... lot 12"})
    assert pasted.status_code == 200
    assert pasted.json()["count"] == 1
    assert client.post("/api/ingest/paste", json={"text": " "}).status_code == 400


# --- read-only enforcement (WP-11 AC #5) --------------------------------------------------------

@pytest.mark.parametrize("method,url,kwargs", [
    ("patch", f"/api/properties/{PID}", {"json": {"gut_rating": 3}}),
    ("post", f"/api/properties/{PID}/notes", {"json": {"body": "x"}}),
    ("post", f"/api/properties/{PID}/recompute", {}),
    ("post", f"/api/properties/{PID}/facts", {"json": {
        "report_id": str(uuid4()), "extraction_unit_id": str(uuid4()), "entity_type": "property",
        "entity_local_id": "p1", "field_path": "property.sqft", "page_number": 1,
        "snippet": "s", "extraction_confidence": 1.0}}),
    ("post", "/api/properties/merge", {"json": {"source_id": str(PID2), "target_id": str(PID)}}),
    ("post", "/api/properties/unmerge", {"json": {"source_id": str(PID2)}}),
    ("post", "/api/properties/quick-add", {"json": {"address_line1": "1 St"}}),
    ("post", "/api/saved-views", {"json": {"name": "v"}}),
    ("delete", f"/api/saved-views/{uuid4()}", {}),
    ("post", f"/api/batches/{uuid4()}/start", {}),
    ("post", "/api/assumption-sets", {"json": {"name": "x", "params": DEFAULT_PARAMS}}),
    ("post", "/api/realized-deals", {"json": {"property_id": str(PID)}}),
    ("post", "/api/uploads", {"files": [("files", ("a.pdf", b"%PDF-1.4 x", "application/pdf"))]}),
    ("post", "/api/ingest/paste", {"json": {"text": "deed"}}),
])
def test_read_only_user_cannot_mutate(readonly_client, method, url, kwargs):
    response = readonly_client.request(method, url, **kwargs)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "read_only"


def test_read_only_user_can_read(readonly_client):
    assert readonly_client.get("/api/properties").status_code == 200
    assert readonly_client.get(f"/api/properties/{PID}/analysis").status_code == 200
    assert readonly_client.post(f"/api/properties/{PID}/offers",
                                json={"offer_price": "300000"}).status_code == 200


def test_flag_resolve_requires_write(readonly_client):
    flag_id = readonly_client.get("/api/flags").json()["items"][0]["id"]
    response = readonly_client.post(f"/api/flags/{flag_id}/resolve", json={"resolution": "approve"})
    assert response.status_code == 403


# --- SPA ---------------------------------------------------------------------------------------

def test_spa_serving(client):
    from pathlib import Path

    index = client.get("/")
    assert index.status_code == 200 and "text/html" in index.headers["content-type"]
    deep = client.get("/properties/some-id")
    assert deep.status_code == 200 and "text/html" in deep.headers["content-type"]
    assets = sorted((Path(__file__).parent.parent / "web" / "dist" / "assets").glob("*")
                    if (Path(__file__).parent.parent / "web" / "dist" / "assets").exists() else [])
    if not assets:
        pytest.skip("web/dist is absent; run the frontend build before SPA asset verification")
    asset = client.get(f"/assets/{assets[0].name}")
    assert asset.status_code == 200
    missing_api = client.get("/api/definitely-not-a-route")
    assert missing_api.status_code == 404
    assert missing_api.json()["error"]["code"] == "not_found"
