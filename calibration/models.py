"""Data model for the calibration loop (spec §22, WP-17).

``RealizedDeal`` mirrors the ``realized_deals`` table (actuals entered when a
deal closes or dies) plus a ``PredictionSnapshot`` — the predictions the
platform made at analysis time, captured so predicted-vs-actual analyses can
join without re-running the engines. All money is Decimal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4


class DealOutcome(StrEnum):
    """Closed set of realized_deals.outcome values."""

    SOLD = "sold"            # closed and resold
    BREAKEVEN = "breakeven"  # resold at ~zero profit
    LOSS = "loss"            # resold at a loss
    DEAD = "dead"            # died before closing


@dataclass(frozen=True)
class PredictionSnapshot:
    """What the platform predicted for the property at analysis time."""

    condition: str | None = None                      # pristine|cosmetic|moderate|heavy|gut
    sqft: Decimal | None = None
    predicted_repairs: Decimal | None = None
    predicted_sale_price: Decimal | None = None
    predicted_holding_days: int | None = None
    valuation_predictions: dict[str, Decimal] = field(default_factory=dict)  # candidate type -> value
    overall_score: Decimal | None = None
    gut_rating: int | None = None                     # 1-5 from keyboard triage (spec §11.4)
    pillar_scores: dict[str, Decimal] = field(default_factory=dict)  # fos/distress/dcs/risk


@dataclass(frozen=True)
class RealizedDeal:
    """One closed (or dead) deal: the actuals plus the prediction snapshot."""

    property_id: UUID
    purchase_price: Decimal | None = None
    actual_repairs: Decimal | None = None
    actual_holding_days: int | None = None
    sale_price: Decimal | None = None
    actual_costs: Decimal | None = None
    outcome: str = DealOutcome.SOLD
    notes: str | None = None
    closed_at: date | None = None
    snapshot: PredictionSnapshot = field(default_factory=PredictionSnapshot)
    id: UUID = field(default_factory=uuid4)

    @property
    def realized_profit(self) -> Decimal | None:
        """sale − purchase − repairs − costs. None for dead deals or missing data."""
        if self.outcome == DealOutcome.DEAD:
            return None
        if None in (self.sale_price, self.purchase_price, self.actual_repairs, self.actual_costs):
            return None
        sale, purchase, repairs, costs = self.sale_price, self.purchase_price, self.actual_repairs, self.actual_costs
        assert sale is not None and purchase is not None and repairs is not None and costs is not None
        return sale - purchase - repairs - costs

    @property
    def is_hit(self) -> bool:
        """A deal the model should have ranked well: closed with profit > 0."""
        profit = self.realized_profit
        return profit is not None and profit > 0
