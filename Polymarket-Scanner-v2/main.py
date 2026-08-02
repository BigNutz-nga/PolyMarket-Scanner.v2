"""Command-line entry point for Polymarket-Scanner."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from rich.console import Console

from fetcher import PolymarketAPIError
from reporter import render_console
from scanner import ScanOptions, execute_scan

CONSOLE = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="Polymarket-Scanner",
        description=(
            "Fetch top Polymarket traders, compare portfolios, detect synchronized "
            "trades, persist SQLite snapshots, and export Markdown/JSON reports."
        ),
    )
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--timeframe", choices=["DAY", "WEEK", "MONTH", "ALL"], default="WEEK")
    parser.add_argument("--metric", choices=["PNL", "VOLUME", "VOL"], default="PNL")
    parser.add_argument(
        "--category",
        choices=[
            "OVERALL", "POLITICS", "SPORTS", "CRYPTO", "CULTURE", "MENTIONS",
            "WEATHER", "ECONOMICS", "TECH", "FINANCE",
        ],
        default="OVERALL",
    )
    parser.add_argument("--window-mins", type=int, default=15)
    parser.add_argument("--min-overlap", type=float, default=0.20)
    parser.add_argument("--min-cosine", type=float, default=0.80)
    parser.add_argument("--min-shared-positions", type=int, default=2)
    parser.add_argument("--min-cooccurrences", type=int, default=2)
    parser.add_argument("--min-sync-rate", type=float, default=0.05)
    parser.add_argument("--min-shared-trade-markets", type=int, default=1)
    parser.add_argument("--trade-limit", type=int, default=200)
    parser.add_argument("--trade-lookback-days", type=int, default=30)
    parser.add_argument("--closed-limit", type=int, default=100)
    parser.add_argument("--max-positions", type=int, default=1000)
    parser.add_argument("--db", default="polymarket_data.db")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--cache-ttl-mins", type=int, default=10)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--request-delay", type=float, default=0.08)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument(
        "--failure-policy",
        choices=["partial", "abort"],
        default="partial",
        help="partial keeps scanning and records per-endpoint errors; abort stops on first wallet failure.",
    )
    parser.add_argument(
        "--max-failed-wallet-ratio",
        type=float,
        default=1.0,
        help="Abort after collection when fully failed wallets exceed this ratio.",
    )
    parser.add_argument("--no-stale-fallback", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def options_from_args(args: argparse.Namespace) -> ScanOptions:
    return ScanOptions(
        timeframe=args.timeframe,
        metric=args.metric,
        limit=args.limit,
        category=args.category,
        trade_limit=args.trade_limit,
        trade_lookback_days=args.trade_lookback_days,
        closed_limit=args.closed_limit,
        max_positions=args.max_positions,
        window_minutes=args.window_mins,
        min_overlap=args.min_overlap,
        min_cosine=args.min_cosine,
        min_shared_positions=args.min_shared_positions,
        min_cooccurrences=args.min_cooccurrences,
        min_sync_rate=args.min_sync_rate,
        min_shared_trade_markets=args.min_shared_trade_markets,
        cache_ttl_minutes=args.cache_ttl_mins,
        timeout_seconds=args.timeout,
        retries=args.retries,
        request_delay_seconds=args.request_delay,
        concurrency=args.concurrency,
        failure_policy=args.failure_policy,
        max_failed_wallet_ratio=args.max_failed_wallet_ratio,
        allow_stale_fallback=not args.no_stale_fallback,
    )


async def run(args: argparse.Namespace) -> int:
    options = options_from_args(args)
    analysis, source, (json_path, markdown_path) = await execute_scan(
        options,
        database_path=args.db,
        output_dir=args.output_dir,
        refresh=args.refresh,
    )
    render_console(analysis, CONSOLE)
    CONSOLE.print(f"\n[green]Source:[/green] {source}")
    CONSOLE.print(f"[green]JSON:[/green] {json_path.resolve()}")
    CONSOLE.print(f"[green]Markdown:[/green] {markdown_path.resolve()}")
    CONSOLE.print(f"[green]SQLite:[/green] {Path(args.db).resolve()}")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(run(args))
    except ValueError as exc:
        parser.error(str(exc))
    except PolymarketAPIError as exc:
        CONSOLE.print(f"[bold red]Scan failed:[/bold red] {exc}")
        return 2
    except KeyboardInterrupt:
        CONSOLE.print("\n[yellow]Interrupted.[/yellow]")
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
