"""Terminal, Markdown, JSON, and Telegram-friendly reporting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from rich.console import Console
from rich.table import Table


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _money(value: Any) -> str:
    amount = _float(value)
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def _percent(value: Any) -> str:
    return f"{100.0 * _float(value):.1f}%"


def _wallet_short(wallet: str) -> str:
    return wallet if len(wallet) <= 13 else f"{wallet[:6]}…{wallet[-4:]}"


def _escape(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def render_console(analysis: Mapping[str, Any], console: Console | None = None) -> None:
    console = console or Console()
    summary = analysis.get("summary", {})
    status = analysis.get("scan_status", "unknown")
    console.print(
        f"[bold]Polymarket Scanner[/bold] — scan {analysis.get('scan_id')} "
        f"([cyan]{status}[/cyan])"
    )
    console.print(
        f"Wallets: {summary.get('trader_count', 0)} | "
        f"partial: {summary.get('wallets_partial', 0)} | failed: {summary.get('wallets_failed', 0)} | "
        f"clusters: {summary.get('cluster_count', 0)}"
    )

    trader_table = Table(title="Top Trader Scores")
    trader_table.add_column("#", justify="right")
    trader_table.add_column("Trader")
    trader_table.add_column("Wallet")
    trader_table.add_column("Score", justify="right")
    trader_table.add_column("PnL", justify="right")
    trader_table.add_column("ROI", justify="right")
    trader_table.add_column("Win", justify="right")
    trader_table.add_column("Data")
    for trader in analysis.get("top_traders", [])[:15]:
        trader_table.add_row(
            str(trader.get("analysis_rank", "")),
            str(trader.get("username") or "anonymous"),
            _wallet_short(str(trader.get("wallet") or "")),
            f"{_float(trader.get('trader_rank_score')):.1f}",
            _money(trader.get("pnl")),
            _percent(trader.get("roi")),
            _percent(trader.get("win_rate")),
            str(trader.get("data_status") or "unknown"),
        )
    console.print(trader_table)

    cluster_table = Table(title="Detected Clusters")
    cluster_table.add_column("Cluster")
    cluster_table.add_column("Members", justify="right")
    cluster_table.add_column("Score", justify="right")
    cluster_table.add_column("Jaccard", justify="right")
    cluster_table.add_column("Cosine", justify="right")
    cluster_table.add_column("Co-trades", justify="right")
    cluster_table.add_column("PnL", justify="right")
    for cluster in analysis.get("clusters", [])[:15]:
        cluster_table.add_row(
            str(cluster.get("cluster_id")),
            str(cluster.get("member_count", 0)),
            f"{_float(cluster.get('cluster_score')):.1f}",
            _percent(cluster.get("average_position_overlap")),
            _percent(cluster.get("average_value_cosine")),
            str(cluster.get("total_trade_cooccurrences", 0)),
            _money(cluster.get("combined_pnl")),
        )
    console.print(cluster_table)

    if analysis.get("fetch_errors"):
        console.print(
            f"[yellow]Partial endpoint errors recorded for "
            f"{len(analysis['fetch_errors'])} wallet(s).[/yellow]"
        )


def build_markdown(analysis: Mapping[str, Any]) -> str:
    summary = analysis.get("summary", {})
    thresholds = analysis.get("thresholds", {})
    validation = analysis.get("validation", {})
    lines = [
        "# Polymarket Scanner Summary",
        "",
        f"Generated: `{_escape(analysis.get('generated_at'))}`  ",
        f"Scan fetched: `{_escape(analysis.get('scan_fetched_at'))}`  ",
        f"Scan ID: `{_escape(analysis.get('scan_id'))}`  ",
        f"Scan status: **{_escape(analysis.get('scan_status'))}**",
        "",
        "## Executive Summary",
        "",
        f"- Wallets analyzed: **{summary.get('trader_count', 0)}**",
        f"- Complete wallets: **{summary.get('wallets_ok', 0)}**",
        f"- Partial wallets: **{summary.get('wallets_partial', 0)}**",
        f"- Failed wallets: **{summary.get('wallets_failed', 0)}**",
        f"- Flagged portfolio pairs: **{summary.get('flagged_position_pair_count', 0)}**",
        f"- Flagged synchronized-trade pairs: **{summary.get('flagged_trade_pair_count', 0)}**",
        f"- Connected clusters: **{summary.get('cluster_count', 0)}**",
        "",
        "## Trader Ranking",
        "",
        "| Rank | Trader | Wallet | Score | PnL | ROI | Win rate | Recent volume | Data status | Confidence |",
        "|---:|---|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for trader in analysis.get("top_traders", []):
        lines.append(
            "| {rank} | {name} | `{wallet}` | {score:.2f} | {pnl} | {roi} | {win} | {volume} | {status} | {confidence} |".format(
                rank=trader.get("analysis_rank", ""),
                name=_escape(trader.get("username") or "anonymous"),
                wallet=_escape(trader.get("wallet")),
                score=_float(trader.get("trader_rank_score")),
                pnl=_money(trader.get("pnl")),
                roi=_percent(trader.get("roi")),
                win=_percent(trader.get("win_rate")),
                volume=_money(trader.get("recent_active_volume")),
                status=_escape(trader.get("data_status")),
                confidence=_escape(trader.get("score_confidence")),
            )
        )

    lines.extend(
        [
            "",
            "## Wallet Clusters",
            "",
            "| Cluster | Members | Score | Avg Jaccard | Avg cosine | Co-trades | Combined PnL |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for cluster in analysis.get("clusters", []):
        lines.append(
            f"| {_escape(cluster.get('cluster_id'))} | {cluster.get('member_count', 0)} | "
            f"{_float(cluster.get('cluster_score')):.2f} | "
            f"{_percent(cluster.get('average_position_overlap'))} | "
            f"{_percent(cluster.get('average_value_cosine'))} | "
            f"{cluster.get('total_trade_cooccurrences', 0)} | {_money(cluster.get('combined_pnl'))} |"
        )
        lines.append("")
        lines.append("Members: " + ", ".join(f"`{wallet}`" for wallet in cluster.get("wallets", [])))
        lines.append("")

    lines.extend(
        [
            "## Flagged Position Overlap",
            "",
            "| Wallet A | Wallet B | Jaccard | Cosine | Shared outcomes |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for pair in analysis.get("position_overlap_pairs", []):
        lines.append(
            f"| `{_escape(pair.get('wallet_a'))}` | `{_escape(pair.get('wallet_b'))}` | "
            f"{_percent(pair.get('jaccard_similarity'))} | {_percent(pair.get('cosine_similarity'))} | "
            f"{pair.get('shared_position_count', 0)} |"
        )
        for position in pair.get("shared_positions", [])[:10]:
            niche = " — niche in this sample" if position.get("niche_in_sample") else ""
            lines.append(
                f"  - **{_escape(position.get('title') or position.get('market_slug'))}** / "
                f"{_escape(position.get('outcome'))}: {_money(position.get('wallet_a_value'))} vs "
                f"{_money(position.get('wallet_b_value'))}{niche}"
            )

    lines.extend(
        [
            "",
            "## Synchronized Trades",
            "",
            "| Wallet A | Wallet B | Matches | Markets | Sync rate | Avg Δt | Combined USDC |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for pair in analysis.get("trade_cooccurrence_pairs", []):
        lines.append(
            f"| `{_escape(pair.get('wallet_a'))}` | `{_escape(pair.get('wallet_b'))}` | "
            f"{pair.get('cooccurrence_count', 0)} | {pair.get('unique_market_count', 0)} | "
            f"{_percent(pair.get('sync_rate'))} | "
            f"{_float(pair.get('average_time_difference_seconds')):.1f}s | "
            f"{_money(pair.get('total_combined_usdc'))} |"
        )

    lines.extend(
        [
            "",
            "## Thresholds",
            "",
            f"- Jaccard minimum: **{thresholds.get('position_jaccard_min')}**",
            f"- Cosine minimum: **{thresholds.get('position_cosine_min')}**",
            f"- Minimum shared active outcomes: **{thresholds.get('minimum_shared_positions')}**",
            f"- Trade window: **{thresholds.get('trade_window_minutes')} minutes**",
            f"- Minimum co-occurrences: **{thresholds.get('minimum_trade_cooccurrences')}**",
            f"- Minimum sync rate: **{thresholds.get('minimum_sync_rate')}**",
            "",
            "## Ground-Truth Validation Status",
            "",
            f"- Status: **{_escape(validation.get('status'))}**",
            f"- Known coordinated-wallet examples checked: **{validation.get('known_coordinated_wallet_ground_truth_checked')}**",
            f"- Reason: {_escape(validation.get('reason'))}",
        ]
    )

    errors = analysis.get("fetch_errors", {})
    if errors:
        lines.extend(["", "## Partial Fetch Errors", ""])
        for wallet, wallet_errors in errors.items():
            lines.append(
                f"- `{wallet}`: "
                + "; ".join(f"{key}: {_escape(value)}" for key, value in wallet_errors.items())
            )

    lines.extend(["", "## Methodology and Caveats", ""])
    for note in analysis.get("methodology_notes", []):
        lines.append(f"- {_escape(note)}")
    lines.append("")
    return "\n".join(lines)


def write_reports(
    analysis: Mapping[str, Any], output_dir: str | Path = "."
) -> tuple[Path, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "analysis.json"
    markdown_path = directory / "summary_report.md"
    json_path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    markdown_path.write_text(build_markdown(analysis), encoding="utf-8")
    return json_path, markdown_path


def telegram_summary(analysis: Mapping[str, Any], *, max_wallets: int = 5, max_clusters: int = 3) -> str:
    summary = analysis.get("summary", {})
    lines = [
        "Polymarket Scanner",
        f"Status: {analysis.get('scan_status', 'unknown')}",
        f"Wallets: {summary.get('trader_count', 0)} "
        f"({summary.get('wallets_partial', 0)} partial, {summary.get('wallets_failed', 0)} failed)",
        f"Clusters: {summary.get('cluster_count', 0)} | "
        f"Position pairs: {summary.get('flagged_position_pair_count', 0)} | "
        f"Trade pairs: {summary.get('flagged_trade_pair_count', 0)}",
        "",
        "Top wallets:",
    ]
    for trader in analysis.get("top_traders", [])[:max_wallets]:
        lines.append(
            f"{trader.get('analysis_rank')}. {trader.get('username') or 'anonymous'} "
            f"({_wallet_short(str(trader.get('wallet') or ''))}) — "
            f"score {_float(trader.get('trader_rank_score')):.1f}, PnL {_money(trader.get('pnl'))}"
        )

    clusters = analysis.get("clusters", [])[:max_clusters]
    if clusters:
        lines.extend(["", "Top clusters:"])
        for cluster in clusters:
            lines.append(
                f"{cluster.get('cluster_id')}: {cluster.get('member_count')} wallets, "
                f"score {_float(cluster.get('cluster_score')):.1f}, "
                f"{cluster.get('total_trade_cooccurrences', 0)} co-trades"
            )
    if analysis.get("fetch_errors"):
        lines.extend(["", f"Warning: endpoint errors affected {len(analysis['fetch_errors'])} wallet(s)."])
    lines.extend(["", "Heuristic signals only; coordinated-wallet ground truth is unvalidated."])
    return "\n".join(lines)


def telegram_wallets(analysis: Mapping[str, Any], limit: int = 10) -> str:
    lines = ["Top trader scores"]
    for trader in analysis.get("top_traders", [])[:limit]:
        lines.append(
            f"{trader.get('analysis_rank')}. {trader.get('username') or 'anonymous'} "
            f"{_wallet_short(str(trader.get('wallet') or ''))} | "
            f"score {_float(trader.get('trader_rank_score')):.1f} | "
            f"PnL {_money(trader.get('pnl'))} | ROI {_percent(trader.get('roi'))} | "
            f"{trader.get('data_status')}"
        )
    return "\n".join(lines)


def telegram_clusters(analysis: Mapping[str, Any], limit: int = 10) -> str:
    clusters = analysis.get("clusters", [])[:limit]
    if not clusters:
        return "No clusters passed the configured thresholds in the latest scan."
    lines = ["Detected clusters"]
    for cluster in clusters:
        members = ", ".join(_wallet_short(wallet) for wallet in cluster.get("wallets", []))
        lines.append(
            f"{cluster.get('cluster_id')} | score {_float(cluster.get('cluster_score')):.1f} | "
            f"Jaccard {_percent(cluster.get('average_position_overlap'))} | "
            f"cosine {_percent(cluster.get('average_value_cosine'))} | "
            f"co-trades {cluster.get('total_trade_cooccurrences', 0)}\n{members}"
        )
    return "\n\n".join(lines)
