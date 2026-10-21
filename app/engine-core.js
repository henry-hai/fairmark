const HEADERS = {
  'Cache-Control': 'public, max-age=0, must-revalidate',
  'Content-Type': 'application/json; charset=utf-8',
  'X-Content-Type-Options': 'nosniff'
}

const response = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: HEADERS })
const finite = value => typeof value === 'number' && Number.isFinite(value)

function publicConfig(config) {
  if (!config || config.schema !== 1 || typeof config.slateDate !== 'string' ||
    typeof config.slateEndDate !== 'string' || !Array.isArray(config.legs) ||
    !Array.isArray(config.slips) || !config.portfolio) return null
  const legs = config.legs.map(leg => ({
    id: leg.id, name: leg.name, event: leg.event, start: leg.start,
    ask: leg.ask, fair: leg.fair, fee: leg.fee, underFair: leg.underFair,
    fairSource: leg.fairSource
  }))
  const slips = config.slips.map(slip => ({
    number: slip.number, legIds: slip.legIds, stake: slip.stake,
    hitChance: slip.hitChance, fee: slip.fee,
    totalPayout: slip.totalPayout, gain: slip.gain,
    fairPayout: slip.fairPayout, underFair: slip.underFair
  }))
  const portfolio = {
    stake: config.portfolio.stake,
    allHitPayout: config.portfolio.allHitPayout,
    allHitGain: config.portfolio.allHitGain,
    allHitFairPayout: config.portfolio.allHitFairPayout,
    allHitFairGain: config.portfolio.allHitFairGain,
    dayProfitChance: config.portfolio.dayProfitChance,
    medianGain: config.portfolio.medianGain,
    trials: config.portfolio.trials
  }
  if (!legs.length || !slips.length || !legs.every(leg =>
    ['id', 'name', 'event', 'start', 'fairSource'].every(key => typeof leg[key] === 'string') &&
    ['ask', 'fair', 'fee', 'underFair'].every(key => finite(leg[key]))) ||
    !slips.every(slip => Number.isInteger(slip.number) && Array.isArray(slip.legIds) &&
      slip.legIds.every(id => typeof id === 'string') &&
      ['stake', 'hitChance', 'fee', 'totalPayout', 'gain', 'fairPayout', 'underFair'].every(key => finite(slip[key]))) ||
    !['stake', 'allHitPayout', 'allHitGain', 'allHitFairPayout', 'allHitFairGain', 'dayProfitChance', 'medianGain', 'trials'].every(key => finite(portfolio[key]))) return null
  return { asOf: config.asOf, slateDate: config.slateDate, slateEndDate: config.slateEndDate,
    timeZone: 'America/Los_Angeles', legs, slips, portfolio }
}

export async function handleEngine({ request, env }) {
  if (request.method !== 'GET') return response({ error: 'Method not allowed' }, 405)
  const config = await env.SCHEDULE.get('config:demo', 'json')
  if (!config) return response({ error: 'Pricing slate unavailable' }, 503)
  const payload = publicConfig(config)
  return payload ? response(payload) : response({ error: 'Pricing slate invalid' }, 503)
}
