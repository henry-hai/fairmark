import { readFileSync, writeFileSync } from 'node:fs'

const core = readFileSync(new URL('../worker-core.js', import.meta.url), 'utf8')
const page = readFileSync(new URL('../page.html', import.meta.url), 'utf8')
const exported = 'export function makeWorker('
if (core.split(exported).length !== 2) throw new Error('Unexpected worker-core.js export')

const bundle = `${core.replace(exported, 'function makeWorker(')}\nexport default makeWorker(${JSON.stringify(page)})\n`
writeFileSync(new URL('./public/_worker.js', import.meta.url), bundle)
