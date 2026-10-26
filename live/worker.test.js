import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { runInNewContext } from 'node:vm'
import { makeWorker } from './worker-core.js'

const config = {
  date: '2026-10-17',
  slips: [{ name: '1', stake: '10', payout: '18', legs: ['Azure'] }],
  legs: [
    { name: 'Azure', ticker: 'KXCS2GAME-EXAMPLE-A', side: 'yes', start: '2026-10-17T17:00:00Z' },
    { name: 'Opponent', ticker: 'KXCS2GAME-EVENT-OPP', side: 'no', start: '2026-10-17T17:00:00Z' }
  ]
}

function kv(initial = {}) {
  const data = new Map(Object.entries(initial))
  return { async get(key) { return data.get(key) } }
}

test('page and JSON reject absent or wrong keys before reading KV', async () => {
  let reads = 0
  const worker = makeWorker('<html></html>')
  const env = { ACCESS_KEY: 'private', SCHEDULE: { async get() { reads++; return null } } }
  for (const path of ['/', '/api/schedule']) {
    for (const query of ['', '?k=wrong']) {
      const result = await worker.fetch(new Request(`https://example.com${path}${query}`), env)
      assert.equal(result.status, 404)
    }
  }
  assert.equal(reads, 0)
})

test('Worker only exposes fetch and serves stored markets with individual timestamps', async () => {
  const store = kv({ 'config:current': config, 'snapshot:current': {
    date: config.date, markets: { [config.legs[0].ticker]: { ticker: config.legs[0].ticker, result: 'yes', fetchedAt: 1000 } },
    legStatus: { Azure: { live: true, start: '2026-10-17T16:00:00Z' } }, firstPrices: { Azure: 0.42 }
  } })
  const worker = makeWorker('<html></html>', { now: () => 62000 })
  assert.deepEqual(Object.keys(worker), ['fetch'])
  const env = { ACCESS_KEY: 'private', SCHEDULE: store }
  const page = await worker.fetch(new Request('https://example.com/?k=private'), env)
  assert.equal(page.status, 200)
  assert.match(page.headers.get('Content-Type'), /text\/html/)
  const payload = await (await worker.fetch(new Request('https://example.com/api/schedule?k=private'), env)).json()
  assert.equal(payload.stale, true)
  assert.equal(Object.keys(payload.markets).length, 1)
  assert.equal(payload.markets[config.legs[0].ticker].fetchedAt, 1000)
  assert.deepEqual(payload.legStatus, { Azure: { live: true, start: '2026-10-17T16:00:00Z' } })
  assert.deepEqual(payload.firstPrices, { Azure: 0.42 })
})

test('five minute polling stays current for six minutes and failures mark data stale', async () => {
  const stored = { date: config.date, markets: Object.fromEntries(config.legs.map(leg => [leg.ticker, { ticker: leg.ticker, fetchedAt: 1000 }])) }
  const store = kv({ 'config:current': config, 'snapshot:current': stored })
  const env = { ACCESS_KEY: 'private', SCHEDULE: store }
  const url = 'https://example.com/api/schedule?k=private'
  const current = makeWorker('', { now: () => 300000 })
  assert.equal((await (await current.fetch(new Request(url), env)).json()).stale, false)
  const expired = makeWorker('', { now: () => 370001 })
  assert.equal((await (await expired.fetch(new Request(url), env)).json()).stale, true)
  const failedEnv = { ...env, SCHEDULE: kv({ 'config:current': config, 'snapshot:current': stored, 'poll:error': { at: 2000 } }) }
  assert.equal((await (await current.fetch(new Request(url), failedEnv)).json()).stale, true)
})

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return { getItem(key) { return values.get(key) ?? null }, setItem(key, value) { values.set(key, value) } }
}

async function renderPage(data, { now = 62000, storage = memoryStorage() } = {}) {
  const html = readFileSync(new URL('./page.html', import.meta.url), 'utf8')
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  const elements = new Map()
  const document = { details: [], addEventListener(type, listener) { if (type === 'toggle') this.toggleListener = listener }, querySelectorAll() { return this.details }, getElementById(id) {
    if (!elements.has(id)) {
      const element = { textContent: '', className: '', addEventListener(type, listener) { this.listener = listener } }
      let html = ''
      Object.defineProperty(element, 'innerHTML', { get() { return html }, set(value) {
        html = value
        if (id === 'slips') document.details = [...value.matchAll(/<details data-expand-key="([^"]+)"( open)?>/g)].map(match => ({ dataset: { expandKey: match[1] }, open: Boolean(match[2]), matches: () => true }))
      } })
      elements.set(id, element)
    }
    return elements.get(id)
  } }
  document.clickFilter = (group, status) => document.getElementById(group === 'timeline' ? 'timeline-filters' : 'slip-filters').listener({ target: { dataset: { filter: status } } })
  document.toggleDetail = (key, open) => {
    const detail = document.details.find(item => item.dataset.expandKey === key)
    assert.ok(detail)
    detail.open = open
    document.toggleListener({ target: detail })
  }
  class TestDate extends Date { static now() { return now } }
  runInNewContext(script + '\n document.rerender = render', { document, Date: TestDate, Intl, localStorage: storage, location: { search: '?k=private' }, fetch: async () => ({ ok: true, json: async () => data }), setInterval() {} })
  await new Promise(resolve => setImmediate(resolve))
  return document
}

test('page shows a separate age or missing label for each leg', async () => {
  const pageConfig = { ...config, slips: [{ ...config.slips[0], legs: ['Azure', 'Opponent'] }] }
  const data = { config: pageConfig, markets: { [config.legs[0].ticker]: { ticker: config.legs[0].ticker, fetchedAt: 1000 } }, stale: true }
  const document = await renderPage(data)
  const timeline = document.getElementById('timeline').innerHTML
  assert.match(timeline, /Updated 61 seconds ago/)
  assert.match(timeline, /No market data yet/)
})

