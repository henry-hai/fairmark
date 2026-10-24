const HEADERS = {
  'Cache-Control': 'public, max-age=0, must-revalidate',
  'Content-Type': 'application/json; charset=utf-8',
  'X-Content-Type-Options': 'nosniff'
}
const DAY = /^\d{4}-\d{2}-\d{2}$/
const response = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: HEADERS })
const finite = value => typeof value === 'number' && Number.isFinite(value)
const demoDate = config => {
  if (DAY.test(config?.date || '')) return config.date
  const asOf = new Date(config?.asOf)
  return Number.isFinite(asOf.getTime()) ? new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/Los_Angeles', year: 'numeric', month: '2-digit', day: '2-digit'
  }).format(asOf) : null
}

function publicView(config, snapshot, days, archived) {
  if (!config || config.schema !== 1 || !Array.isArray(config.legs) ||
      !Array.isArray(config.slips) || !config.portfolio ||
      !demoDate(config) || !config.slips.length) return null
  const date = demoDate(config)
  const used = new Set(config.slips.flatMap(slip => slip.legIds))
  const legs = config.legs.filter(leg => used.has(leg.id)).map(leg => ({
    id: leg.id, name: leg.name, event: leg.event, start: leg.start,
    ...(typeof leg.priceAt === 'string' ? { priceAt: leg.priceAt } : {}),
    ask: leg.ask, fair: leg.fair, fee: leg.fee, underFair: leg.underFair,
    fairSource: leg.fairSource,
    bovadaOdds: leg.bovadaOdds ?? null,
    bovadaImplied: leg.bovadaImplied ?? null,
    pinnacleRaw: leg.pinnacleRaw ?? null
  }))
  const ids = new Set(legs.map(leg => leg.id))
  const slips = config.slips.map(slip => ({
    number: slip.number, legIds: slip.legIds, stake: slip.stake,
    placed: slip.placed || (archived ? null : config.asOf), hitChance: slip.hitChance,
    fee: slip.fee, totalPayout: slip.totalPayout, gain: slip.gain,
    fairPayout: slip.fairPayout, underFair: slip.underFair,
    ...(config.cashouts?.[String(slip.number)] ? { cashout: {
      amount: config.cashouts[String(slip.number)].amount,
      remainingFraction: config.cashouts[String(slip.number)].remainingFraction,
      at: config.cashouts[String(slip.number)].at
    } } : {})
  }))
  const portfolio = {
    stake: config.portfolio.stake, allHitPayout: config.portfolio.allHitPayout,
    allHitGain: config.portfolio.allHitGain, allHitFairPayout: config.portfolio.allHitFairPayout,
    allHitFairGain: config.portfolio.allHitFairGain,
    dayProfitChance: config.portfolio.dayProfitChance,
    medianGain: config.portfolio.medianGain, trials: config.portfolio.trials
  }
  if (!legs.length || ids.size !== used.size || !legs.every(leg =>
    ['id', 'name', 'event', 'start', 'fairSource'].every(key => typeof leg[key] === 'string') &&
    ['ask', 'fee'].every(key => finite(leg[key])) &&
    ['fair', 'underFair'].every(key => leg[key] === null || finite(leg[key]))) ||
    !slips.every(slip => Number.isInteger(slip.number) && Array.isArray(slip.legIds) &&
      slip.legIds.length && slip.legIds.every(id => typeof id === 'string' && ids.has(id)) &&
      ['stake', 'fee', 'totalPayout', 'gain'].every(key => finite(slip[key])) &&
      // A leg without a sportsbook fair leaves the slip's fair figures blank instead of hiding the day.
      ['hitChance', 'fairPayout', 'underFair'].every(key => slip[key] === null || finite(slip[key]))) ||
    !['stake', 'allHitPayout', 'allHitGain', 'allHitFairPayout', 'allHitFairGain', 'dayProfitChance', 'medianGain', 'trials'].every(key => finite(portfolio[key]))) return null
  const markets = {}
  const snapshotMarkets = snapshot?.date === date ? snapshot.markets : null
  for (const leg of legs) {
    const market = snapshotMarkets?.[leg.id]
    if (market && typeof market === 'object') markets[leg.id] = {
      yes_bid_dollars: market.yes_bid_dollars,
      yes_ask_dollars: market.yes_ask_dollars,
      result: market.result, settlement_ts: market.settlement_ts,
      close_time: market.close_time, fetchedAt: market.fetchedAt
    }
  }
  return { date, asOf: config.asOf, timeZone: 'America/Los_Angeles',
    days, archived, legs, slips, portfolio, markets }
}

export async function handleEngine({ request, env }) {
  if (request.method !== 'GET') return response({ error: 'Method not allowed' }, 405)
  const requested = new URL(request.url).searchParams.get('d') || ''
  if (requested && !DAY.test(requested)) return response({ error: 'Bad date' }, 400)
  const [config, days] = await Promise.all([
    env.SCHEDULE.get('config:demo', 'json'), env.SCHEDULE.get('days:demo', 'json')
  ])
  if (!config) return response({ error: 'Pricing slate unavailable' }, 503)
  const currentDate = demoDate(config)
  const available = [...new Set([...(Array.isArray(days) ? days.filter(day => DAY.test(day)) : []), currentDate].filter(Boolean))].sort().reverse()
  const archived = Boolean(requested && requested !== currentDate)
  if (archived && !available.includes(requested)) return response({ error: 'No demo for that day' }, 404)
  const records = await Promise.all(available.filter(date => date !== currentDate).map(async date => ({
    date, record: await env.SCHEDULE.get(`day:demo:${date}`, 'json')
  })))
  const record = records.find(item => item.date === requested)?.record
  if (archived && !record?.config) return response({ error: 'No demo for that day' }, 404)
  const currentSnapshot = await env.SCHEDULE.get('snapshot:demo', 'json')
  const view = publicView(archived ? record.config : config,
    archived ? record.snapshot : currentSnapshot, available, archived)
  if (!view) return response({ error: 'Pricing slate invalid' }, 503)
  const history = available.map(date => {
    const archive = records.find(item => item.date === date)?.record
    const day = date === currentDate ? publicView(config, currentSnapshot, [], false) :
      archive?.config ? publicView(archive.config, archive.snapshot, [], true) : null
    return day && day.date === date ? {
      date, legs: day.legs.map(({ id, start, ask }) => ({ id, start, ask })),
      slips: day.slips.map(({ legIds, stake, totalPayout, cashout }) => ({ legIds, stake, totalPayout, cashout })),
      markets: day.markets
    } : { date, missing: true }
  })
  return response({ ...view, history })
}
