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

## Live schedule

Add a local `data/slips/YYYY-MM-DD.txt` file with one line per slip. Put a confirmed payout after the stake when known, for example `slip 1 10 18: Team A, Team B`. Add `@6:40a` before the colon to record when the slip was placed (Pacific time). An optional `.tickers` file alongside it pins a leg to a Kalshi ticker and side. An optional `data/lists.json` with `ban`, `doghouse` and `hateWatch` name lists shows those lists on the page. These files and generated `schedule/` pages are ignored by git.

Run `UV_CACHE_DIR=/private/tmp/fairmark-uv-cache uv run python scripts/push_slips.py YYYY-MM-DD` to resolve the legs and upload the schedule to KV. See [live/DEPLOY.md](live/DEPLOY.md) for the initial Cloudflare setup and deploy command. After the first deploy, pushing a new day does not require another deploy. Pushing a new day freezes the previous one under its date, and the page shows a tab for every stored day.
