import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { runInNewContext } from 'node:vm'
import { onRequest } from './functions/api/engine.js'

const sample = {
  schema:1, date:'2026-10-21', asOf:'2026-10-21T12:00:00Z', depositFee:2, lists:['private'],
  legs:[
    { id:'KXNFLREC-ONE', name:'Alice to win', event:'Alice vs Bob', start:'2026-10-22T22:00:00Z', ask:.7, fair:.75, fee:.0147, underFair:.047, fairSource:'Derived from Pinnacle prop line', bovadaOdds:'+150', bovadaImplied:.4, pinnacleRaw:.8, secret:'private' },
    { id:'KXUFCFIGHT-TWO', name:'Carol to win', event:'Carol vs Dan', start:'2026-10-23T22:00:00Z', ask:.8, fair:.84, fee:.0112, underFair:.034, fairSource:'Pinnacle no-vig' },
    { id:'UNUSED', name:'Unused', event:'Unused', start:'2026-10-23T22:00:00Z', ask:.5, fair:.5, fee:.0175, underFair:0, fairSource:'Pinnacle no-vig' }
  ],
  slips:[{ number:1, legIds:['KXNFLREC-ONE','KXUFCFIGHT-TWO'], stake:100, placed:'2026-10-21T12:00:00Z', hitChance:.63, fee:.0173, totalPayout:173.2, gain:73.2, fairPayout:158.73, underFair:.08, private:'private' }],
  portfolio:{ stake:100, allHitPayout:173.2, allHitGain:73.2, allHitFairPayout:158.73, allHitFairGain:58.73, dayProfitChance:.63, medianGain:73.2, trials:10000 }
}
const snapshot = { date:sample.date, markets:{ 'KXNFLREC-ONE':{ yes_bid_dollars:'0.70', yes_ask_dollars:'0.72', fetchedAt:1792600000000 }, 'KXUFCFIGHT-TWO':{ yes_bid_dollars:'0.80', yes_ask_dollars:'0.82', fetchedAt:1792600000000 } } }
const makeEnv = (values, reads) => ({ SCHEDULE:{ async get(key, type) { reads.push(key); assert.equal(type, 'json'); return values[key] ?? null } } })

test('queries and paths read only demo keys and project no personal fields or unused legs', async () => {
  const reads = [], values = { 'config:demo':sample, 'snapshot:demo':snapshot, 'days:demo':[] }
  for (const path of ['/', '/api/engine', '/api/schedule', '/config:current']) for (const query of ['', '?k=secret&depositFee=2', '?path=../../snapshot:current']) {
    const result = await onRequest({ request:new Request(`https://example.com${path}${query}`), env:makeEnv(values, reads) })
    assert.equal(result.status, 200)
    const body = await result.json()
    assert.equal(body.legs.length, 2)
    assert.equal(body.legs[0].bovadaOdds, '+150')
    assert.equal(body.legs[0].bovadaImplied, .4)
    assert.equal(body.legs[0].pinnacleRaw, .8)
    assert.equal(body.slips[0].placed, sample.slips[0].placed)
    for (const forbidden of ['depositFee','lists','private','UNUSED','config:current','snapshot:current']) assert.ok(!JSON.stringify(body).includes(forbidden), forbidden)
  }
  assert.ok(reads.every(key => ['config:demo','snapshot:demo','days:demo'].includes(key)))
  assert.equal(reads.length, 36)
})

test('archive reads only day:demo and rejects private or arbitrary keys', async () => {
  const reads = [], old = structuredClone(sample); old.date = '2026-10-20'
  const values = { 'config:demo':sample, 'days:demo':['2026-10-20'], 'day:demo:2026-10-20':{ config:old, snapshot } }
  const result = await onRequest({ request:new Request('https://example.com/api/engine?d=2026-10-20'), env:makeEnv(values, reads) })
  assert.equal(result.status, 200)
  assert.equal((await result.json()).archived, true)
  assert.deepEqual(reads.sort(), ['config:demo','day:demo:2026-10-20','days:demo','snapshot:demo'].sort())
  for (const query of ['?d=../../config:current','?d=2026-10-19','?d=days']) {
    const response = await onRequest({ request:new Request(`https://example.com/api/engine${query}`), env:makeEnv(values, reads) })
    assert.ok(response.status === 400 || response.status === 404)
  }
  assert.ok(reads.every(key => ['config:demo','days:demo','day:demo:2026-10-20','snapshot:demo'].includes(key)))
})

