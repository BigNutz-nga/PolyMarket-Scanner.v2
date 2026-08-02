# Browser-Layer Live Check

- Endpoint: `https://data-api.polymarket.com/v1/leaderboard`
- Request type: unauthenticated production GET
- Result: HTTP content retrieved as a live JSON array during this build
- Observed first-item keys: `rank`, `proxyWallet`, `userName`, `xUsername`, `verifiedBadge`, `vol`, `pnl`, `profileImage`
- Scope: confirms only the default leaderboard route and observed field names
- Not confirmed by this check: explicit filters, offset pagination, response headers, throttling, positions, activity, trades, or closed positions

The application execution container separately failed DNS resolution, so the full `live_validate.py` run remains failed and must be rerun from an internet-connected local or Railway environment.
