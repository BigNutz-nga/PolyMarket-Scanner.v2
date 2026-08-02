"""Portfolio similarity, synchronized-trading, ranking, and clustering."""

from __future__ import annotations

import itertools
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import mean, pstdev
from typing import Any, Mapping, Sequence


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _outcome_key(row: Mapping[str, Any]) -> int | str:
    try:
        index = int(row.get("outcome_index"))
    except (TypeError, ValueError):
        index = -1
    return index if index >= 0 else str(row.get("outcome") or "").casefold()


def _position_key(row: Mapping[str, Any]) -> tuple[str, int | str]:
    return str(row.get("market_id") or row.get("market_slug") or ""), _outcome_key(row)


def _trade_key(row: Mapping[str, Any]) -> tuple[str, int | str, str]:
    return (
        str(row.get("market_id") or row.get("market_slug") or ""),
        _outcome_key(row),
        str(row.get("side") or "").upper(),
    )


def _percentile_ranks(values: Mapping[str, float]) -> dict[str, float]:
    """Return deterministic percentile ranks in [0, 1], averaging ties."""
    if not values:
        return {}
    sorted_items = sorted(values.items(), key=lambda item: item[1])
    n = len(sorted_items)
    if n == 1:
        return {sorted_items[0][0]: 1.0}
    result: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sorted_items[j + 1][1] == sorted_items[i][1]:
            j += 1
        average_rank = (i + j) / 2
        percentile = average_rank / (n - 1)
        for index in range(i, j + 1):
            result[sorted_items[index][0]] = percentile
        i = j + 1
    return result