test('modeled track record sums cashed-out and riding demo days without private reads', async () => {
  const old = structuredClone(sample)
  old.date = '2026-10-20'
  old.cashouts = { '1': { amount:120, remainingFraction:0 } }
  const reads = []
  const values = {
    'config:demo':sample, 'snapshot:demo':snapshot, 'days:demo':['2026-10-20'],
    'day:demo:2026-10-20':{ config:old, snapshot:{ date:old.date, markets:{} } },
    'config:current':{ secret:'private' }, 'day:2026-10-20':{ secret:'private' }
  }
  const body = await (await onRequest({ request:new Request('https://example.com/api/engine'), env:makeEnv(values, reads) })).json()
  assert.deepEqual(body.history.map(item => item.date), ['2026-10-21','2026-10-20'])
  assert.equal(body.history[0].slips[0].cashout, undefined)
  assert.equal(body.history[1].slips[0].cashout.amount, 120)
  assert.ok(reads.every(key => ['config:demo','snapshot:demo','days:demo','day:demo:2026-10-20'].includes(key)))
  assert.ok(!JSON.stringify(body).includes('private'))
  const { get } = page(body)
  await new Promise(resolve => setImmediate(resolve))
  const rendered = get('content').innerHTML
  assert.match(rendered, /Net profit, all days.*\+\$20\.00.*2 days/)
  assert.match(rendered, /2026-10-20.*\+\$20\.00/)
  assert.match(rendered, /Still riding.*\$100\.00/)
})

test('non-GET methods never read KV', async () => {
  for (const method of ['POST','PUT','DELETE']) assert.equal((await onRequest({ request:new Request('https://example.com/api/engine', { method }), env:{ SCHEDULE:{ get() { throw Error('KV read') } } } })).status, 405)
})

test('legacy demo config keeps serving while the poller moves to dated keys', async () => {
  const old = structuredClone(sample)
  delete old.date
  const reads = []
  const result = await onRequest({ request:new Request('https://example.com/api/engine'),
    env:makeEnv({ 'config:demo':old, 'days:demo':[] }, reads) })
  assert.equal(result.status, 200)
  assert.equal((await result.json()).date, '2026-10-21')
  assert.ok(reads.every(key => ['config:demo','days:demo','snapshot:demo'].includes(key)))
})

test('demo snapshot publishes only slip leg quotes and final results', async () => {
  const reads = []
  const finished = structuredClone(snapshot)
  finished.markets['KXNFLREC-ONE'] = { ...finished.markets['KXNFLREC-ONE'], result:'yes', settlement_ts:'2026-10-22T23:00:00Z', secret:'private' }
  finished.markets['KXUFCFIGHT-TWO'] = { ...finished.markets['KXUFCFIGHT-TWO'], result:'no', settlement_ts:'2026-10-23T23:00:00Z' }
  finished.markets.UNUSED = { result:'yes' }
  const result = await onRequest({ request:new Request('https://example.com/api/engine'),
    env:makeEnv({ 'config:demo':sample, 'days:demo':[], 'snapshot:demo':finished }, reads) })
  const body = await result.json()
  assert.equal(body.markets['KXNFLREC-ONE'].result, 'yes')
  assert.equal(body.markets['KXUFCFIGHT-TWO'].result, 'no')
  assert.ok(!JSON.stringify(body).includes('UNUSED'))
  assert.ok(!JSON.stringify(body).includes('private'))
})

function page(data = { ...sample, markets:snapshot.markets, days:[sample.date], archived:false }, storage = { getItem() { return null }, setItem() {} }) {
  const html = readFileSync(new URL('./public/index.html', import.meta.url), 'utf8')
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  const listeners = {}, elements = new Map()
  const get = id => {
    if (!elements.has(id)) elements.set(id, { textContent:'', innerHTML:'', addEventListener(type, fn) { listeners[type] = fn } })
    return elements.get(id)
  }
  const document = { getElementById:get, documentElement:{ getAttribute() { return null }, setAttribute() {} } }
  const context = { document, localStorage:storage, Intl, Date, fetch:async () => ({ ok:true, json:async () => data }), setInterval() {}, CSS:{ escape:id => id } }
  runInNewContext(script + '\n globalThis.rerender = render', context)
  return { html, get, listeners, rerender:() => context.rerender(data) }
}

test('finished timeline rows fade and dates stay on one line', async () => {
  const data = { ...sample, markets:{
    'KXNFLREC-ONE':{ result:'yes' }, 'KXUFCFIGHT-TWO':{ result:'no' }
  }, days:[sample.date], archived:true }
  const { html, get } = page(data)
  await new Promise(resolve => setImmediate(resolve))
  const rendered = get('content').innerHTML
  assert.match(html, /\.timeline-item\.won-leg,\.timeline-item\.dead-leg \{ opacity:\.55 \}/)
  assert.match(html, /\.timeline-item time \{[^}]*white-space:nowrap/)
  assert.match(rendered, /class="timeline-item won-leg"><time>10\/22\/26 · 3:00 PM<\/time>/)
  assert.match(rendered, /class="timeline-item dead-leg"><time>10\/23\/26 · 3:00 PM<\/time>/)
})

