#!/usr/bin/env python3
"""Build a public, hypothetical Thu to Sat slate from open exchange markets."""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from zoneinfo import ZoneInfo

from fairmark import clients
from fairmark.markets import Quote, kalshi_quotes, pinnacle_quotes
from fairmark.pricing import SimulatedSlip, simulate

ROOT = Path(__file__).resolve().parents[1]
PACIFIC = ZoneInfo('America/Los_Angeles')
FEE_RATE = 0.07
TRIALS = 100_000
SEED = 17
SERIES = ('KXNFLREC', 'KXNFLRECYDS', 'KXNFLRSHYDS', 'KXNFLPASSYDS',
          'KXNBAGAME', 'KXNHLGAME', 'KXNCAAFGAME', 'KXUFCFIGHT', 'KXUFCMOV')
TNF_START = datetime.fromisoformat('2026-10-22T17:15:00-07:00')
TARGETS = (
    ('KXNFLRECYDS-26OCT22NECHI-CHIRODUNZE15-25', 'Rome Odunze 25+ receiving yards', 'Rome Odunze', 'Total Receiving Yards', 25),
    ('KXNFLREC-26OCT22NECHI-CHILBURDEN10-3', 'Luther Burden III 3+ receptions', 'Luther Burden', 'Total Receptions', 3),
    ('KXNFLRECYDS-26OCT22NECHI-CHILBURDEN10-40', 'Luther Burden III 40+ receiving yards', 'Luther Burden', 'Total Receiving Yards', 40),
    ('KXNFLRSHYDS-26OCT22NECHI-CHIDSWIFT4-40', "D'Andre Swift 40+ rushing yards", "D'Andre Swift", 'Total Rushing Yards', 40),
    ('KXNFLRSHYDS-26OCT22NECHI-NERSTEVENSON38-30', 'Rhamondre Stevenson 30+ rushing yards', 'Rhamondre Stevenson', 'Total Rushing Yards', 30),
    ('KXNFLPASSYDS-26OCT22NECHI-NEDMAYE10-200', 'Drake Maye 200+ passing yards', 'Drake Maye', 'Total Passing Yards', 200),
    ('KXNFLREC-26OCT22NECHI-NEHHENRY85-2', 'Hunter Henry 2+ receptions', 'Hunter Henry', 'Total Receptions', 2),
    ('KXNCAAFGAME-26OCT23PITTMIA-MIA', 'Miami (FL) to win', 'Miami Florida', None, None),
    ('KXNCAAFGAME-26OCT23MSUUCLA-UCLA', 'UCLA to win', 'UCLA', None, None),
    ('KXNCAAFGAME-26OCT24TXAMALA-ALA', 'Alabama to win', 'Alabama', None, None),
    ('KXNCAAFGAME-26OCT23COLOOKST-OKST', 'Oklahoma State to win', 'Oklahoma State', None, None),
    ('KXNCAAFGAME-26OCT23CALSMU-SMU', 'SMU to win', 'SMU', None, None),
    ('KXNCAAFGAME-26OCT23ISUARIZ-ARIZ', 'Arizona to win', 'Arizona', None, None),
    ('KXUFCFIGHT-26OCT24THICHO-CHO', 'Asaf Chopurov to win', 'Asaf Chopurov', None, None),
    ('KXUFCFIGHT-26OCT24YANDVA-YAN', 'Petr Yan to win', 'Petr Yan', None, None),
    ('KXUFCFIGHT-26OCT24VOLEVL-EVL', 'Movsar Evloev to win', 'Movsar Evloev', None, None),
    ('KXUFCFIGHT-26OCT24KRYYAK-YAK', 'Abdul-Rakhman Yakhyaev to win', 'Abdul Rakhman Yakhyaev', None, None),
    ('KXUFCMOV-26OCT24PICKEI-PICKOTKODQ', 'Aaron Pico by KO/TKO', None, None, None),
)
SLIP_LAYOUT = (
    (25, (TARGETS[0][0], TARGETS[13][0])),
    (25, (TARGETS[4][0], TARGETS[10][0])),
    (100, (TARGETS[0][0], TARGETS[16][0])),
)
ARCHIVE_SOURCE = ROOT / 'scripts' / 'demo_archive_sources.json'


def fee(price: float) -> float:
    return math.ceil(FEE_RATE * price * (1 - price) * 10_000 - 1e-12) / 10_000


