import { readFileSync } from 'node:fs'
import { DatabaseSync } from 'node:sqlite'

// A small stand-in for the D1 binding, running the real schema and SQL on SQLite.
export function fakeD1() {
  const sqlite = new DatabaseSync(':memory:')
  sqlite.exec(readFileSync(new URL('./schema.sql', import.meta.url), 'utf8'))
  const writes = []
  const statement = (sql, values = []) => ({
    bind: (...bound) => statement(sql, bound),
    first: async () => sqlite.prepare(sql).get(...values) ?? null,
    all: async () => ({ results: sqlite.prepare(sql).all(...values) }),
    run: async () => { writes.push(values[0]); return sqlite.prepare(sql).run(...values) }
  })
  return { prepare: sql => statement(sql), writes }
}
