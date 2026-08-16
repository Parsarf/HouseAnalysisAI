"""Outcome recording for the calibration loop (spec §22, WP-17).

``CalibrationStore`` is the persistence seam: production backs it with the
``realized_deals`` table, tests use ``InMemoryCalibrationStore``. Recording
is append-only; analyses read the full list.
"""
from __future__ import annotations

from typing import Protocol
from uuid import UUID

from .models import RealizedDeal


class CalibrationStore(Protocol):
    """Persistence seam for realized deals."""

    def record(self, deal: RealizedDeal) -> UUID: ...

    def list(self) -> list[RealizedDeal]: ...


class InMemoryCalibrationStore:
    """Offline store used by tests and previews."""

    def __init__(self) -> None:
        self._deals: list[RealizedDeal] = []

    def record(self, deal: RealizedDeal) -> UUID:
        self._deals.append(deal)
        return deal.id

    def list(self) -> list[RealizedDeal]:
        return list(self._deals)
