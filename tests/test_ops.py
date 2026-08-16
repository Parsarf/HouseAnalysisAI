from decimal import Decimal
from uuid import uuid4

from ops import Budget, BudgetDecision, reserve_budget


def test_reserve_within_limit_is_allowed():
    budget = Budget(Decimal("10.00"))
    decision = budget.check_and_reserve(Decimal("4.00"))
    assert decision == BudgetDecision(True, Decimal("6.00"))
    assert budget.reserved == Decimal("4.00")


def test_reserve_exact_limit_is_allowed():
    budget = Budget(Decimal("10.00"))
    assert budget.check_and_reserve(Decimal("10.00")).allowed
    assert budget.check_and_reserve(Decimal("0.01")).allowed is False


def test_exceeding_limit_pauses_without_overspending():
    budget = Budget(Decimal("10.00"))
    budget.check_and_reserve(Decimal("8.00"))
    decision = budget.check_and_reserve(Decimal("3.00"))
    assert decision.allowed is False
    assert decision.remaining == Decimal("2.00")
    assert decision.reason == "budget_exceeded"
    # the rejected reservation must not consume budget
    assert budget.reserved == Decimal("8.00")


def test_release_returns_unused_reservation():
    budget = Budget(Decimal("10.00"))
    budget.check_and_reserve(Decimal("5.00"))
    budget.release(actual_cost=Decimal("3.50"), reserved_cost=Decimal("5.00"))
    assert budget.reserved == Decimal("3.50")
    assert budget.check_and_reserve(Decimal("6.50")).allowed


def test_release_never_increases_reservation():
    budget = Budget(Decimal("10.00"))
    budget.check_and_reserve(Decimal("5.00"))
    budget.release(actual_cost=Decimal("7.00"), reserved_cost=Decimal("5.00"))
    assert budget.reserved == Decimal("5.00")


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeSession:
    def __init__(self, row):
        self._row = row
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return _FakeResult(self._row)


def test_db_reserve_budget_success():
    batch_id = uuid4()
    session = _FakeSession(row=(batch_id,))
    assert reserve_budget(session, batch_id, Decimal("0.25")) is True
    (sql, params), = session.calls
    assert "UPDATE batches" in sql
    assert "budget_limit_usd" in sql
    assert "RETURNING id" in sql
    assert params == {"batch_id": batch_id, "estimate": Decimal("0.25")}


def test_db_reserve_budget_rejected_when_over_limit():
    session = _FakeSession(row=None)
    assert reserve_budget(session, uuid4(), Decimal("0.25")) is False
