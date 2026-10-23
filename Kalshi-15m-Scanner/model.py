"""Settlement-aware probability model for Kalshi 15-minute crypto contracts.

A 15-minute market settles on the simple average of the CF Benchmarks index
(BRTI for Bitcoin) over the final 60 seconds before close, sampled once per
second over ``(close - 60s, close]``, compared against the market strike
(``floor_strike``/``cap_strike`` with ``strike_type``).

Over these horizons the log index is treated as a driftless Brownian motion.
The settlement average is then approximately normal:

* before the window opens, its variance is ``sigma^2 * S^2 * (tau_pre + (n+1)(2n+1)/(6n))``
  where ``tau_pre`` is the time until the window starts and ``n = 60`` ticks;
* inside the window, ticks already published are fixed (Kalshi's
  ``last_60s_windowed_average_15min`` field reports their running average and
  count) and only the remaining ticks are random.

This is a deliberately simple baseline. It ignores drift, jumps, fat tails and
volatility clustering beyond the trailing estimate, which is why the strategy
subtracts a model-error margin before comparing against market prices.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0
SETTLEMENT_WINDOW_SECONDS = 60
SUPPORTED_STRIKE_TYPES = {"greater", "greater_or_equal", "less", "less_or_equal", "between"}


def annual_to_per_second(annual_vol: float) -> float:
    return annual_vol / math.sqrt(SECONDS_PER_YEAR)


def per_second_to_annual(sigma: float) -> float:
    return sigma * math.sqrt(SECONDS_PER_YEAR)


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


@dataclass(frozen=True, slots=True)
class IndexTick:
    """One ``cfbenchmarks_value`` update, timestamped by source, Kalshi, and this recorder."""

    index_id: str
    source_ts_ms: int
    received_at_ms: int
    recorded_at_ms: int
    value: float
    avg60_value: float | None = None
    avg60_count: int | None = None
    window_value: float | None = None
    window_count: int | None = None
    window_start_ms: int | None = None
    window_end_ms: int | None = None

    @property
    def window_close_ts(self) -> int | None:
        """Quarter-hour close (epoch seconds) of the final-minute average carried by this tick."""
        if self.window_start_ms is None or self.window_value is None:
            return None
        return int(round((self.window_start_ms + SETTLEMENT_WINDOW_SECONDS * 1000) / 1000.0))


@dataclass(frozen=True, slots=True)
class WindowProgress:
    """Settlement-window ticks already published: their sum and how many there are."""

    total: float
    count: int

    @property
    def average(self) -> float | None:
        return self.total / self.count if self.count else None


def settlement_distribution(
    *,
    spot: float,
    sigma: float,
    seconds_to_close: float,
    window: WindowProgress | None = None,
    window_ticks: int = SETTLEMENT_WINDOW_SECONDS,
) -> tuple[float, float]:
    """Mean and standard deviation (in index units) of the settlement average."""
    if spot <= 0:
        raise ValueError("spot must be positive")
    if sigma < 0:
        raise ValueError("sigma cannot be negative")
    n = window_ticks
    tau = max(0.0, seconds_to_close)

    if tau >= n and (window is None or window.count == 0):
        variance_units = (tau - n) + (n + 1) * (2 * n + 1) / (6 * n)
        return spot, spot * sigma * math.sqrt(variance_units)

    observed_total = window.total if window else 0.0
    observed_count = window.count if window else 0
    # Never assume more future ticks than whole seconds remain; upstream gaps shrink the denominator.
    remaining = max(0, min(n - observed_count, math.ceil(tau - 1e-9)))
    count = observed_count + remaining
    if count == 0:
        return spot, 0.0
    offset = max(0.0, tau - remaining)
    variance_units = remaining**2 * offset + remaining * (remaining + 1) * (2 * remaining + 1) / 6
    mean = (observed_total + remaining * spot) / count
    return mean, spot * sigma * math.sqrt(variance_units) / count


def _prob_above(level: float, mean: float, sd: float, *, inclusive: bool) -> float:
    if sd <= 0:
        if mean == level:
            return 1.0 if inclusive else 0.0
        return 1.0 if mean > level else 0.0
    return 1.0 - _norm_cdf((level - mean) / sd)


def probability_yes(
    *,
    strike_type: str,
    floor_strike: float | None,
    cap_strike: float | None,
    mean: float,
    sd: float,
) -> float | None:
    """P(YES) for a strike definition, or ``None`` when the strike cannot be modeled."""
    if strike_type in {"greater", "greater_or_equal"}:
        if floor_strike is None:
            return None
        return _prob_above(floor_strike, mean, sd, inclusive=strike_type == "greater_or_equal")
    if strike_type in {"less", "less_or_equal"}:
        if cap_strike is None:
            return None
        return 1.0 - _prob_above(cap_strike, mean, sd, inclusive=strike_type == "less")
    if strike_type == "between":
        if floor_strike is None or cap_strike is None:
            return None
        lower = _prob_above(floor_strike, mean, sd, inclusive=True)
        upper = _prob_above(cap_strike, mean, sd, inclusive=False)
        return max(0.0, lower - upper)
    return None


def settles_yes(
    *,
    strike_type: str,
    floor_strike: float | None,
    cap_strike: float | None,
    settlement_value: float,
) -> bool | None:
    """Apply a strike definition to a known settlement value."""
    probability = probability_yes(
        strike_type=strike_type,
        floor_strike=floor_strike,
        cap_strike=cap_strike,
        mean=settlement_value,
        sd=0.0,
    )
    return None if probability is None else probability >= 0.5


class RollingVolatility:
    """Trailing realized volatility from index ticks, sampled every ``sample_seconds``.

    Returns per-second log volatility. Sampling coarser than the 1-second feed
    reduces the influence of index micro-noise on the estimate.
    """

    def __init__(self, lookback_seconds: float = 1800.0, sample_seconds: float = 10.0) -> None:
        self.lookback_seconds = max(1.0, lookback_seconds)
        self.sample_seconds = max(1.0, sample_seconds)
        self._returns: deque[tuple[float, float, float]] = deque()  # (end_ts, r^2, dt)
        self._sum_r2 = 0.0
        self._sum_dt = 0.0
        self._sample_ts: float | None = None
        self._sample_value: float | None = None
        self._latest_ts: float | None = None

    def add(self, ts: float, value: float) -> None:
        if value <= 0 or (self._latest_ts is not None and ts <= self._latest_ts):
            return
        self._latest_ts = ts
        if self._sample_ts is None or self._sample_value is None:
            self._sample_ts, self._sample_value = ts, value
            return
        dt = ts - self._sample_ts
        if dt < self.sample_seconds:
            return
        r2 = math.log(value / self._sample_value) ** 2
        self._returns.append((ts, r2, dt))
        self._sum_r2 += r2
        self._sum_dt += dt
        self._sample_ts, self._sample_value = ts, value
        cutoff = ts - self.lookback_seconds
        while self._returns and self._returns[0][0] - self._returns[0][2] < cutoff:
            _, old_r2, old_dt = self._returns.popleft()
            self._sum_r2 -= old_r2
            self._sum_dt -= old_dt

    @property
    def sample_count(self) -> int:
        return len(self._returns)

    def sigma(self) -> float | None:
        if not self._returns or self._sum_dt <= 0:
            return None
        return math.sqrt(max(0.0, self._sum_r2) / self._sum_dt)


@dataclass(slots=True)
class IndexState:
    """Everything known about the reference index at a point in (recorder) time."""

    volatility: RollingVolatility
    last_value: float | None = None
    last_recorded_ms: int | None = None
    windows: dict[int, WindowProgress] = field(default_factory=dict)
    window_seen_ms: dict[int, int] = field(default_factory=dict)

    @classmethod
    def create(cls, lookback_seconds: float = 1800.0, sample_seconds: float = 10.0) -> IndexState:
        return cls(volatility=RollingVolatility(lookback_seconds, sample_seconds))

    def update(self, tick: IndexTick) -> None:
        self.last_value = tick.value
        self.last_recorded_ms = tick.recorded_at_ms
        self.volatility.add(tick.source_ts_ms / 1000.0, tick.value)
        close_ts = tick.window_close_ts
        if close_ts is not None and tick.window_value is not None and tick.window_count is not None:
            self.windows[close_ts] = WindowProgress(
                total=tick.window_value * tick.window_count, count=tick.window_count
            )
            self.window_seen_ms[close_ts] = tick.recorded_at_ms
            if len(self.windows) > 16:
                for stale in sorted(self.windows)[:-16]:
                    self.windows.pop(stale, None)
                    self.window_seen_ms.pop(stale, None)

    def window_for(self, close_ts: float) -> WindowProgress | None:
        return self.windows.get(int(round(close_ts)))

    def final_average(self, close_ts: float) -> float | None:
        """Recorded settlement average once all 60 window ticks were observed."""
        window = self.window_for(close_ts)
        if window is None or window.count < SETTLEMENT_WINDOW_SECONDS:
            return None
        return window.average
