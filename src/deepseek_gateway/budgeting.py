from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class BudgetStatus:
    spent_usd: float
    limit_usd: float

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.limit_usd > 0


def month_key(now: datetime | None = None) -> str:
    current = now or datetime.now()
    return current.strftime("%Y-%m")
