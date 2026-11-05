# Deploy the live schedule

The slip file, ticker overrides and generated schedule stay under ignored local paths. Cloudflare Pages serves the keyed page and JSON endpoint from D1. A cron Worker in `live/poller` runs every minute, checks Kalshi six times, 10 seconds apart, and saves `snapshot:current` to D1. Legs more than 5 minutes from their start are checked once a minute. The page refreshes every 10 seconds. Markets count as stale after two minutes or a poll failure.

The free plan allows 100,000 D1 rows written a day, and the poller uses at most 8,640. The poller's CPU per run has measured 20 to 49 ms, above the free plan's documented 10 ms, with no failed runs so far. If runs start failing, check `npx wrangler tail` in `live/poller`.

Run these steps from the repository root.

1. Create the table once. The database ID is in `live/poller/wrangler.toml`.

   ```bash
   npx wrangler d1 execute fairmark-live --remote --file live/poller/schema.sql --config live/poller/wrangler.toml
   ```

2. Deploy the poller. New crons can take up to 15 minutes to start.

   ```bash
   cd live/poller
   npx wrangler deploy
   ```

3. Set the private access key as a Pages secret. Set `ACCESS_KEY` in your shell without saving it to a file, then pipe it to Wrangler.

   ```bash
   printf '%s' "$ACCESS_KEY" | npx wrangler pages secret put ACCESS_KEY --project-name fairmark-live
   ```

   Keep the value and keyed URL private.

4. Write `data/slips/YYYY-MM-DD.txt` with `slip NAME STAKE [PAYOUT]: Leg, Leg` lines. A missing payout displays as `Not entered` and is excluded from payout totals. An optional `data/slips/YYYY-MM-DD.tickers` file pins legs with `Leg | KALSHI_TICKER | yes` or `Leg | KALSHI_TICKER | no`. A fourth field can supply a start with a UTC offset, followed by `true` or `false` to mark it estimated. A ticker file start takes priority. Sportsbook starts require both names from the pinned Kalshi matchup. Record cash-outs in an optional `data/slips/YYYY-MM-DD.cashouts` file with `SLIP AMOUNT [REMAINING_FRACTION]` lines. Omit the fraction for a full cash-out. A partial cash-out uses a fraction between 0 and 1 for the payout still riding. An optional `data/slips/YYYY-MM-DD.prices` file logs the prices seen at each buy. Upload the private config to D1.

   ```bash
   UV_CACHE_DIR=/private/tmp/fairmark-uv-cache uv run python scripts/push_slips.py YYYY-MM-DD
   ```

5. Build and deploy the private Pages project. The build reads `live/worker-core.js` and `live/page.html` and writes an ignored Pages Worker bundle.

   ```bash
   node live/pages/build.mjs
   cd live/pages
   npx wrangler pages deploy public --project-name fairmark-live --branch main
   ```

6. Open `https://fairmark-live.pages.dev/?k=YOUR_ACCESS_KEY` and check `/api/schedule?k=YOUR_ACCESS_KEY`.

For a later day, repeat step 4. The next poller run reads the new config. Keep slip files, generated pages, the access key and the keyed URL private.

Automatic cash-out detection needs the Kalshi private key, so it runs on the Mac instead of Cloudflare. While the Mac is off, a cash-out shows on the page once the Mac is back or the next push runs. The cloud poller has no Pinnacle live flag, so a leg turns Live at its start time.
