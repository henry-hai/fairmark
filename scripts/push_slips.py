#!/usr/bin/env python3
"""Resolve a local slip file and upload its private schedule to Workers KV."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
import time
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
    # A sportsbook price logged at the buy (book, bookOdds, bookChance) stands in for Bovada's.
    required = ('bovadaOdds', 'bovadaImplied', 'pinnacleRaw', 'fair')
    missing = [f"{leg.get('name', '<unnamed>')}: {field}"
               for leg in config['legs'] for field in required if leg.get(field) is None
               and not (field.startswith('bovada') and leg.get('bookOdds') is not None)]
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
        if leg.get('fair') is not None and leg.get('fairSource') not in FAIR_SOURCES:
            raise ValueError('Fair reference must be Pinnacle no-vig or Derived')
        odds, chance = leg.get('bookOdds'), leg.get('bookChance')
        if odds is not None and (not isinstance(leg.get('book'), str) or not isinstance(odds, str)
                                 or not isinstance(chance, (int, float))
                                 or not math.isclose(implied_probability(odds), chance)):
            raise ValueError('Sportsbook chance does not match its odds')
        ask = leg.get('askAtBuy')
        if ask is not None and (not isinstance(ask, (int, float)) or not 0 < ask < 1):
            raise ValueError('Invalid ask at buy')


# Kept when a later push finds nothing, so a push after kickoff no longer wipes the pregame prices.
SAVED_PRICES = ('bovadaOdds', 'bovadaImplied', 'pinnacleRaw', 'fair', 'fairSource',
                'book', 'bookOdds', 'bookChance', 'askAtBuy')
FAIR_SOURCES = ('Pinnacle no-vig', 'Derived')
BOOK_MARKS = (('DraftKings', ('draftkings', ' dk ')), ('Bovada', ('bovada',)),
              ('FanDuel', ('fanduel',)), ('BetMGM', ('betmgm',)))


def book_named(text: str) -> str | None:
    padded = f" {text.casefold().replace(':', ' ')} "
    return next((book for book, marks in BOOK_MARKS if any(mark in padded for mark in marks)), None)


def quoted_pinnacle(source: str) -> bool:
    """True when the fair is Pinnacle's own no-vig quote, e.g. "Bovada odds, fair Pinnacle no-vig"."""
    lowered = source.casefold()
    return 'pinnacle no-vig' in lowered and not any(word in lowered for word in ('estimate', 'derived', 'minus', 'model'))


def number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def read_prices(path: Path) -> dict[str, dict]:
    """Read data/slips/<date>.prices, the prices logged at each buy, keyed by leg name.

    Each line is: leg | Kalshi ask at buy | sportsbook odds | sportsbook implied | Pinnacle line |
    fair | fair source | pulled. The sportsbook comes from the header's odds column when it names
    one, else from the fair source text.
    """
    if not path.is_file():
        return {}
    header_book = None
    prices = {}
    for line in path.read_text().splitlines():
        fields = [field.strip() for field in line.lstrip('#').split('|')]
        if line.startswith('#'):
            header_book = header_book or (book_named(fields[2]) if len(fields) > 2 else None)
            continue
        if len(fields) < 7 or not fields[0]:
            continue
        name, ask, odds, _, _, fair, source = fields[:7]
        entry = {}
        ask_value = number(ask)
        if ask_value is not None and 0 < ask_value < 1:
            entry['askAtBuy'] = ask_value
        book = header_book or book_named(source)
        # Odds with no named sportsbook could be anyone's, Pinnacle's included, so they are left out.
        if book and odds.lstrip('+-').isdigit() and odds[0] in '+-':
            entry.update(book=book, bookOdds=odds, bookChance=implied_probability(odds))
        fair_value = number(fair)
        if fair_value is not None and 0 < fair_value <= 1:
            entry.update(fair=fair_value, fairSource='Pinnacle no-vig' if quoted_pinnacle(source) else 'Derived')
        prices[name.casefold()] = entry
    return prices


def fill_prices(config: dict, saved: dict | None, prices: dict[str, dict]) -> None:
    """Fill each leg from the last upload for the same day, then from the prices logged at the buy.

    The logged ask and sportsbook price are what Henry actually saw, so they win. A logged fair only
    fills a gap, since a quoted Pinnacle fair beats a derived one.
    """
    previous = {(leg.get('name'), leg.get('ticker')): leg for leg in (saved or {}).get('legs', [])} \
        if isinstance(saved, dict) and saved.get('date') == config.get('date') else {}
    for leg in config['legs']:
        old = previous.get((leg.get('name'), leg.get('ticker')), {})
        for key in SAVED_PRICES:
            if leg.get(key) is None and old.get(key) is not None:
                leg[key] = old[key]
        entry = prices.get(str(leg.get('name', '')).casefold(), {})
        for key in ('askAtBuy', 'book', 'bookOdds', 'bookChance'):
            if key in entry:
                leg[key] = entry[key]
        if leg.get('fair') is None and 'fair' in entry:
            leg['fair'], leg['fairSource'] = entry['fair'], entry['fairSource']


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


def d1_put(key: str, text: str) -> None:
    """Upsert one key in D1. D1 caps a SQL statement at 100 KB, so long values go in pieces."""
    pieces = [text[i:i + 60000].replace("'", "''") for i in range(0, len(text), 60000)] or ['']
    now = int(time.time() * 1000)
    lines = [f"INSERT INTO store (key, value, updated_at) VALUES ('{key}', '{pieces[0]}', {now}) "
             'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;']
    lines += [f"UPDATE store SET value = value || '{piece}' WHERE key = '{key}';" for piece in pieces[1:]]
    with tempfile.NamedTemporaryFile('w', suffix='.sql', delete=False) as handle:
        handle.write('\n'.join(lines))
    try:
        subprocess.run(['npx', 'wrangler', 'd1', 'execute', 'fairmark-live', '--remote', '--file', handle.name,
                        '--config', str(ROOT / 'live' / 'poller' / 'wrangler.toml')],
                       check=True, cwd=ROOT, capture_output=True, text=True)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def mirror_to_d1(keys: dict[str, str]) -> None:
    """Copy an upload to D1 for the cloud poller. KV is still what the live page reads, so a failure only warns."""
    try:
        for key, text in keys.items():
            d1_put(key, text)
    except (subprocess.CalledProcessError, OSError) as error:
        print(f'Warning, D1 copy failed: {type(error).__name__}', file=sys.stderr)


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
    config = json.loads(payload.read_text())
    fill_prices(config, kv_get('config:current'), read_prices(source.with_suffix('.prices')))
    payload.write_text(json.dumps(config, indent=2))
    validate_schedule(payload)
    archived = archive_previous(args.date.isoformat())
    if archived:
        print(f'Archived {archived}')
    wrangler_kv('put', 'config:current', '--path', str(payload))
    lists = ROOT / 'data' / 'lists.json'
    if lists.is_file():
        wrangler_kv('put', 'lists', '--path', str(lists))
    mirror_to_d1({'config:current': json.dumps(config, separators=(',', ':')),
                  **({'lists': lists.read_text()} if lists.is_file() else {})})
    print(f'Uploaded {args.date} slips to Workers KV')


if __name__ == '__main__':
    main()