def poisson_tail(threshold: int, mean: float) -> float:
    return 1 - sum(math.exp(-mean) * mean ** count / math.factorial(count)
                   for count in range(threshold))


def derived_prop_fair(prices: list[Quote], player: str, stat: str, threshold: int) -> float | None:
    anchors = []
    for quote in prices:
        if (quote.kind != 'prop' or quote.probability is None or
                'Bears' not in quote.event or 'Patriots' not in quote.event or
                quote.market != f'{player} {stat}'):
            continue
        match = re.fullmatch(r'Over (\d+(?:\.\d+)?) ' + re.escape(quote.market), quote.outcome)
        if match:
            anchors.append((float(match.group(1)), quote.probability))
    if len(anchors) != 1:
        return None
    line, over = anchors[0]
    if stat == 'Total Receptions':
        minimum = int(line + 0.5)
        low, high = 0.0, 30.0
        for _ in range(70):
            middle = (low + high) / 2
            if poisson_tail(minimum, middle) < over:
                low = middle
            else:
                high = middle
        return poisson_tail(threshold, (low + high) / 2)
    spread = 65.0 if stat == 'Total Passing Yards' else max(18.0, line * 0.45)
    mean = line - spread * NormalDist().inv_cdf(1 - over)
    return 1 - NormalDist(mean, spread).cdf(threshold - 0.5)


def pin_fair(prices: list[Quote], outcome: str, category: str) -> Quote | None:
    matches = [price for price in prices if price.kind == 'winner' and
               price.probability is not None and price.outcome == outcome and
               price.sport.upper() == ('MIXED MARTIAL ARTS' if category == 'ufc' else 'FOOTBALL')]
    return matches[0] if len(matches) == 1 else None


def team_key(name: str) -> str:
    words = re.findall(r'[a-z0-9]+', name.lower())
    return ' '.join('state' if word == 'st' else word for word in words)


def same_team(left: str, right: str) -> bool:
    first, second = team_key(left), team_key(right)
    return first == second or first.startswith(second + ' ') or second.startswith(first + ' ')


def friday_favorites(open_quotes: dict[str, Quote], pinnacle: list[Quote]) -> list[dict]:
    candidates = []
    for market in open_quotes.values():
        if not market.identifier.startswith(('KXNBAGAME-', 'KXNHLGAME-', 'KXNCAAFGAME-')):
            continue
        names = re.split(r'\s+vs\.?\s+', market.event.split(': ', 1)[-1], flags=re.I)
        if len(names) != 2:
            continue
        for price in pinnacle:
            if (price.kind != 'winner' or price.start is None or price.probability is None or
                    price.start.astimezone(PACIFIC).date().isoformat() != '2026-10-23' or
                    price.probability < 0.75 or market.price > price.probability):
                continue
            opponents = price.event.split(' vs ')
            if (len(opponents) != 2 or not same_team(market.outcome, price.outcome) or
                    not all(any(same_team(name, other) for other in opponents) for name in names)):
                continue
            ask = market.price
            fair = price.probability
            candidates.append({'id': market.identifier, 'name': f'{market.outcome} to win',
                               'event': market.event, 'start': price.start.isoformat(),
                               'ask': ask, 'fair': fair, 'fee': fee(ask),
                               'underFair': (fair - ask - fee(ask)) / fair,
                               'fairSource': 'Pinnacle no-vig'})
            break
    return sorted(candidates, key=lambda leg: (-leg['underFair'], leg['id']))


