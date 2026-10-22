"""Command-line entry point for the Kalshi 15-minute recorder and paper-trading study."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import fields
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from backtest import EvidenceCriteria, day_start, run_study
from dashboard import render_console, when, write_report_files
from fees import FeeSchedule, hold_to_settlement, round_trip
from kalshi_api import (
    DEMO_REST_URL,
    DEMO_WS_URL,
    PROD_REST_URL,
    PROD_WS_URL,
    ClientSettings,
    KalshiAPIError,
    KalshiClient,
    KalshiSigner,
)
from live_check import run_checks
from recorder import Recorder, RecorderSettings
from store import RecorderStore
from strategy import StrategyConfig

CONSOLE = Console()
DEFAULT_DB = "data/kalshi_15m.db"


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--series", default="KXBTC15M", help="Kalshi series ticker")
    parser.add_argument("--index", default="BRTI", help="CF Benchmarks index ID that settles the series")


def _add_strategy_overrides(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("strategy settings (freeze these before unsealing the evaluation period)")
    for item in fields(StrategyConfig):
        flag = "--" + item.name.replace("_", "-")
        group.add_argument(
            flag,
            dest=f"strategy_{item.name}",
            type=type(item.default),
            default=None,
            metavar="VALUE",
            help=f"default: {item.default}",
        )


def strategy_from_args(args: argparse.Namespace) -> StrategyConfig:
    overrides = {
        item.name: getattr(args, f"strategy_{item.name}")
        for item in fields(StrategyConfig)
        if getattr(args, f"strategy_{item.name}", None) is not None
    }
    config = StrategyConfig(**overrides)
    config.validate()
    return config


def fees_from_args(args: argparse.Namespace, store: RecorderStore) -> FeeSchedule:
    series = store.get_series(args.series) or {}
    fee_type = args.fee_type or series.get("fee_type") or "quadratic"
    multiplier = args.fee_multiplier if args.fee_multiplier is not None else series.get("fee_multiplier")
    if multiplier is None:
        CONSOLE.print("[yellow]Series fee data not recorded yet; assuming fee multiplier 1.0.[/yellow]")
        multiplier = 1.0
    return FeeSchedule(fee_type=fee_type, multiplier=float(multiplier), rounding=args.fee_rounding)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kalshi-15m",
        description="Record Kalshi 15-minute crypto markets and run an honest paper-trading study. Never places orders.",
    )
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    record = commands.add_parser("record", help="Record markets, orderbooks, trades, settlements, and the index feed")
    _add_common(record)
    record.add_argument("--demo", action="store_true", help="Use Kalshi's demo environment")
    record.add_argument("--book-interval", type=float, default=2.0, help="Seconds between orderbook polls")
    record.add_argument("--book-depth", type=int, default=10)
    record.add_argument(
        "--allow-no-index",
        action="store_true",
        help="Record market data even without API credentials (the model cannot run on such periods)",
    )

    check = commands.add_parser("check", help="Verify live access, payload fields, and the index feed (read-only)")
    check.add_argument("--series", default="KXBTC15M", help="Kalshi series ticker")
    check.add_argument("--index", default="BRTI", help="CF Benchmarks index ID that settles the series")
    check.add_argument("--demo", action="store_true", help="Use Kalshi's demo environment")
    check.add_argument("--timeout", type=float, default=20.0, help="Seconds to wait for index ticks")

    report = commands.add_parser("report", help="Replay recorded data and write the dashboard")
    _add_common(report)
    report.add_argument("--output-dir", default="reports")
    report.add_argument("--split-date", help="First evaluation day (YYYY-MM-DD, in --day-timezone)")
    report.add_argument("--eval-fraction", type=float, default=0.3, help="Share of recorded days held out")
    report.add_argument(
        "--evaluate",
        action="store_true",
        help="Unseal the held-out evaluation period (logged; do this once, after freezing settings)",
    )
    report.add_argument("--every", type=float, default=0.0, help="Regenerate every N seconds (0 = once)")
    report.add_argument("--fee-type", default=None, help="Override the recorded series fee_type")
    report.add_argument("--fee-multiplier", type=float, default=None, help="Override the recorded series multiplier")
    report.add_argument("--fee-rounding", choices=["cent", "centicent", "none"], default="cent")
    report.add_argument("--min-trades", type=int, default=EvidenceCriteria().min_trades)
    report.add_argument("--min-active-days", type=int, default=EvidenceCriteria().min_active_days)
    _add_strategy_overrides(report)

    status = commands.add_parser("status", help="Show what has been recorded so far")
    _add_common(status)

    fee = commands.add_parser("fees", help="Fee calculator for a round trip or a hold to settlement")
    fee.add_argument("--entry", type=float, required=True, help="Entry price in dollars, e.g. 0.50")
    fee.add_argument("--exit", type=float, help="Exit price in dollars; omit to show hold-to-settlement")
    fee.add_argument("--contracts", type=int, default=1)
    fee.add_argument("--multiplier", type=float, default=1.0)
    fee.add_argument("--rounding", choices=["cent", "centicent", "none"], default="cent")
    return parser


async def run_record(args: argparse.Namespace) -> int:
    signer = KalshiSigner.from_env()
    if signer is None and not args.allow_no_index:
        CONSOLE.print(
            "[bold red]No Kalshi API credentials found.[/bold red] The settlement index (CF Benchmarks) is only "
            "available on Kalshi's authenticated WebSocket.\nSet KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH "
            "(a local path to your private key file), or pass --allow-no-index to record market data only."
        )
        return 2
    settings = RecorderSettings(
        series_ticker=args.series,
        index_id=args.index,
        database_path=args.db,
        rest_url=DEMO_REST_URL if args.demo else PROD_REST_URL,
        ws_url=DEMO_WS_URL if args.demo else PROD_WS_URL,
        book_interval_seconds=max(0.5, args.book_interval),
        book_depth=args.book_depth,
    )
    CONSOLE.print(f"Recording {args.series} and {args.index} into {Path(args.db).resolve()} (Ctrl+C to stop)")
    await Recorder(settings, signer=signer, console=CONSOLE).run()
    return 0


async def run_check(args: argparse.Namespace) -> int:
    signer = KalshiSigner.from_env()
    client = KalshiClient(ClientSettings(retries=1), base_url=DEMO_REST_URL if args.demo else PROD_REST_URL, signer=signer)
    CONSOLE.print(f"Checking Kalshi access for {args.series} / {args.index} (read-only)...")
    results = await run_checks(
        client,
        signer,
        series_ticker=args.series,
        index_id=args.index,
        ws_url=DEMO_WS_URL if args.demo else PROD_WS_URL,
        timeout_seconds=args.timeout,
    )
    table = Table(title="Kalshi live check")
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Detail")
    styles = {"pass": "green", "warn": "yellow", "skip": "yellow", "fail": "bold red"}
    for result in results:
        table.add_row(result.name, f"[{styles[result.status]}]{result.status.upper()}[/]", result.detail)
    CONSOLE.print(table)
    blocked = [result for result in results if result.status in {"fail", "skip"}]
    if blocked:
        CONSOLE.print("[bold red]Not ready to record.[/bold red] Fix the items above, then run the check again.")
        return 1
    CONSOLE.print("[green]Ready to record:[/green] python main.py record")
    return 0


def run_report(args: argparse.Namespace) -> int:
    store = RecorderStore(args.db)
    config = strategy_from_args(args)
    fees = fees_from_args(args, store)
    criteria = EvidenceCriteria(min_trades=args.min_trades, min_active_days=args.min_active_days)
    split_ts = day_start(args.split_date, config.day_timezone) if args.split_date else None
    while True:
        report = run_study(
            store,
            series_ticker=args.series,
            index_id=args.index,
            config=config,
            fees=fees,
            split_ts=split_ts,
            eval_fraction=args.eval_fraction,
            evaluate=args.evaluate,
            criteria=criteria,
        )
        json_path, html_path = write_report_files(report, args.output_dir)
        render_console(report, CONSOLE)
        CONSOLE.print(f"[green]Dashboard:[/green] {html_path.resolve()}")
        CONSOLE.print(f"[green]JSON:[/green] {json_path.resolve()}")
        if args.every <= 0 or args.evaluate:
            return 0
        time.sleep(args.every)


def run_status(args: argparse.Namespace) -> int:
    store = RecorderStore(args.db)
    coverage = store.coverage(args.series, args.index)
    table = Table(title=f"Recorded data in {args.db}")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Markets (settled)", f"{coverage['markets']} ({coverage['markets_resolved']})")
    table.add_row("Orderbook snapshots", str(coverage["book_snapshots"]))
    table.add_row("First / last snapshot", f"{when(coverage['first_book_ts'])} / {when(coverage['last_book_ts'])}")
    table.add_row("Index ticks (last 24h)", f"{coverage['index_ticks']} ({coverage['index_ticks_last_24h']})")
    table.add_row("Last index tick", when(coverage["last_tick_ts"]))
    table.add_row("Trades", str(coverage["trades"]))
    CONSOLE.print(table)
    for event in coverage["recent_events"]:
        CONSOLE.print(f"[dim]{when(event['ts'])}[/dim] {event['level']}: {event['message']}")
    return 0


def _cents(amount: float) -> str:
    sign = "−" if amount < 0 else ""
    return f"{sign}{abs(amount) * 100:.2f}¢"


def run_fees(args: argparse.Namespace) -> int:
    schedule = FeeSchedule(multiplier=args.multiplier, rounding=args.rounding)
    if args.exit is not None:
        result = round_trip(args.entry, args.exit, args.contracts, schedule)
        CONSOLE.print(
            f"Buy {args.contracts} @ {args.entry:.2f} → sell @ {args.exit:.2f}: gross {_cents(result['gross'])}, "
            f"fees {_cents(result['entry_fee'])} + {_cents(result['exit_fee'])}, "
            f"net [bold]{_cents(result['net'])}[/bold] (fee rounding: {args.rounding})"
        )
    else:
        for won in (True, False):
            result = hold_to_settlement(args.entry, won, args.contracts, schedule)
            CONSOLE.print(
                f"Buy {args.contracts} @ {args.entry:.2f}, hold, {'wins' if won else 'loses'}: "
                f"fee {_cents(result['entry_fee'])}, net [bold]{_cents(result['net'])}[/bold]"
            )
        breakeven = args.entry + schedule.taker_fee(args.entry, args.contracts) / args.contracts
        CONSOLE.print(f"Break-even win probability: {breakeven:.1%}")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.command == "record":
            return asyncio.run(run_record(args))
        if args.command == "check":
            return asyncio.run(run_check(args))
        if args.command == "report":
            if args.split_date:
                datetime.fromisoformat(args.split_date)
            return run_report(args)
        if args.command == "status":
            return run_status(args)
        if args.command == "fees":
            return run_fees(args)
    except ValueError as exc:
        parser.error(str(exc))
    except KalshiAPIError as exc:
        CONSOLE.print(f"[bold red]Kalshi error:[/bold red] {exc}")
        return 2
    except KeyboardInterrupt:
        CONSOLE.print("\n[yellow]Stopped.[/yellow]")
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
