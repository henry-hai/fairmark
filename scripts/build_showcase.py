"""Build the locked demo showcase day from one pregame capture of Kalshi and sportsbook prices.

    uv run python scripts/build_showcase.py candidates 2026-10-25
    uv run python scripts/build_showcase.py build 2026-10-25 layout.json

`candidates` prices every captured Kalshi leg that has both a sportsbook line and a Pinnacle fair.
`build` turns a chosen layout into config:demo and snapshot:demo files, with results read from
Kalshi after the games. Every price comes from the capture, so the slips were all placed pregame.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist

ROOT = Path(__file__).resolve().parents[1]
KALSHI = 'https://api.elections.kalshi.com/trade-api/v2'
BOOKS = (('draftkings', 'DraftKings'), ('fanduel', 'FanDuel'), ('betmgm', 'BetMGM'))
STATS = {'KXNFLREC': ('player_receptions', 'receptions'), 'KXNFLRECYDS': ('player_reception_yds', 'receiving yards'),
         'KXNFLRSHYDS': ('player_rush_yds', 'rushing yards'), 'KXNFLPASSYDS': ('player_pass_yds', 'passing yards')}


def implied(odds: int) -> float:
    return -odds / (-odds + 100) if odds < 0 else 100 / (odds + 100)


def american(odds: int) -> str:
    return f'+{odds}' if odds > 0 else str(odds)


def fee(price: float) -> float:
    return math.ceil(0.07 * price * (1 - price) * 10000 - 1e-12) / 10000


def key(name: str) -> str:
    return ' '.join(re.findall(r'[a-z0-9]+', re.sub(r'\b(jr|sr|ii|iii)\b\.?', '', name.lower())))


def poisson_tail(threshold: int, mean: float) -> float:
    return 1 - sum(math.exp(-mean) * mean ** count / math.factorial(count) for count in range(threshold))


def derived(line: float, over: float, stat: str, threshold: int) -> float:
    """Carry Pinnacle's no-vig chance at its main line over to Kalshi's threshold, as build_demo does."""
    if stat == 'player_receptions':
        low, high = 0.0, 30.0
        for _ in range(70):
            middle = (low + high) / 2
            low, high = (middle, high) if poisson_tail(int(line + 0.5), middle) < over else (low, middle)
        return poisson_tail(threshold, (low + high) / 2)
    spread = 65.0 if stat == 'player_pass_yds' else max(18.0, line * 0.45)
    mean = line - spread * NormalDist().inv_cdf(1 - over)
    return 1 - NormalDist(mean, spread).cdf(threshold - 0.5)


def latest(folder: Path, prefix: str) -> dict:
    return json.loads(sorted(folder.glob(f'{prefix}_*.json'))[-1].read_text())


def outcomes(event: dict, book: str, market: str) -> list[dict]:
    return [o for b in event['bookmakers'] if b['key'] == book for m in b['markets'] if m['key'] == market for o in m['outcomes']]


def sportsbook_line(event: dict, market: str, pick) -> dict | None:
    """First of DraftKings, FanDuel, BetMGM that lists the exact line."""
    for book, label in BOOKS:
        for name in (market, f'{market}_alternate'):
            found = [o for o in outcomes(event, book, name) if pick(o)]
            if found:
                return {'book': label, 'bookOdds': american(found[0]['price']), 'bookChance': implied(found[0]['price'])}
    return None


def price_leg(market: dict, events: list[dict]) -> dict | None:
    series = market['ticker'].split('-')[0]
    ask = float(market['yes_ask_dollars'] or 0)
    if not 0.05 <= ask <= 0.97:
        return None
    if series == 'KXNFLGAME':
        team = key(market['yes_sub_title'])
        for event in events:
            names = [n for n in (event['home_team'], event['away_team']) if key(n).startswith(team)]
            if len(names) != 1:
                continue
            pin = outcomes(event, 'pinnacle', 'h2h')
            if len(pin) != 2:
                return None
            raw = {o['name']: implied(o['price']) for o in pin}
            fair, source = raw[names[0]] / sum(raw.values()), 'Pinnacle no-vig'
            book = sportsbook_line(event, 'h2h', lambda o: o['name'] == names[0])
            name = f'{names[0]} to win'
            break
        else:
            return None
    else:
        stat, words = STATS[series]
        player, threshold = re.fullmatch(r'(.+): (\d+)\+', market['yes_sub_title']).groups()
        threshold = int(threshold)
        for event in events:
            pin = [o for o in outcomes(event, 'pinnacle', stat) if key(o.get('description', '')) == key(player)]
            if pin:
                break
        else:
            return None
        over = [o for o in pin if o['name'] == 'Over']
        under = [o for o in pin if o['name'] == 'Under']
        if len(over) != 1 or len(under) != 1:
            return None
        line = over[0]['point']
        chance = implied(over[0]['price']) / (implied(over[0]['price']) + implied(under[0]['price']))
        if line == threshold - 0.5:
            fair, source = chance, 'Pinnacle no-vig'
        else:
            fair, source = derived(line, chance, stat, threshold), 'Derived from Pinnacle prop line'
        book = sportsbook_line(event, stat, lambda o: o['name'] == 'Over' and key(o.get('description', '')) == key(player)
                               and o.get('point') == threshold - 0.5)
        name = f'{player} {threshold}+ {words}'
    if book is None:
        return None
    return {'id': market['ticker'], 'icon': '🏈', 'name': name, 'event': f"{event['away_team']} at {event['home_team']}",
            'start': event['commence_time'], 'ask': ask, 'fee': fee(ask), 'fair': fair, 'fairSource': source,
            'underFair': (fair - ask - fee(ask)) / fair, **book}


def kalshi_market(ticker: str) -> dict:
    with urllib.request.urlopen(f'{KALSHI}/markets/{ticker}', timeout=30) as response:
        return json.load(response)['market']


def candidates(day: str) -> list[dict]:
    folder = ROOT / 'data' / 'captures' / day
    books, kalshi = latest(folder, 'sportsbooks'), latest(folder, 'kalshi')
    stamp = datetime.strptime(day, '%Y-%m-%d').strftime('%y%b%d').upper()
    legs = []
    for rows in kalshi['series'].values():
        for market in rows:
            if f'-{stamp}' in market['ticker']:
                leg = price_leg(market, books['events'])
                if leg:
                    legs.append({**leg, 'priceAt': books['capturedAt']})
    return sorted(legs, key=lambda leg: -leg['underFair'])


def build(day: str, layout: dict) -> tuple[dict, dict]:
    """layout: {"slips": [{"stake": 25, "legs": [...]}], "extraLegs": [...], "cashouts": {"2": {...}}}"""
    priced = {leg['id']: leg for leg in candidates(day)}
    priced.update({leg['id']: leg for leg in layout.get('extraLegs', [])})
    used = [ticker for slip in layout['slips'] for ticker in slip['legs']]
    legs = [priced[ticker] for ticker in dict.fromkeys(used)]
    slips = []
    for number, slip in enumerate(layout['slips'], 1):
        ask = math.prod(priced[t]['ask'] for t in slip['legs'])
        fairs = [priced[t]['fair'] for t in slip['legs']]
        fair = math.prod(fairs) if all(f is not None for f in fairs) else None
        entry = ask + fee(ask)
        payout = round(slip['stake'] / entry, 2)
        slips.append({'number': number, 'legIds': slip['legs'], 'stake': slip['stake'],
                      'placed': slip.get('placed') or priced[slip['legs'][0]]['priceAt'], 'hitChance': fair,
                      'fee': fee(ask), 'totalPayout': payout, 'gain': round(payout - slip['stake'], 2),
                      'fairPayout': round(slip['stake'] / fair, 2) if fair else None,
                      'underFair': (fair - entry) / fair if fair else None})
    now = datetime.now(timezone.utc).isoformat()
    markets = {}
    for leg in legs:
        market = kalshi_market(leg['id'])
        markets[leg['id']] = {field: market.get(field) for field in
                              ('yes_bid_dollars', 'yes_ask_dollars', 'result', 'settlement_ts', 'close_time')}
        markets[leg['id']]['fetchedAt'] = now
    config = {'schema': 1, 'locked': True, 'asOf': legs[0]['priceAt'], 'date': day, 'slateDate': day,
              'slateEndDate': max(leg['start'] for leg in legs)[:10], 'timeZone': 'America/Los_Angeles',
              'legs': legs, 'slips': slips, 'cashouts': layout.get('cashouts', {}),
              'portfolio': {'stake': sum(s['stake'] for s in slips),
                            'allHitPayout': round(sum(s['totalPayout'] for s in slips), 2),
                            'allHitGain': round(sum(s['gain'] for s in slips), 2),
                            'allHitFairPayout': round(sum(s['fairPayout'] or s['totalPayout'] for s in slips), 2),
                            'allHitFairGain': round(sum((s['fairPayout'] or s['totalPayout']) - s['stake'] for s in slips), 2),
                            'dayProfitChance': layout['dayProfitChance'], 'medianGain': layout['medianGain'],
                            'trials': 100000, 'seed': 17}}
    return config, {'date': day, 'markets': markets}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('candidates', 'build'))
    parser.add_argument('date')
    parser.add_argument('layout', nargs='?', type=Path)
    args = parser.parse_args()
    folder = ROOT / 'data' / 'captures' / args.date
    if args.command == 'candidates':
        legs = candidates(args.date)
        (folder / 'candidates.json').write_text(json.dumps(legs, indent=1))
        print(f'{len(legs)} priced legs written to {folder / "candidates.json"}')
        return
    config, snapshot = build(args.date, json.loads(args.layout.read_text()))
    (folder / 'config_demo.json').write_text(json.dumps(config, indent=1))
    (folder / 'snapshot_demo.json').write_text(json.dumps(snapshot, indent=1))
    print(f'{len(config["slips"])} slips and {len(config["legs"])} legs written to {folder}')


if __name__ == '__main__':
    main()
