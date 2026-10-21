"""Match slip purchases to account sells and persist cash-outs."""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CENT = Decimal('0.01')


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def same_cashout(left: dict, right: dict) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    try:
        return (Decimal(str(left['amount'])) == Decimal(str(right['amount'])) and
                Decimal(str(left.get('remainingFraction', '0'))) == Decimal(str(right['remainingFraction'])))
    except (KeyError, TypeError, ValueError):
        return False


def detect_cashouts(config: dict, fills: list[dict]) -> dict[str, dict[str, str]]:
    """Use each slip's purchase count, cost and minute to identify its combo ticker."""
    buys = defaultdict(list)
    sells = defaultdict(list)
    for fill in fills:
        if not isinstance(fill, dict) or not fill.get('ticker'):
            continue
        if fill.get('action') == 'buy':
            buys[fill.get('order_id'), fill['ticker'], fill.get('side')].append(fill)
        elif fill.get('action') == 'sell':
            sells[fill['ticker']].append(fill)

    matched = {}
    detected = {}
    shared = set()
    for slip in config.get('slips', []):
        if not isinstance(slip, dict) or not slip.get('name'):
            continue
        try:
            payout = Decimal(str(slip['payout']))
            stake = Decimal(str(slip['stake']))
        except (KeyError, TypeError, ValueError):
            continue
        try:
            placed = datetime.fromisoformat(slip['placed'].replace('Z', '+00:00')) if slip.get('placed') else None
            if placed and placed.tzinfo is None:
                continue
        except (TypeError, ValueError):
            continue
        candidates = []
        for (_, ticker, side), group in buys.items():
            if side not in ('yes', 'no') or (slip.get('ticker') and slip['ticker'] != ticker):
                continue
            try:
                count = sum(Decimal(str(item['count_fp'])) for item in group)
                cost = sum(Decimal(str(item['count_fp'])) * Decimal(str(item[f'{side}_price_dollars']))
                           + Decimal(str(item.get('fee_cost') or '0')) for item in group)
                bought_at = min(datetime.fromisoformat(item['created_time'].replace('Z', '+00:00'))
                                for item in group)
            except (KeyError, TypeError, ValueError):
                continue
            if (count == payout and cents(cost) == stake and
                    bought_at.astimezone(ZoneInfo('America/Los_Angeles')).date() == date.fromisoformat(config['date']) and
                    (placed is None or bought_at.replace(second=0, microsecond=0) == placed.replace(second=0, microsecond=0))):
                candidates.append((ticker, side, count))
        if len(candidates) != 1:
            continue
        ticker, side, bought = candidates[0]
        if ticker in matched:
            print(f'Cash-out warning: ticker shared by slips {matched[ticker]} and {slip["name"]}', file=sys.stderr)
            detected.pop(matched[ticker], None)
            shared.add(ticker)
            continue
        matched[ticker] = slip['name']
        if ticker in shared:
            continue
        try:
            sales = [item for item in sells[ticker] if item.get('side') != side]
            sold = sum(Decimal(str(item['count_fp'])) for item in sales)
            if not sold:
                continue
            if sold > bought:
                print(f'Cash-out warning: sales exceed purchase for slip {slip["name"]}', file=sys.stderr)
                continue
            amount = sum(Decimal(str(item['count_fp'])) * Decimal(str(item[f'{side}_price_dollars']))
                         - Decimal(str(item.get('fee_cost') or '0')) for item in sales)
            if amount < 0:
                continue
            remaining = (bought - sold) / bought
        except (KeyError, TypeError, ValueError):
            continue
        detected[slip['name']] = {'amount': str(cents(amount)), 'remainingFraction': str(remaining.normalize())}
    return detected


def persist_cashouts(path: Path, detected: dict[str, dict[str, str]]) -> tuple[dict[str, dict[str, str]], set[str]]:
    """Preserve manual rows and only revise rows marked as automatic."""
    lines = path.read_text().splitlines() if path.exists() else []
    rows = {}
    for index, raw in enumerate(lines):
        parts = raw.split()
        if parts and not raw.lstrip().startswith('#'):
            rows[parts[0]] = (index, parts, index > 0 and lines[index - 1] == f'# automatic cash-out: {parts[0]}')
    accepted = {}
    automatic_names = set()
    changed = False
    for name, value in detected.items():
        entry = f'{name} {value["amount"]} {value["remainingFraction"]}'
        if name in rows:
            index, parts, automatic = rows[name]
            existing = {'amount': parts[1] if len(parts) > 1 else '',
                        'remainingFraction': parts[2] if len(parts) > 2 else '0'}
            if not automatic and not same_cashout(existing, value):
                print(f'Cash-out warning: manual cash-out differs for slip {name} in {path.name}', file=sys.stderr)
                continue
            if automatic and lines[index] != entry:
                lines[index] = entry
                changed = True
            if automatic:
                automatic_names.add(name)
        else:
            lines.extend((f'# automatic cash-out: {name}', entry))
            changed = True
            automatic_names.add(name)
        accepted[name] = value
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(lines) + '\n')
    return accepted, automatic_names


def update_cashouts(kv, config: dict, *, root: Path = ROOT, client=None) -> None:
    key_id, key_path = os.environ.get('KALSHI_API_KEY_ID'), os.environ.get('KALSHI_PRIVATE_KEY_PATH')
    if client is None:
        if not key_id or not key_path:
            print('Cash-out detection skipped: Kalshi credentials missing', file=sys.stderr)
            return
        from kalshi_fills import KalshiFills

        path = Path(key_path).expanduser()
        client = KalshiFills(key_id, path if path.is_absolute() else root / path)
    day = date.fromisoformat(config['date'])
    start = datetime.combine(day, time(), ZoneInfo('America/Los_Angeles'))
    detected = detect_cashouts(config, client.fills(min_ts=int(start.timestamp())))
    if not detected:
        return
    path = root / 'data' / 'slips' / f'{day}.cashouts'
    previous = config.get('cashouts') or {}
    lines = path.read_text().splitlines() if path.exists() else []
    automatic_in_file = {line.removeprefix('# automatic cash-out: ') for line in lines
                         if line.startswith('# automatic cash-out: ')}
    for name, value in list(detected.items()):
        existing = previous.get(name)
        if existing and not same_cashout(existing, value) and name not in automatic_in_file:
            print(f'Cash-out warning: KV cash-out differs for slip {name}', file=sys.stderr)
            detected.pop(name)
    accepted, _ = persist_cashouts(path, detected)
    merged = dict(previous)
    for name, value in accepted.items():
        merged[name] = value
    if any(name not in previous or not same_cashout(previous[name], value) for name, value in accepted.items()):
        kv.put('config:current', {**config, 'cashouts': merged})
