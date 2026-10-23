"""Static HTML dashboard, JSON export, and console summary for a paper-trading study."""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

POLICY_LABELS = {"hold": "Hold to settlement", "early_exit": "Sell early when edge is gone"}
REASON_LABELS = {
    "edge_below_threshold": "Edge below threshold after fees and margin",
    "outside_time_window": "Outside allowed time-to-close window",
    "position_open": "Already holding or ordering (one position at a time)",
    "daily_loss_stop": "Daily loss stop reached",
    "price_out_of_range": "Ask outside allowed price range",
    "no_liquidity": "No offers in the book",
    "budget_or_depth_too_small": "Budget or visible depth below one contract",
    "no_index_data": "No CF Benchmarks data recorded yet",
    "stale_index": "CF Benchmarks data stale",
    "insufficient_vol_history": "Not enough index history for volatility",
    "missing_window_average": "Settlement-window average missing",
    "missing_strike": "Market strike not available",
    "unsupported_strike_type": "Unsupported strike type",
}


def _f(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def money(value: Any) -> str:
    amount = _f(value)
    if amount is None:
        return "—"
    sign = "−" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def cents(value: Any) -> str:
    amount = _f(value)
    return "—" if amount is None else f"{amount * 100:.1f}¢"


def pct(value: Any) -> str:
    amount = _f(value)
    return "—" if amount is None else f"{amount * 100:.1f}%"


def when(ts: Any) -> str:
    value = _f(ts)
    if value is None:
        return "—"
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def ci_text(ci: Sequence[float] | None) -> str:
    return "—" if not ci else f"{money(ci[0])} to {money(ci[1])}"


def _ci_plain(ci: Sequence[float] | None) -> str | None:
    return None if not ci else f"{ci[0]:+.4f} to {ci[1]:+.4f}"


def check_value(value: Any) -> str:
    """Readable form of a readiness check's measured value."""
    if value is None:
        return "not enough data"
    if isinstance(value, Mapping):
        parts = [f"{key.replace('_', ' ')} {check_value(item)}" for key, item in value.items()]
        return ", ".join(parts)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return f"{value[0]:+.4g} to {value[1]:+.4g}"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _e(value: Any) -> str:
    return html.escape("—" if value is None or value == "" else str(value))


def _results(report: Mapping[str, Any]) -> list[tuple[str, str, Mapping[str, Any]]]:
    columns: list[tuple[str, str, Mapping[str, Any]]] = []
    for period in ("development", "evaluation"):
        for policy, result in (report.get(period) or {}).items():
            columns.append((period, policy, result))
    return columns


METRIC_ROWS: list[tuple[str, Any]] = [
    ("Trades", lambda r: r["summary"]["trades"]),
    ("Net P&L after fees", lambda r: money(r["summary"]["total_pnl"])),
    ("Fees paid", lambda r: money(r["summary"]["total_fees"])),
    ("Average per trade", lambda r: money(r["summary"]["avg_pnl_per_trade"])),
    ("Per-trade 95% CI", lambda r: ci_text(r["summary"]["trade_pnl_ci95"])),
    ("Win rate", lambda r: pct(r["summary"]["win_rate"])),
    ("Max drawdown", lambda r: money(r["summary"]["max_drawdown"])),
    ("Days / active / positive", lambda r: f"{r['summary']['days']} / {r['summary']['active_days']} / {r['summary']['positive_days']}"),
    ("Mean daily P&L", lambda r: money(r["summary"]["mean_daily_pnl"])),
    ("Daily 95% CI", lambda r: ci_text(r["summary"]["daily_pnl_ci95"])),
    ("Entry orders / fills / missed", lambda r: f"{r['counters']['entry_orders']} / {r['counters']['fills']} / {r['counters']['missed_fills']}"),
    ("Partial fills", lambda r: r["counters"]["partial_fills"]),
    ("Early-exit orders / fills", lambda r: f"{r['counters']['exit_orders']} / {r['counters']['exit_fills']}"),
    ("Provisional / unresolved settlements", lambda r: f"{r['counters']['provisional_settlements']} / {r['counters']['unresolved_positions']}"),
]


def _equity_svg(series: Sequence[tuple[str, Sequence[Sequence[float]], str]], *, width: int = 720, height: int = 220) -> str:
    points_all = [point for _, points, _ in series for point in points]
    if not points_all:
        return '<p class="muted">No closed trades yet, so there is no equity curve.</p>'
    xs = [point[0] for point in points_all]
    ys = [point[1] for point in points_all] + [0.0]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 == x0:
        x1 = x0 + 1
    if y1 == y0:
        y1 = y0 + 1
    pad = 36

    def sx(x: float) -> float:
        return pad + (x - x0) / (x1 - x0) * (width - pad - 8)

    def sy(y: float) -> float:
        return 8 + (y1 - y) / (y1 - y0) * (height - 8 - 24)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Cumulative paper P&amp;L" class="chart">',
        f'<line x1="{pad}" x2="{width - 8}" y1="{sy(0):.1f}" y2="{sy(0):.1f}" class="axis"/>',
        f'<text x="4" y="{sy(y1) + 4:.1f}" class="tick">{html.escape(money(y1))}</text>',
        f'<text x="4" y="{sy(y0) + 4:.1f}" class="tick">{html.escape(money(y0))}</text>',
        f'<text x="{pad}" y="{height - 6}" class="tick">{html.escape(when(x0)[:16])}</text>',
        f'<text x="{width - 8}" y="{height - 6}" class="tick" text-anchor="end">{html.escape(when(x1)[:16])}</text>',
    ]
    for label, points, css in series:
        if not points:
            continue
        path = [(sx(points[0][0]), sy(0.0))] + [(sx(x), sy(y)) for x, y in points]
        coordinates = " ".join(f"{x:.1f},{y:.1f}" for x, y in path)
        parts.append(f'<polyline points="{coordinates}" class="line {css}"><title>{html.escape(label)}</title></polyline>')
    parts.append("</svg>")
    legend = "".join(
        f'<span class="legend"><span class="swatch {css}"></span>{html.escape(label)}</span>' for label, _, css in series
    )
    return "".join(parts) + f'<div class="legends">{legend}</div>'


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]], *, numeric_from: int = 1) -> str:
    head = "".join(f'<th class="{"num" if i >= numeric_from else ""}">{html.escape(h)}</th>' for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f'<td class="{"num" if i >= numeric_from else ""}">{_e(cell)}</td>' for i, cell in enumerate(row)) + "</tr>"
        for row in rows
    )
    if not rows:
        body = f'<tr><td colspan="{len(headers)}" class="muted">Nothing to show yet.</td></tr>'
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def build_dashboard_html(report: Mapping[str, Any]) -> str:
    coverage = report.get("coverage", {})
    columns = _results(report)
    period_label = {"development": "Development", "evaluation": "Evaluation"}

    # Readiness
    readiness_html = []
    for policy, verdict in (report.get("readiness") or {}).items():
        status = verdict.get("status", "sealed")
        label = {"met": "Evidence criteria met", "not_met": "Evidence criteria not met", "sealed": "Evaluation sealed"}[status]
        checks = "".join(
            f'<li class="{"pass" if check["passed"] else "fail"}">{"✓" if check["passed"] else "✗"} '
            f'{html.escape(check["check"])} <span class="muted">({html.escape(check_value(check["value"]))})</span></li>'
            for check in verdict.get("checks", [])
        )
        readiness_html.append(
            f'<div class="card status-{status}"><div class="label">{html.escape(POLICY_LABELS.get(policy, policy))}</div>'
            f'<div class="value">{label}</div><ul class="checks">{checks}</ul></div>'
        )

    # Policy comparison
    comparison_headers = ["Metric"] + [f"{period_label[p]} · {POLICY_LABELS.get(k, k)}" for p, k, _ in columns]
    comparison_rows = [[name] + [getter(result) for _, _, result in columns] for name, getter in METRIC_ROWS]

    # Equity curves
    css_cycle = ["s1", "s2", "s3", "s4"]
    equity_series = [
        (f"{period_label[p]} · {POLICY_LABELS.get(k, k)}", result.get("equity", []), css_cycle[i % 4])
        for i, (p, k, result) in enumerate(columns)
    ]

    primary = (report.get("development") or {}).get("hold")
    skip_rows: list[list[Any]] = []
    opportunity_rows: list[list[Any]] = []
    trade_rows: list[list[Any]] = []
    daily_rows: list[list[Any]] = []
    calibration_rows: list[list[Any]] = []
    calibration_summary = ""
    if primary:
        total_skips = sum(primary["skip_reasons"].values()) or 1
        skip_rows = [
            [REASON_LABELS.get(reason, reason), count, pct(count / total_skips)]
            for reason, count in primary["skip_reasons"].items()
        ]
        for item in reversed(primary.get("opportunities", [])[-60:]):
            opportunity_rows.append(
                [
                    when(item.get("ts")),
                    item.get("ticker"),
                    (item.get("side") or "").upper(),
                    f"{_f(item.get('seconds_to_close')) or 0:.0f}s",
                    pct(item.get("p_yes")),
                    cents(item.get("best_ask")),
                    cents(item.get("limit_price")),
                    cents(item.get("edge")),
                    f"{item.get('filled', 0):.0f}/{item.get('requested', 0):.0f}",
                    item.get("fill_status"),
                ]
            )
        for trade in reversed(primary.get("trades", [])[-60:]):
            trade_rows.append(
                [
                    when(trade.get("entry_ts")),
                    trade.get("ticker"),
                    (trade.get("side") or "").upper(),
                    f"{trade.get('contracts', 0):.0f}",
                    cents(trade.get("avg_entry_price")),
                    money(trade.get("fees")),
                    trade.get("exit_type"),
                    money(trade.get("pnl")),
                ]
            )
        daily_rows = [[row["day"], row["trades"], money(row["pnl"])] for row in reversed(primary.get("daily", []))]
        calibration = primary.get("calibration", {})
        calibration_rows = [
            [bucket["range"], bucket["n"], pct(bucket["mean_model_p"]), pct(bucket["observed_yes_rate"])]
            for bucket in calibration.get("buckets", [])
        ]
        calibration_summary = (
            f"Brier score on {calibration.get('paired_samples', 0)} paired samples from "
            f"{calibration.get('markets', 0)} settled markets: model "
            f"{_e(calibration.get('paired_model_brier'))} vs market mid {_e(calibration.get('paired_market_brier'))}; "
            f"95% CI of the per-market difference (model minus market): "
            f"{_e(_ci_plain(calibration.get('brier_difference_ci95')))}. Lower is better: the model must beat the "
            "market by more than noise for any edge to be plausible."
        )

    config_rows = [[key, value] for key, value in (report.get("config") or {}).items()]
    fee_rows = [[key, value] for key, value in (report.get("fees") or {}).items()]
    events = coverage.get("recent_events", [])
    event_rows = [[when(event["ts"]), event["level"], event["message"]] for event in events]
    notes = "".join(f"<li>{html.escape(note)}</li>" for note in report.get("notes", []))
    periods = report.get("periods", {})
    evaluation_period = periods.get("evaluation")
    period_text = (
        f"Development {when(periods['development']['start'])} → {when(periods['development']['end'])}"
        if periods.get("development")
        else "No data yet"
    )
    if evaluation_period:
        sealed = " (sealed)" if evaluation_period.get("sealed") else ""
        period_text += f" · Evaluation {when(evaluation_period['start'])} → {when(evaluation_period['end'])}{sealed}"

    cards = [
        ("Markets recorded", f"{coverage.get('markets', 0):,} ({coverage.get('markets_resolved', 0):,} settled)"),
        ("Orderbook snapshots", f"{coverage.get('book_snapshots', 0):,}"),
        ("Index ticks", f"{coverage.get('index_ticks', 0):,} ({coverage.get('index_ticks_last_24h', 0):,} in last 24h)"),
        ("Trades captured", f"{coverage.get('trades', 0):,}"),
    ]
    card_html = "".join(
        f'<div class="card"><div class="label">{html.escape(label)}</div><div class="value">{html.escape(value)}</div></div>'
        for label, value in cards
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalshi 15m Paper Study</title>
<style>
:root {{
  --bg: #f7f7f5; --surface: #ffffff; --text: #1d1d1b; --muted: #6b6b66; --border: #e3e2dc;
  --good: #1f7a4d; --bad: #b3261e; --warn: #8a6d00;
  --s1: #2f6fdf; --s2: #d0711b; --s3: #2b9c8f; --s4: #9a4fc8;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg: #161615; --surface: #20201e; --text: #ecebe6; --muted: #a3a29b; --border: #34332f;
    --good: #5cc28d; --bad: #f08a80; --warn: #e5c158;
    --s1: #7aa7ff; --s2: #f0a35e; --s3: #5fd0c2; --s4: #c99af0;
  }}
}}
:root[data-theme="dark"] {{
  --bg: #161615; --surface: #20201e; --text: #ecebe6; --muted: #a3a29b; --border: #34332f;
  --good: #5cc28d; --bad: #f08a80; --warn: #e5c158;
  --s1: #7aa7ff; --s2: #f0a35e; --s3: #5fd0c2; --s4: #c99af0;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
h2 {{ font-size: 16px; margin: 32px 0 10px; }}
.muted {{ color: var(--muted); }}
ul.banner {{ padding-left: 32px; }}
.banner {{ border: 1px solid var(--border); background: var(--surface); padding: 10px 14px; border-radius: 8px; margin: 14px 0; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }}
.card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }}
.card .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
.card .value {{ font-size: 17px; font-weight: 600; margin-top: 2px; }}
.status-met .value {{ color: var(--good); }}
.status-not_met .value {{ color: var(--bad); }}
.status-sealed .value {{ color: var(--warn); }}
.checks {{ list-style: none; padding: 0; margin: 8px 0 0; font-size: 13px; }}
.checks .pass {{ color: var(--good); }} .checks .fail {{ color: var(--bad); }}
.scroll {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ padding: 7px 10px; border-bottom: 1px solid var(--border); text-align: left; white-space: nowrap; }}
th {{ color: var(--muted); font-weight: 600; }}
td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
tr:last-child td {{ border-bottom: 0; }}
.chart {{ width: 100%; height: auto; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; }}
.chart .axis {{ stroke: var(--border); stroke-width: 1; }}
.chart .tick {{ fill: var(--muted); font-size: 11px; }}
.chart .line {{ fill: none; stroke-width: 2; }}
.line.s1, .swatch.s1 {{ stroke: var(--s1); background: var(--s1); }}
.line.s2, .swatch.s2 {{ stroke: var(--s2); background: var(--s2); }}
.line.s3, .swatch.s3 {{ stroke: var(--s3); background: var(--s3); }}
.line.s4, .swatch.s4 {{ stroke: var(--s4); background: var(--s4); }}
.legends {{ display: flex; flex-wrap: wrap; gap: 14px; margin-top: 6px; color: var(--muted); font-size: 12px; }}
.swatch {{ display: inline-block; width: 12px; height: 3px; margin-right: 6px; vertical-align: middle; }}
</style>
</head>
<body>
<main>
<h1>Kalshi 15-minute paper study</h1>
<div class="muted">{_e(report.get("series_ticker"))} vs {_e(report.get("index_id"))} · generated {_e(when(report.get("generated_at")))} · strategy {_e(report.get("config_fingerprint"))}</div>
<div class="banner">Paper trading only: this tool records data and simulates taker fills. It never places orders. {_e(period_text)}</div>
{f'<ul class="banner">{notes}</ul>' if notes else ''}

