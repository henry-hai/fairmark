#!/usr/bin/env python3
"""Resolve local slips and prepare a schedule for local viewing or KV upload."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fairmark import clients
from fairmark.cli import actual_start, fetch_quotes, resolve, sport_family, unique_legs
from fairmark.input import Slip as SlateSlip
from fairmark.markets import kalshi_quotes
from fairmark.matching import best_quote
from fairmark.pricing import implied_probability
from fairmark.sports import sport_icon

ROOT = Path(__file__).resolve().parents[1]
API = clients.KALSHI
PACIFIC = ZoneInfo('America/Los_Angeles')
LISTS = ('ban', 'doghouse', 'hateWatch')


@dataclass(frozen=True)
class Slip:
    name: str
    stake: Decimal
    payout: Decimal | None
    legs: tuple[str, ...]
    placed: datetime | None = None


@dataclass(frozen=True)
class Override:
    ticker: str
    side: str
    fallback_start: datetime | None = None
    fallback_estimated: bool = False


def read_slips(path: Path) -> list[Slip]:
    slips = []
    for line_number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'slip\s+(.+?)\s+(\d+(?:\.\d{1,2})?)(?:\s+(\d+(?:\.\d{1,2})?))?(?:\s+@(\S+))?\s*:\s*(.+)', line, re.I)
        if not match:
            raise ValueError(f'Line {line_number}: expected slip NAME STAKE [PAYOUT] [@TIME]: Leg, Leg')
        name, stake, payout, placed, raw_legs = match.groups()
        legs = tuple(part.strip() for part in raw_legs.split(','))
        if not all(legs) or len({leg.casefold() for leg in legs}) != len(legs):
            raise ValueError(f'Line {line_number}: legs must be named and unique')
        if Decimal(stake) <= 0 or payout is not None and Decimal(payout) <= 0:
            raise ValueError(f'Line {line_number}: amounts must be positive')
        slips.append(Slip(name, Decimal(stake), Decimal(payout) if payout else None, legs,
                          placed_time(placed, path, line_number) if placed else None))
    if not slips or len({slip.name.casefold() for slip in slips}) != len(slips):
        raise ValueError('Slips must be present and uniquely named')
    return slips


def placed_time(value: str, path: Path, line_number: int) -> datetime:
    """Read @6:40a, @18:40 or @10-17 11:50p as Pacific time on the slip file's day."""
    match = re.fullmatch(r'(?:(\d{1,2})-(\d{1,2})-)?(\d{1,2}):(\d{2})([ap])?m?', value, re.I)
    try:
        day = date.fromisoformat(path.stem)
        month, day_of_month, hour, minute, half = match.groups()
        hour, minute = int(hour), int(minute)
        if half:
            if not 1 <= hour <= 12:
                raise ValueError
            hour = hour % 12 + (12 if half.lower() == 'p' else 0)
        if month:
            day = day.replace(month=int(month), day=int(day_of_month))
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=PACIFIC)
    except (AttributeError, ValueError):
        raise ValueError(f'Line {line_number}: placed time must look like @6:40a, @18:40 or @10-17-11:50p') from None


def read_lists(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) - set(LISTS) or any(
            not isinstance(names, list) or not all(isinstance(name, str) and name.strip() for name in names)
            for names in data.values()):
        raise ValueError(f'{path} must map {", ".join(LISTS)} to lists of names')
    return {key: [name.strip() for name in names] for key, names in data.items()}


def read_cashouts(path: Path, slips: list[Slip]) -> dict[str, dict]:
    if not path.exists():
        return {}
    names = {slip.name.casefold(): slip.name for slip in slips}
    cashouts = {}
    for line_number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) not in (2, 3):
            raise ValueError(f'{path}:{line_number}: expected SLIP AMOUNT [REMAINING_FRACTION]')
        name, amount = parts[:2]
        try:
            received = Decimal(amount)
            remaining = Decimal(parts[2]) if len(parts) == 3 else Decimal('0')
        except Exception as exc:
            raise ValueError(f'{path}:{line_number}: invalid cash-out amount or fraction') from exc
        if (name.casefold() not in names or name.casefold() in cashouts
                or not received.is_finite() or received < 0
                or not remaining.is_finite() or remaining < 0 or remaining >= 1):
            raise ValueError(f'{path}:{line_number}: unknown or duplicate slip, or invalid cash-out value')
        cashouts[name.casefold()] = {'amount': str(received), 'remainingFraction': str(remaining)}
    return {names[key]: value for key, value in cashouts.items()}


