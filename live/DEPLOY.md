# Deploy the live schedule

The slip file, ticker overrides and generated schedule stay under ignored local paths. Cloudflare Pages serves the keyed page and JSON endpoint from Workers KV. GitHub Actions reads `config:current`, fetches Kalshi markets every five minutes, and writes `snapshot:current` back to KV. The page refreshes every 30 seconds and marks markets stale after six minutes or a poll failure.

Run these steps from the repository root.

1. Use the existing Workers KV namespace. Its ID is in `live/pages/wrangler.toml`.

   ```bash
   npx wrangler kv namespace list
   ```

2. In Cloudflare, open **My Profile > API Tokens > Create Token > Create Custom Token**. Give the token the account permission **Workers KV Storage Edit**, scoped to the account that owns the namespace. This allows the poller to read the private config and update the snapshot. Copy the token once.

3. In GitHub, open **Settings > Secrets and variables > Actions > New repository secret**. Add `CF_API_TOKEN` with the token, `CF_ACCOUNT_ID` with the Cloudflare account ID, `CF_KV_NAMESPACE_ID` with the namespace ID from step 1, and `PINNACLE_API_KEY` with the guest API key. Keep these values out of the repository. Without the Pinnacle key, live status uses start times and Kalshi price movement.

4. Set the private access key as a Pages secret. Set `ACCESS_KEY` in your shell without saving it to a file, then pipe it to Wrangler.

   ```bash
   printf '%s' "$ACCESS_KEY" | npx wrangler pages secret put ACCESS_KEY --project-name fairmark-live
   ```

   Keep the value and keyed URL private.

5. Write `data/slips/YYYY-MM-DD.txt` with `slip NAME STAKE [PAYOUT]: Leg, Leg` lines. A missing payout displays as `Not entered` and is excluded from payout totals. An optional `data/slips/YYYY-MM-DD.tickers` file pins legs with `Leg | KALSHI_TICKER | yes` or `Leg | KALSHI_TICKER | no`. A fourth field can supply a start with a UTC offset, followed by `true` or `false` to mark it estimated. A ticker file start takes priority. Sportsbook starts require both names from the pinned Kalshi matchup. Record cash-outs in an optional `data/slips/YYYY-MM-DD.cashouts` file with `SLIP AMOUNT [REMAINING_FRACTION]` lines. Omit the fraction for a full cash-out. A partial cash-out uses a fraction between 0 and 1 for the payout still riding. Upload the private config to KV.

   ```bash
   UV_CACHE_DIR=/private/tmp/fairmark-uv-cache uv run python scripts/push_slips.py YYYY-MM-DD
   ```

6. Build and deploy the private Pages project. The build reads `live/worker-core.js` and `live/page.html` and writes an ignored Pages Worker bundle.

   ```bash
   node live/pages/build.mjs
   cd live/pages
   npx wrangler pages deploy public --project-name fairmark-live --branch main
   ```

7. Open `https://fairmark-live.pages.dev/?k=YOUR_ACCESS_KEY` and check `/api/schedule?k=YOUR_ACCESS_KEY`. GitHub runs scheduled workflows only from the default branch, and the manual **Run workflow** button requires the workflow file there too. The poller still writes to the same KV namespace.

For a later day, repeat step 5. The next poll reads the new config. Keep slip files, generated pages, the access key and the keyed URL private.

Standard GitHub hosted Actions minutes are free for public repositories. On a private repository, each job uses at least one full billable minute, first from the plan's included minutes and then as paid usage. A five minute schedule can use about 8,640 minutes in a 30 day month. See [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions) and [job execution time](https://docs.github.com/en/actions/how-tos/monitor-workflows/view-job-execution-time).
