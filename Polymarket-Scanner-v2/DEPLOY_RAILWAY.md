# Deploy the Telegram bot on Railway

## Why Railway

This bot needs a continuously running process for Telegram long polling and its interval scheduler. Railway supports a normal worker service, GitHub auto-deploys, environment variables, and persistent volumes without requiring a public HTTP domain. Its Hobby plan is currently $5/month with $5 of included resource usage; actual cost can exceed that if resource use exceeds the included amount.

Render's free web services can sleep, which is a poor fit for reliable long polling and scheduled pushes. Railway is therefore the simpler choice, though it is not a guaranteed free deployment.

## 1. Create a Telegram bot

1. Open Telegram and message `@BotFather`.
2. Run `/newbot` and follow the prompts.
3. Copy the bot token. Treat it as a password.
4. Do not commit it to Git.

## 2. Publish this folder to your GitHub account

From the project directory:

```bash
git init
git add .
git commit -m "Initial Polymarket-Scanner Telegram service"
git branch -M main
```

Create an empty GitHub repository in the GitHub web UI, then run:

```bash
git remote add origin git@github.com:YOUR_GITHUB_USER/polymarket-scanner.git
git push -u origin main
```

Using GitHub CLI instead:

```bash
gh auth login
gh repo create polymarket-scanner --private --source=. --remote=origin --push
```

The resulting GitHub URL is the non-expiring clone URL controlled by your account.

## 3. First local launch to obtain your chat ID

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN='YOUR_TOKEN'
python bot.py
```

Message the bot with `/start` or `/chatid`. It will show your numeric chat ID even though scan commands remain locked. Stop the local process, then retain the number for Railway.

## 4. Create the Railway service

1. Sign in to Railway.
2. Choose **New Project** → **Deploy from GitHub repo**.
3. Select the repository created above.
4. Railway will use the included `Dockerfile` and `railway.json`.
5. No public domain is required because Telegram long polling is outbound HTTPS.

## 5. Add a persistent volume

1. Open the service.
2. Add a Volume.
3. Mount it at `/data`.

This preserves SQLite snapshots and reports across deploys/restarts.

## 6. Add variables

At minimum:

```text
TELEGRAM_BOT_TOKEN=<BotFather token>
TELEGRAM_ALLOWED_CHAT_IDS=<your numeric chat ID>
DATA_DIR=/data
DATABASE_PATH=/data/polymarket_data.db
OUTPUT_DIR=/data/reports
SCAN_INTERVAL_MINUTES=360
SCAN_ON_START=true
WALLET_FAILURE_POLICY=partial
```

Recommended scan defaults:

```text
SCAN_TIMEFRAME=WEEK
SCAN_METRIC=PNL
SCAN_CATEGORY=OVERALL
SCAN_LIMIT=25
SCAN_WINDOW_MINUTES=15
SCAN_MIN_OVERLAP=0.20
SCAN_MIN_COSINE=0.80
SCAN_MIN_SHARED_POSITIONS=2
SCAN_MIN_COOCCURRENCES=2
SCAN_MIN_SYNC_RATE=0.05
HTTP_CONCURRENCY=6
HTTP_REQUEST_DELAY_SECONDS=0.08
HTTP_RETRIES=4
```

Railway variables are runtime secrets. Never place `TELEGRAM_BOT_TOKEN` in `.env.example`, source code, logs, reports, or screenshots.

## 7. Deploy and verify

1. Apply the staged variables and deploy.
2. Check logs for `Scheduled scans every ... minutes`.
3. In Telegram, run `/status`.
4. Run `/scan`.
5. Run `/report` to download the JSON and Markdown files to the iPhone.

## 8. Validate Polymarket from the deployment network

Open a Railway shell or run a one-off command in the service:

```bash
python live_validate.py --output-dir /data/validation
```

For the optional throttle observation:

```bash
python live_validate.py \
  --output-dir /data/validation \
  --probe-throttle \
  --probe-count 170 \
  --probe-concurrency 25
```

Inspect `/data/validation/live_validation_report.json`. Only `overall_status: passed` is a successful live endpoint validation.

## Operational tradeoffs

- **Pros:** one service, no frontend/domain/TLS, direct iPhone push, allowlisted commands, persistent reports.
- **Cons:** Telegram is a third-party dependency; the bot worker must stay running; Railway is approximately $5/month at the Hobby tier and can cost more with higher usage.
- **Restart behavior:** the in-process interval starts again after a deployment/restart. It does not backfill missed intervals.
- **Concurrency:** one bot instance is assumed. Do not run multiple replicas with long polling against the same bot token.
