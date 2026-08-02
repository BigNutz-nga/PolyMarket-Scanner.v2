# Polymarket-Scanner

A read-only Python 3.10+ analytics service for Polymarket trader leaderboards, active positions, recent trades, closed positions, wallet similarity, and synchronized-trading signals.

The project has two access modes:

1. **Telegram bot** for iPhone access, scheduled scans, push summaries, on-demand scans, and report downloads.
2. **CLI** for local/manual execution and automation.

## Why Telegram

Telegram is the simplest reliable phone access point for this workload. It supplies the mobile UI, push delivery, user/chat identity, command routing, and file delivery. The deployment is one long-running Python worker; there is no separate frontend, web domain, TLS setup, or PWA service worker to maintain.

## Project layout

- `fetcher.py` — Data API client, pagination, retry/backoff, partial-wallet failure handling, SQLite persistence.
- `analyzer.py` — trader scoring, Jaccard/cosine overlap, synchronized-trade matching, connected-component clustering.
- `scanner.py` — shared scan orchestration and cache fallback.
- `reporter.py` — Rich CLI, JSON, Markdown, and Telegram summaries.
- `main.py` — CLI entry point.
- `bot.py` — Telegram commands and interval scheduler.
- `live_validate.py` — real HTTP schema, pagination, header, latency, and optional throttle validation.
- `DEPLOY_RAILWAY.md` — exact Railway deployment steps.
- `VALIDATION_STATUS.md` — what was and was not tested in the build environment.

## Data API authentication

The leaderboard, positions, activity, trades, value, and closed-position endpoints used by this scanner are public Data API reads and do not require an API key. The scanner does not accept or store a wallet private key.

The Telegram bot token is a secret. It is read only from `TELEGRAM_BOT_TOKEN`; it is never hardcoded or written to SQLite/reports. Access is restricted by the `TELEGRAM_ALLOWED_CHAT_IDS` environment variable.

## Install locally

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

## CLI scan

```bash
python main.py \
  --limit 25 \
  --timeframe WEEK \
  --metric PNL \
  --window-mins 15 \
  --min-overlap 0.20 \
  --min-cosine 0.80 \
  --failure-policy partial \
  --refresh
```

Outputs:

- `polymarket_data.db`
- `reports/analysis.json`
- `reports/summary_report.md`

## Live API verification from your network

Run the non-destructive endpoint/pagination validation:

```bash
python live_validate.py --output-dir validation
```

Run an opt-in read-only `/positions` burst to observe real throttle behavior, latency inflation, HTTP statuses, `Retry-After`, and rate-related response headers:

```bash
python live_validate.py \
  --output-dir validation \
  --probe-throttle \
  --probe-count 170 \
  --probe-concurrency 25
```

The validator exits `0` only when leaderboard, positions, activity, trades, and closed-position checks all return HTTP 200. It writes:

- `validation/live_validation_report.json`
- `validation/live_validation_report.md`

Do not claim live validation from a report whose `overall_status` is `failed`.

## Wallet failure behavior

The default `partial` policy is explicit:

- A leaderboard failure aborts the scan because there is no wallet set to analyze.
- An individual positions, trades, closed-positions, or portfolio-value failure is recorded and the remaining endpoint calls and wallets continue.
- If pagination fails after earlier pages succeeded, already fetched rows are retained and the endpoint is marked partial.
- A wallet is `failed` only when positions, trades, and closed positions all fail. It remains in leaderboard output with leaderboard-only metrics.
- `--failure-policy abort` stops on the first per-wallet endpoint failure.
- `--max-failed-wallet-ratio` can reject scans with too many fully failed wallets.

## Telegram commands

- `/scan` — run a fresh scan now.
- `/latest` — latest push-style summary.
- `/wallets` — top trader scores.
- `/clusters` — correlated wallet groups.
- `/report` — download `analysis.json` and `summary_report.md` to the phone.
- `/status` — scheduler, source, latest scan, and error status.
- `/chatid` — display the Telegram chat ID needed for the allowlist.

The scheduler is controlled by `SCAN_INTERVAL_MINUTES`; `360` means every six hours. Scheduled and on-demand scans share a lock, so overlapping scans are skipped rather than duplicated.

## Threshold status

Defaults are exploratory heuristics:

- Jaccard `0.20`
- cosine `0.80`
- at least two shared active outcomes
- 15-minute co-trade window
- at least two co-occurrences
- at least 5% sync rate

They were selected as sensitivity-oriented starting points for small top-trader samples. They have **not** been calibrated against a labeled set of known coordinated/Sybil wallets. Every report marks ground-truth validation as `unvalidated`. See `THRESHOLDS.md`.

## Security

- Read-only public Polymarket endpoints only.
- No private keys or trading credentials.
- Telegram token and chat allowlist only through environment variables.
- Unauthorized chats can only retrieve their own chat ID; scan data and commands are denied.
- SQLite and reports belong on a persistent Railway volume mounted at `/data`.

## Tests

```bash
python -m pip install -r requirements-dev.txt
pytest -q
python -m compileall -q .
ruff check .
```

## Deployment

See [DEPLOY_RAILWAY.md](DEPLOY_RAILWAY.md).
