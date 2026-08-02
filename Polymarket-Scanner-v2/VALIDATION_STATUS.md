# Validation status

## Actual live checks during this build

Two different network paths were available, with different results:

1. The browsing retrieval layer successfully called the unauthenticated production endpoint `https://data-api.polymarket.com/v1/leaderboard` and received a live JSON array. The observed item fields were `rank`, `proxyWallet`, `userName`, `xUsername`, `verifiedBadge`, `vol`, `pnl`, and `profileImage`. This confirms the default leaderboard route and those field names only.
2. The application execution container attempted a real request with explicit leaderboard query parameters, but failed before HTTP connection establishment because its DNS resolver could not resolve `data-api.polymarket.com`:

```text
NameResolutionError: Failed to resolve 'data-api.polymarket.com'
```

Accordingly, the application runtime did **not** live-validate leaderboard pagination, positions, activity, trades, closed positions, or rate-limit behavior. The included failed validator report records that limitation. Documentation and synthetic/mock tests were used for those code paths; they are not represented as successful live tests.

## Exact local verification

From a normal internet-connected machine:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python live_validate.py --output-dir validation
```

Successful output must show:

```text
Overall: passed
```

Then inspect:

```bash
cat validation/live_validation_report.md
python -m json.tool validation/live_validation_report.json | less
```

To observe real rate/throttle behavior against the read-only one-row positions endpoint:

```bash
python live_validate.py \
  --output-dir validation \
  --probe-throttle \
  --probe-count 170 \
  --probe-concurrency 25
```

The report records status counts, latency distribution, rate-related headers, and whether `Retry-After` appeared. Polymarket documents Cloudflare throttling as potentially delayed/queued rather than always returning HTTP 429, so latency must be reviewed alongside status codes.

## Documentation-checked, not live-confirmed here

- Data API reads are public and require no authentication.
- Leaderboard: `/v1/leaderboard`, offset pagination, maximum 50 rows per request.
- Current positions: `/positions`, maximum 500 rows per request.
- Activity: `/activity` with `type=TRADE`.
- Trades fallback: `/trades`.
- Closed positions: `/closed-positions`, maximum 50 rows per request.
- Data API rate limits are IP-based and Cloudflare-enforced.

## Tests completed without external network

- Python compilation.
- Synthetic overlap, synchronized-trade, ranking, and cluster analysis.
- Partial per-wallet failure policy.
- Abort-on-wallet-failure policy.
- JSON/Markdown generation.
- SQLite persistence and latest-analysis retrieval.
