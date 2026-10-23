"""Entry filter, sizing, exit rule, and risk limits for the paper strategy.

The strategy only buys a side when a conservative model probability exceeds the
worst price it is willing to pay plus that price's taker fee by ``min_edge``:

    edge = (p_side - model_margin) - limit_price - taker_fee(limit_price, 1)

``limit_price`` is the best ask plus ``max_slippage``. Fills are simulated
later, after ``latency_seconds``, against whatever the book shows then.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from fees import FeeSchedule
from kalshi_api import OrderBook
from model import (
    SETTLEMENT_WINDOW_SECONDS,
    SUPPORTED_STRIKE_TYPES,
    IndexState,
    annual_to_per_second,
    per_second_to_annual,
    probability_yes,
    settlement_distribution,
)

SIDES = ("yes", "no")


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    # Edge requirements
    model_margin: float = 0.03
    min_edge: float = 0.02
    max_slippage: float = 0.01
    min_entry_price: float = 0.10  # the normal model is least reliable in the tails
    max_entry_price: float = 0.90
    # Timing
    min_seconds_to_close: float = 45.0
    max_seconds_to_close: float = 840.0
    max_index_age_seconds: float = 5.0
    latency_seconds: float = 1.0
    max_fill_wait_seconds: float = 5.0
    # Volatility model
    min_annual_vol: float = 0.20
    vol_multiplier: float = 1.0
    vol_lookback_seconds: float = 1800.0
    vol_sample_seconds: float = 10.0
    min_vol_samples: int = 30
    # Exit rule (early-exit policy only): sell when bid - fee >= p_side + exit_buffer
    exit_buffer: float = 0.0
    # Risk limits
    max_loss_per_position: float = 2.0
    daily_loss_stop: float = 3.0
    day_timezone: str = "UTC"

    def validate(self) -> None:
        for name in ("model_margin", "max_slippage", "latency_seconds", "max_fill_wait_seconds", "min_annual_vol"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        if not 0.0 < self.min_entry_price < self.max_entry_price < 1.0:
            raise ValueError("entry price bounds must satisfy 0 < min < max < 1")
        if self.min_seconds_to_close >= self.max_seconds_to_close:
            raise ValueError("min_seconds_to_close must be below max_seconds_to_close")
        if self.max_loss_per_position <= 0 or self.daily_loss_stop <= 0:
            raise ValueError("risk limits must be positive")
        if self.vol_multiplier <= 0:
            raise ValueError("vol_multiplier must be positive")

    def fingerprint(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:12]


@dataclass(slots=True)
class ModelView:
    p_yes: float | None
    reason: str
    sigma_annual: float | None = None
    mean: float | None = None
    sd: float | None = None


@dataclass(slots=True)
class Evaluation:
    ticker: str
    ts: float
    seconds_to_close: float
    action: str  # enter | skip
    reason: str
    p_yes: float | None = None
    sigma_annual: float | None = None
    yes_bid: float | None = None
    yes_ask: float | None = None
    side: str | None = None
    best_ask: float | None = None
    limit_price: float | None = None
    contracts: int = 0
    edge: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def model_view(market: Mapping[str, Any], index: IndexState, ts: float, config: StrategyConfig) -> ModelView:
    """Model P(YES) for ``market`` using only index data recorded at or before ``ts``."""
    strike_type = str(market.get("strike_type") or "")
    if strike_type not in SUPPORTED_STRIKE_TYPES:
        return ModelView(None, "unsupported_strike_type")
    if market.get("floor_strike") is None and market.get("cap_strike") is None:
        return ModelView(None, "missing_strike")
    if index.last_value is None or index.last_recorded_ms is None:
        return ModelView(None, "no_index_data")
    if ts - index.last_recorded_ms / 1000.0 > config.max_index_age_seconds:
        return ModelView(None, "stale_index")
    if index.volatility.sample_count < config.min_vol_samples:
        return ModelView(None, "insufficient_vol_history")
    measured = index.volatility.sigma() or 0.0
    sigma = max(measured, annual_to_per_second(config.min_annual_vol)) * config.vol_multiplier

    close_ts = float(market["close_ts"])
    seconds_to_close = close_ts - ts
    window = None
    if seconds_to_close < SETTLEMENT_WINDOW_SECONDS:
        window = index.window_for(close_ts)
        window_open_age = SETTLEMENT_WINDOW_SECONDS - seconds_to_close
        if window is None and window_open_age > config.max_index_age_seconds:
            return ModelView(None, "missing_window_average")
    mean, sd = settlement_distribution(
        spot=index.last_value, sigma=sigma, seconds_to_close=seconds_to_close, window=window
    )
    p_yes = probability_yes(
        strike_type=strike_type,
        floor_strike=market.get("floor_strike"),
        cap_strike=market.get("cap_strike"),
        mean=mean,
        sd=sd,
    )
    if p_yes is None:
        return ModelView(None, "missing_strike")
    return ModelView(p_yes, "ok", per_second_to_annual(sigma), mean, sd)


def contracts_for_budget(
    asks: tuple[tuple[float, float], ...], limit_price: float, budget: float, fees: FeeSchedule
) -> int:
    """Whole contracts whose worst-case cost (limit price plus fee) fits ``budget`` and visible depth."""
    available = sum(size for price, size in asks if price <= limit_price + 1e-9)
    if limit_price <= 0 or budget <= 0:
        return 0
    contracts = min(int(math.floor(available + 1e-9)), int(math.floor(budget / limit_price + 1e-9)))
    while contracts > 0 and contracts * limit_price + fees.taker_fee(limit_price, contracts) > budget + 1e-9:
        contracts -= 1
    return max(0, contracts)


def evaluate_entry(
    market: Mapping[str, Any],
    book: OrderBook,
    view: ModelView,
    ts: float,
    config: StrategyConfig,
    fees: FeeSchedule,
    *,
    budget: float,
) -> Evaluation:
    close_ts = float(market["close_ts"])
    seconds_to_close = close_ts - ts
    evaluation = Evaluation(
        ticker=str(market["ticker"]),
        ts=ts,
        seconds_to_close=seconds_to_close,
        action="skip",
        reason="",
        p_yes=view.p_yes,
        sigma_annual=view.sigma_annual,
        yes_bid=book.best_bid("yes"),
        yes_ask=book.best_ask("yes"),
    )
    if not config.min_seconds_to_close <= seconds_to_close <= config.max_seconds_to_close:
        evaluation.reason = "outside_time_window"
        return evaluation
    if view.p_yes is None:
        evaluation.reason = view.reason
        return evaluation

    best: tuple[float, str, float, float] | None = None
    any_ask = False
    for side in SIDES:
        ask = book.best_ask(side)
        if ask is None:
            continue
        any_ask = True
        if not config.min_entry_price <= ask <= config.max_entry_price:
            continue
        p_side = view.p_yes if side == "yes" else 1.0 - view.p_yes
        limit_price = round(min(0.99, ask + config.max_slippage), 6)
        edge = (p_side - config.model_margin) - limit_price - fees.taker_fee(limit_price, 1)
        if best is None or edge > best[0]:
            best = (edge, side, ask, limit_price)

    if best is None:
        evaluation.reason = "price_out_of_range" if any_ask else "no_liquidity"
        return evaluation
    edge, side, ask, limit_price = best
    evaluation.side, evaluation.best_ask, evaluation.limit_price, evaluation.edge = side, ask, limit_price, edge
    if edge < config.min_edge:
        evaluation.reason = "edge_below_threshold"
        return evaluation

    contracts = contracts_for_budget(book.asks(side), limit_price, budget, fees)
    if contracts <= 0:
        evaluation.reason = "budget_or_depth_too_small"
        return evaluation
    evaluation.action, evaluation.reason, evaluation.contracts = "enter", "edge", contracts
    return evaluation


def should_exit(
    side: str,
    contracts: float,
    book: OrderBook,
    view: ModelView,
    config: StrategyConfig,
    fees: FeeSchedule,
) -> float | None:
    """Best bid to sell at when selling now beats the model's hold value; otherwise ``None``."""
    if view.p_yes is None or contracts <= 0:
        return None
    bid = book.best_bid(side)
    if bid is None:
        return None
    p_side = view.p_yes if side == "yes" else 1.0 - view.p_yes
    proceeds_per_contract = bid - fees.taker_fee(bid, contracts) / contracts
    if proceeds_per_contract >= p_side + config.exit_buffer:
        return bid
    return None
