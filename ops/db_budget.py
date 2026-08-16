from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


def reserve_budget(session: Session, batch_id: UUID, estimate: Decimal) -> bool:
    row = session.execute(text("""
        UPDATE batches
        SET spent_usd = COALESCE(spent_usd, 0) + :estimate,
            status = CASE WHEN COALESCE(spent_usd, 0) + :estimate > budget_limit_usd THEN 'paused_budget' ELSE status END
        WHERE id = :batch_id
          AND (budget_limit_usd IS NULL OR COALESCE(spent_usd, 0) + :estimate <= budget_limit_usd)
        RETURNING id
    """), {"batch_id": batch_id, "estimate": estimate}).first()
    return row is not None
