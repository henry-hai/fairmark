import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import test from 'node:test'

execFileSync(process.execPath, [new URL('./build.mjs', import.meta.url).pathname])
const { default: pageWorker } = await import('./public/_worker.js')

test('Pages build uses the current private page', async () => {
  const html = readFileSync(new URL('../page.html', import.meta.url), 'utf8')
  const response = await pageWorker.fetch(new Request('https://example.com/?k=private'), { ACCESS_KEY: 'private' })
  assert.equal(response.status, 200)
  assert.equal(await response.text(), html)
})

test('Pages build refuses missing and wrong keys before KV reads', async () => {
  let reads = 0
  const env = { ACCESS_KEY: 'private', SCHEDULE: { async get() { reads++; return null } } }
  for (const path of ['/', '/api/schedule']) {
    for (const query of ['', '?k=wrong']) {
      const response = await pageWorker.fetch(new Request(`https://example.com${path}${query}`), env)
      assert.equal(response.status, 404)
      assert.equal(await response.text(), 'Not found')
    }
  }
  assert.equal(reads, 0)
})
