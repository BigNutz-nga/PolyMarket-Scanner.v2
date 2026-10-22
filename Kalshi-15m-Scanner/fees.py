"""Kalshi trading-fee arithmetic.

General trading fee from the Kalshi fee schedule::

    fee = round_up(M * 0.07 * C * P * (1 - P))

where ``C`` is the contract count, ``P`` the fill price in dollars and ``M`` the
series ``fee_multiplier`` (returned by ``GET /series/{ticker}``). Series whose
``fee_type`` includes maker fees charge resting orders a fraction of the same
formula. Kalshi charges no settlement fee, so holding to expiry pays one fee.

Rounding defaults to the next whole cent, which is the conservative reading of
the schedule; ``centicent`` and ``none`` are available for sensitivity checks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TAKER_COEFFICIENT = 0.07
MAKER_FRACTIONS = {
    "quadratic": 0.0,
    "quadratic_with_maker_fees": 0.25,
    "quadratic_with_combo_maker_fees": 0.5,
}
ROUNDING_STEPS = {"cent": 0.01, "centicent": 0.0001, "none": 0.0}


def round_up(amount: float, step: float) -> float:
    if step <= 0:
        return amount
    # The epsilon keeps float noise on exact multiples (0.02 -> 2.0000000000000004 cents) from adding a step.
    return math.ceil(amount / step - 1e-9) * step


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    fee_type: str = "quadratic"
    multiplier: float = 1.0
    rounding: str = "cent"

    def __post_init__(self) -> None:
        if self.fee_type not in MAKER_FRACTIONS:
            raise ValueError(
                f"Unsupported fee_type {self.fee_type!r}; only quadratic fee schedules are modeled"
            )
        if self.rounding not in ROUNDING_STEPS:
            raise ValueError("rounding must be cent, centicent, or none")
        if self.multiplier < 0:
            raise ValueError("fee multiplier cannot be negative")

    def _fee(self, price: float, contracts: float, fraction: float) -> float:
        if contracts <= 0 or fraction <= 0 or self.multiplier == 0:
            return 0.0
        price = min(1.0, max(0.0, price))
        raw = fraction * self.multiplier * TAKER_COEFFICIENT * contracts * price * (1.0 - price)
        return round(round_up(raw, ROUNDING_STEPS[self.rounding]), 6)

    def taker_fee(self, price: float, contracts: float = 1) -> float:
        return self._fee(price, contracts, 1.0)

    def maker_fee(self, price: float, contracts: float = 1) -> float:
        return self._fee(price, contracts, MAKER_FRACTIONS[self.fee_type])

    def taker_fee_for_levels(self, fills: list[tuple[float, float]]) -> float:
        """Fee for a fill that sweeps several price levels, rounded per level (conservative)."""
        return round(sum(self.taker_fee(price, size) for price, size in fills), 6)


def round_trip(
    entry_price: float,
    exit_price: float,
    contracts: float = 1,
    schedule: FeeSchedule | None = None,
) -> dict[str, float]:
    """Net result of buying and later selling the same side, paying taker fees both ways."""
    schedule = schedule or FeeSchedule()
    entry_fee = schedule.taker_fee(entry_price, contracts)
    exit_fee = schedule.taker_fee(exit_price, contracts)
    gross = (exit_price - entry_price) * contracts
    return {
        "gross": round(gross, 6),
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "net": round(gross - entry_fee - exit_fee, 6),
    }


def hold_to_settlement(
    entry_price: float,
    won: bool,
    contracts: float = 1,
    schedule: FeeSchedule | None = None,
) -> dict[str, float]:
    """Net result of buying and holding to settlement (no settlement fee)."""
    schedule = schedule or FeeSchedule()
    entry_fee = schedule.taker_fee(entry_price, contracts)
    payout = contracts if won else 0.0
    return {
        "gross": round(payout - entry_price * contracts, 6),
        "entry_fee": entry_fee,
        "net": round(payout - entry_price * contracts - entry_fee, 6),
    }