def build_demo(events: list[dict], pinnacle: list[Quote], *, now: datetime,
               trials: int = TRIALS, seed: int = SEED) -> dict:
    if now.tzinfo is None:
        raise ValueError('now needs a time zone')
    open_quotes = {quote.identifier: quote for quote in kalshi_quotes(events)
                   if quote.status == 'active' and quote.price is not None}
    legs = []
    missing = []
    for ticker, name, outcome, stat, threshold in TARGETS:
        market = open_quotes.get(ticker)
        if market is None:
            missing.append(name)
            continue
        if stat:
            fair = derived_prop_fair(pinnacle, outcome, stat, threshold)
            start = TNF_START
            source = 'Derived from Pinnacle prop line'
            event = 'New England Patriots at Chicago Bears'
        else:
            category = 'ufc' if ticker.startswith('KXUFC') else 'cfb'
            priced = pin_fair(pinnacle, outcome, category) if outcome else None
            fair = priced.probability if priced else None
            start = market.start if category == 'ufc' else priced.start if priced else None
            source = 'Pinnacle no-vig'
            event = market.event
        if fair is None or start is None or start <= now:
            missing.append(name + ' (fair or start unavailable)')
            continue
        ask = market.price
        legs.append({'id': ticker, 'name': name, 'event': event,
                     'start': start.isoformat(), 'ask': ask, 'fair': fair,
                     'fee': fee(ask), 'underFair': (fair - ask - fee(ask)) / fair,
                     'fairSource': source})
    friday = [leg for leg in friday_favorites(open_quotes, pinnacle)
              if datetime.fromisoformat(leg['start']) > now]
    legs.extend(friday)
    legs.sort(key=lambda leg: (leg['start'], leg['name']))
    by_id = {leg['id']: leg for leg in legs}
    slips = []
    simulated = []
    layouts = list(SLIP_LAYOUT)
    if friday:
        layouts[1] = (25, (TARGETS[4][0], friday[0]['id']))
    for number, (stake, ids) in enumerate(layouts, 1):
        if not all(ticker in by_id for ticker in ids):
            continue
        chosen = [by_id[ticker] for ticker in ids]
        if any(leg['fair'] < 0.60 for leg in chosen):
            continue
        ask_product = math.prod(leg['ask'] for leg in chosen)
        fair_product = math.prod(leg['fair'] for leg in chosen)
        entry = ask_product + fee(ask_product)
        if entry >= fair_product:
            continue
        payout = round(stake / entry, 2)
        slip = {'number': number, 'legIds': list(ids), 'stake': stake,
                'placed': now.astimezone(timezone.utc).isoformat(),
                'hitChance': fair_product, 'fee': fee(ask_product),
                'totalPayout': payout, 'gain': round(payout - stake, 2),
                'fairPayout': round(stake / fair_product, 2),
                'underFair': (fair_product - entry) / fair_product}
        slips.append(slip)
        simulated.append(SimulatedSlip(ids, stake, payout))
    if not slips:
        raise ValueError('No qualifying slips have open, priced markets')
    outcome = simulate(simulated, {leg['id']: leg['fair'] for leg in legs},
                       trials=trials, seed=seed)
    return {'schema': 1, 'asOf': now.astimezone(timezone.utc).isoformat(),
            'date': '2026-10-22',
            'slateDate': '2026-10-22', 'slateEndDate': '2026-10-24',
            'timeZone': 'America/Los_Angeles', 'legs': legs, 'slips': slips,
            'missingMarkets': missing,
            'portfolio': {'stake': sum(slip['stake'] for slip in slips),
                          'allHitPayout': round(sum(slip['totalPayout'] for slip in slips), 2),
                          'allHitGain': round(sum(slip['gain'] for slip in slips), 2),
                          'allHitFairPayout': round(sum(slip['fairPayout'] for slip in slips), 2),
                          'allHitFairGain': round(sum(slip['fairPayout'] - slip['stake'] for slip in slips), 2),
                          'dayProfitChance': outcome.any_profit,
                          'medianGain': round(outcome.median, 2),
                          'trials': trials, 'seed': seed}}


