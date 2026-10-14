# Fairmark

Prices Kalshi sports markets against no-vig sportsbook odds, flags legs that are cheap or overpriced, and buys at a target price I approve ahead of time.

Work in progress. Paper mode comes before any real-money execution.

## Run locally

```bash
cp .env.example .env
docker compose up -d
uv run pytest
```