def rank_traders(
    traders: Sequence[Mapping[str, Any]],
    trades_by_wallet: Mapping[str, Sequence[Mapping[str, Any]]],
    closed_by_wallet: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    raw: dict[str, dict[str, float | int | str]] = {}

    for trader in traders:
        wallet = str(trader.get("wallet") or "")
        closed = list(closed_by_wallet.get(wallet, []))
        trades = list(trades_by_wallet.get(wallet, []))
        realized = [_float(row.get("realized_pnl")) for row in closed]
        total_bought = sum(max(0.0, _float(row.get("total_bought"))) for row in closed)
        realized_total = sum(realized)
        sampled_roi = realized_total / total_bought if total_bought > 0 else 0.0
        leaderboard_volume = max(0.0, _float(trader.get("volume")))
        proxy_roi = _float(trader.get("pnl")) / leaderboard_volume if leaderboard_volume > 0 else 0.0
        roi = sampled_roi if len(closed) >= 5 and total_bought > 0 else proxy_roi
        win_rate = sum(value > 0 for value in realized) / len(realized) if realized else 0.0

        if len(realized) >= 2:
            scale = abs(mean(realized)) + pstdev(realized) + 1e-9
            consistency = max(0.0, min(1.0, 1.0 - pstdev(realized) / scale))
        elif len(realized) == 1:
            consistency = 0.25
        else:
            consistency = 0.0

        active_volume = sum(max(0.0, _float(row.get("usdc_size"))) for row in trades)
        raw[wallet] = {
            "roi": roi,
            "sampled_roi": sampled_roi,
            "proxy_roi": proxy_roi,
            "win_rate": win_rate,
            "consistency": consistency,
            "active_volume": active_volume,
            "closed_sample_size": len(closed),
            "score_confidence": "high" if len(closed) >= 20 else "medium" if len(closed) >= 5 else "low",
        }

    roi_rank = _percentile_ranks({wallet: _float(row["roi"]) for wallet, row in raw.items()})
    win_rank = _percentile_ranks({wallet: _float(row["win_rate"]) for wallet, row in raw.items()})
    consistency_rank = _percentile_ranks(
        {wallet: _float(row["consistency"]) for wallet, row in raw.items()}
    )
    volume_rank = _percentile_ranks(
        {wallet: math.log1p(_float(row["active_volume"])) for wallet, row in raw.items()}
    )

    ranked: list[dict[str, Any]] = []
    for trader in traders:
        wallet = str(trader.get("wallet") or "")
        metrics = raw[wallet]
        score = 100.0 * (
            0.35 * roi_rank.get(wallet, 0.0)
            + 0.25 * win_rank.get(wallet, 0.0)
            + 0.20 * consistency_rank.get(wallet, 0.0)
            + 0.20 * volume_rank.get(wallet, 0.0)
        )
        row = dict(trader)
        row.update(
            {
                "roi": round(_float(metrics["roi"]), 8),
                "sampled_closed_roi": round(_float(metrics["sampled_roi"]), 8),
                "leaderboard_pnl_volume_proxy_roi": round(_float(metrics["proxy_roi"]), 8),
                "win_rate": round(_float(metrics["win_rate"]), 8),
                "consistency": round(_float(metrics["consistency"]), 8),
                "recent_active_volume": round(_float(metrics["active_volume"]), 6),
                "closed_sample_size": int(metrics["closed_sample_size"]),
                "score_confidence": str(metrics["score_confidence"]),
                "trader_rank_score": round(score, 4),
            }
        )
        ranked.append(row)

    ranked.sort(
        key=lambda row: (
            _float(row.get("trader_rank_score")),
            _float(row.get("pnl")),
            _float(row.get("volume")),
        ),
        reverse=True,
    )
    for index, row in enumerate(ranked, start=1):
        row["analysis_rank"] = index
    return ranked


def compute_position_overlap(
    positions_by_wallet: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    min_jaccard: float = 0.20,
    min_cosine: float = 0.80,
    min_shared_positions: int = 2,
    niche_holder_threshold: int = 2,
) -> list[dict[str, Any]]:
    position_maps: dict[str, dict[tuple[str, int | str], Mapping[str, Any]]] = {}
    holder_counts: Counter[tuple[str, int | str]] = Counter()

    for wallet, positions in positions_by_wallet.items():
        mapped: dict[tuple[str, int | str], Mapping[str, Any]] = {}
        for position in positions:
            key = _position_key(position)
            if not key[0] or _float(position.get("shares")) <= 0:
                continue
            mapped[key] = position
        position_maps[wallet] = mapped
        holder_counts.update(mapped.keys())

    results: list[dict[str, Any]] = []
    for wallet_a, wallet_b in itertools.combinations(sorted(position_maps), 2):
        map_a = position_maps[wallet_a]
        map_b = position_maps[wallet_b]
        keys_a = set(map_a)
        keys_b = set(map_b)
        shared = keys_a & keys_b
        union = keys_a | keys_b
        jaccard = len(shared) / len(union) if union else 0.0

        dot = 0.0
        norm_a = 0.0
        norm_b = 0.0
        for key in union:
            value_a = max(0.0, _float(map_a.get(key, {}).get("current_value")))
            value_b = max(0.0, _float(map_b.get(key, {}).get("current_value")))
            dot += value_a * value_b
            norm_a += value_a * value_a
            norm_b += value_b * value_b
        cosine = dot / math.sqrt(norm_a * norm_b) if norm_a > 0 and norm_b > 0 else 0.0

        shared_rows: list[dict[str, Any]] = []
        for key in sorted(shared, key=lambda item: (item[0], str(item[1]))):
            a = map_a[key]
            b = map_b[key]
            shared_rows.append(
                {
                    "market_id": key[0],
                    "market_slug": str(a.get("market_slug") or b.get("market_slug") or ""),
                    "title": str(a.get("title") or b.get("title") or ""),
                    "outcome": str(a.get("outcome") or b.get("outcome") or ""),
                    "wallet_a_value": round(_float(a.get("current_value")), 6),
                    "wallet_b_value": round(_float(b.get("current_value")), 6),
                    "sample_holder_count": holder_counts[key],
                    "niche_in_sample": holder_counts[key] <= max(1, niche_holder_threshold),
                }
            )

        enough_shared = len(shared) >= max(1, min_shared_positions)
        flagged = enough_shared and (jaccard >= min_jaccard or cosine >= min_cosine)
        results.append(
            {
                "wallet_a": wallet_a,
                "wallet_b": wallet_b,
                "jaccard_similarity": round(jaccard, 6),
                "cosine_similarity": round(cosine, 6),
                "shared_position_count": len(shared),
                "union_position_count": len(union),
                "flagged": flagged,
                "shared_positions": shared_rows,
            }
        )

    return sorted(
        results,
        key=lambda row: (
            bool(row["flagged"]),
            _float(row["jaccard_similarity"]),
            _float(row["cosine_similarity"]),
            int(row["shared_position_count"]),
        ),
        reverse=True,
    )


def _nearest_trade_matches(
    trades_a: Sequence[Mapping[str, Any]],
    trades_b: Sequence[Mapping[str, Any]],
    window_seconds: int,
) -> list[tuple[Mapping[str, Any], Mapping[str, Any], int]]:
    """Greedy one-to-one nearest-in-time matching to avoid split-fill inflation."""
    a = sorted(trades_a, key=lambda row: int(row.get("timestamp") or 0))
    b = sorted(trades_b, key=lambda row: int(row.get("timestamp") or 0))
    used_b: set[int] = set()
    matches: list[tuple[Mapping[str, Any], Mapping[str, Any], int]] = []

    for trade_a in a:
        timestamp_a = int(trade_a.get("timestamp") or 0)
        best_index: int | None = None
        best_difference = window_seconds + 1
        for index, trade_b in enumerate(b):
            if index in used_b:
                continue
            difference = abs(timestamp_a - int(trade_b.get("timestamp") or 0))
            if difference <= window_seconds and difference < best_difference:
                best_index = index
                best_difference = difference
            if int(trade_b.get("timestamp") or 0) > timestamp_a + window_seconds:
                break
        if best_index is not None:
            used_b.add(best_index)
            matches.append((trade_a, b[best_index], best_difference))
    return matches


def detect_trade_cooccurrence(
    trades_by_wallet: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    window_minutes: int = 15,
    min_cooccurrences: int = 2,
    min_sync_rate: float = 0.05,
    min_shared_markets: int = 1,
) -> list[dict[str, Any]]:
    window_seconds = max(1, window_minutes) * 60
    grouped: dict[tuple[str, int | str, str], dict[str, list[Mapping[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    wallet_trade_counts: Counter[str] = Counter()

    for wallet, trades in trades_by_wallet.items():
        for trade in trades:
            key = _trade_key(trade)
            if not key[0] or key[2] not in {"BUY", "SELL"}:
                continue
            grouped[key][wallet].append(trade)
            wallet_trade_counts[wallet] += 1

    pair_events: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for key, wallet_groups in grouped.items():
        wallets = sorted(wallet_groups)
        if len(wallets) < 2:
            continue
        for wallet_a, wallet_b in itertools.combinations(wallets, 2):
            for trade_a, trade_b, difference in _nearest_trade_matches(
                wallet_groups[wallet_a], wallet_groups[wallet_b], window_seconds
            ):
                pair_events[(wallet_a, wallet_b)].append(
                    {
                        "market_id": key[0],
                        "market_slug": str(trade_a.get("market_slug") or trade_b.get("market_slug") or ""),
                        "title": str(trade_a.get("title") or trade_b.get("title") or ""),
                        "outcome": str(trade_a.get("outcome") or trade_b.get("outcome") or ""),
                        "side": key[2],
                        "wallet_a_timestamp": int(trade_a.get("timestamp") or 0),
                        "wallet_b_timestamp": int(trade_b.get("timestamp") or 0),
                        "time_difference_seconds": difference,
                        "wallet_a_usdc": round(_float(trade_a.get("usdc_size")), 6),
                        "wallet_b_usdc": round(_float(trade_b.get("usdc_size")), 6),
                        "wallet_a_price": round(_float(trade_a.get("price")), 6),
                        "wallet_b_price": round(_float(trade_b.get("price")), 6),
                    }
                )

    results: list[dict[str, Any]] = []
    for (wallet_a, wallet_b), events in pair_events.items():
        count = len(events)
        denominator = min(wallet_trade_counts[wallet_a], wallet_trade_counts[wallet_b])
        sync_rate = count / denominator if denominator else 0.0
        market_count = len({event["market_id"] for event in events})
        average_difference = mean(event["time_difference_seconds"] for event in events)
        total_usdc = sum(
            _float(event["wallet_a_usdc"]) + _float(event["wallet_b_usdc"]) for event in events
        )
        flagged = (
            count >= max(1, min_cooccurrences)
            and sync_rate >= max(0.0, min_sync_rate)
            and market_count >= max(1, min_shared_markets)
        )
        results.append(
            {
                "wallet_a": wallet_a,
                "wallet_b": wallet_b,
                "cooccurrence_count": count,
                "unique_market_count": market_count,
                "sync_rate": round(sync_rate, 6),
                "average_time_difference_seconds": round(average_difference, 3),
                "total_combined_usdc": round(total_usdc, 6),
                "flagged": flagged,
                "events": sorted(events, key=lambda event: event["time_difference_seconds"]),
            }
        )

    return sorted(
        results,
        key=lambda row: (
            bool(row["flagged"]),
            int(row["cooccurrence_count"]),
            _float(row["sync_rate"]),
            _float(row["total_combined_usdc"]),
        ),
        reverse=True,
    )


class _UnionFind:
    def __init__(self, values: Sequence[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, a: str, b: str) -> None:
        root_a = self.find(a)
        root_b = self.find(b)
        if root_a != root_b:
            self.parent[root_b] = root_a


def build_clusters(
    wallets: Sequence[str],
    position_pairs: Sequence[Mapping[str, Any]],
    trade_pairs: Sequence[Mapping[str, Any]],
    trader_lookup: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    union_find = _UnionFind(wallets)
    for pair in itertools.chain(position_pairs, trade_pairs):
        if pair.get("flagged"):
            union_find.union(str(pair["wallet_a"]), str(pair["wallet_b"]))

    groups: dict[str, list[str]] = defaultdict(list)
    for wallet in wallets:
        groups[union_find.find(wallet)].append(wallet)

    clusters: list[dict[str, Any]] = []
    cluster_number = 0
    for members in sorted(groups.values(), key=lambda group: (-len(group), group)):
        if len(members) < 2:
            continue
        cluster_number += 1
        member_set = set(members)
        position_edges = [
            pair
            for pair in position_pairs
            if pair.get("flagged")
            and pair.get("wallet_a") in member_set
            and pair.get("wallet_b") in member_set
        ]
        trade_edges = [
            pair
            for pair in trade_pairs
            if pair.get("flagged")
            and pair.get("wallet_a") in member_set
            and pair.get("wallet_b") in member_set
        ]
        avg_jaccard = mean(_float(edge.get("jaccard_similarity")) for edge in position_edges) if position_edges else 0.0
        avg_cosine = mean(_float(edge.get("cosine_similarity")) for edge in position_edges) if position_edges else 0.0
        total_matches = sum(int(edge.get("cooccurrence_count") or 0) for edge in trade_edges)
        combined_pnl = sum(_float(trader_lookup.get(wallet, {}).get("pnl")) for wallet in members)
        score = 100.0 * (
            0.35 * min(1.0, avg_jaccard / 0.50)
            + 0.25 * min(1.0, avg_cosine / 0.90)
            + 0.25 * min(1.0, total_matches / 10.0)
            + 0.15 * min(1.0, math.log1p(max(0.0, combined_pnl)) / math.log1p(1_000_000.0))
        )
        clusters.append(
            {
                "cluster_id": f"cluster-{cluster_number}",
                "wallets": sorted(members),
                "member_count": len(members),
                "cluster_score": round(score, 4),
                "average_position_overlap": round(avg_jaccard, 6),
                "average_value_cosine": round(avg_cosine, 6),
                "total_trade_cooccurrences": total_matches,
                "combined_pnl": round(combined_pnl, 6),
                "position_edge_count": len(position_edges),
                "trade_edge_count": len(trade_edges),
            }
        )

    return sorted(clusters, key=lambda cluster: cluster["cluster_score"], reverse=True)


def analyze_scan(
    scan: Mapping[str, Any],
    *,
    window_minutes: int = 15,
    min_overlap: float = 0.20,
    min_cosine: float = 0.80,
    min_shared_positions: int = 2,
    min_cooccurrences: int = 2,
    min_sync_rate: float = 0.05,
    min_shared_trade_markets: int = 1,
) -> dict[str, Any]:
    traders = list(scan.get("traders", []))
    positions_by_wallet = scan.get("positions_by_wallet", {})
    trades_by_wallet = scan.get("trades_by_wallet", {})
    closed_by_wallet = scan.get("closed_positions_by_wallet", {})

    ranked = rank_traders(traders, trades_by_wallet, closed_by_wallet)
    trader_lookup = {str(row.get("wallet")): row for row in ranked}
    position_pairs_all = compute_position_overlap(
        positions_by_wallet,
        min_jaccard=min_overlap,
        min_cosine=min_cosine,
        min_shared_positions=min_shared_positions,
    )
    trade_pairs_all = detect_trade_cooccurrence(
        trades_by_wallet,
        window_minutes=window_minutes,
        min_cooccurrences=min_cooccurrences,
        min_sync_rate=min_sync_rate,
        min_shared_markets=min_shared_trade_markets,
    )
    flagged_positions = [pair for pair in position_pairs_all if pair["flagged"]]
    flagged_trades = [pair for pair in trade_pairs_all if pair["flagged"]]
    wallets = [str(row.get("wallet")) for row in ranked if row.get("wallet")]
    clusters = build_clusters(wallets, flagged_positions, flagged_trades, trader_lookup)

    correlated_position_count = sum(pair["shared_position_count"] for pair in flagged_positions)
    fetched_status = str(scan.get("status") or "unknown")
    return {
        "scan_id": scan.get("scan_id"),
        "scan_fetched_at": scan.get("fetched_at"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scan_status": fetched_status,
        "scan_config": scan.get("config", {}),
        "request_metrics": scan.get("request_metrics", {}),
        "fetch_errors": scan.get("fetch_errors", {}),
        "summary": {
            "trader_count": len(ranked),
            "wallets_ok": sum(row.get("data_status") == "ok" for row in ranked),
            "wallets_partial": sum(row.get("data_status") == "partial" for row in ranked),
            "wallets_failed": sum(row.get("data_status") == "failed" for row in ranked),
            "flagged_position_pair_count": len(flagged_positions),
            "flagged_trade_pair_count": len(flagged_trades),
            "cluster_count": len(clusters),
            "correlated_position_count": correlated_position_count,
        },
        "thresholds": {
            "position_jaccard_min": min_overlap,
            "position_cosine_min": min_cosine,
            "minimum_shared_positions": min_shared_positions,
            "trade_window_minutes": window_minutes,
            "minimum_trade_cooccurrences": min_cooccurrences,
            "minimum_sync_rate": min_sync_rate,
            "minimum_shared_trade_markets": min_shared_trade_markets,
        },
        "validation": {
            "known_coordinated_wallet_ground_truth_checked": False,
            "status": "unvalidated",
            "reason": (
                "Default thresholds are exploratory heuristics, not calibrated against a labeled "
                "set of known coordinated or Sybil-controlled wallets. Correlation is not proof of common control."
            ),
        },
        "top_traders": ranked,
        "clusters": clusters,
        "position_overlap_pairs": flagged_positions,
        "trade_cooccurrence_pairs": flagged_trades,
        "all_position_pairs": position_pairs_all,
        "all_trade_pairs": trade_pairs_all,
        "methodology_notes": [
            "Jaccard measures shared active outcome keys divided by the union of active outcome keys.",
            "Cosine similarity weights those same outcome keys by current position value.",
            "A position edge requires the minimum shared-position count and either the Jaccard or cosine threshold.",
            "Trade co-occurrence requires the same market, outcome, and side inside the configured time window; matches are one-to-one to reduce split-fill inflation.",
            "The default 0.20 Jaccard threshold is an exploratory sensitivity setting for compact top-trader portfolios; 0.80 cosine identifies strongly value-aligned portfolios.",
            "The default two co-occurrences and 5% sync-rate floor suppress isolated coincidences but have not been calibrated to labeled coordination cases.",
            "Trader ROI uses sampled closed-position return when at least five closed positions are available; otherwise it falls back to leaderboard PnL divided by leaderboard volume, which is only a proxy.",
            "Clusters are connected components over flagged portfolio or synchronized-trade pair edges.",
            "niche_in_sample means rare among wallets in this scan; it is not a market-liquidity measurement.",
        ],
    }
