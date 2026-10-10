"""Cost per call = voice-platform cost reported at end of call (USD → INR, or INR)
+ telephony per minute + a share of fixed monthly costs (hosting, number rental,
subscriptions)."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings


def variable_cost_inr(
    platform_cost_usd: float | None,
    duration_seconds: float | None,
    platform_cost_inr: float | None = None,
    settings: Settings | None = None,
) -> float:
    s = settings or get_settings()
    platform = (platform_cost_usd or 0.0) * s.usd_inr_rate + (platform_cost_inr or 0.0)
    telephony = (duration_seconds or 0.0) / 60 * s.telephony_cost_inr_per_min
    return round(platform + telephony, 2)


@dataclass(frozen=True)
class CostSummary:
    calls: int
    variable_inr: float
    fixed_inr: float

    @property
    def total_inr(self) -> float:
        return round(self.variable_inr + self.fixed_inr, 2)

    @property
    def per_call_inr(self) -> float | None:
        return round(self.total_inr / self.calls, 2) if self.calls else None

    def per(self, n: int) -> float | None:
        return round(self.total_inr / n, 2) if n else None


def summarise(variable_costs: list[float], months: float, settings: Settings | None = None) -> CostSummary:
    """`months` is the length of the reporting window in months (30 days = 1.0)."""
    s = settings or get_settings()
    return CostSummary(
        calls=len(variable_costs),
        variable_inr=round(sum(variable_costs), 2),
        fixed_inr=round(s.fixed_monthly_cost_inr * months, 2),
    )