<h2>Pilot readiness (judged on the held-out evaluation period only)</h2>
<div class="grid">{''.join(readiness_html)}</div>

<h2>Data coverage</h2>
<div class="grid">{card_html}</div>

<h2>Results by exit policy</h2>
{_table(comparison_headers, comparison_rows)}

<h2>Cumulative paper P&amp;L</h2>
{_equity_svg(equity_series)}

<h2>Why opportunities were skipped (development · hold)</h2>
{_table(["Reason", "Snapshots", "Share"], skip_rows)}

<h2>Recent entry signals and simulated fills (development · hold)</h2>
{_table(["Decided", "Market", "Side", "To close", "Model P(yes)", "Ask", "Limit", "Edge", "Filled", "Status"], opportunity_rows, numeric_from=3)}

<h2>Closed paper trades (development · hold)</h2>
{_table(["Entered", "Market", "Side", "Contracts", "Avg price", "Fees", "Exit", "Net P&L"], trade_rows, numeric_from=3)}

<h2>Daily P&amp;L (development · hold)</h2>
{_table(["Day", "Trades", "Net P&L"], daily_rows)}

<h2>Model calibration (development)</h2>
<p class="muted">{calibration_summary or "No settled markets with model probabilities yet."}</p>
{_table(["Model P(yes) bucket", "Samples", "Mean model P", "Observed YES rate"], calibration_rows)}

