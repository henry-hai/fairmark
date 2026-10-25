#!/usr/bin/env python3
"""Poll selected Kalshi markets and store the live snapshot in Workers KV."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cashout_detection import update_cashouts

KALSHI_MARKETS = 'https://external-api.kalshi.com/trade-api/v2/markets'
PINNACLE = 'https://guest.api.arcadia.pinnacle.com/0.1'
PINNACLE_SPORTS = (22, 6, 12, 3, 4, 15, 19, 29)
CF_KV = 'https://api.cloudflare.com/client/v4/accounts/{account}/storage/kv/namespaces/{namespace}/values/{key}'
DEMO_INTERVAL_MS = 30 * 60 * 1000
PACIFIC = ZoneInfo('America/Los_Angeles')


class PollError(Exception):
    """An error safe to report without response bodies or credentials."""


def request(url, *, service, token=None, method='GET', body=None, sleep=None, extra_headers=None):
    if sleep is None:
        sleep = time.sleep
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    if extra_headers:
        headers.update(extra_headers)
    if body is not None:
        headers['Content-Type'] = 'application/octet-stream'
    for attempt in range(5):
        try:
            with urlopen(Request(url, data=body, headers=headers, method=method), timeout=20) as response:
                return response.read()
        except HTTPError as error:
            if error.code == 404 and method == 'GET' and service == 'Cloudflare':
                return None
            if (error.code != 429 and error.code < 500) or attempt == 4:
                raise PollError(f'{service} HTTP {error.code}') from None
            try:
                retry_after = max(0, min(60, float(error.headers.get('Retry-After', '0'))))
            except (TypeError, ValueError):
                retry_after = 0
            sleep(max(2 ** attempt, retry_after))
        except (URLError, TimeoutError):
            if attempt == 4:
                raise PollError(f'{service} network error') from None
            sleep(2 ** attempt)
    raise PollError(f'{service} request failed')


def parse_json(data, service):
    try:
        return json.loads(data)
    except (TypeError, ValueError):
        raise PollError(f'{service} returned invalid JSON') from None


class KV:
    def __init__(self, token, account, namespace):
        self.token = token
        self.base = CF_KV.format(account=quote(account, safe=''), namespace=quote(namespace, safe=''), key='{key}')

    def url(self, key):
        return self.base.format(key=quote(key, safe=''))

    def get(self, key):
        data = request(self.url(key), service='Cloudflare', token=self.token)
        return None if data is None else parse_json(data, 'Cloudflare')

    def put(self, key, value):
        body = json.dumps(value, separators=(',', ':')).encode('utf-8')
        data = request(self.url(key), service='Cloudflare', token=self.token, method='PUT', body=body)
        result = parse_json(data, 'Cloudflare')
        if not isinstance(result, dict) or not result.get('success'):
            raise PollError('Cloudflare write failed')

    def delete(self, key):
        data = request(self.url(key), service='Cloudflare', token=self.token, method='DELETE')
        result = parse_json(data, 'Cloudflare')
        if not isinstance(result, dict) or not result.get('success'):
            raise PollError('Cloudflare delete failed')


def quoted_price(market, side):
    try:
        bid = float(market[f'{side}_bid_dollars'])
        ask = float(market[f'{side}_ask_dollars'])
    except (KeyError, TypeError, ValueError):
        return None
    return (bid + ask) / 2 if 0 <= bid <= 1 and 0 <= ask <= 1 else None


def normalized_name(value):
    aliases = {'st': 'state', 'fl': 'florida', 'oh': 'ohio'}
    return ' '.join(aliases.get(word, word) for word in re.findall(r'[a-z0-9]+', value.casefold()))


def same_matchup(event_title, matchup):
    if not isinstance(event_title, str):
        return False
    names = re.split(r'\s+(?:vs\.?|versus|at|@)\s+', event_title.split(':', 1)[0], maxsplit=1, flags=re.I)
    if len(names) != 2:
        return False
    participants = (matchup.get('parent') or matchup).get('participants') or []
    matchup_names = ' '.join(participant.get('name') or '' for participant in participants if isinstance(participant, dict))
    normalized = f' {normalized_name(matchup_names)} '
    return all(f' {normalized_name(name)} ' in normalized for name in names)


def iso_start(value):
    if not isinstance(value, str):
        return None
    try:
        return value if datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo else None
    except ValueError:
        return None


def near_start(matchup, scheduled, hours=3):
    """City names repeat across sports and days, so a match must start within a few hours of the slip's own start."""
    start = iso_start(matchup.get('startTime') or (matchup.get('parent') or {}).get('startTime'))
    scheduled = iso_start(scheduled)
    if not start or not scheduled:
        return True
    gap = abs(datetime.fromisoformat(start.replace('Z', '+00:00')) - datetime.fromisoformat(scheduled.replace('Z', '+00:00')))
    return gap.total_seconds() <= hours * 3600


