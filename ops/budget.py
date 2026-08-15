from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    remaining: Decimal
    reason: str | None = None


class Budget:
    def __init__(self, limit: Decimal):
        self.limit = limit
        self.reserved = Decimal("0")

    def check_and_reserve(self, estimated_cost: Decimal) -> BudgetDecision:
        remaining = self.limit - self.reserved
        if estimated_cost > remaining:
            return BudgetDecision(False, remaining, "budget_exceeded")
        self.reserved += estimated_cost
        return BudgetDecision(True, self.limit - self.reserved)

    def release(self, actual_cost: Decimal, reserved_cost: Decimal) -> None:
        self.reserved -= max(Decimal("0"), reserved_cost - actual_cost)
