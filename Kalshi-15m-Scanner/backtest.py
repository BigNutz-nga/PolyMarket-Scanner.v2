"""Replay recorded data through the strategy with delayed, depth-limited simulated fills.

Honesty rules enforced here:

* Decisions use only data this recorder had at decision time: book snapshots by
  local receipt time and index ticks by local ``recorded_at_ms``.
* Orders are immediate-or-cancel taker orders that execute ``latency_seconds``
  later against the next recorded book for that market, limited by the limit
  price and visible depth. Missed and partial fills therefore happen naturally.
* Resting (maker) orders are not simulated: realistic queue position and
  adverse-selection modeling needs data the recorder does not capture.
* The most recent days are held out as an evaluation period that stays sealed
  until explicitly requested, and every unsealing is logged.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from typing import Any

from fees import FeeSchedule
from kalshi_api import OrderBook
from model import IndexState, settles_yes
from store import RecorderStore
from strategy import Evaluation, StrategyConfig, evaluate_entry, model_view, should_exit

EXIT_POLICIES = ("hold", "early_exit")
SETTLE_GRACE_SECONDS = 5.0
MAX_OPPORTUNITIES = 500


def _tzinfo(name: str) -> Any:
    if name.upper() == "UTC":
        return timezone.utc
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def day_key(ts: float, tz_name: str = "UTC") -> str:
    return datetime.fromtimestamp(ts, _tzinfo(tz_name)).date().isoformat()


def day_start(day: str, tz_name: str = "UTC") -> float:
    parsed = datetime.fromisoformat(day).date()
    return datetime.combine(parsed, dt_time(0), tzinfo=_tzinfo(tz_name)).timestamp()


def days_between(start_ts: float, end_ts: float, tz_name: str = "UTC") -> list[str]:
    first = datetime.fromisoformat(day_key(start_ts, tz_name)).date()
    last = datetime.fromisoformat(day_key(end_ts, tz_name)).date()
    return [(first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)]


def sweep(
    levels: Sequence[tuple[float, float]], limit_price: float, contracts: float, *, buying: bool
) -> list[tuple[float, float]]:
    """Take whole contracts from best to worst level until ``contracts`` or the limit price is reached."""
    fills: list[tuple[float, float]] = []
    remaining = contracts
    for price, size in levels:
        if remaining < 1 - 1e-9:
            break
        if (buying and price > limit_price + 1e-9) or (not buying and price < limit_price - 1e-9):
            break
        quantity = float(math.floor(min(size, remaining) + 1e-9))
        if quantity <= 0:
            continue
        fills.append((price, quantity))
        remaining -= quantity
    return fills


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    iterations: int = 2000,
    seed: int = 7,
    alpha: float = 0.05,
    min_samples: int = 5,
) -> tuple[float, float] | None:
    """Percentile bootstrap CI of the mean; ``None`` below ``min_samples`` to avoid false precision."""
    if len(values) < max(2, min_samples):
        return None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(iterations))
    low = means[int(alpha / 2 * iterations)]
    high = means[max(0, int((1 - alpha / 2) * iterations) - 1)]
    return round(low, 6), round(high, 6)


def max_drawdown(equity: Sequence[tuple[float, float]]) -> float:
    peak = 0.0
    worst = 0.0
    for _, value in equity:
        peak = max(peak, value)
        worst = min(worst, value - peak)
    return round(worst, 6)


@dataclass(slots=True)
class Position:
    ticker: str
    side: str
    contracts: float
    premium: float
    entry_fee: float
    entry_ts: float
    close_ts: float
    p_yes_entry: float | None
    edge_entry: float | None
    requested: int
    open_contracts: float = 0.0
    exit_proceeds: float = 0.0
    exit_fees: float = 0.0
    exit_ts: float | None = None

    @property
    def cost(self) -> float:
        return self.premium + self.entry_fee


@dataclass(slots=True)
class PendingOrder:
    kind: str  # entry | exit
    ticker: str
    side: str
    contracts: float
    limit_price: float
    decided_ts: float
    due_ts: float
    budget: float = 0.0
    evaluation: Evaluation | None = None


@dataclass(slots=True)
class Replay:
    store: RecorderStore
    series_ticker: str
    index_id: str
    config: StrategyConfig
    fees: FeeSchedule
    exit_policy: str = "hold"
    start_ts: float = 0.0
    end_ts: float = 0.0

    markets: dict[str, dict[str, Any]] = field(default_factory=dict)
    index: IndexState | None = None
    position: Position | None = None
    pending: PendingOrder | None = None
    daily_pnl: dict[str, float] = field(default_factory=dict)
    data_days: set[str] = field(default_factory=set)
    trades: list[dict[str, Any]] = field(default_factory=list)
    opportunities: list[dict[str, Any]] = field(default_factory=list)
    skip_reasons: dict[str, int] = field(default_factory=dict)
    equity: list[tuple[float, float]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    calibration_samples: list[tuple[str, float, float | None]] = field(default_factory=list)
    _calibration_seen: set[tuple[str, int]] = field(default_factory=set)
    _cumulative: float = 0.0

    def __post_init__(self) -> None:
        if self.exit_policy not in EXIT_POLICIES:
            raise ValueError(f"exit_policy must be one of {EXIT_POLICIES}")
        self.config.validate()
        self.index = IndexState.create(self.config.vol_lookback_seconds, self.config.vol_sample_seconds)
        for name in (
            "snapshots",
            "entry_orders",
            "fills",
            "partial_fills",
            "missed_fills",
            "exit_orders",
            "exit_fills",
            "missed_exits",
            "provisional_settlements",
            "unresolved_positions",
            "open_at_end",
        ):
            self.counters[name] = 0

    # -- bookkeeping --------------------------------------------------------------------------

    def _count(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def _skip(self, reason: str) -> None:
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1

    def _realize(self, ts: float, amount: float) -> None:
        day = day_key(ts, self.config.day_timezone)
        self.daily_pnl[day] = round(self.daily_pnl.get(day, 0.0) + amount, 6)
        self._cumulative = round(self._cumulative + amount, 6)
        self.equity.append((ts, self._cumulative))

    def _budget(self, ts: float) -> float:
        realized_today = self.daily_pnl.get(day_key(ts, self.config.day_timezone), 0.0)
        return min(self.config.max_loss_per_position, self.config.daily_loss_stop + realized_today)

    def outcome(self, ticker: str) -> tuple[bool | None, str]:
        market = self.markets[ticker]
        result = str(market.get("result") or "").lower()
        if result in {"yes", "no"}:
            return result == "yes", "official"
        assert self.index is not None
        average = self.index.final_average(float(market["close_ts"]))
        if average is None:
            return None, "unresolved"
        won = settles_yes(
            strike_type=str(market.get("strike_type") or ""),
            floor_strike=market.get("floor_strike"),
            cap_strike=market.get("cap_strike"),
            settlement_value=average,
        )
        return (won, "provisional") if won is not None else (None, "unresolved")

    # -- order handling -----------------------------------------------------------------------

    def _fill_entry(self, order: PendingOrder, ts: float, book: OrderBook) -> None:
        fills = sweep(book.asks(order.side), order.limit_price, order.contracts, buying=True)
        # Per-level fee rounding can add a cent; trim so worst-case loss never exceeds the budget.
        while fills:
            premium = sum(price * size for price, size in fills)
            if premium + self.fees.taker_fee_for_levels(fills) <= order.budget + 1e-9:
                break
            price, size = fills[-1]
            fills[-1:] = [(price, size - 1)] if size > 1 else []
        evaluation = order.evaluation.to_dict() if order.evaluation else {}
        record = {**evaluation, "fill_ts": ts, "requested": order.contracts}
        if not fills:
            self._count("missed_fills")
            self.opportunities.append({**record, "fill_status": "missed", "filled": 0, "fill_price": None})
            return
        quantity = sum(size for _, size in fills)
        premium = sum(price * size for price, size in fills)
        entry_fee = self.fees.taker_fee_for_levels(fills)
        self._count("fills")
        if quantity < order.contracts:
            self._count("partial_fills")
        self.opportunities.append(
            {
                **record,
                "fill_status": "partial" if quantity < order.contracts else "filled",
                "filled": quantity,
                "fill_price": round(premium / quantity, 6),
            }
        )
        market = self.markets[order.ticker]
        self.position = Position(
            ticker=order.ticker,
            side=order.side,
            contracts=quantity,
            open_contracts=quantity,
            premium=round(premium, 6),
            entry_fee=entry_fee,
            entry_ts=ts,
            close_ts=float(market["close_ts"]),
            p_yes_entry=order.evaluation.p_yes if order.evaluation else None,
            edge_entry=order.evaluation.edge if order.evaluation else None,
            requested=int(order.contracts),
        )

    def _fill_exit(self, order: PendingOrder, ts: float, book: OrderBook) -> None:
        position = self.position
        if position is None:
            return
        fills = sweep(book.bids(order.side), order.limit_price, position.open_contracts, buying=False)
        if not fills:
            self._count("missed_exits")
            return
        quantity = sum(size for _, size in fills)
        proceeds = sum(price * size for price, size in fills)
        fee = self.fees.taker_fee_for_levels(fills)
        self._count("exit_fills")
        share = quantity / position.contracts
        self._realize(ts, proceeds - fee - position.cost * share)
        position.exit_proceeds += proceeds
        position.exit_fees += fee
        position.open_contracts = round(position.open_contracts - quantity, 6)
        position.exit_ts = ts
        if position.open_contracts <= 1e-9:
            self._close_position(ts, settlement=None, source="early_exit")

    def _close_position(self, ts: float, *, settlement: bool | None, source: str) -> None:
        position = self.position
        if position is None:
            return
        payout = 0.0
        if settlement is not None and position.open_contracts > 0:
            won = settlement if position.side == "yes" else not settlement
            payout = position.open_contracts if won else 0.0
            self._realize(position.close_ts, payout - position.cost * position.open_contracts / position.contracts)
        pnl = position.exit_proceeds - position.exit_fees + payout - position.cost
        if source == "unresolved":
            # Without an outcome the remaining contracts' result is unknown; only realized early exits count.
            pnl = position.exit_proceeds - position.exit_fees - position.cost * (
                1 - position.open_contracts / position.contracts
            )
        exit_type = source
        if position.exit_proceeds and source in {"official", "provisional"}:
            exit_type = "partial_exit+settlement"
        exit_ts = ts if source == "early_exit" else position.close_ts
        self.trades.append(
            {
                "ticker": position.ticker,
                "side": position.side,
                "entry_ts": position.entry_ts,
                "exit_ts": exit_ts,
                "close_ts": position.close_ts,
                "day": day_key(exit_ts, self.config.day_timezone),
                "contracts": position.contracts,
                "requested": position.requested,
                "avg_entry_price": round(position.premium / position.contracts, 6),
                "entry_fee": position.entry_fee,
                "exit_proceeds": round(position.exit_proceeds, 6),
                "exit_fees": round(position.exit_fees, 6),
                "settlement_payout": payout,
                "fees": round(position.entry_fee + position.exit_fees, 6),
                "pnl": round(pnl, 6),
                "exit_type": exit_type,
                "p_yes_entry": position.p_yes_entry,
                "edge_entry": position.edge_entry,
            }
        )
        self.position = None

    def _settle_due(self, ts: float) -> None:
        position = self.position
        if position is None or ts < position.close_ts + SETTLE_GRACE_SECONDS:
            return
        settlement, source = self.outcome(position.ticker)
        if settlement is None:
            self._count("unresolved_positions")
            self._close_position(ts, settlement=None, source="unresolved")
            return
        if source == "provisional":
            self._count("provisional_settlements")
        self._close_position(ts, settlement=settlement, source=source)

    def _expire_pending(self, ts: float) -> None:
        order = self.pending
        if order is None or ts <= order.due_ts + self.config.max_fill_wait_seconds:
            return
        if order.kind == "entry":
            self._count("missed_fills")
            record = order.evaluation.to_dict() if order.evaluation else {}
            self.opportunities.append(
                {
                    **record,
                    "fill_ts": None,
                    "requested": order.contracts,
                    "fill_status": "no_book",
                    "filled": 0,
                    "fill_price": None,
                }
            )
        else:
            self._count("missed_exits")
        self.pending = None

    def _sample_calibration(self, ticker: str, seconds_to_close: float, book: OrderBook, p_yes: float | None) -> None:
        key = (ticker, int(seconds_to_close // 60))
        if p_yes is None or key in self._calibration_seen:
            return
        self._calibration_seen.add(key)
        self.calibration_samples.append((ticker, p_yes, book.mid_yes()))

    # -- main loop ----------------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        config = self.config
        assert self.index is not None
        self.markets = self.store.load_markets(self.series_ticker, self.start_ts, self.end_ts)
        tick_start_ms = int((self.start_ts - config.vol_lookback_seconds - 120) * 1000)
        tick_end_ms = int((self.end_ts + 120) * 1000)
        ticks = iter(self.store.iter_ticks(self.index_id, tick_start_ms, tick_end_ms))
        next_tick = next(ticks, None)

        def advance(until_ts: float) -> None:
            nonlocal next_tick
            assert self.index is not None
            limit_ms = until_ts * 1000.0
            while next_tick is not None and next_tick.recorded_at_ms <= limit_ms:
                self.index.update(next_tick)
                next_tick = next(ticks, None)

        for ticker, ts, book in self.store.iter_books(self.series_ticker, self.start_ts, self.end_ts):
            advance(ts)
            self._settle_due(ts)
            self._expire_pending(ts)
            market = self.markets.get(ticker)
            if market is None or market.get("open_ts") is None or market.get("close_ts") is None:
                continue
            if not float(market["open_ts"]) <= ts < float(market["close_ts"]):
                continue
            self._count("snapshots")
            self.data_days.add(day_key(ts, config.day_timezone))
            view = model_view(market, self.index, ts, config)
            self._sample_calibration(ticker, float(market["close_ts"]) - ts, book, view.p_yes)

            order = self.pending
            if order is not None and order.ticker == ticker and ts >= order.due_ts:
                self.pending = None
                if order.kind == "entry":
                    self._fill_entry(order, ts, book)
                else:
                    self._fill_exit(order, ts, book)
                continue

            position = self.position
            if position is not None and position.ticker == ticker:
                if self.exit_policy == "early_exit" and self.pending is None:
                    bid = should_exit(position.side, position.open_contracts, book, view, config, self.fees)
                    if bid is not None:
                        self._count("exit_orders")
                        self.pending = PendingOrder(
                            kind="exit",
                            ticker=ticker,
                            side=position.side,
                            contracts=position.open_contracts,
                            limit_price=round(max(0.01, bid - config.max_slippage), 6),
                            decided_ts=ts,
                            due_ts=ts + config.latency_seconds,
                        )
                continue
            if position is not None or self.pending is not None:
                self._skip("position_open")
                continue

            budget = self._budget(ts)
            if budget <= 0:
                self._skip("daily_loss_stop")
                continue
            evaluation = evaluate_entry(market, book, view, ts, config, self.fees, budget=budget)
            if evaluation.action != "enter":
                self._skip(evaluation.reason)
                continue
            self._count("entry_orders")
            self.pending = PendingOrder(
                kind="entry",
                ticker=ticker,
                side=str(evaluation.side),
                contracts=evaluation.contracts,
                limit_price=float(evaluation.limit_price or 0.0),
                decided_ts=ts,
                due_ts=ts + config.latency_seconds,
                budget=budget,
                evaluation=evaluation,
            )

        advance(self.end_ts + 120)
        if self.pending is not None:
            self._expire_pending(math.inf)
        if self.position is not None:
            settled_in_data = self.position.close_ts + SETTLE_GRACE_SECONDS <= self.end_ts + 120
            if settled_in_data or self.markets[self.position.ticker].get("result"):
                self._settle_due(math.inf)
            else:
                self._count("open_at_end")
                self.position = None
        return self.summary()

    # -- results ------------------------------------------------------------------------------

    def calibration(self) -> dict[str, Any]:
        outcomes: dict[str, bool | None] = {}
        model_errors: list[float] = []
        market_errors: list[float] = []
        buckets = [{"n": 0, "p_sum": 0.0, "hits": 0} for _ in range(10)]
        for ticker, p_model, p_market in self.calibration_samples:
            if ticker not in outcomes:
                outcomes[ticker] = self.outcome(ticker)[0]
            outcome = outcomes[ticker]
            if outcome is None:
                continue
            value = 1.0 if outcome else 0.0
            model_errors.append((p_model - value) ** 2)
            if p_market is not None:
                market_errors.append((p_market - value) ** 2)
            bucket = buckets[min(9, int(p_model * 10))]
            bucket["n"] += 1
            bucket["p_sum"] += p_model
            bucket["hits"] += int(outcome)
        paired: list[tuple[float, float]] = []
        differences_by_market: dict[str, list[float]] = {}
        for ticker, p_model, p_market in self.calibration_samples:
            if outcomes.get(ticker) is None or p_market is None:
                continue
            value = 1.0 if outcomes[ticker] else 0.0
            pair = ((p_model - value) ** 2, (p_market - value) ** 2)
            paired.append(pair)
            differences_by_market.setdefault(ticker, []).append(pair[0] - pair[1])
        # Samples within a market share one outcome, so the uncertainty is bootstrapped across markets.
        market_differences = [sum(values) / len(values) for values in differences_by_market.values()]
        return {
            "samples": len(model_errors),
            "markets": sum(1 for value in outcomes.values() if value is not None),
            "model_brier": round(sum(model_errors) / len(model_errors), 6) if model_errors else None,
            "market_brier": round(sum(market_errors) / len(market_errors), 6) if market_errors else None,
            "paired_samples": len(paired),
            "paired_model_brier": round(sum(m for m, _ in paired) / len(paired), 6) if paired else None,
            "paired_market_brier": round(sum(k for _, k in paired) / len(paired), 6) if paired else None,
            "brier_difference_ci95": bootstrap_mean_ci(market_differences),
            "buckets": [
                {
                    "range": f"{index / 10:.1f}-{(index + 1) / 10:.1f}",
                    "n": bucket["n"],
                    "mean_model_p": round(bucket["p_sum"] / bucket["n"], 4) if bucket["n"] else None,
                    "observed_yes_rate": round(bucket["hits"] / bucket["n"], 4) if bucket["n"] else None,
                }
                for index, bucket in enumerate(buckets)
            ],
        }

    def summary(self) -> dict[str, Any]:
        closed = [trade for trade in self.trades if trade["exit_type"] != "unresolved"]
        pnls = [trade["pnl"] for trade in closed]
        days = sorted(self.data_days | set(self.daily_pnl))
        daily = [{"day": day, "pnl": round(self.daily_pnl.get(day, 0.0), 6)} for day in days]
        trades_by_day: dict[str, int] = {}
        for trade in closed:
            trades_by_day[trade["day"]] = trades_by_day.get(trade["day"], 0) + 1
        for row in daily:
            row["trades"] = trades_by_day.get(row["day"], 0)
        daily_values = [row["pnl"] for row in daily]
        wins = sum(1 for value in pnls if value > 0)
        return {
            "exit_policy": self.exit_policy,
            "period": {"start": self.start_ts, "end": self.end_ts},
            "counters": dict(self.counters),
            "summary": {
                "trades": len(closed),
                "wins": wins,
                "win_rate": round(wins / len(closed), 4) if closed else None,
                "total_pnl": round(sum(pnls), 6),
                "total_fees": round(sum(trade["fees"] for trade in closed), 6),
                "avg_pnl_per_trade": round(sum(pnls) / len(pnls), 6) if pnls else None,
                "trade_pnl_ci95": bootstrap_mean_ci(pnls),
                "max_drawdown": max_drawdown(self.equity),
                "days": len(daily),
                "active_days": sum(1 for row in daily if row["trades"]),
                "positive_days": sum(1 for value in daily_values if value > 0),
                "mean_daily_pnl": round(sum(daily_values) / len(daily_values), 6) if daily_values else None,
                "daily_pnl_ci95": bootstrap_mean_ci(daily_values),
            },
            "daily": daily,
            "equity": [[ts, value] for ts, value in self.equity],
            "trades": self.trades,
            "opportunities": self.opportunities[-MAX_OPPORTUNITIES:],
            "skip_reasons": dict(sorted(self.skip_reasons.items(), key=lambda item: -item[1])),
            "calibration": self.calibration(),
        }


@dataclass(frozen=True, slots=True)
class EvidenceCriteria:
    """What the untouched evaluation period must show before a small live pilot is considered."""

    min_trades: int = 50
    min_active_days: int = 10
    min_positive_day_fraction: float = 0.5
    require_daily_ci_above_zero: bool = True
    require_model_beats_market: bool = True


def readiness(result: Mapping[str, Any] | None, criteria: EvidenceCriteria) -> dict[str, Any]:
    if result is None:
        return {"status": "sealed", "checks": []}
    summary = result["summary"]
    calibration = result["calibration"]
    active_days = summary["active_days"]
    ci = summary["daily_pnl_ci95"]
    model_brier, market_brier = calibration["paired_model_brier"], calibration["paired_market_brier"]
    difference_ci = calibration["brier_difference_ci95"]
    checks = [
        {"check": f"at least {criteria.min_trades} trades", "value": summary["trades"], "passed": summary["trades"] >= criteria.min_trades},
        {"check": f"at least {criteria.min_active_days} active days", "value": active_days, "passed": active_days >= criteria.min_active_days},
        {"check": "net P&L after fees is positive", "value": summary["total_pnl"], "passed": summary["total_pnl"] > 0},
        {
            "check": f"positive on at least {criteria.min_positive_day_fraction:.0%} of active days",
            "value": f"{summary['positive_days']}/{active_days}",
            "passed": active_days > 0 and summary["positive_days"] / active_days >= criteria.min_positive_day_fraction,
        },
    ]
    if criteria.require_daily_ci_above_zero:
        checks.append(
            {"check": "95% CI of mean daily P&L above zero", "value": ci, "passed": ci is not None and ci[0] > 0}
        )
    if criteria.require_model_beats_market:
        checks.append(
            {
                "check": "model beats market mid on Brier score (95% CI of model minus market below zero)",
                "value": {"model": model_brier, "market": market_brier, "difference_ci95": difference_ci},
                "passed": difference_ci is not None and difference_ci[1] < 0,
            }
        )
    return {"status": "met" if all(check["passed"] for check in checks) else "not_met", "checks": checks}


def split_periods(
    data_range: tuple[float, float],
    *,
    tz_name: str = "UTC",
    split_ts: float | None = None,
    eval_fraction: float = 0.3,
) -> tuple[tuple[float, float], tuple[float, float] | None]:
    """Chronological development/evaluation split on whole days."""
    first, last = data_range
    if split_ts is None:
        days = days_between(first, last, tz_name)
        eval_days = int(round(len(days) * eval_fraction)) if len(days) >= 2 else 0
        if eval_fraction > 0 and len(days) >= 2:
            eval_days = max(1, min(len(days) - 1, eval_days))
        if eval_days == 0:
            return (first, last), None
        split_ts = day_start(days[-eval_days], tz_name)
    if split_ts <= first:
        return (first, last), None
    if split_ts > last:
        return (first, last), None
    return (first, split_ts - 1e-6), (split_ts, last)


def run_study(
    store: RecorderStore,
    *,
    series_ticker: str,
    index_id: str,
    config: StrategyConfig,
    fees: FeeSchedule,
    split_ts: float | None = None,
    eval_fraction: float = 0.3,
    evaluate: bool = False,
    criteria: EvidenceCriteria | None = None,
) -> dict[str, Any]:
    """Run both exit policies on the development period and, if unsealed, the evaluation period."""
    config.validate()
    criteria = criteria or EvidenceCriteria()
    data_range = store.book_time_range(series_ticker)
    report: dict[str, Any] = {
        "generated_at": time.time(),
        "series_ticker": series_ticker,
        "index_id": index_id,
        "config": asdict(config),
        "config_fingerprint": config.fingerprint(),
        "fees": asdict(fees),
        "criteria": asdict(criteria),
        "coverage": store.coverage(series_ticker, index_id),
        "periods": {},
        "development": {},
        "evaluation": None,
        "readiness": {},
        "notes": [],
    }
    if data_range is None:
        report["notes"].append("No recorded orderbook snapshots yet. Run `python main.py record` first.")
        return report

    development, evaluation = split_periods(
        data_range, tz_name=config.day_timezone, split_ts=split_ts, eval_fraction=eval_fraction
    )
    report["periods"]["development"] = {"start": development[0], "end": development[1]}
    report["periods"]["evaluation"] = (
        {"start": evaluation[0], "end": evaluation[1], "sealed": not evaluate} if evaluation else None
    )
    for policy in EXIT_POLICIES:
        replay = Replay(
            store, series_ticker, index_id, config, fees, policy, start_ts=development[0], end_ts=development[1]
        )
        report["development"][policy] = replay.run()

    if evaluation is None:
        report["notes"].append("Not enough whole days recorded for a held-out evaluation period yet.")
    elif evaluate:
        store.record_evaluation_view(config.fingerprint(), evaluation[0], evaluation[1])
        report["evaluation"] = {}
        for policy in EXIT_POLICIES:
            replay = Replay(
                store, series_ticker, index_id, config, fees, policy, start_ts=evaluation[0], end_ts=evaluation[1]
            )
            report["evaluation"][policy] = replay.run()
    else:
        report["notes"].append(
            "Evaluation period is sealed. Freeze the strategy settings, then rerun with --evaluate once."
        )

    views = store.evaluation_views()
    report["evaluation_views"] = views
    fingerprints = {view["config_fingerprint"] for view in views}
    if len(fingerprints) > 1:
        report["notes"].append(
            f"The evaluation period has been viewed with {len(fingerprints)} different strategy settings. "
            "Treat it as development data now and wait for fresh days before trusting it."
        )
    for policy in EXIT_POLICIES:
        result = report["evaluation"][policy] if report["evaluation"] else None
        report["readiness"][policy] = readiness(result, criteria)
    return report
