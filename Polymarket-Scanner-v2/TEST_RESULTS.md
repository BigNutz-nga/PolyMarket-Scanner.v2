# Test Results

## Passed in the build environment

```text
python -m compileall -q .
pytest -q
.....                                                                    [100%]
5 passed
```

The tests cover:

- Jaccard/cosine position overlap and synchronized-trade clustering.
- Partial per-wallet endpoint failures and abort behavior.
- SQLite persistence and latest-analysis retrieval.
- JSON and Markdown report generation.
- Telegram chat-ID allowlist behavior with a mocked transport.

A synthetic CLI scan also completed through analysis, report generation, and SQLite storage without external network access.

## Not passed live here

- Full Polymarket Data API validator: failed at container DNS resolution.
- Telegram production Bot API: not run because no user bot token was available and the container lacked outbound DNS.
- Railway deployment: configuration generated, but not deployed because no Railway/GitHub credentials were available.

See `VALIDATION_STATUS.md` and `validation/live_validation_report.md`.
