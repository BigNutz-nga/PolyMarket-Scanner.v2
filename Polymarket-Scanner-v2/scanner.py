"""Scan orchestration shared by the CLI and Telegram bot."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from analyzer import analyze_scan
from fetcher import ClientSettings, PolymarketAPIError, PolymarketClient, SQLiteStore, make_cache_key
from reporter import write_reports


@dataclass(slots=True)
class ScanOptions:
    timeframe: str = "WEEK"
    metric: str = "PNL"
    limit: int = 25
    category: str = "OVERALL"
    trade_limit: int = 200
    trade_lookback_days: int = 30
    closed_limit: int = 100
    max_positions: int = 1_000
    window_minutes: int = 15
    min_overlap: float = 0.20
    min_cosine: float = 0.80
    min_shared_positions: int = 2
    min_cooccurrences: int = 2
    min_sync_rate: float = 0.05
    min_shared_trade_markets: int = 1
    cache_ttl_minutes: int = 10
    timeout_seconds: float = 30.0
    retries: int = 4
    request_delay_seconds: float = 0.08
    concurrency: int = 6
    failure_policy: str = "partial"
    max_failed_wallet_ratio: float = 1.0
    allow_stale_fallback: bool = True

    def validate(self) -> None:
        self.timeframe = self.timeframe.upper()
        self.metric = "VOLUME" if self.metric.upper() in {"VOLUME", "VOL"} else "PNL"
        self.category = self.category.upper()
        if self.timeframe not in {"DAY", "WEEK", "MONTH", "ALL"}:
            raise ValueError("timeframe must be DAY, WEEK, MONTH, or ALL")
        if not 1 <= self.limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        if not 0.0 <= self.min_overlap <= 1.0:
            raise ValueError("min_overlap must be between 0 and 1")
        if not 0.0 <= self.min_cosine <= 1.0:
            raise ValueError("min_cosine must be between 0 and 1")
        if not 0.0 <= self.min_sync_rate <= 1.0:
            raise ValueError("min_sync_rate must be between 0 and 1")
        if self.failure_policy not in {"partial", "abort"}:
            raise ValueError("failure_policy must be partial or abort")
        if not 0.0 <= self.max_failed_wallet_ratio <= 1.0:
            raise ValueError("max_failed_wallet_ratio must be between 0 and 1")
        for name in (
            "trade_limit",
            "trade_lookback_days",
            "closed_limit",
            "max_positions",
            "window_minutes",
            "min_shared_positions",
            "min_cooccurrences",
            "min_shared_trade_markets",
            "cache_ttl_minutes",
            "retries",
            "concurrency",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")

    def fetch_config(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "metric": self.metric,
            "limit": self.limit,
            "category": self.category,
            "trade_limit": self.trade_limit,
            "trade_lookback_days": self.trade_lookback_days,
            "closed_limit": self.closed_limit,
            "max_positions": self.max_positions,
        }


async def execute_scan(
    options: ScanOptions,
    *,
    database_path: str | Path,
    output_dir: str | Path,
    refresh: bool = False,
) -> tuple[dict[str, Any], str, tuple[Path, Path]]:
    """Run or reuse a scan and return analysis, source label, and report paths."""
    options.validate()
    store = SQLiteStore(database_path)
    fetch_config = options.fetch_config()
    cache_key = make_cache_key({**fetch_config, "failure_policy": options.failure_policy})
    scan: dict[str, Any] | None = None
    source = "live-api"

    if not refresh and options.cache_ttl_minutes > 0:
        scan = store.get_cached_scan(cache_key, options.cache_ttl_minutes * 60)
        if scan is not None:
            source = "fresh-cache"

    if scan is None:
        settings = ClientSettings(
            timeout_seconds=options.timeout_seconds,
            retries=options.retries,
            request_delay_seconds=options.request_delay_seconds,
            concurrency=max(1, options.concurrency),
            failure_policy=options.failure_policy,
            max_failed_wallet_ratio=options.max_failed_wallet_ratio,
        )
        try:
            async with PolymarketClient(settings=settings) as client:
                scan = await client.collect_scan(**fetch_config)
            store.save_scan(scan, cache_key=cache_key)
            store.prune(keep_latest=30)
        except (PolymarketAPIError, OSError) as exc:
            if not options.allow_stale_fallback:
                raise
            stale = store.get_latest_scan(cache_key)
            if stale is None:
                raise
            scan = stale
            source = "stale-cache"
            scan = dict(scan)
            scan["stale_fallback_error"] = str(exc)

    analysis = analyze_scan(
        scan,
        window_minutes=max(1, options.window_minutes),
        min_overlap=options.min_overlap,
        min_cosine=options.min_cosine,
        min_shared_positions=max(1, options.min_shared_positions),
        min_cooccurrences=max(1, options.min_cooccurrences),
        min_sync_rate=options.min_sync_rate,
        min_shared_trade_markets=max(1, options.min_shared_trade_markets),
    )
    analysis["data_source"] = source
    if scan.get("stale_fallback_error"):
        analysis["stale_fallback_error"] = scan["stale_fallback_error"]
    store.save_analysis(analysis)
    paths = write_reports(analysis, output_dir)
    return analysis, source, paths
