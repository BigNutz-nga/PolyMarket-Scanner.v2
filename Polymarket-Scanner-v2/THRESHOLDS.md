# Similarity and clustering thresholds

## Defaults

| Signal | Default | Purpose |
|---|---:|---|
| Jaccard overlap | 0.20 | Flags portfolios sharing at least 20% of their active outcome-key union. |
| Value cosine | 0.80 | Flags portfolios whose current-value allocation vectors are strongly aligned. |
| Shared active outcomes | 2 | Prevents a single common position from forming a portfolio edge. |
| Trade window | 15 minutes | Captures nearby entries/exits without requiring same-block execution. |
| Co-occurrences | 2 | Suppresses one-off timing coincidences. |
| Sync rate | 0.05 | Requires matches to represent at least 5% of the smaller wallet's sampled trades. |
| Shared trade markets | 1 | Allows repeated synchronization in one market; raise to 2+ for broader behavior. |

## Why these values

These are exploratory sensitivity settings, not statistically estimated cutoffs. A 0.20 Jaccard threshold is intentionally permissive for compact top-trader portfolios where two shared outcomes can be material. A 0.80 cosine threshold captures strong value allocation similarity even when wallets hold additional small positions. The two-event and 5% trade requirements reduce isolated coincidences while retaining possible copy-trading patterns.

## Ground-truth status

The defaults have **not** been checked against a labeled set of known coordinated, commonly controlled, or Sybil wallets. No known-coordination precision, recall, false-positive rate, or ROC analysis is claimed. Reports explicitly include:

```json
{
  "known_coordinated_wallet_ground_truth_checked": false,
  "status": "unvalidated"
}
```

## Recommended calibration workflow

1. Build a labeled evaluation set from publicly documented common-control cases or independently verified wallet relationships.
2. Freeze a historical time window and avoid tuning/evaluating on the same period.
3. Sweep Jaccard, cosine, time window, co-occurrence count, sync-rate, and shared-market thresholds.
4. Measure pair-level precision/recall and cluster-level purity.
5. Stratify by market popularity and wallet activity to quantify chance overlap among high-frequency traders.
6. Publish the chosen operating point and confidence intervals.

Until this work is completed, treat cluster output as an investigation queue, not a finding of coordination or misconduct.
