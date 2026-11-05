import assert from 'node:assert/strict'
import test from 'node:test'
import { fakeD1 } from './fake-d1.js'
import { d1Store, mergeSnapshot, run } from './poller-core.js'

const config = { date: '2026-11-04', legs: [
  { name: 'Open', ticker: 'OPEN', side: 'yes' },
  { name: 'Final', ticker: 'FINAL', side: 'no' }
] }
const open = { ticker: 'OPEN', status: 'active', yes_bid_dollars: '0.40', yes_ask_dollars: '0.42', no_bid_dollars: '0.58', no_ask_dollars: '0.60', title: 'dropped', rules_primary: 'dropped' }
const final = { ticker: 'FINAL', status: 'finalized', result: 'no', settlement_ts: '2026-11-04T05:00:00Z', no_ask_dollars: '1.0000' }
const reply = (markets, ok = true, status = 200) => ({ ok, status, json: async () => ({ markets }) })

test('merging keeps only what the page reads and records first prices once', () => {
  const merged = mergeSnapshot(config, null, { OPEN: open }, 1000)
  assert.deepEqual(Object.keys(merged.markets.OPEN).sort(), ['fetchedAt', 'no_ask_dollars', 'no_bid_dollars', 'status', 'ticker', 'yes_ask_dollars', 'yes_bid_dollars'])
  assert.equal(merged.firstPrices.Open.toFixed(2), '0.41')
  const later = mergeSnapshot(config, merged, { OPEN: { ...open, yes_bid_dollars: '0.90', yes_ask_dollars: '0.92' }, FINAL: final }, 2000)
  assert.equal(later.firstPrices.Open.toFixed(2), '0.41')
  assert.equal(later.firstPrices.Final, undefined)
  assert.equal(later.markets.FINAL.result, 'no')
  assert.equal(mergeSnapshot({ ...config, date: '2026-11-05' }, later, {}, 3000).markets.OPEN, undefined)
})

test('a run checks six times 10 seconds apart and writes each result', async () => {
  const DB = fakeD1()
  await d1Store(DB).put('config:current', config, 1)
  DB.writes.length = 0
  const urls = []
  let clock = 0
  const waits = []
  await run({ DB }, {
    now: () => clock,
    wait: async ms => { waits.push(ms); clock += ms },
    request: async url => { urls.push(url); return reply([open, final]) }
  })
  assert.equal(urls.length, 6)
  assert.deepEqual(waits, [10000, 10000, 10000, 10000, 10000])
  assert.equal(DB.writes.length, 6)
  assert.match(urls[0], /tickers=OPEN%2CFINAL/)
  assert.match(urls[1], /tickers=OPEN&/)
  const snapshot = await d1Store(DB).get('snapshot:current')
  assert.equal(snapshot.markets.FINAL.fetchedAt, 50000)
  assert.equal(snapshot.markets.OPEN.title, undefined)
})

test('legs far from starting are checked once a minute, live legs every check', async () => {
  const DB = fakeD1()
  const store = d1Store(DB)
  const legs = [{ name: 'Live', ticker: 'LIVE', side: 'yes', start: '2026-11-04T01:00:00Z' }, { name: 'Later', ticker: 'LATER', side: 'yes', start: '2026-11-04T09:00:00Z' }]
  await store.put('config:current', { date: config.date, legs }, 1)
  const urls = []
  let clock = Date.parse('2026-11-04T02:00:00Z')
  await run({ DB }, {
    now: () => clock,
    wait: async ms => { clock += ms },
    request: async url => { urls.push(url); return reply([{ ...open, ticker: 'LIVE' }, { ...open, ticker: 'LATER' }]) }
  })
  assert.equal(urls.filter(url => url.includes('LATER')).length, 1)
  assert.equal(urls.filter(url => url.includes('LIVE')).length, 6)
})

test('a run stops when every leg is final and reads nothing from Kalshi', async () => {
  const DB = fakeD1()
  const store = d1Store(DB)
  await store.put('config:current', { ...config, legs: [config.legs[1]] }, 1)
  await store.put('snapshot:current', { date: config.date, markets: { FINAL: final } }, 1)
  DB.writes.length = 0
  await run({ DB }, { request: async () => { throw new Error('should not fetch') } })
  assert.equal(DB.writes.length, 0)
})

test('a Kalshi failure is saved once and cleared by the next good check', async () => {
  const DB = fakeD1()
  const store = d1Store(DB)
  await store.put('config:current', config, 1)
  let call = 0
  const seen = []
  await run({ DB }, {
    now: () => 5,
    wait: async () => { seen.push(await store.get('poll:error')) },
    request: async () => ++call <= 2 ? reply([], false, 429) : reply([open, final])
  })
  assert.equal(seen[0].message, 'Kalshi returned 429')
  assert.equal(seen[1].message, 'Kalshi returned 429')
  assert.equal(seen[2], null)
  assert.equal(await store.get('poll:error'), null)
})

test('a run without a schedule does nothing', async () => {
  const DB = fakeD1()
  await run({ DB }, { request: async () => { throw new Error('should not fetch') } })
  assert.equal(DB.writes.length, 0)
})