def read_overrides(path: Path) -> dict[str, Override]:
    if not path.exists():
        return {}
    overrides = {}
    for line_number, raw in enumerate(path.read_text().splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        parts = [part.strip() for part in raw.split('|')]
        if len(parts) < 2 or len(parts) > 5 or not parts[0] or not parts[1]:
            raise ValueError(f'{path}:{line_number}: expected Leg | TICKER | yes/no | start | estimated')
        name, ticker = parts[:2]
        side = parts[2].lower() if len(parts) >= 3 else 'yes'
        if side not in ('yes', 'no') or name.casefold() in overrides:
            raise ValueError(f'{path}:{line_number}: invalid side or duplicate leg')
        start = datetime.fromisoformat(parts[3]) if len(parts) >= 4 else None
        if start is not None and start.tzinfo is None:
            raise ValueError(f'{path}:{line_number}: start needs a UTC offset')
        if len(parts) == 5 and parts[4].lower() not in ('true', 'false'):
            raise ValueError(f'{path}:{line_number}: estimated must be true or false')
        overrides[name.casefold()] = Override(ticker, side, start, len(parts) == 5 and parts[4].lower() == 'true')
    return overrides


def event_of(ticker: str) -> str:
    """Kalshi event tickers are the first two parts, even for props like KXNFLREC-EXAMPLE-PLAYER-2."""
    return '-'.join(ticker.split('-')[:2])


def pinned_events(overrides: dict[str, Override]) -> tuple[list[dict], dict[str, dict]]:
    events = []
    markets = {}
    limiter = clients.KalshiRateLimiter()
    event_tickers = {event_of(item.ticker) for item in overrides.values()}
    for event_ticker in sorted(event_tickers):
        payload = clients._kalshi_get(f'{API}/events/{event_ticker}?with_nested_markets=true', limiter)
        event = payload['event']
        events.append(event)
        markets.update((market['ticker'], market) for market in event.get('markets', []))
    missing = {item.ticker for item in overrides.values()} - markets.keys()
    if missing:
        raise ValueError('Pinned Kalshi tickers missing: ' + ', '.join(sorted(missing)))
    return events, markets


def matchup_names(event_title: str) -> tuple[str, str] | None:
    parts = re.split(r'\s+(?:vs\.?|versus|at|@)\s+', event_title, maxsplit=1, flags=re.I)
    return (parts[0], parts[1].split(':', 1)[0]) if len(parts) == 2 else None


def normalized_name(value: str) -> str:
    words = re.findall(r'[a-z0-9]+', value.casefold())
    aliases = {'st': 'state', 'fl': 'florida', 'oh': 'ohio'}
    return ' '.join(aliases.get(word, word) for word in words)


def same_matchup(pinned_title: str, sportsbook_title: str) -> bool:
    names = matchup_names(pinned_title)
    if not names:
        return False
    event = f' {normalized_name(sportsbook_title)} '
    return all(f' {normalized_name(name)} ' in event for name in names)


def selected_quote(quote, quotes, side):
    if quote is None or side == 'yes':
        return quote
    siblings = [item for item in quotes if item.identifier.rsplit(':', 1)[0] == quote.identifier.rsplit(':', 1)[0]]
    others = [item for item in siblings if item.identifier != quote.identifier]
    return others[0] if len(siblings) == 2 and len(others) == 1 else None


def build(day: date, slips: list[Slip], overrides: dict[str, Override]) -> tuple[dict, dict]:
    names = unique_legs([SlateSlip(s.name, float(s.stake), s.legs) for s in slips])
    canonical = {name.casefold(): name for name in names}
    unknown = set(overrides) - {name.casefold() for name in names}
    if unknown:
        raise ValueError('Ticker overrides without slip legs: ' + ', '.join(sorted(unknown)))
    by_source, warnings = fetch_quotes(names, include_kalshi=len(overrides) < len(names))
    pinned, pinned_markets = pinned_events(overrides) if overrides else ([], {})
    pinned_titles = {event['event_ticker']: event.get('title', '') for event in pinned}
    by_source['Kalshi'].extend(kalshi_quotes(pinned))
    resolved, notes = resolve(names, by_source, [SlateSlip(s.name, float(s.stake), s.legs) for s in slips])
    items = []
    for name in names:
        row = resolved[name]
        override = overrides.get(name.casefold())
        if override:
            ticker, side = override.ticker, override.side
            kalshi = next((quote for quote in by_source['Kalshi'] if quote.identifier == ticker), None)
            if kalshi is None:
                raise ValueError(f'Pinned market has no quote: {ticker}')
        else:
            kalshi = row['Kalshi']
            side = 'yes'
            if not kalshi or row.get('ambiguous'):
                raise ValueError(f'Kalshi match missing or ambiguous for {name}. Add a ticker override.')
            ticker = kalshi.identifier
        pinnacle, bovada = row['Pinnacle'], row['Bovada']
        if override and side == 'no':
            for source in ('Pinnacle', 'Bovada'):
                if (pinnacle if source == 'Pinnacle' else bovada) is None:
                    candidates = [quote for quote in by_source[source]
                                  if sport_family(quote.sport) == sport_family(kalshi.sport)]
                    replacement, _ = best_quote(kalshi.outcome, candidates)
                    if source == 'Pinnacle':
                        pinnacle = replacement
                    else:
                        bovada = replacement
        def valid_start(quote):
            return (quote is not None and quote.start is not None
                    and sport_family(quote.sport) == sport_family(kalshi.sport)
                    and same_matchup(kalshi.event, quote.event))

        # Any line from the same game can supply the start time, but only the leg's own matched
        # line may supply a price. Otherwise a prop picks up the moneyline as its fair.
        def priced(matched, source):
            if valid_start(matched):
                return matched
            same_game = [quote for quote in by_source[source] if valid_start(quote)]
            return best_quote(kalshi.outcome, same_game)[0] if same_game else None

        priced_pinnacle, priced_bovada = priced(pinnacle, 'Pinnacle'), priced(bovada, 'Bovada')
        if not valid_start(pinnacle):
            pinnacle = next((quote for quote in by_source['Pinnacle'] if valid_start(quote)), None)
        if not valid_start(bovada):
            bovada = next((quote for quote in by_source['Bovada'] if valid_start(quote)), None)
        if override and override.fallback_start:
            start, estimated = override.fallback_start, override.fallback_estimated
            start_source = 'estimated' if estimated else 'ticker file'
        else:
            start, estimated = actual_start(pinnacle, bovada, kalshi)
            start_source = ('Pinnacle' if pinnacle and pinnacle.start else 'Bovada' if bovada and bovada.start else 'estimated')
        if start is None:
            raise ValueError(f'No start time for {name}')
        # Soccer has a draw, so the other side of a two-way sportsbook line is not this leg's "No".
        three_way = sport_family(kalshi.sport) == 'soccer'
        selected_pinnacle = selected_quote(priced_pinnacle, by_source['Pinnacle'], side)
        selected_bovada = None if three_way and side == 'no' else selected_quote(priced_bovada, by_source['Bovada'], side)
        fair = (selected_pinnacle.probability if selected_pinnacle and selected_pinnacle.probability is not None
                else 1 - priced_pinnacle.probability if side == 'no' and priced_pinnacle and priced_pinnacle.probability is not None
                else None)
        pinnacle_raw = implied_probability(selected_pinnacle.odds) if selected_pinnacle and selected_pinnacle.odds else None
        bovada_odds = selected_bovada.odds if selected_bovada else None
        bovada_implied = implied_probability(bovada_odds) if bovada_odds else None
        items.append({'name': name, 'ticker': ticker, 'side': side,
                      'eventTicker': event_of(ticker),
                      'eventTitle': pinned_titles.get(event_of(ticker), kalshi.event),
                      'start': start.astimezone(timezone.utc).isoformat(), 'estimated': estimated,
                      'startSource': start_source,
                      'bovadaOdds': bovada_odds, 'bovadaImplied': bovada_implied,
                      'pinnacleRaw': pinnacle_raw, 'fair': fair,
                      'fairSource': 'Pinnacle no-vig' if fair is not None else None,
                      'icon': sport_icon(ticker)})
    for message in warnings + notes:
        print(message)
    config = {'date': day.isoformat(), 'slips': [
        {'name': s.name, 'stake': str(s.stake), 'payout': str(s.payout) if s.payout is not None else None,
         'legs': [canonical[leg.casefold()] for leg in s.legs],
         **({'placed': s.placed.astimezone(timezone.utc).isoformat()} if s.placed else {})} for s in slips
    ], 'legs': items}
    return config, pinned_markets


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('date', type=date.fromisoformat, help='YYYY-MM-DD')
    args = parser.parse_args()
    source = ROOT / 'data' / 'slips' / f'{args.date}.txt'
    slips = read_slips(source)
    cashouts = read_cashouts(source.with_suffix('.cashouts'), slips)
    config, markets = build(args.date, slips, read_overrides(source.with_suffix('.tickers')))
    config['cashouts'] = cashouts
    lists = read_lists(ROOT / 'data' / 'lists.json')
    if lists:
        config['lists'] = lists
    target = ROOT / 'schedule' / f'{args.date}.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2) + '\n')
    page = (ROOT / 'live' / 'page.html').read_text()
    local_data = {'config': config, 'markets': markets, 'fetchedAt': int(datetime.now(timezone.utc).timestamp() * 1000) if markets else None, 'stale': not markets}
    page = page.replace('/* LOCAL_DATA */ null', json.dumps(local_data).replace('<', '\\u003c'))
    html_target = target.with_suffix('.html')
    html_target.write_text(page)
    print(f'Wrote {target} and {html_target} with {len(config["legs"])} legs and {len(config["slips"])} slips')


if __name__ == '__main__':
    main()