test('zero demo profit has no positive class', async () => {
  const data = structuredClone(sample)
  data.slips[0].totalPayout = data.slips[0].stake
  data.markets = { 'KXNFLREC-ONE':{ result:'yes' }, 'KXUFCFIGHT-TWO':{ result:'yes' } }
  data.days = [data.date]
  data.archived = true
  const { get } = page(data)
  await new Promise(resolve => setImmediate(resolve))
  const rendered = get('content').innerHTML
  assert.match(rendered, /Profit now<\/span><strong class="">\+\$0\.00/)
  assert.match(rendered, /Net profit, all days <span class="">\+\$0\.00/)
  assert.match(rendered, /class="profit-value ">\+\$0\.00/)
})

test('dead demo slips show losing legs and keep their own expanded state', async () => {
  const data = structuredClone(sample)
  data.slips.push({ ...data.slips[0], number:2, legIds:['KXUFCFIGHT-TWO'] })
  data.markets = { 'KXNFLREC-ONE':{ result:'yes' }, 'KXUFCFIGHT-TWO':{ result:'no' } }
  data.days = [data.date]
  data.archived = true
  const values = new Map()
  const storage = { getItem:key => values.get(key) ?? null, setItem:(key, value) => values.set(key, value) }
  const { html, get, listeners, rerender } = page(data, storage)
  await new Promise(resolve => setImmediate(resolve))
  let rendered = get('content').innerHTML
  assert.match(html, /\.leg-chip\.lost \.leg-name \{ text-decoration:line-through \}/)
  assert.match(rendered, /id="slip-1".*?<details data-expand-key="slip:1"><summary>#1 <span class="badge dead">dead<\/span><\/summary>.*?leg-chip lost killer.*?Killed slip/)
  listeners.toggle({ target:{ matches:() => true, dataset:{ expandKey:'slip:1' }, open:true } })
  rerender()
  rendered = get('content').innerHTML
  assert.match(rendered, /data-expand-key="slip:1" open/)
  assert.doesNotMatch(rendered, /data-expand-key="slip:2" open/)
  const reloaded = page(data, storage)
  await new Promise(resolve => setImmediate(resolve))
  assert.match(reloaded.get('content').innerHTML, /data-expand-key="slip:1" open/)
})

test('archived net and daily list exclude later days', async () => {
  const entry = (date, amount) => ({ ...structuredClone(sample), date, slips:[{ ...sample.slips[0], cashout:{ amount, remainingFraction:0 } }], markets:{} })
  const data = { ...entry('2026-10-20', 120), archived:true, days:['2026-10-19','2026-10-20','2026-10-21'], history:[entry('2026-10-21', 130), entry('2026-10-19', 110), entry('2026-10-20', 120)] }
  const { get } = page(data)
  await new Promise(resolve => setImmediate(resolve))
  const record = get('content').innerHTML.match(/<div class="track-record">[\s\S]*?<\/details><\/div>/)?.[0]
  assert.match(record, /Net profit, all days.*\+\$30\.00.*2 days/)
  assert.match(record, /2026-10-19.*2026-10-20/)
  assert.doesNotMatch(record, /2026-10-21/)
})

test('page renders summary, timeline, slips, engine, checker and public palette', async () => {
  const { html, get } = page(); await new Promise(resolve => setImmediate(resolve))
  const rendered = get('content').innerHTML
  const pricing = rendered.match(/<section aria-labelledby="pricing-title">[\s\S]*?<\/section>/)?.[0]
  assert.match(pricing, /Kalshi ask<\/th><th>Sportsbook<\/th><th>Pinnacle no-vig fair<\/th><th>Fee<\/th><th>Under fair/)
  assert.match(pricing, /data-label="Sportsbook"><span class="muted">—<\/span>/)
  assert.doesNotMatch(pricing, /Not available|undefined|N\/A|raw implied/)
  for (const expected of ['Profit now','Still riding','Max profit','Staked','Payout locked in','Max still possible','Timeline','Slips','Cashed Out','Placed','Price vs. Fair','Common Questions','100,000 trials']) assert.ok(rendered.includes(expected), expected)
  assert.equal(rendered.match(/<span class="chance-pill "><span class="status-dot live"><\/span>Live · Day profit chance [^<]+<\/span>/g)?.length, 2)
  assert.ok(!rendered.includes('Live model ·'))
  assert.ok(!rendered.includes('Unused') && !rendered.includes('Lists'))
  for (const token of ['#091321','#5ff5c8','#ff7b8a','30%']) assert.ok(html.includes(token))
})

test('demo pricing keeps sportsbook columns when every leg has saved prices', async () => {
  const data = structuredClone(sample)
  Object.assign(data.legs[1], { bovadaOdds:'-110', bovadaImplied:110 / 210, pinnacleRaw:.86 })
  data.markets = snapshot.markets
  data.days = [data.date]
  const { get } = page(data)
  await new Promise(resolve => setImmediate(resolve))
  const pricing = get('content').innerHTML.match(/<section aria-labelledby="pricing-title">[\s\S]*?<\/section>/)?.[0]
  assert.match(pricing, /\+150<small class="source">40\.0% implied<\/small>.*75\.0%<small class="source">From prop line<\/small>/)
  assert.doesNotMatch(pricing.match(/<tr class="pricing-row[^>]*>[\s\S]*?<\/tr>/g).find(row => row.includes('+150')), /N\/A|Not available/)
})

test('demo cards use two full rows and archived filters keep live and upcoming', async () => {
  const data = { ...structuredClone(sample), markets: snapshot.markets, days:[sample.date], archived:true }
  const { html, get } = page(data)
  await new Promise(resolve => setImmediate(resolve))
  const rendered = get('content').innerHTML
  const summary = rendered.match(/<section class="metrics" aria-label="Day summary">([\s\S]*?)<\/section>/)?.[1]
  assert.deepEqual([...summary.matchAll(/<span class="label">([^<]+)<\/span>/g)].map(match => match[1]), ['Profit now','Still riding','Max profit','Staked','Payout locked in','Max still possible'])
  assert.doesNotMatch(rendered, /aria-label="Day chance"/)
  for (const status of ['live','upcoming','won','dead']) assert.match(rendered, new RegExp(`data-group="timeline" data-filter="${status}"`))
  assert.match(html, /\.metrics \{ display:grid; grid-template-columns:repeat\(3,minmax\(0,1fr\)\)/)
  assert.match(html, /\.chance-pill \{ border:1px solid/)
  assert.match(rendered, /<section class="questions" id="common-questions"><h2>Common Questions/)
})

test('finished demo day shows final result instead of modeled morning chance', async () => {
  const data = { ...structuredClone(sample), archived:true, markets:{
    'KXNFLREC-ONE':{ result:'yes', settlement_ts:'2026-10-22T23:00:00Z' },
    'KXUFCFIGHT-TWO':{ result:'no', settlement_ts:'2026-10-23T23:00:00Z' }
  }, days:[sample.date] }
  const { get } = page(data)
  await new Promise(resolve => setImmediate(resolve))
  const pills = get('content').innerHTML.match(/<span class="chance-pill[^"]*">[^<]*<\/span>/g)
  assert.deepEqual(pills, Array(2).fill('<span class="chance-pill negative">Day lost · −$100.00</span>'))
})

test('demo sorts full cash-outs before dead slips', async () => {
  const data = structuredClone(sample)
  data.slips = [
    { ...data.slips[0], number:1, legIds:['KXNFLREC-ONE'] },
    { ...data.slips[0], number:2, legIds:['KXUFCFIGHT-TWO'], cashout:{ amount:20, remainingFraction:0 } }
  ]
  data.markets = { 'KXNFLREC-ONE':{ result:'no' }, 'KXUFCFIGHT-TWO':{ result:'no' } }
  data.days = [data.date]
  data.archived = true
  const { get } = page(data)
  await new Promise(resolve => setImmediate(resolve))
  const rendered = get('content').innerHTML
  assert.ok(rendered.indexOf('id="slip-2"') < rendered.indexOf('id="slip-1"'))
})

test('theme follows system and tolerates blocked storage', () => {
  const html = readFileSync(new URL('./public/index.html', import.meta.url), 'utf8')
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  const attributes = new Map(); let onClick
  const button = { textContent:'', addEventListener(type, fn) { if (type === 'click') onClick = fn } }
  const other = { addEventListener() {} }
  const document = { documentElement:{ setAttribute(key,value) { attributes.set(key,value) }, getAttribute(key) { return attributes.get(key) } }, getElementById(id) { return id === 'theme-toggle' ? button : other } }
  runInNewContext(script, { document, localStorage:{ getItem() { throw Error('blocked') }, setItem() { throw Error('blocked') } }, matchMedia:() => ({ matches:true }), Intl, Date, setInterval() {}, fetch:() => new Promise(() => {}) })
  assert.equal(button.textContent, 'Light mode'); onClick(); assert.equal(attributes.get('data-theme'), 'light')
})