test('finished timeline rows fade and show a one-line date before time', async () => {
  const legs = [
    { name:'Won', ticker:'WON', side:'yes', start:'2026-10-21T02:00:00Z', estimated:true },
    { name:'Lost', ticker:'LOST', side:'yes', start:'2026-10-21T02:00:00Z' }
  ]
  const slips = [{ name:'1', stake:'10', payout:'20', legs:['Won'] }, { name:'2', stake:'10', payout:'20', legs:['Lost'] }]
  const document = await renderPage({ config:{ date:'2026-10-20', legs, slips }, markets:{ WON:{ result:'yes' }, LOST:{ result:'no' } }, stale:false })
  const timeline = document.getElementById('timeline').innerHTML
  const html = readFileSync(new URL('./page.html', import.meta.url), 'utf8')
  assert.match(html, /\.timeline-item\.won-leg, \.timeline-item\.dead-leg \{ opacity: \.55; \}/)
  assert.match(html, /\.time \{[^}]*white-space: nowrap/)
  assert.match(timeline, /class="timeline-item won-leg"><div class="time">10\/20\/26 · 7:00 PM<span class="estimate">estimated<\/span>/)
  assert.match(timeline, /class="timeline-item dead-leg"><div class="time">10\/20\/26 · 7:00 PM/)
})

test('profit tracker totals won and alive payouts and updates when a slip dies', async () => {
  const html = readFileSync(new URL('./page.html', import.meta.url), 'utf8')
  assert.ok(html.indexOf('id="profit-tracker"') < html.indexOf('id="summary"'))
  const legs = ['Settled win', 'Price win', 'Settled loss', 'Open yes', 'Open no'].map((name, index) => ({
    name, ticker: `PROFIT-${index}`, side: index === 4 ? 'no' : 'yes', start: '2026-10-17T17:00:00Z'
  }))
  const slips = [
    { name: 'result win', stake: '10', payout: '18', legs: ['Settled win'] },
    { name: 'price win', stake: '20', payout: '30', legs: ['Price win'] },
    { name: 'dead', stake: '5', payout: '50', legs: ['Settled loss', 'Open yes'] },
    { name: 'riding', stake: '10', payout: '40', legs: ['Open yes', 'Open no'] }
  ]
  const markets = {
    'PROFIT-0': { result: 'yes' },
    'PROFIT-1': { yes_bid_dollars: '0.97', yes_ask_dollars: '0.99' },
    'PROFIT-2': { result: 'no', yes_bid_dollars: '0.99', yes_ask_dollars: '1.00' },
    'PROFIT-3': { yes_bid_dollars: '0.50', yes_ask_dollars: '0.70' },
    'PROFIT-4': { no_bid_dollars: '0.40', no_ask_dollars: '0.60' }
  }
  const pageConfig = { date: config.date, legs, slips, depositFee: '2.00' }
  const document = await renderPage({ config: pageConfig, markets, stale: false })
  const tracker = document.getElementById('profit-tracker').innerHTML
  assert.match(tracker, /Profit now<\/span><strong class="positive">\+\$1\.00/)
  assert.match(tracker, /Still riding<\/span><strong>\$30\.00<\/strong><small class="detail">Max payout \$70\.00/)
  assert.match(tracker, /Max profit<\/span><strong class="positive">\+\$41\.00<\/strong>/)
  assert.doesNotMatch(tracker, /Expected finish|Best case/)
  assert.match(tracker, /Deposit fee \$2\.00 included in profit/)
  assert.match(document.getElementById('slips').innerHTML, /id="slip-riding"/)
  document.clickFilter('slips', 'won')
  assert.match(document.getElementById('slips').innerHTML, /id="slip-price%20win".*?class="badge alive"/)
  assert.match(document.getElementById('profit-tracker').innerHTML, /Profit now<\/span><strong class="positive">\+\$1\.00/)
  document.rerender({ config: pageConfig, markets: { ...markets, 'PROFIT-1': { result: 'yes' },
    'PROFIT-3': { result: 'yes' }, 'PROFIT-4': { result: 'no' } }, stale: false })
  assert.match(document.getElementById('profit-tracker').innerHTML, /Max profit<\/span><strong class="positive">\+\$41\.00<\/strong>/)
  document.rerender({ config: pageConfig, markets: { ...markets, 'PROFIT-3': { result: 'no' } }, stale: false })
  const afterLoss = document.getElementById('profit-tracker').innerHTML
  assert.match(afterLoss, /Still riding<\/span><strong>\$20\.00<\/strong>/)
  assert.match(afterLoss, /Max profit<\/span><strong class="positive">\+\$1\.00<\/strong>/)
  assert.doesNotMatch(afterLoss, /\$88\.00/)
})

test('profit tracker shows losses without a fee and counts payout without quotes', async () => {
  const pageConfig = { date: config.date, legs: config.legs, slips: [
    { name: 'lost', stake: '12', payout: '24', legs: ['Azure'] },
    { name: 'open', stake: '5', payout: '15', legs: ['Opponent'] }
  ] }
  const document = await renderPage({ config: pageConfig, markets: { [config.legs[0].ticker]: { result: 'no' } }, stale: true })
  const tracker = document.getElementById('profit-tracker').innerHTML
  assert.match(tracker, /Profit now<\/span><strong class="negative">-\$12\.00/)
  assert.match(tracker, /Still riding<\/span><strong>\$5\.00<\/strong><small class="detail">Max payout \$15\.00/)
  assert.match(tracker, /Max profit<\/span><strong class="negative">-\$2\.00<\/strong>/)
  assert.doesNotMatch(tracker, /Expected finish|Best case/)
  assert.doesNotMatch(tracker, /Deposit fee/)
})