def build_archived_days(source: dict, *, trials: int = TRIALS, seed: int = SEED) -> dict:
    """Build hypothetical past slips only from documented final Kalshi markets."""
    expected = {f'2026-10-{day}' for day in range(15, 20)}
    if set(source['layout']) != expected:
        raise ValueError('Archive must contain Oct 15 through Oct 19')
    records = {}
    for date, layout in source['layout'].items():
        if len(layout) != 3:
            raise ValueError(f'{date} needs three slips')
        legs_by_id = {}
        slips = []
        simulated = []
        snapshot = {}
        for number, (stake, ids) in enumerate(layout, 1):
            if stake not in (25, 50, 75, 100) or len(ids) not in (2, 3) or len(set(ids)) != len(ids):
                raise ValueError(f'{date} has an invalid slip')
            starts = []
            quoted = []
            for ticker in ids:
                market = source['markets'][ticker]
                start = datetime.fromisoformat(market['start'])
                candle = datetime.fromisoformat(market['candleEnd'])
                if (market['ticker'] != ticker or market['status'] != 'finalized' or
                        market['result'] not in ('yes', 'no') or
                        start.astimezone(PACIFIC).date().isoformat() != date or
                        candle >= start or not 0.70 <= market['ask'] < 1 or
                        not 0 <= market['bid'] <= market['ask'] or
                        market['fairSource'] != 'Kalshi pregame mid' or
                        not market['settlement_ts'] or
                        not market['marketSource'].startswith('https://api.elections.kalshi.com/trade-api/v2/markets/') or
                        not market['priceSource'].startswith('https://api.elections.kalshi.com/trade-api/v2/series/')):
                    raise ValueError(f'{date} has an unverified market: {ticker}')
                starts.append(start)
                quoted.append(candle)
                fair = (market['bid'] + market['ask']) / 2
                ask = market['ask']
                legs_by_id[ticker] = {
                    'id': ticker, 'name': market['name'], 'event': market['event'],
                    'start': market['start'], 'priceAt': market['candleEnd'],
                    'ask': ask, 'fair': fair, 'fee': fee(ask),
                    'underFair': (fair - ask - fee(ask)) / fair,
                    'fairSource': market['fairSource']}
                snapshot[ticker] = {
                    'result': market['result'], 'settlement_ts': market['settlement_ts'],
                    'close_time': market['close_time']}
            if max(quoted) >= min(starts):
                raise ValueError(f'{date} has a quote after a slip began')
            selected = [legs_by_id[ticker] for ticker in ids]
            ask_product = math.prod(leg['ask'] for leg in selected)
            fair_product = math.prod(leg['fair'] for leg in selected)
            entry = ask_product + fee(ask_product)
            payout = round(stake / entry, 2)
            slips.append({
                'number': number, 'legIds': ids, 'stake': stake,
                'hitChance': fair_product, 'fee': fee(ask_product),
                'totalPayout': payout, 'gain': round(payout - stake, 2),
                'fairPayout': round(stake / fair_product, 2),
                'underFair': (fair_product - entry) / fair_product})
            simulated.append(SimulatedSlip(tuple(ids), stake, payout))
        legs = sorted(legs_by_id.values(), key=lambda leg: (leg['start'], leg['name']))
        outcome = simulate(simulated, {leg['id']: leg['fair'] for leg in legs},
                           trials=trials, seed=seed)
        config = {
            'schema': 1, 'date': date,
            'asOf': max(leg['priceAt'] for leg in legs),
            'timeZone': 'America/Los_Angeles', 'legs': legs, 'slips': slips,
            'portfolio': {
                'stake': sum(slip['stake'] for slip in slips),
                'allHitPayout': round(sum(slip['totalPayout'] for slip in slips), 2),
                'allHitGain': round(sum(slip['gain'] for slip in slips), 2),
                'allHitFairPayout': round(sum(slip['fairPayout'] for slip in slips), 2),
                'allHitFairGain': round(sum(slip['fairPayout'] - slip['stake'] for slip in slips), 2),
                'dayProfitChance': outcome.any_profit,
                'medianGain': round(outcome.median, 2),
                'trials': trials, 'seed': seed}}
        records[date] = {'config': config, 'snapshot': {'date': date, 'markets': snapshot}}
    return {'days': sorted(records, reverse=True), 'records': records}


def write_archive_records(output: Path = ROOT / 'schedule' / 'demo-archives.json') -> dict:
    records = build_archived_days(json.loads(ARCHIVE_SOURCE.read_text()))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(records, indent=2) + '\n')
    return records


def refresh_demo(kv=None, *, output: Path = ROOT / 'schedule' / 'demo.json',
                 now: datetime | None = None) -> dict:
    """Build a review file, then optionally publish only config:demo via a caller's KV client."""
    now = now or datetime.now(timezone.utc)
    limiter = clients.KalshiRateLimiter()
    events = [event for series in SERIES for event in clients._kalshi_series_events(series, limiter)]
    pinnacle = [quote for sport in (15, 22, 4, 19)
                for quote in pinnacle_quotes(*clients.pinnacle_sport(sport))]
    config = build_demo(events, pinnacle, now=now)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, indent=2) + '\n')
    if kv is not None:
        kv.put('config:demo', config)
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'schedule' / 'demo.json')
    parser.add_argument('--archives-output', type=Path)
    args = parser.parse_args()
    if args.archives_output:
        records = write_archive_records(args.archives_output)
        print(f"Wrote {args.archives_output} with {len(records['days'])} final days")
        return
    config = refresh_demo(output=args.output)
    print(f"Wrote {args.output} with {len(config['slips'])} slips and {len(config['legs'])} public markets")
    print('Missing markets: ' + ', '.join(config['missingMarkets']))


if __name__ == '__main__':
    main()
