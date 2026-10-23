# Kalshi-15m-Scanner

A recorder and paper-trading study for Kalshi's 15-minute crypto markets (Bitcoin `KXBTC15M` by default). It answers one question with recorded data: **is there an edge after fees, latency, and missed fills?**

It never places orders. There is no order-entry code in this project.

## How these markets settle, and why that matters

A 15-minute market compares **the simple average of the CF Benchmarks index (BRTI for Bitcoin) over the final 60 seconds before close** with a target price that was set from the minute before the market opened. A normal Bitcoin chart is not the settlement price, so the model works from the same index Kalshi uses:

- The **target** comes from the market itself (`floor_strike` / `cap_strike` with `strike_type`).
- The **reference index** comes from Kalshi's authenticated WebSocket channel `cfbenchmarks_value`. During the final minute it also carries `last_60s_windowed_average_15min`, the running average and tick count of the settlement window. The model locks in the ticks already published and only treats the remaining ones as uncertain.

## What it does

| Component | Module | What it does |
|---|---|---|
| Market recorder | `recorder.py`, `kalshi_api.py`, `cf_feed.py`, `live_check.py` | Records open markets and targets, bid depth for both sides every 2 s, trades, final results, and every BRTI tick (stamped with the time this machine received it). |
| Probability model | `model.py` | Estimates P(YES) from the distance to the target, trailing realized volatility, time remaining, and the 60-second settlement average. |
| Entry filter | `strategy.py` | Buys a side only if `(model probability − model-error margin) − worst acceptable price − taker fee ≥ minimum edge`. |
| Fill simulator and exit comparison | `backtest.py` | Replays recorded data with no look-ahead. Orders fill after a delay against the next recorded book, limited by price and visible depth. Runs **hold to settlement** and **sell early when the edge is gone** side by side. |
| Dashboard | `dashboard.py` | Self-contained HTML: entry signals and fill status, closed paper trades, net P&L, drawdown, skip reasons, calibration against the market, and a pilot-readiness checklist. |
| Fees | `fees.py` | Kalshi's quadratic fee: `round_up(multiplier × 0.07 × contracts × P × (1 − P))`. No settlement fee. |

## Setup

