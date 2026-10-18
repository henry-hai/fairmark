#!/usr/bin/env python3
"""Resolve a local slip file and upload its private schedule to Workers KV."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate_schedule(payload: Path) -> None:
    config = json.loads(payload.read_text())
    sources = {'ticker file', 'Pinnacle', 'Bovada', 'estimated'}
    if not config.get('legs') or any(not leg.get('start') or leg.get('startSource') not in sources
                                     for leg in config['legs']):
        raise ValueError('Generated schedule has a leg without a resolved start source')
    if not isinstance(config.get('cashouts'), dict):
        raise ValueError('Generated schedule is missing cash-outs')


def wrangler_kv(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(['npx', 'wrangler', 'kv', 'key', *args, '--binding', 'SCHEDULE', '--remote',
                           '--config', str(ROOT / 'live' / 'wrangler.toml')],
                          check=check, cwd=ROOT, capture_output=True, text=True)


def kv_get(key: str):
    result = wrangler_kv('get', key, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def kv_put(key: str, value) -> None:
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as handle:
        json.dump(value, handle, separators=(',', ':'))
    try:
        wrangler_kv('put', key, '--path', handle.name)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def archive_previous(new_day: str, get=kv_get, put=kv_put) -> str | None:
    """Freeze the day currently live under day:<date> before a different day replaces it."""
    current = get('config:current')
    if not isinstance(current, dict) or not current.get('date') or current['date'] == new_day:
        return None
    snapshot = get('snapshot:current')
    put(f"day:{current['date']}", {'config': current,
                                   'snapshot': snapshot if isinstance(snapshot, dict) and snapshot.get('date') == current['date'] else None})
    days = get('days')
    days = sorted(set(day for day in days if isinstance(day, str)) | {current['date']}) if isinstance(days, list) else [current['date']]
    put('days', days)
    return current['date']


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('date', type=date.fromisoformat, help='YYYY-MM-DD')
    args = parser.parse_args()
    source = ROOT / 'data' / 'slips' / f'{args.date}.txt'
    if not source.is_file():
        parser.error(f'Missing {source}')
    subprocess.run([sys.executable, str(ROOT / 'scripts' / 'schedule.py'), str(args.date)], check=True, cwd=ROOT)
    payload = ROOT / 'schedule' / f'{args.date}.json'
    validate_schedule(payload)
    archived = archive_previous(args.date.isoformat())
    if archived:
        print(f'Archived {archived}')
    wrangler_kv('put', 'config:current', '--path', str(payload))
    lists = ROOT / 'data' / 'lists.json'
    if lists.is_file():
        wrangler_kv('put', 'lists', '--path', str(lists))
    print(f'Uploaded {args.date} slips to Workers KV')


if __name__ == '__main__':
    main()
