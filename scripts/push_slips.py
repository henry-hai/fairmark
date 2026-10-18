#!/usr/bin/env python3
"""Resolve a local slip file and upload its private schedule to Workers KV."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
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
    subprocess.run(['npx', 'wrangler', 'kv', 'key', 'put', 'config:current',
                    '--binding', 'SCHEDULE', '--path', str(payload),
                    '--remote', '--config', str(ROOT / 'live' / 'wrangler.toml')], check=True, cwd=ROOT)
    print(f'Uploaded {args.date} slips to Workers KV')


if __name__ == '__main__':
    main()
