#!/usr/bin/env python3
"""Resolve a local slip file and upload its private schedule to Workers KV."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from fairmark.pricing import implied_probability

ROOT = Path(__file__).resolve().parents[1]


def validate_schedule(payload: Path) -> None:
    config = json.loads(payload.read_text())
    sources = {'ticker file', 'Pinnacle', 'Bovada', 'estimated'}
    if not config.get('legs') or any(not leg.get('start') or leg.get('startSource') not in sources
                                     for leg in config['legs']):
        raise ValueError('Generated schedule has a leg without a resolved start source')
    if not isinstance(config.get('cashouts'), dict):
        raise ValueError('Generated schedule is missing cash-outs')
    # Many props and esports legs have no sportsbook line, so a gap warns instead of blocking
    # the upload. The page hides any price column that a day's legs don't all have.
    required = ('bovadaOdds', 'bovadaImplied', 'pinnacleRaw', 'fair')
    missing = [f"{leg.get('name', '<unnamed>')}: {field}"
               for leg in config['legs'] for field in required if leg.get(field) is None]
    if missing:
        print('Warning, missing sportsbook prices:\n' + '\n'.join(missing), file=sys.stderr)
    for leg in config['legs']:
        odds = leg.get('bovadaOdds')
        implied = leg.get('bovadaImplied')
        if odds is not None and implied is not None:
            if (not isinstance(odds, str) or not isinstance(implied, (int, float))
                    or not math.isfinite(implied) or not math.isclose(implied_probability(odds), implied)):
                raise ValueError('Bovada implied chance does not match its odds')
        for key in ('pinnacleRaw', 'fair'):
            value = leg.get(key)
            if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 1):
                raise ValueError(f'Invalid {key} chance')
        if leg.get('fair') is not None and leg.get('fairSource') != 'Pinnacle no-vig':
            raise ValueError('Fair reference must be Pinnacle no-vig')


def wrangler_kv(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(['npx', 'wrangler', 'kv', 'key', *args, '--binding', 'SCHEDULE', '--remote',
                           '--config', str(ROOT / 'live' / 'wrangler.toml')],
                          check=check, cwd=ROOT, capture_output=True, text=True)


def kv_get(key: str):
    """Return the stored JSON, None when the key does not exist, and raise on any other failure."""
    result = wrangler_kv('get', key, check=False)
    if result.returncode != 0:
        if '404' in result.stderr or '404' in result.stdout:
            return None
        raise RuntimeError(f'Could not read {key} from Workers KV, nothing was uploaded')
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise RuntimeError(f'{key} in Workers KV is not valid JSON, nothing was uploaded') from None


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