def pinnacle_status(config, api_key):
    if not api_key:
        return {leg['name']: {'live': False, 'start': None} for leg in config['legs'] if leg.get('name')}
    headers = {'X-API-Key': api_key, 'User-Agent': 'Mozilla/5.0',
               'Referer': 'https://www.pinnacle.com/'}
    matchups = []
    failed = False
    for sport in PINNACLE_SPORTS:
        try:
            data = request(f'{PINNACLE}/sports/{sport}/matchups', service='Pinnacle', extra_headers=headers)
            payload = parse_json(data, 'Pinnacle')
            if not isinstance(payload, list):
                raise PollError('Pinnacle returned invalid matchups')
            matchups.extend(item for item in payload if isinstance(item, dict))
        except PollError:
            failed = True
            continue
    status = {}
    for leg in config['legs']:
        if not leg.get('name'):
            continue
        title = leg.get('eventTitle', '')
        matches = [matchup for matchup in matchups if title and same_matchup(title, matchup)
                   and near_start(matchup, leg.get('start'))]
        if matches:
            match = next((item for item in matches if not item.get('parentId')), matches[0])
            start = iso_start(match.get('startTime') or (match.get('parent') or {}).get('startTime'))
            status[leg['name']] = {'live': any(bool(item.get('isLive') or (item.get('parent') or {}).get('isLive')) for item in matches),
                                   'start': start}
        elif not failed:
            status[leg['name']] = {'live': False, 'start': None}
    return status


def merge_snapshot(config, previous, fetched, fetched_at):
    if isinstance(previous, dict) and previous.get('date') == config['date']:
        markets = {ticker: {**market, 'fetchedAt': market.get('fetchedAt', previous.get('fetchedAt'))}
                   for ticker, market in (previous.get('markets') or {}).items()}
        first_prices = dict(previous.get('firstPrices') or {})
        leg_status = dict(previous.get('legStatus') or {})
    else:
        markets = {}
        first_prices = {}
        leg_status = {}
    for ticker, market in fetched.items():
        markets[ticker] = {**market, 'fetchedAt': fetched_at}
    for leg in config['legs']:
        if leg.get('name') not in first_prices and leg['ticker'] in fetched:
            price = quoted_price(fetched[leg['ticker']], leg.get('side', 'yes'))
            if price is not None:
                first_prices[leg['name']] = price
    return {'date': config['date'], 'markets': markets, 'firstPrices': first_prices,
            'legStatus': leg_status}


def poll(kv, *, now=lambda: int(time.time() * 1000)):
    config = kv.get('config:current')
    if not isinstance(config, dict) or not config.get('date') or not isinstance(config.get('legs'), list) or not config['legs']:
        raise PollError('No valid schedule config in KV')
    if any(not isinstance(leg, dict) or not isinstance(leg.get('ticker'), str) or not leg['ticker']
           for leg in config['legs']):
        raise PollError('Schedule config has invalid tickers')
    tickers = list(dict.fromkeys(leg['ticker'] for leg in config['legs']))
    try:
        update_cashouts(kv, config)
    except Exception as error:
        print(f'Cash-out detection failed: {type(error).__name__}: {error}', file=sys.stderr)
    snapshot = kv.get('snapshot:current')
    try:
        live_status = pinnacle_status(config, os.environ.get('PINNACLE_API_KEY'))
    except Exception:
        live_status = {}
    try:
        for offset in range(0, len(tickers), 100):
            batch = tickers[offset:offset + 100]
            url = KALSHI_MARKETS + '?' + urlencode({'tickers': ','.join(batch)})
            data = request(url, service='Kalshi')
            payload = parse_json(data, 'Kalshi')
            if not isinstance(payload, dict) or not isinstance(payload.get('markets'), list):
                raise PollError('Kalshi returned invalid markets')
            fetched = {market['ticker']: market for market in payload['markets']
                       if isinstance(market, dict) and market.get('ticker') in batch}
            if fetched:
                updated = merge_snapshot(config, snapshot, fetched, now())
                updated['legStatus'].update(live_status)
                if updated != snapshot:
                    kv.put('snapshot:current', updated)
                snapshot = updated
        if kv.get('poll:error') is not None:
            kv.delete('poll:error')
    except PollError as error:
        kv.put('poll:error', {'at': now(), 'message': str(error)})
        raise


def build_public_demo(attempted_at):
    from build_demo import refresh_demo
    return refresh_demo(now=datetime.fromtimestamp(attempted_at / 1000, timezone.utc))


def demo_date(config):
    if not isinstance(config, dict):
        return None
    if config.get('date'):
        return config['date']
    try:
        return datetime.fromisoformat(config['asOf'].replace('Z', '+00:00')).astimezone(PACIFIC).date().isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def demo_snapshot(config, previous, fetched, fetched_at):
    used = {ticker for slip in config['slips'] for ticker in slip['legIds']}
    old = previous.get('markets', {}) if isinstance(previous, dict) and previous.get('date') == demo_date(config) else {}
    markets = {ticker: market for ticker, market in old.items() if ticker in used}
    for ticker, market in fetched.items():
        if ticker in used:
            markets[ticker] = {key: market.get(key) for key in (
                'yes_bid_dollars', 'yes_ask_dollars', 'result', 'settlement_ts', 'close_time')}
            markets[ticker]['fetchedAt'] = fetched_at
    return {'date': demo_date(config), 'markets': markets}


