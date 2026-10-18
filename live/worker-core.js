const NO_STORE = { 'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer' }
const STALE_AFTER_MS = 6 * 60 * 1000

function authorized(request, key) {
  const given = new URL(request.url).searchParams.get('k') || ''
  if (!key || !given) return false
  let difference = given.length ^ key.length
  for (let i = 0; i < Math.max(given.length, key.length); i++) {
    difference |= (given.charCodeAt(i) || 0) ^ (key.charCodeAt(i) || 0)
  }
  return difference === 0
}

export function makeWorker(page, { now = Date.now } = {}) {
  return {
    async fetch(request, env) {
      if (!authorized(request, env.ACCESS_KEY)) return new Response('Not found', { status: 404, headers: NO_STORE })
      const path = new URL(request.url).pathname
      if (path === '/') return new Response(page, { headers: { ...NO_STORE, 'Content-Type': 'text/html; charset=utf-8', 'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'" } })
      if (path !== '/api/schedule') return new Response('Not found', { status: 404, headers: NO_STORE })
      const config = await env.SCHEDULE.get('config:current', 'json')
      if (!config) return new Response(JSON.stringify({ error: 'No slips uploaded' }), { status: 503, headers: { ...NO_STORE, 'Content-Type': 'application/json' } })
      const [snapshot, failure] = await Promise.all([
        env.SCHEDULE.get('snapshot:current', 'json'), env.SCHEDULE.get('poll:error', 'json')
      ])
      const stored = snapshot?.date === config.date ? snapshot.markets || {} : {}
      const markets = {}
      for (const leg of config.legs) {
        const market = stored[leg.ticker]
        if (market) markets[leg.ticker] = { ...market, fetchedAt: market.fetchedAt ?? snapshot.fetchedAt }
      }
      const timestamps = Object.values(markets).map(market => market.fetchedAt).filter(Number.isFinite)
      const latest = timestamps.length ? Math.max(...timestamps) : null
      const stale = config.legs.some(leg => !markets[leg.ticker] || !Number.isFinite(markets[leg.ticker].fetchedAt) || now() - markets[leg.ticker].fetchedAt > STALE_AFTER_MS) || Boolean(failure && (latest === null || failure.at >= latest))
      const legStatus = snapshot?.date === config.date ? snapshot.legStatus || {} : {}
      const firstPrices = snapshot?.date === config.date ? snapshot.firstPrices || {} : {}
      return new Response(JSON.stringify({ config, markets, legStatus, firstPrices, stale }), {
        headers: { ...NO_STORE, 'Content-Type': 'application/json' }
      })
    }
  }
}