<h2>Strategy settings</h2>
{_table(["Setting", "Value"], config_rows)}

<h2>Fee model</h2>
{_table(["Setting", "Value"], fee_rows)}

<h2>Recent recorder events</h2>
{_table(["Time", "Level", "Message"], event_rows, numeric_from=99)}
</main>
</body>
</html>
"""


def write_report_files(report: Mapping[str, Any], output_dir: str | Path) -> tuple[Path, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "study.json"
    html_path = directory / "dashboard.html"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    html_path.write_text(build_dashboard_html(report), encoding="utf-8")
    return json_path, html_path


def render_console(report: Mapping[str, Any], console: Console | None = None) -> None:
    console = console or Console()
    console.print(
        f"[bold]Kalshi 15m paper study[/bold] — {report.get('series_ticker')} vs {report.get('index_id')} "
        f"(strategy {report.get('config_fingerprint')})"
    )
    coverage = report.get("coverage", {})
    console.print(
        f"Markets: {coverage.get('markets', 0)} ({coverage.get('markets_resolved', 0)} settled) | "
        f"books: {coverage.get('book_snapshots', 0)} | index ticks: {coverage.get('index_ticks', 0)}"
    )
    columns = _results(report)
    if columns:
        table = Table(title="Results by exit policy")
        table.add_column("Metric")
        for period, policy, _ in columns:
            table.add_column(f"{period[:3]} · {policy}", justify="right")
        for name, getter in METRIC_ROWS:
            table.add_row(name, *[str(getter(result)) for _, _, result in columns])
        console.print(table)
        primary = (report.get("development") or {}).get("hold")
        if primary and primary["skip_reasons"]:
            reasons = Table(title="Top skip reasons (development · hold)")
            reasons.add_column("Reason")
            reasons.add_column("Snapshots", justify="right")
            for reason, count in list(primary["skip_reasons"].items())[:8]:
                reasons.add_row(REASON_LABELS.get(reason, reason), str(count))
            console.print(reasons)
    for policy, verdict in (report.get("readiness") or {}).items():
        console.print(f"Readiness ({POLICY_LABELS.get(policy, policy)}): [bold]{verdict.get('status')}[/bold]")
        for check in verdict.get("checks", []):
            mark = "[green]✓[/green]" if check["passed"] else "[red]✗[/red]"
            console.print(f"  {mark} {check['check']} ({check_value(check['value'])})")
    for note in report.get("notes", []):
        console.print(f"[yellow]Note:[/yellow] {note}")