test('Pinnacle live flag and start override the estimated schedule time', async () => {
  const leg = { name: 'Fight', ticker: 'FIGHT', side: 'yes', start: '2026-10-17T22:00:00Z', estimated: true }
  const pageConfig = { date: config.date, legs: [leg], slips: [{ name: '1', stake: '10', payout: '20', legs: ['Fight'] }] }
  const data = { config: pageConfig, markets: {}, legStatus: { Fight: { live: true, start: '2026-10-17T20:00:00Z' } }, stale: true }
  const document = await renderPage(data, { now: Date.parse('2026-10-17T19:00:00Z') })
  let timeline = document.getElementById('timeline').innerHTML
  assert.match(timeline, /1:00 PM.*?Fight \(quote unavailable\).*?class="badge live"/)
  assert.doesNotMatch(timeline, /estimated/)
  document.rerender({ ...data, legStatus: { Fight: { live: false, start: '2026-10-17T18:00:00Z' } } })
  timeline = document.getElementById('timeline').innerHTML
  assert.match(timeline, /11:00 AM.*?Fight \(quote unavailable\).*?class="badge live"/)
  document.rerender({ ...data, legStatus: { Fight: { live: false, start: '2026-10-17T20:00:00Z' } } })
  assert.match(document.getElementById('timeline').innerHTML, /class="badge upcoming"/)
  document.rerender({ ...data, markets: { FIGHT: { result: 'no' } } })
  assert.match(document.getElementById('timeline').innerHTML, /class="badge dead"/)
})

test('a pregame price jump keeps a leg upcoming until its start time', async () => {
  const legs = [{ name: 'Pregame', ticker: 'P', side: 'yes', start: '2026-10-17T22:00:00Z' }]
  const pageConfig = { date: config.date, legs, slips: legs.map(leg => ({ name: leg.name, stake: '10', payout: '20', legs: [leg.name] })) }
  const data = { config: pageConfig, markets: {
    P: { yes_bid_dollars: '0.73', yes_ask_dollars: '0.74' }
  }, firstPrices: { Pregame: 0.60 }, stale: false }
  const document = await renderPage(data, { now: Date.parse('2026-10-17T19:00:00Z') })
  assert.match(document.getElementById('timeline').innerHTML, /Pregame \(73\.5%\).*?class="badge upcoming"/)
})

