# Fairmark

Prices Kalshi sports markets against no-vig sportsbook odds, flags legs that are cheap or overpriced, and buys at a target price I approve ahead of time.

Work in progress. Paper mode comes before any real-money execution.

## Run locally

```bash
cp .env.example .env
docker compose up -d
uv run pytest
```

## Price a slate

Write a text file with one slip per line. Use the fighter or team and opponent when a short name could match several events.

```text
slip 1 10: Team A, Team B
```

```bash
uv run fairmark slate slate.txt
```

The command reads public Kalshi, Pinnacle and Bovada markets. It reports the Kalshi yes ask, Bovada's raw implied percentage and Pinnacle's no-vig percentage. Slip payouts use fixed heuristic combo cuts. They are not live Kalshi quotes. Buy-by times are five minutes before the earliest matched start in Pacific time. Monte Carlo outcomes treat distinct legs as independent and reuse one outcome for any leg shared across slips.
