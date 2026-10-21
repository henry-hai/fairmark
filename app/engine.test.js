import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { runInNewContext } from 'node:vm'
import { onRequest } from './functions/api/engine.js'

const sample = {
  schema: 1, asOf: '2026-10-21T12:00:00+00:00', slateDate: '2026-10-22', slateEndDate: '2026-10-24',
  depositFee: 2, lists: ['private'], days: ['2026-10-20'],
  legs: [{ id: 'PUBLIC', name: 'Alice to win', event: 'Alice vs Bob',
    start: '2026-10-24T22:00:00+00:00', ask: 0.48, fair: 0.52,
    fee: 0.0175, underFair: 0.04, fairSource: 'Pinnacle no-vig', secret: 'private' }],
  slips: [{ number: 1, legIds: ['PUBLIC'], stake: 50, hitChance: 0.52,
    fee: 0.0175, totalPayout: 100, gain: 50, fairPayout: 96,
    underFair: 0.04, placed: 'private' }],
  portfolio: { stake: 50, allHitPayout: 100, allHitGain: 50,
    allHitFairPayout: 96, allHitFairGain: 46,
    dayProfitChance: 0.52, medianGain: 50, trials: 10000, seed: 17 }
}

test('every path and query can access only config:demo', async () => {
  const reads = []
  const env = { SCHEDULE: { async get(key, type) {
    reads.push([key, type])
    assert.equal(key, 'config:demo')
    return sample
  } } }
  const paths = ['/', '/api/engine', '/api/schedule', '/day:2026-10-20', '/config:current', '/snapshot:current', '/lists', '/days']
  const queries = ['', '?d=2026-10-20', '?key=config:current&list=days', '?k=secret&depositFee=2', '?path=../../snapshot:current']
  for (const path of paths) for (const query of queries) {
    const result = await onRequest({ request: new Request(`https://fairmark-app.pages.dev${path}${query}`), env })
    assert.equal(result.status, 200)
    const body = await result.text()
    for (const forbidden of ['depositFee', 'lists', 'days', 'placed', 'private', 'config:current', 'snapshot:current']) {
      assert.ok(!body.includes(forbidden), forbidden)
    }
  }
  assert.equal(reads.length, paths.length * queries.length)
  assert.ok(reads.every(([key, type]) => key === 'config:demo' && type === 'json'))
})

test('methods other than GET do not read KV', async () => {
  const env = { SCHEDULE: { get() { throw Error('unexpected KV read') } } }
  for (const method of ['POST', 'PUT', 'DELETE']) {
    const result = await onRequest({ request: new Request('https://example.com/api/engine?d=private', { method }), env })
    assert.equal(result.status, 405)
  }
})

test('page renders the engine and does not expose private data', async () => {
  const html = readFileSync(new URL('./public/index.html', import.meta.url), 'utf8')
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  const elements = new Map()
  const document = { getElementById(id) {
    if (!elements.has(id)) elements.set(id, { textContent: '', innerHTML: '' })
    return elements.get(id)
  } }
  runInNewContext(script, { document, Intl, Date, fetch: async () => ({ ok: true, json: async () => sample }) })
  await new Promise(resolve => setImmediate(resolve))
  const rendered = document.getElementById('content').innerHTML
  for (const expected of ['Day-profit odds', 'Kalshi ask', 'No-vig fair', 'Fee', 'Under fair', 'Hit chance', 'Total payout', 'Gain if hit', 'Pacific']) {
    assert.match(html + rendered + document.getElementById('slate-date').textContent, new RegExp(expected))
  }
  for (const forbidden of ['depositFee', 'Lists', 'Archived', 'placed at', '>demo<']) assert.ok(!rendered.includes(forbidden))
  assert.match(rendered, /\[52\.0%\]/)
  assert.match(html, /How it works/)
  assert.ok(!html.includes('class="mark"'))
})

test('every displayed leg carries its sport and the explainer has the bottom heading', async () => {
  const html = readFileSync(new URL('./public/index.html', import.meta.url), 'utf8')
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  const data = structuredClone(sample)
  data.legs = ['KXNFLREC', 'KXNCAAFGAME', 'KXUFCFIGHT'].map((series, index) => ({
    ...sample.legs[0], id: `${series}-EXAMPLE-${index}`, name: `Leg ${index + 1}`
  }))
  data.slips[0].legIds = data.legs.map(leg => leg.id)
  const elements = new Map()
  const document = { getElementById(id) {
    if (!elements.has(id)) elements.set(id, { textContent: '', innerHTML: '' })
    return elements.get(id)
  } }
  runInNewContext(script, { document, Intl, Date, fetch: async () => ({ ok: true, json: async () => data }) })
  await new Promise(resolve => setImmediate(resolve))
  const rendered = document.getElementById('content').innerHTML
  for (const [index, sport] of ['NFL', 'NCAAF', 'UFC'].entries()) {
    assert.equal(rendered.split(`Leg ${index + 1} (${sport})`).length - 1, 2)
  }
  assert.doesNotMatch(html.match(/<header>[\s\S]*?<\/header>/)[0], /How it works/)
  assert.match(rendered, /<h2 id="method-title">How it works<\/h2>/)
})

test('theme follows system, remembers choice, and tolerates blocked storage', () => {
  const html = readFileSync(new URL('./public/index.html', import.meta.url), 'utf8')
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  const attributes = new Map()
  let onClick
  const button = { textContent: '', addEventListener(type, callback) { if (type === 'click') onClick = callback } }
  const document = {
    documentElement: { setAttribute(key, value) { attributes.set(key, value) }, getAttribute(key) { return attributes.get(key) } },
    getElementById(id) { return id === 'theme-toggle' ? button : { textContent: '', innerHTML: '' } }
  }
  const storage = { getItem() { throw Error('blocked') }, setItem() { throw Error('blocked') } }
  runInNewContext(script, { document, localStorage: storage, matchMedia: () => ({ matches: true }),
    Intl, Date, fetch: () => new Promise(() => {}) })
  assert.equal(button.textContent, 'Light mode')
  onClick()
  assert.equal(attributes.get('data-theme'), 'light')
  assert.equal(button.textContent, 'Dark mode')
})
