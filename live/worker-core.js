const NO_STORE = { 'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer' }
const STALE_AFTER_MS = 2 * 60 * 1000
const DAY = /^\d{4}-\d{2}-\d{2}$/

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { ...NO_STORE, 'Content-Type': 'application/json' } })

function view(config, snapshot) {
  const sameDay = snapshot?.date === config.date
  const stored = sameDay ? snapshot.markets || {} : {}
  const markets = {}
  for (const leg of config.legs) {
    const market = stored[leg.ticker]
    if (market) markets[leg.ticker] = { ...market, fetchedAt: market.fetchedAt ?? snapshot.fetchedAt }
  }
  return { config, markets, legStatus: sameDay ? snapshot.legStatus || {} : {}, firstPrices: sameDay ? snapshot.firstPrices || {} : {} }
}

function staleness(config, result, failure, now) {
  const timestamps = Object.values(result.markets).map(market => market.fetchedAt).filter(Number.isFinite)
  const latest = timestamps.length ? Math.max(...timestamps) : null
  return config.legs.some(leg => !result.markets[leg.ticker]?.result) && config.legs.some(leg => !result.markets[leg.ticker] || !Number.isFinite(result.markets[leg.ticker].fetchedAt) || now() - result.markets[leg.ticker].fetchedAt > STALE_AFTER_MS) || Boolean(failure && (latest === null || failure.at >= latest))
}

// Reads from D1 when the deployment has it, else from Workers KV. D1 takes up to 100 bound values per
// query and 50 queries per request, so the archive is read in batches rather than one key at a time.
async function readKeys(env, keys) {
  const found = {}
  if (env.DB) {
    for (let offset = 0; offset < keys.length; offset += 90) {
      const batch = keys.slice(offset, offset + 90)
      const { results } = await env.DB.prepare(`SELECT key, value FROM store WHERE key IN (${batch.map(() => '?').join(', ')})`).bind(...batch).all()
      for (const row of results) found[row.key] = JSON.parse(row.value)
    }
  } else {
    await Promise.all(keys.map(async key => { found[key] = await env.SCHEDULE.get(key, 'json') }))
  }
  return found
}

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
      const params = new URL(request.url).searchParams
      const requested = params.get('d') || ''
      if (requested && !DAY.test(requested)) return json({ error: 'Bad date' }, 400)
      // A routine refresh reads only today's three keys. Archived days never change, so the page keeps
      // them from its last full fetch.
      if (params.get('lite') === '1') {
        const today = await readKeys(env, ['config:current', 'snapshot:current', 'poll:error'])
        const config = today['config:current']
        const snapshot = today['snapshot:current']
        const failure = today['poll:error']
        if (!config) return json({ error: 'No slips uploaded' }, 503)
        if (requested && requested !== config.date) return json({ error: 'A lite refresh covers the current day only' }, 400)
        const result = view(config, snapshot)
        return json({ ...result, stale: staleness(config, result, failure, now), archived: false, lite: true })
      }
      const top = await readKeys(env, ['config:current', 'days', 'lists', 'snapshot:current', 'poll:error'])
      const current = top['config:current']
      const days = top.days
      const lists = top.lists
      const shared = { days: [], lists: lists && typeof lists === 'object' && !Array.isArray(lists) ? lists : {} }
      const archivedDays = Array.isArray(days) ? days.filter(day => DAY.test(day)) : []
      shared.days = [...new Set([...archivedDays, ...(current?.date ? [current.date] : [])])].sort()
      const pastDays = shared.days.filter(day => day !== current?.date)
      const stored = await readKeys(env, pastDays.map(day => `day:${day}`))
      const archivedRecords = pastDays.map(day => ({ date: day, record: stored[`day:${day}`] ?? null }))
      const snapshot = current ? top['snapshot:current'] : null
      const failure = current ? top['poll:error'] : null
      shared.history = archivedRecords.map(({ date, record }) => record?.config?.date === date
        ? { date, ...view(record.config, record.snapshot) } : { date, missing: true })
      if (current?.date) shared.history.push({ date: current.date, ...view(current, snapshot) })
      shared.history.sort((a, b) => a.date.localeCompare(b.date))
      if (requested && requested !== current?.date) {
        const archived = archivedRecords.find(item => item.date === requested)?.record
        if (!archived?.config) return json({ error: 'No slips for that day' }, 404)
        return json({ ...view(archived.config, archived.snapshot), stale: false, archived: true, ...shared })
      }
      const config = current
      if (!config) return json({ error: 'No slips uploaded' }, 503)
      const result = view(config, snapshot)
      return json({ ...result, stale: staleness(config, result, failure, now), archived: false, ...shared })
    }
  }
}
