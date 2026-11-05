const KALSHI_MARKETS = 'https://external-api.kalshi.com/trade-api/v2/markets'
const BATCH = 100
// Cloudflare runs a cron at most once a minute, so each run checks six times, 10 seconds apart.
const CHECKS = 6
const GAP_MS = 10000
// Legs not close to starting move slowly, so they are checked once a minute instead of every 10 seconds.
const SOON_MS = 5 * 60 * 1000
// Only what the page reads. Kalshi sends about 2 KB per market, and the free plan allows 10 ms of CPU per run.
const KEPT = ['ticker', 'status', 'result', 'settlement_value_dollars', 'settlement_ts', 'close_time',
  'yes_bid_dollars', 'yes_ask_dollars', 'no_bid_dollars', 'no_ask_dollars']

const trim = market => Object.fromEntries(KEPT.filter(key => market[key] != null).map(key => [key, market[key]]))
const settled = market => Boolean(market?.result) && (market.status === 'finalized' || market.status === 'settled')

function firstPrice(market, side) {
  const bid = Number(market[`${side}_bid_dollars`])
  const ask = Number(market[`${side}_ask_dollars`])
  const quoted = market[`${side}_bid_dollars`] != null && market[`${side}_ask_dollars`] != null
  return quoted && !market.result && bid >= 0 && bid <= 1 && ask >= 0 && ask <= 1 ? (bid + ask) / 2 : null
}

export function d1Store(db) {
  return {
    async get(key) {
      const row = await db.prepare('SELECT value FROM store WHERE key = ?').bind(key).first()
      return row ? JSON.parse(row.value) : null
    },
    async put(key, value, at) {
      await db.prepare('INSERT INTO store (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at')
        .bind(key, JSON.stringify(value), at).run()
    },
    async delete(key) {
      await db.prepare('DELETE FROM store WHERE key = ?').bind(key).run()
    }
  }
}

export function mergeSnapshot(config, previous, fetched, at) {
  const same = previous?.date === config.date
  const markets = {}
  for (const [ticker, market] of Object.entries(same ? previous.markets || {} : {})) markets[ticker] = { ...trim(market), fetchedAt: at }
  for (const [ticker, market] of Object.entries(fetched)) markets[ticker] = { ...trim(market), fetchedAt: at }
  const firstPrices = { ...(same ? previous.firstPrices : {}) }
  for (const leg of config.legs) {
    const market = fetched[leg.ticker]
    if (leg.name in firstPrices || !market) continue
    const price = firstPrice(market, leg.side || 'yes')
    if (price !== null) firstPrices[leg.name] = price
  }
  return { date: config.date, markets, firstPrices, legStatus: same ? previous.legStatus || {} : {} }
}

async function fetchMarkets(tickers, request) {
  const fetched = {}
  for (let offset = 0; offset < tickers.length; offset += BATCH) {
    const batch = tickers.slice(offset, offset + BATCH)
    const response = await request(`${KALSHI_MARKETS}?tickers=${encodeURIComponent(batch.join(','))}&limit=${BATCH}`, { headers: { Accept: 'application/json' } })
    if (!response.ok) throw new Error(`Kalshi returned ${response.status}`)
    const payload = await response.json()
    if (!Array.isArray(payload?.markets)) throw new Error('Kalshi returned invalid markets')
    for (const market of payload.markets) if (batch.includes(market?.ticker)) fetched[market.ticker] = market
  }
  return fetched
}

export async function run(env, { request = fetch, now = Date.now, wait = ms => new Promise(resolve => setTimeout(resolve, ms)) } = {}) {
  const store = d1Store(env.DB)
  const config = await store.get('config:current')
  if (!Array.isArray(config?.legs) || !config.legs.length) return
  const tickers = [...new Set(config.legs.map(leg => leg.ticker))]
  const starts = Object.fromEntries(config.legs.map(leg => [leg.ticker, Date.parse(leg.start)]))
  let snapshot = await store.get('snapshot:current')
  let failure = await store.get('poll:error')
  const started = now()
  for (let check = 0; check < CHECKS; check++) {
    if (check) await wait(Math.max(0, started + check * GAP_MS - now()))
    const known = snapshot?.date === config.date ? snapshot.markets || {} : {}
    const open = tickers.filter(ticker => !settled(known[ticker]))
    // Every leg is final, so there is nothing to spend reads or writes on until the next push.
    if (!open.length) return
    const due = check ? open.filter(ticker => !known[ticker] || !(starts[ticker] > now() + SOON_MS)) : open
    if (!due.length) continue
    try {
      const fetched = await fetchMarkets(due, request)
      snapshot = mergeSnapshot(config, snapshot, fetched, now())
      await store.put('snapshot:current', snapshot, now())
      if (failure) {
        await store.delete('poll:error')
        failure = null
      }
    } catch (error) {
      if (!failure) {
        failure = { at: now(), message: String(error.message || error) }
        await store.put('poll:error', failure, now())
      }
    }
  }
}
