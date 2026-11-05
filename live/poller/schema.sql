-- One row per key, the same keys Workers KV used. WITHOUT ROWID keeps each write to a single
-- billed row, since a normal table also writes its primary key index.
CREATE TABLE IF NOT EXISTS store (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
) WITHOUT ROWID;