def fetch_demo_markets(config):
    tickers = list(dict.fromkeys(ticker for slip in config['slips'] for ticker in slip['legIds']))
    fetched = {}
    for offset in range(0, len(tickers), 100):
        batch = tickers[offset:offset + 100]
        payload = parse_json(request(KALSHI_MARKETS + '?' + urlencode({'tickers': ','.join(batch)}),
                                     service='Kalshi'), 'Kalshi')
        if not isinstance(payload, dict) or not isinstance(payload.get('markets'), list):
            raise PollError('Kalshi returned invalid demo markets')
        fetched.update({market['ticker']: market for market in payload['markets']
                        if isinstance(market, dict) and market.get('ticker') in batch})
    return fetched


def demo_finished(config, snapshot):
    if not isinstance(config, dict) or not isinstance(snapshot, dict):
        return False
    markets = snapshot.get('markets') or {}
    return all(str((markets.get(ticker) or {}).get('result', '')).lower() in ('yes', 'no')
               for slip in config.get('slips', []) for ticker in slip.get('legIds', []))


def refresh_public_demo(kv, *, now=lambda: int(time.time() * 1000)):
    try:
        attempted_at = now()
        previous = kv.get('config:demo')
        previous_snapshot = kv.get('snapshot:demo')
        if isinstance(previous, dict) and previous.get('slips'):
            current_markets = fetch_demo_markets(previous)
            updated_snapshot = demo_snapshot(previous, previous_snapshot, current_markets, attempted_at)
            if updated_snapshot != previous_snapshot:
                kv.put('snapshot:demo', updated_snapshot)
                previous_snapshot = updated_snapshot
        # A locked showcase day keeps its slips for good. Its quotes still refresh above.
        if isinstance(previous, dict) and previous.get('locked') is True:
            return
        previous_attempt = kv.get('demo:last_attempt')
        previous_at = previous_attempt.get('at') if isinstance(previous_attempt, dict) else None
        if isinstance(previous_at, (int, float)):
            elapsed = attempted_at - previous_at
            if 0 <= elapsed < DEMO_INTERVAL_MS:
                return
        kv.put('demo:last_attempt', {'at': attempted_at})
        config = build_public_demo(attempted_at)
        slips = config.get('slips') if isinstance(config, dict) else None
        if not isinstance(slips, list) or not slips or any(
                not isinstance(slip, dict) or not isinstance(slip.get('legIds'), list) or
                not slip['legIds'] for slip in slips):
            raise ValueError('Demo build has no valid slips')
        config['date'] = demo_date(config)
        if not config['date']:
            raise ValueError('Demo build has no date')
        old_date = demo_date(previous)
        old_ids = [slip['legIds'] for slip in previous.get('slips', [])] if isinstance(previous, dict) else []
        new_ids = [slip['legIds'] for slip in slips]
        if previous and (old_date != config['date'] or old_ids != new_ids):
            if not demo_finished(previous, previous_snapshot):
                config = {**previous, 'date': old_date}
            else:
                if old_date and old_date != config['date']:
                    kv.put(f'day:demo:{old_date}', {'config': {**previous, 'date': old_date},
                                                    'snapshot': previous_snapshot})
                    days = kv.get('days:demo')
                    kv.put('days:demo', sorted(set((days if isinstance(days, list) else []) + [old_date])))
        if previous and old_date == config['date']:
            old_slips = {tuple(slip['legIds']): slip for slip in previous.get('slips', [])}
            for slip in config['slips']:
                old = old_slips.get(tuple(slip['legIds']))
                if old:
                    slip['placed'] = old.get('placed') or previous.get('asOf')
        snapshot = previous_snapshot if config is previous else None
        if not previous or demo_date(config) != old_date or [slip['legIds'] for slip in config['slips']] != old_ids:
            snapshot = demo_snapshot(config, None, fetch_demo_markets(config), attempted_at)
        elif snapshot is None:
            snapshot = demo_snapshot(config, previous_snapshot, {}, attempted_at)
        if snapshot != previous_snapshot:
            kv.put('snapshot:demo', snapshot)
        old_content = ({key: value for key, value in previous.items() if key != 'asOf'}
                       if isinstance(previous, dict) else None)
        new_content = {key: value for key, value in config.items() if key != 'asOf'}
        if new_content != old_content:
            kv.put('config:demo', config)
    except Exception as error:
        print(f'Demo refresh failed: {type(error).__name__}: {error}', file=sys.stderr)


def main():
    names = ('CF_API_TOKEN', 'CF_ACCOUNT_ID', 'CF_KV_NAMESPACE_ID')
    if any(not os.environ.get(name) for name in names):
        raise PollError('Cloudflare credentials are missing')
    kv = KV(*(os.environ[name] for name in names))
    poll(kv)
    refresh_public_demo(kv)
    print('Live poll completed')


if __name__ == '__main__':
    try:
        main()
    except PollError as error:
        print(f'Live poll failed: {error}', file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('Live poll failed unexpectedly', file=sys.stderr)
        sys.exit(1)