test('default filters show every status, while toggles hide settled rows', async () => {
  const legs = ['Won', 'Lost', 'Dead future', 'Shared future', 'Alive future'].map((name, index) => ({
    name, ticker: `TICKER-${index}`, side: 'yes', start: `2026-10-17T${String(17 + index).padStart(2, '0')}:00:00Z`
  }))
  const pageConfig = { date: config.date, legs, slips: [
    { name: 'dead first', stake: '10', payout: '20', legs: ['Won', 'Lost', 'Dead future', 'Shared future'] },
    { name: 'alive', stake: '5', payout: '9', legs: ['Shared future', 'Alive future'] },
    { name: 'dead last', stake: '2', payout: '4', legs: ['Lost', 'Dead future'] }
  ] }
  const markets = { [legs[0].ticker]: { result: 'yes' }, [legs[1].ticker]: { result: 'no' } }
  const document = await renderPage({ config: pageConfig, markets, stale: true })
  let timeline = document.getElementById('timeline').innerHTML
  let slips = document.getElementById('slips').innerHTML
  assert.match(document.getElementById('timeline-filters').innerHTML, /data-filter="live" aria-pressed="true"/)
  assert.match(document.getElementById('timeline-filters').innerHTML, /data-filter="upcoming" aria-pressed="true"/)
  assert.match(document.getElementById('timeline-filters').innerHTML, /data-filter="won" aria-pressed="true"/)
  assert.match(document.getElementById('timeline-filters').innerHTML, /data-filter="dead" aria-pressed="true"/)
  assert.match(document.getElementById('slip-filters').innerHTML, /data-filter="alive" aria-pressed="true"/)
  assert.match(document.getElementById('slip-filters').innerHTML, /data-filter="won" aria-pressed="true"/)
  assert.match(document.getElementById('slip-filters').innerHTML, /data-filter="dead" aria-pressed="true"/)
  assert.match(document.getElementById('slip-filters').innerHTML, /data-filter="cashed" aria-pressed="true"/)
  assert.match(timeline, /Shared future/)
  assert.match(timeline, /Alive future/)
  assert.match(timeline, /class="timeline-item won-leg"[^>]*>.*?Won \(100\.0%\)/)
  assert.match(timeline, /Dead future \(quote unavailable\).*?class="badge dead"/)
  assert.match(timeline, /Lost \(0\.0%\).*?class="badge dead"/)
  assert.ok(timeline.indexOf('Won (100.0%)') < timeline.indexOf('Lost (0.0%)'))
  assert.match(slips, /id="slip-alive"/)
  assert.ok(slips.indexOf('id="slip-alive"') < slips.indexOf('id="slip-dead%20first"'))
  assert.equal((slips.match(/<details data-expand-key=/g) || []).length, 2)
  assert.doesNotMatch(slips, /<details open/)
  assert.match(slips, /<summary>#dead first <span class="badge dead">dead<\/span><\/summary>/)
  document.clickFilter('timeline', 'won')
  document.clickFilter('timeline', 'dead')
  document.clickFilter('slips', 'dead')
  timeline = document.getElementById('timeline').innerHTML
  slips = document.getElementById('slips').innerHTML
  assert.doesNotMatch(timeline, /Dead future|Lost \(0\.0%\)|Won \(100\.0%\)/)
  assert.match(timeline, /Shared future|Alive future/)
  assert.match(slips, /id="slip-alive"/)
  assert.doesNotMatch(slips, /id="slip-dead/)
})

test('timeline orders live then upcoming by start and counts only alive stakes', async () => {
  const names = ['Later live', 'Earlier live', 'Far upcoming', 'Near upcoming', 'Won', 'Lost', 'Dead future']
  const starts = ['18', '17', '22', '20', '16', '15', '21']
  const legs = names.map((name, index) => ({ name, ticker: `T-${index}`, side: 'yes', start: `2026-10-17T${starts[index]}:00:00Z` }))
  const slips = [
    { name: 'far', stake: '100', payout: '200', legs: ['Far upcoming'] },
    { name: 'near', stake: '200', payout: '400', legs: ['Near upcoming'] },
    { name: 'shared 1', stake: '30', payout: '50', legs: ['Earlier live', 'Far upcoming'] },
    { name: 'shared 2', stake: '250', payout: '500', legs: ['Earlier live', 'Near upcoming'] },
    { name: 'shared 3', stake: '250', payout: '500', legs: ['Earlier live'] },
    { name: 'shared 4', stake: '250', payout: '500', legs: ['Earlier live'] },
    { name: 'dead', stake: '50', payout: '100', legs: ['Earlier live', 'Lost', 'Dead future'] },
    { name: 'late', stake: '50', payout: '100', legs: ['Later live'] }
  ]
  const markets = { 'T-4': { result: 'yes' }, 'T-5': { result: 'no' } }
  const document = await renderPage({ config: { date: config.date, legs, slips }, markets, stale: true }, { now: Date.parse('2026-10-17T19:00:00Z') })
  const timeline = document.getElementById('timeline').innerHTML
  const positions = ['Earlier live', 'Later live', 'Near upcoming', 'Far upcoming'].map(name => timeline.indexOf(`</span>${name} (`))
  assert.ok(positions.every((position, index) => position >= 0 && (index === 0 || position > positions[index - 1])))
  assert.match(timeline, /Earlier live \(quote unavailable\).*?In 4 alive slips, \$780 riding/)
  const rows = document.getElementById('slips').innerHTML
  assert.ok(rows.indexOf('id="slip-shared%201"') < rows.indexOf('id="slip-near"'))
  assert.ok(rows.indexOf('id="slip-near"') < rows.indexOf('id="slip-far"'))
  assert.ok(timeline.indexOf('Far upcoming') < timeline.indexOf('Won (100.0%)'))
  assert.ok(timeline.indexOf('Won (100.0%)') < timeline.indexOf('Lost (0.0%)'))
})

test('timeline links use the same numbered alive slips as the count and stake', async () => {
  const legs = [{ name: 'Team C', ticker: 'R', side: 'yes', start: '2026-10-17T17:00:00Z' }, { name: 'Lost', ticker: 'L', side: 'yes', start: '2026-10-17T17:00:00Z' }]
  const slips = [
    { name: '16', stake: '50', legs: ['Team C', 'Lost'] },
    { name: '10', stake: '100', legs: ['Team C'] },
    { name: '4', stake: '200', legs: ['Team C', 'Lost'] },
    { name: '3', stake: '250', legs: ['Team C'] },
    { name: '17', stake: '25', legs: ['Team C'] },
    { name: '20', stake: '20', legs: ['Team C'] }
  ]
  const document = await renderPage({ config: { date: config.date, legs, slips }, markets: { L: { result: 'no' } }, stale: true }, { now: Date.parse('2026-10-17T19:00:00Z') })
  const timeline = document.getElementById('timeline').innerHTML
  assert.match(timeline, /Team C \(quote unavailable\).*?In 4 alive slips, \$395 riding/)
  assert.match(timeline, /Slips <a href="#slip-3">#3<\/a> <a href="#slip-10">#10<\/a> <a href="#slip-17">#17<\/a> <a href="#slip-20">#20<\/a>/)
  assert.doesNotMatch(timeline, /href="#slip-(4|16)"/)
  document.clickFilter('slips', 'dead')
  assert.doesNotMatch(document.getElementById('timeline').innerHTML, /href="#slip-(4|16)"/)
})

test('timeline sorts by instant within each status and shows nearly won as won', async () => {
  const legs = [
    { name: 'Team D', ticker: 'U', side: 'yes', start: '2026-10-17T17:00:00-07:00' },
    { name: 'Team E', ticker: 'M', side: 'yes', start: '2026-10-17T16:00:00-07:00' },
    { name: 'Later won', ticker: 'W2', side: 'yes', start: '2026-10-17T14:00:00-07:00' },
    { name: 'Earlier won', ticker: 'W1', side: 'yes', start: '2026-10-17T12:00:00-07:00' },
    { name: 'Future 2', ticker: 'F2', side: 'yes', start: '2026-10-17T21:00:00-07:00' },
    { name: 'Future 1', ticker: 'F1', side: 'yes', start: '2026-10-17T20:00:00-07:00' }
  ]
  const slips = legs.map((leg, index) => ({ name: String(index + 1), stake: '1', legs: [leg.name] }))
  const markets = { M: { yes_bid_dollars: '0.97', yes_ask_dollars: '0.99' }, W1: { result: 'yes' }, W2: { result: 'yes' } }
  const document = await renderPage({ config: { date: config.date, legs, slips }, markets, stale: false }, { now: Date.parse('2026-10-18T01:00:00Z') })
  const timeline = document.getElementById('timeline').innerHTML
  const positions = ['Team D', 'Future 1', 'Future 2', 'Team E', 'Later won', 'Earlier won'].map(name => timeline.indexOf(`</span>${name} (`))
  assert.ok(positions.every((position, index) => position >= 0 && (index === 0 || position > positions[index - 1])))
  assert.match(timeline, /Team E \(98\.0%\)<\/strong><span class="badge won">won \(nearly\)/)
  assert.match(document.getElementById('slips').innerHTML, /id="slip-2".*?class="badge won"/)
})

test('live quote thresholds settle slips and remove nearly lost exposure', async () => {
  const names = ['Near loss', 'Near win', 'Above loss', 'Below win', 'Shared']
  const legs = names.map((name, index) => ({ name, ticker: `EDGE-${index}`, side: index === 1 ? 'no' : 'yes', start: '2026-10-17T17:00:00Z' }))
  const slips = [
    { name: 'dead', stake: '10', payout: '40', legs: ['Near loss', 'Shared'] },
    { name: 'won', stake: '20', payout: '35', legs: ['Near win'] },
    { name: 'above', stake: '30', payout: '60', legs: ['Above loss'] },
    { name: 'below', stake: '40', payout: '80', legs: ['Below win'] },
    { name: 'shared', stake: '5', payout: '10', legs: ['Shared'] }
  ]
  const markets = {
    'EDGE-0': { yes_bid_dollars: '0.01', yes_ask_dollars: '0.03' },
    'EDGE-1': { no_bid_dollars: '0.97', no_ask_dollars: '0.99' },
    'EDGE-2': { yes_bid_dollars: '0.02', yes_ask_dollars: '0.02001' },
    'EDGE-3': { yes_bid_dollars: '0.97999', yes_ask_dollars: '0.98' },
    'EDGE-4': { yes_bid_dollars: '0.50', yes_ask_dollars: '0.60' }
  }
  const document = await renderPage({ config: { date: config.date, legs, slips }, markets, stale: false }, { now: Date.parse('2026-10-17T19:00:00Z') })
  const tracker = document.getElementById('profit-tracker').innerHTML
  assert.match(tracker, /Profit now<\/span><strong class="positive">\+\$5\.00/)
  assert.match(tracker, /Still riding<\/span><strong>\$75\.00<\/strong><small class="detail">Max payout \$150\.00/)
  assert.match(tracker, /Max profit<\/span><strong class="positive">\+\$80\.00/)
  assert.match(document.getElementById('summary').innerHTML, /Payout locked in<\/span><strong>\$35\.00/)
  assert.match(document.getElementById('slips').innerHTML, /id="slip-above".*?Legs hit">0\/1.*?class="badge alive"/)
  assert.match(document.getElementById('slips').innerHTML, /id="slip-below".*?Legs hit">0\/1.*?class="badge alive"/)
  assert.match(document.getElementById('slips').innerHTML, /id="slip-won".*?Legs hit">1\/1.*?class="badge won"/)
  assert.match(document.getElementById('slips').innerHTML, /id="slip-dead".*?class="badge dead"/)
  const timeline = document.getElementById('timeline').innerHTML
  assert.match(timeline, /Near loss \(2\.0%\)<\/strong><span class="badge lost">lost \(nearly\)<\/span>.*?In 0 alive slips, \$0 riding/)
  assert.match(timeline, /Near win \(98\.0%\)<\/strong><span class="badge won">won \(nearly\)/)
  assert.match(timeline, /Shared \(55\.0%\).*?In 1 alive slip, \$5 riding.*?href="#slip-shared"/)
  assert.doesNotMatch(timeline, /href="#slip-dead"/)
})

test('every slip shows state chips and dead slips identify losing legs', async () => {
  const legs = ['Won', 'Near won', 'Lost', 'Near lost', 'Live', 'Upcoming'].map((name, index) => ({
    name, ticker: `CHIP-${index}`, side: 'yes', start: index === 5 ? '2026-10-17T22:00:00Z' : '2026-10-17T17:00:00Z'
  }))
  const slips = [
    { name: 'alive', stake: '10', payout: '20', legs: ['Won', 'Near won', 'Live', 'Upcoming'] },
    { name: 'won', stake: '10', payout: '20', legs: ['Won', 'Near won'] },
    { name: 'dead', stake: '10', payout: '20', legs: ['Won', 'Lost', 'Near lost', 'Upcoming'] },
    { name: 'cashed', stake: '20', payout: '30', legs: ['Lost'] }
  ]
  const markets = {
    'CHIP-0': { result: 'yes' }, 'CHIP-1': { yes_bid_dollars: '0.97', yes_ask_dollars: '0.99' },
    'CHIP-2': { result: 'no' }, 'CHIP-3': { yes_bid_dollars: '0.01', yes_ask_dollars: '0.03' },
    'CHIP-4': { yes_bid_dollars: '0.54', yes_ask_dollars: '0.56' }
  }
  const document = await renderPage({ config: { date: config.date, legs, slips, cashouts: { cashed: { amount: '2.50', remainingFraction: '0' } } }, markets, stale: false }, { now: Date.parse('2026-10-17T19:00:00Z') })
  const rows = document.getElementById('slips').innerHTML
  assert.match(rows, /id="slip-alive".*?leg-chip won.*?Won.*?✓.*?leg-chip won.*?Near won.*?✓.*?leg-chip live.*?Live.*?55\.0%.*?leg-chip upcoming.*?Upcoming.*?3:00 PM.*?Legs hit">2\/4/)
  assert.match(rows, /id="slip-won".*?Legs hit">2\/2.*?class="badge won"/)
  assert.match(rows, /id="slip-dead".*?<details.*?leg-chip lost killer.*?Lost.*?Killed slip.*?leg-chip lost killer.*?Near lost.*?Killed slip.*?Legs hit 1\/4/)
  assert.match(rows, /id="slip-cashed".*?leg-chip lost.*?Lost.*?Profit">−\$17\.50.*?class="badge cashed">Cashed Out/)
})

test('full and partial cash-outs reduce exposure and count received money', async () => {
  const legs = [
    { name: 'Lost', ticker: 'L', side: 'yes', start: '2026-10-17T17:00:00Z' },
    { name: 'Open', ticker: 'O', side: 'yes', start: '2026-10-17T17:00:00Z' }
  ]
  const slips = [
    { name: 'sample', stake: '12', payout: '36', legs: ['Lost'] },
    { name: 'partial', stake: '100', payout: '200', legs: ['Open'] },
    { name: 'untouched', stake: '10', payout: '20', legs: ['Open'] }
  ]
  const cashouts = { sample: { amount: '2.50', remainingFraction: '0' }, partial: { amount: '40', remainingFraction: '0.5' } }
  const document = await renderPage({ config: { date: config.date, legs, slips, cashouts }, markets: { L: { result: 'no' } }, stale: true }, { now: Date.parse('2026-10-17T19:00:00Z') })
  let tracker = document.getElementById('profit-tracker').innerHTML
  assert.match(tracker, /Profit now<\/span><strong class="negative">-\$19\.50/)
  assert.match(tracker, /Still riding<\/span><strong>\$60\.00<\/strong><small class="detail">Max payout \$120\.00/)
  assert.match(tracker, /Max profit<\/span><strong class="positive">\+\$40\.50/)
  assert.match(document.getElementById('summary').innerHTML, /Payout locked in<\/span><strong>\$42\.50/)
  assert.match(document.getElementById('summary').innerHTML, /Max still possible<\/span><strong>\$120\.00/)
  assert.match(document.getElementById('timeline').innerHTML, /Open \(quote unavailable\).*?In 2 alive slips, \$60 riding/)
  assert.match(document.getElementById('slips').innerHTML, /id="slip-sample".*?Profit">−\$9\.50.*?badge cashed/)
  document.rerender({ config: { date: config.date, legs, slips, cashouts }, markets: { L: { result: 'no' }, O: { result: 'yes' } }, stale: false })
  tracker = document.getElementById('profit-tracker').innerHTML
  assert.match(tracker, /Profit now<\/span><strong class="positive">\+\$40\.50/)
  assert.match(tracker, /Still riding<\/span><strong>\$0\.00/)
  assert.match(tracker, /Max profit<\/span><strong class="positive">\+\$40\.50/)
})

test('expanded slip state survives rerenders, filter changes, reloads, and storage errors', async () => {
  const pageConfig = { date: config.date, legs: config.legs, slips: [
    { name: '4', stake: '5', legs: ['Azure'] },
    { name: '16', stake: '10', legs: ['Azure'] }
  ] }
  const data = { config: pageConfig, markets: { [config.legs[0].ticker]: { result: 'no' } }, stale: false }
  const storage = memoryStorage()
  const first = await renderPage(data, { storage })
  first.toggleDetail('slip:4', true)
  first.rerender(data)
  assert.match(first.getElementById('slips').innerHTML, /data-expand-key="slip:4" open/)
  assert.doesNotMatch(first.getElementById('slips').innerHTML, /data-expand-key="slip:16" open/)
  first.clickFilter('slips', 'dead')
  first.clickFilter('slips', 'dead')
  assert.match(first.getElementById('slips').innerHTML, /data-expand-key="slip:4" open/)
  const second = await renderPage(data, { storage })
  assert.match(second.getElementById('slips').innerHTML, /data-expand-key="slip:4" open/)
  second.toggleDetail('slip:4', false)
  second.rerender(data)
  assert.doesNotMatch(second.getElementById('slips').innerHTML, /data-expand-key="slip:4" open/)
  const broken = { getItem() { throw new Error('blocked') }, setItem() { throw new Error('blocked') } }
  const fallback = await renderPage(data, { storage: broken })
  fallback.toggleDetail('slip:16', true)
  fallback.rerender(data)
  assert.match(fallback.getElementById('slips').innerHTML, /data-expand-key="slip:16" open/)
})

test('filters persist and storage failures do not break rendering', async () => {
  const data = { config, markets: {}, stale: true }
  const storage = memoryStorage()
  const first = await renderPage(data, { storage })
  first.clickFilter('timeline', 'upcoming')
  first.clickFilter('timeline', 'dead')
  first.clickFilter('slips', 'alive')
  const second = await renderPage(data, { storage })
  assert.match(second.getElementById('timeline-filters').innerHTML, /data-filter="upcoming" aria-pressed="false"/)
  assert.match(second.getElementById('timeline-filters').innerHTML, /data-filter="dead" aria-pressed="false"/)
  assert.match(second.getElementById('slip-filters').innerHTML, /data-filter="alive" aria-pressed="false"/)
  assert.equal(second.getElementById('timeline').innerHTML, '')
  assert.equal(second.getElementById('slips').innerHTML, '')
  const broken = { getItem() { throw new Error('blocked') }, setItem() { throw new Error('blocked') } }
  const fallback = await renderPage(data, { storage: broken })
  fallback.clickFilter('timeline', 'upcoming')
  fallback.clickFilter('timeline', 'dead')
  assert.equal(fallback.getElementById('timeline').innerHTML, '')
})

test('archived days are served frozen and listed with the current day', async () => {
  const oldConfig = { ...config, date: '2026-10-16' }
  const store = kv({ 'config:current': config, days: ['2026-10-16', 'bad'], 'day:2026-10-16': {
    config: oldConfig, snapshot: { date: '2026-10-16', markets: { [config.legs[0].ticker]: { result: 'yes', fetchedAt: 1 } } }
  } })
  const worker = makeWorker('', { now: () => 10 ** 12 })
  const env = { ACCESS_KEY: 'private', SCHEDULE: store }
  const archived = await (await worker.fetch(new Request('https://example.com/api/schedule?k=private&d=2026-10-16'), env)).json()
  assert.equal(archived.archived, true)
  assert.equal(archived.stale, false)
  assert.equal(archived.config.date, '2026-10-16')
  assert.equal(archived.markets[config.legs[0].ticker].result, 'yes')
  assert.deepEqual(archived.days, ['2026-10-16', '2026-10-17'])
  const current = await (await worker.fetch(new Request('https://example.com/api/schedule?k=private&d=2026-10-17'), env)).json()
  assert.equal(current.archived, false)
  assert.equal((await worker.fetch(new Request('https://example.com/api/schedule?k=private&d=2026-10-01'), env)).status, 404)
  assert.equal((await worker.fetch(new Request('https://example.com/api/schedule?k=private&d=x'), env)).status, 400)
})

test('all-time net includes a cashed-out archive and a day still riding', async () => {
  const oldDate = '2026-10-16'
  const oldConfig = { ...config, date: oldDate, depositFee: '1', cashouts: { '1': { amount: '12', remainingFraction: 0 } } }
  const currentConfig = { ...config, slips: [{ ...config.slips[0], stake: '20', payout: '40' }] }
  const reads = [], writes = []
  const values = {
    'config:current': currentConfig, days: [oldDate],
    [`day:${oldDate}`]: { config: oldConfig, snapshot: { date: oldDate, markets: {} } },
    'snapshot:current': { date: config.date, markets: {} }
  }
  const env = { ACCESS_KEY: 'private', SCHEDULE: {
    async get(key) { reads.push(key); return values[key] ?? null },
    async put(...args) { writes.push(args) }
  } }
  const worker = makeWorker('')
  const current = await (await worker.fetch(new Request('https://example.com/api/schedule?k=private'), env)).json()
  const document = await renderPage(current)
  const record = document.getElementById('track-record').innerHTML
  assert.match(record, /All-time net \+\$1\.00.*2 days tracked/)
  assert.match(record, /2026-10-16.*\+\$1\.00.*2026-10-17.*\+\$0\.00/)
  assert.match(document.getElementById('profit-tracker').innerHTML, /Still riding<\/span><strong>\$20\.00/)
  const archive = await (await worker.fetch(new Request(`https://example.com/api/schedule?k=private&d=${oldDate}`), env)).json()
  assert.match((await renderPage(archive)).getElementById('track-record').innerHTML, /All-time net \+\$1\.00/)
  assert.ok(reads.includes(`day:${oldDate}`))
  assert.deepEqual(writes, [])
})

test('daily results and tracker cards keep the demo order', async () => {
  const document = await renderPage({ config, markets: {}, stale: true })
  const record = document.getElementById('track-record').innerHTML
  const tracker = document.getElementById('profit-tracker').innerHTML
  const summary = document.getElementById('summary').innerHTML
  assert.match(record, /<summary>Daily Results<\/summary>/)
  assert.deepEqual([...tracker.matchAll(/<span>(Profit now|Still riding|Max profit)<\/span>/g)].map(match => match[1]), ['Profit now', 'Still riding', 'Max profit'])
  assert.deepEqual([...summary.matchAll(/<span>(Staked|Payout locked in|Max still possible)<\/span>/g)].map(match => match[1]), ['Staked', 'Payout locked in', 'Max still possible'])
  assert.match(tracker, /Still riding<\/span><strong>.*?<\/strong><small class="detail">Max payout /)
})

test('private pricing shows a dash for missing sportsbook prices', async () => {
  const html = readFileSync(new URL('./page.html', import.meta.url), 'utf8')
  const legs = [
    { name:'Quoted', ticker:'Q', side:'yes', start:'2026-10-17T17:00:00Z', fair:0.75, fairSource:'Pinnacle no-vig', bovadaOdds:'+150', bovadaImplied:0.4, pinnacleRaw:0.8 },
    { name:'Unpriced', ticker:'U', side:'no', start:'2026-10-17T17:00:00Z' }
  ]
  const slips = [{ name:'1', stake:'10', payout:'20', legs:['Quoted'] }]
  const document = await renderPage({ config:{ date:config.date, legs, slips }, markets:{ Q:{ yes_ask_dollars:'0.70' }, U:{ no_ask_dollars:'0.40' } }, stale:false })
  const rows = document.getElementById('pricing').innerHTML
  assert.match(rows, /Quoted.*70\.0¢.*1\.5¢/)
  assert.match(rows, /Unpriced.*40\.0¢/)
  const unpriced = rows.match(/<tr class="pricing-row"><td class="outcome"><span class="leg-line"><span class="sport-tile"[^>]*>🏆<\/span>Unpriced[\s\S]*?<\/tr>/)[0]
  assert.equal(unpriced.match(/<span class="figure muted">—<\/span>/g).length, 3)
  assert.doesNotMatch(rows, /Not available|undefined|N\/A/)
  assert.match(html, /Kalshi ask<\/th><th scope="col">Sportsbook<\/th><th scope="col">Fair, no vig<\/th><th scope="col">Fee<\/th><th scope="col">Edge after fee/)
  assert.match(html, /--bg: #091321; --panel: #112136; --panel2: #172a43/)
  assert.match(html, /font: 15px\/1\.55 Inter/)
  assert.match(html, /thead th \{[^}]*text-transform: uppercase; letter-spacing: \.07em/)
  assert.match(html, /<section class="questions" id="common-questions"><h2>Common Questions/)
})

test('private pricing keeps all saved sportsbook columns', async () => {
  const leg = { name:'Quoted', ticker:'Q', side:'yes', start:'2026-10-17T17:00:00Z', fair:0.75, fairSource:'Pinnacle no-vig', bovadaOdds:'+150', bovadaImplied:0.4, pinnacleRaw:0.8 }
  const document = await renderPage({ config:{ date:config.date, legs:[leg], slips:[{ name:'1', stake:'10', payout:'20', legs:['Quoted'] }] }, markets:{ Q:{ yes_ask_dollars:'0.70' } }, stale:false })
  assert.match(document.getElementById('pricing').innerHTML, /Quoted.*70\.0¢.*<span class="book-tag ">Bovada<\/span><span class="figure">40\.0%<\/span><small class="source">\+150<\/small>.*<span class="book-tag pin">Pinnacle<\/span><span class="figure">75\.0%.*1\.5¢.*\+4\.7%/)
  assert.doesNotMatch(document.getElementById('pricing').innerHTML, /—/)
  const drafted = await renderPage({ config:{ date:config.date, legs:[{ ...leg, book:'DraftKings', bookOdds:'-340', bookChance:.773 }], slips:[{ name:'1', stake:'10', payout:'20', legs:['Quoted'] }] }, markets:{ Q:{ yes_ask_dollars:'0.70' } }, stale:false })
  assert.match(drafted.getElementById('pricing').innerHTML, /<span class="book-tag dk">DraftKings<\/span><span class="figure">77\.3%<\/span><small class="source">-340<\/small>/)
})

test('private page says when prices paused because the poller slept', async () => {
  const now = Date.parse('2026-10-17T03:00:00Z')
  const document = await renderPage({ config:{ date:config.date,
    legs:[{ name:'Later', ticker:'L', side:'yes', start:'2026-10-17T05:00:00Z' }],
    slips:[{ name:'1', stake:'10', payout:'20', legs:['Later'] }] },
    markets:{ L:{ yes_ask_dollars:'0.70', fetchedAt:now - 30 * 60 * 1000 } }, stale:true }, { now })
  assert.match(document.getElementById('updated').textContent, /^Prices paused since .+ PT, 30 minutes ago\. The Mac poller may be asleep\.$/)
})

test('finished private day shows the settled result by the header', async () => {
  const document = await renderPage({ config:{ date:config.date,
    legs:[{ name:'Winner', ticker:'W', side:'yes', start:'2026-10-17T17:00:00Z' }],
    slips:[{ name:'1', stake:'10', payout:'20', legs:['Winner'] }] },
    markets:{ W:{ result:'yes' } }, stale:false })
  assert.equal(document.getElementById('day-chance-timeline').innerHTML, 'Day Won · +$10.00')
  assert.match(document.getElementById('slips').innerHTML, /<tr class="total-row"><th scope="row">Total<\/th><td data-label="Stake">\$10\.00<\/td>.*<td data-label="Status">Day Won<\/td><\/tr>$/)
})

test('a futures slip does not hold the private day open', async () => {
  const document = await renderPage({ config:{ date:config.date,
    legs:[{ name:'Winner', ticker:'W', side:'yes', start:'2026-10-17T17:00:00Z' }, { name:'Champion', ticker:'C', side:'yes', start:'2027-02-14T23:30:00Z' }],
    slips:[{ name:'1', stake:'10', payout:'20', legs:['Winner'] }, { name:'2', stake:'10', payout:'60', legs:['Champion'] }] },
    markets:{ W:{ result:'yes' }, C:{ yes_bid_dollars:'0.16', yes_ask_dollars:'0.17' } }, stale:false })
  assert.equal(document.getElementById('day-chance-timeline').innerHTML, 'Day Won · +$10.00 · 1 Future Open')
})

test('private sorts full cash-outs before dead slips', async () => {
  const legs = [{ name:'Lost', ticker:'L', side:'yes', start:'2026-10-17T17:00:00Z' }]
  const slips = [
    { name:'1', stake:'10', payout:'20', legs:['Lost'] },
    { name:'2', stake:'10', payout:'20', legs:['Lost'] }
  ]
  const cashouts = { '2':{ amount:'5', remainingFraction:0 } }
  const document = await renderPage({ config:{ date:config.date, legs, slips, cashouts }, markets:{ L:{ result:'no' } }, stale:false })
  const rows = document.getElementById('slips').innerHTML
  assert.ok(rows.indexOf('id="slip-2"') < rows.indexOf('id="slip-1"'))
})

test('slips show placed and settled times, day tabs and lists', async () => {
  const legs = ['Won', 'Other', 'Lost'].map((name, index) => ({ name, ticker: `T-${index}`, side: 'yes', start: '2026-10-17T17:00:00Z' }))
  const slips = [
    { name: 'won', stake: '10', payout: '20', legs: ['Won', 'Other'], placed: '2026-10-17T13:40:00Z' },
    { name: 'dead', stake: '10', payout: '20', legs: ['Won', 'Lost'] }
  ]
  const markets = {
    'T-0': { result: 'yes', settlement_ts: '2026-10-18T01:45:00Z' },
    'T-1': { result: 'yes', close_time: '2026-10-18T03:10:00Z' },
    'T-2': { result: 'no', settlement_ts: '2026-10-17T20:05:00Z' }
  }
  const lists = { ban: ['Team B'], doghouse: ['Azure'], hateWatch: [] }
  const document = await renderPage({ config: { date: config.date, legs, slips, lists }, markets, stale: false, archived: true, days: ['2026-10-17', '2026-10-18'] })
  const rows = document.getElementById('slips').innerHTML
  assert.match(rows, /id="slip-won".*?Placed Oct 17, 6:40 AM · Settled Oct 17, 8:10 PM/)
  assert.match(rows, /id="slip-dead".*?Settled Oct 17, 1:05 PM/)
  assert.doesNotMatch(rows, /id="slip-dead".*?Placed/)
  assert.match(document.getElementById('days').innerHTML, /data-day="2026-10-17" aria-pressed="true">Sat, Oct 17.*?data-day="2026-10-18" aria-pressed="false">Sun, Oct 18/)
  assert.equal(document.getElementById('updated').textContent, 'Pacific time. Archived day, final results.')
  assert.equal(document.getElementById('lists-section').hidden, false)
  const shown = document.getElementById('lists').innerHTML
  assert.match(shown, /Ban list.*?Team B.*?The Doghouse.*?Azure/)
  assert.doesNotMatch(shown, /Hate Watch/)
})

test('lists come from their own key and fully settled days are never stale', async () => {
  const settledConfig = { ...config, legs: [config.legs[0]] }
  const store = kv({ 'config:current': settledConfig, lists: { ban: ['Team B'] }, 'snapshot:current': {
    date: config.date, markets: { [config.legs[0].ticker]: { result: 'yes', fetchedAt: 1 } } } })
  const worker = makeWorker('', { now: () => 10 ** 12 })
  const payload = await (await worker.fetch(new Request('https://example.com/api/schedule?k=private'), { ACCESS_KEY: 'private', SCHEDULE: store })).json()
  assert.equal(payload.stale, false)
  assert.deepEqual(payload.lists, { ban: ['Team B'] })
  const document = await renderPage({ ...payload, config: { ...settledConfig, slips: [] } }, { now: 10 ** 12 })
  assert.equal(document.getElementById('updated').textContent, 'Pacific time. Every leg has settled.')
  assert.match(document.getElementById('lists').innerHTML, /Ban list.*?Team B/)
})