```bash
cd Kalshi-15m-Scanner
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

### Kalshi API credentials (needed for the reference index)

Market data is public, but the CF Benchmarks feed requires a signed WebSocket connection.

1. In your Kalshi account, create an API key and download its private key file. If Kalshi offers a read-only permission level, choose it; this tool only reads data either way.
2. Store the private key file **outside this repository**, for example `~/.kalshi/kalshi-key.pem`, and restrict it: `chmod 600 ~/.kalshi/kalshi-key.pem`.
3. Set two environment variables in your shell. Never paste the key itself into chats, issues, or files in this repo.

```bash
export KALSHI_API_KEY_ID="your-key-id"
export KALSHI_PRIVATE_KEY_PATH="$HOME/.kalshi/kalshi-key.pem"
```

`.env.example` lists the same variables. `.gitignore` excludes `.env`, `*.pem`, `data/`, and `reports/`.

## Workflow

### 0. Check live access (one minute)

```bash
python main.py check
```

Read-only. It reaches the series, open markets, orderbook and trades endpoints, verifies the raw payloads still have every field this project relies on, then opens the signed index feed and waits for a few BRTI ticks. It ends with **Ready to record** or names exactly what is blocking (network, credentials, a renamed field, no open markets right now).

### 1. Record (weeks, continuously)

```bash
python main.py record                 # BTC 15-minute series vs BRTI, into data/kalshi_15m.db
python main.py status                 # what has been captured, plus recent recorder warnings
```

Leave it running on an always-on machine. Gaps in the index feed show up as `stale_index` skips and recorder warnings, never as invented data. `--demo` targets Kalshi's demo environment. `--allow-no-index` records market data without credentials, but the model cannot evaluate those periods.

### 2. Develop on the earlier period

```bash
python main.py report                 # writes reports/dashboard.html and reports/study.json
python main.py report --every 300     # regenerate every 5 minutes while recording
```

By default the most recent 30% of recorded days (`--eval-fraction`, or a fixed `--split-date YYYY-MM-DD`) is held out and **sealed**. Results shown are development-period only. Strategy settings can be changed with flags such as `--min-edge 0.03 --model-margin 0.04 --latency-seconds 2`; run `python main.py report --help` for all of them.

### 3. Freeze, then evaluate the untouched period once

```bash
python main.py report --min-edge 0.03 --evaluate
```

`--evaluate` runs the frozen settings on the held-out days and logs that it did so, along with the settings fingerprint. If the evaluation period is later viewed with different settings, the report warns that it is no longer untouched. Wait for fresh days before trusting it again.

### 4. Evidence before any live pilot

The readiness checklist judges the evaluation period only. By default it requires all of the following:

- at least 50 trades and 10 active days (`--min-trades`, `--min-active-days`)
- positive net P&L after fees, positive on at least half of active days
- the bootstrap 95% CI of mean daily P&L entirely above zero
- the model beating the market mid on Brier score, with the 95% CI of the per-market difference entirely below zero

Meeting these is a reason to consider a small pilot, not a promise of profit. The built-in risk limits mirror the proposed pilot: **$2 maximum loss per position, one position at a time, $3 daily loss stop.** Whether and how to trade live is your decision; this project does not automate it.

## Strategy settings (defaults)

| Setting | Default | Meaning |
|---|---:|---|
| `model_margin` | 0.03 | Subtracted from the model probability before comparing with prices. |
| `min_edge` | 0.02 | Required net edge per contract after the limit price and its taker fee. |
| `max_slippage` | 0.01 | Limit price = best ask + this. Fills never exceed it. |
| `min_entry_price` / `max_entry_price` | 0.10 / 0.90 | Avoid the tails, where a normal model is least reliable. |
| `min_seconds_to_close` / `max_seconds_to_close` | 45 / 840 | Trading window inside each 15-minute market. |
| `latency_seconds` | 1.0 | Delay between decision and fill attempt. With 2 s book polling, the effective delay is about 2 s. |
| `max_fill_wait_seconds` | 5.0 | Order is cancelled (missed) if no book arrives in time. Keep this above the book polling interval. |
| `min_annual_vol`, `vol_multiplier` | 0.20, 1.0 | Floor and scale for the trailing realized-volatility estimate (30 min lookback, 10 s samples). |
| `exit_buffer` | 0.0 | Early exit sells when `bid − fee ≥ model probability + buffer`. |
| `max_loss_per_position`, `daily_loss_stop` | 2.00, 3.00 | Position size is the largest whole-contract count whose worst case (cost + fee) fits both. |
| `day_timezone` | UTC | Day boundary for the daily stop and daily statistics (IANA name). |

## Fees

```bash
python main.py fees --entry 0.50 --exit 0.53 --rounding none   # net −0.49¢
python main.py fees --entry 0.50 --exit 0.55 --rounding none   # net +1.52¢
python main.py fees --entry 0.50 --exit 0.55                   # net +1.00¢ with each fee rounded up to the cent
python main.py fees --entry 0.40                               # hold to settlement, and the break-even win rate
```

| Buy → sell, 1 contract, taker both ways | Gross | Net, unrounded | Net, rounded up to the cent |
|---|---:|---:|---:|
| 50¢ → 53¢ | 3¢ | −0.49¢ | −1¢ |
| 50¢ → 55¢ | 5¢ | +1.52¢ | +1¢ |

The replay rounds each fee **up to the next cent per price level filled**, the conservative reading of the schedule. Use `--fee-rounding centicent` or `none` to see how sensitive results are to that choice. The fee multiplier and type are read from the recorded series metadata (`GET /series/KXBTC15M`). `--fee-multiplier` overrides them.

## Simulation rules and known limits

- **No look-ahead.** Book snapshots and index ticks are replayed in the order and at the time this machine received them.
- **Taker orders only.** Resting (maker) orders are not simulated. Their fills depend on queue position and on being filled just before the price moves against you, which this recorder cannot observe. Any maker strategy needs its own data and testing.
- **Missed and partial fills are real outcomes.** If the offer is gone or smaller by the time the order arrives, the dashboard records that.
- **Settlement** uses Kalshi's official `result`. If it is missing, the recorded final-minute average is used and labeled *provisional*. It can disagree with the official value by cents, which matters only when the average lands right on the target.
- **Model assumptions:** driftless log-normal index moves, trailing realized volatility, no jumps or fat tails. The model-error margin and the calibration check exist because these assumptions are wrong in the details.
- **Small samples mislead.** On synthetic test data, changing only the entry price band flipped development P&L from −$8.90 to +$11.97. Treat per-period P&L as noise until the confidence intervals say otherwise.

## Verification status

Verified in the build environment:

- 56 unit and integration tests: fee arithmetic, settlement-window variance, volatility estimation, request signing (RSA-PSS verified with the public key), WebSocket handshake and subscription against a local server, orderbook normalization, latency and missed fills, daily stop, early exit, provisional settlement, evaluation sealing and logging, dashboard, the live check, and CLI.
- An end-to-end run on 4 days of **synthetic** data (384 markets, 172,800 book snapshots, 347,520 index ticks) to check replay speed (about 16 s for 3 days × 2 policies) and dashboard rendering. Those numbers are not evidence of anything about real markets.

Not verified here: the build environment's network policy blocked `api.elections.kalshi.com`, so **no live Kalshi request was made**. Endpoint paths and field names (`orderbook_fp.yes_dollars`, `yes_bid_dollars`, `floor_strike`, `strike_type`, `cfbenchmarks_value`, `last_60s_windowed_average_15min`, the `fee_multiplier` series field) and the signing scheme (RSA-PSS for RSA keys, Ed25519 for Ed25519 keys) come from Kalshi's published OpenAPI and AsyncAPI specifications. Run `python main.py check` on a machine that can reach Kalshi before relying on it.

## Tests

```bash
python -m pip install -r requirements-dev.txt
pytest -q
ruff check .
```
