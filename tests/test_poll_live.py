import importlib.util
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location('poll_live', Path(__file__).resolve().parents[1] / 'scripts' / 'poll_live.py')
poll_live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poll_live)


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


class FakeHTTP:
    def __init__(self, config, snapshot=None, fail_second=False, pinnacle=None, pinnacle_error=None):
        self.values = {'config:current': config}
        if snapshot is not None:
            self.values['snapshot:current'] = snapshot
        self.fail_second = fail_second
        self.batches = []
        self.failures = 0
        self.pinnacle = pinnacle or {}
        self.pinnacle_error = pinnacle_error
        self.pinnacle_calls = []
        self.market_payloads = {}

    def __call__(self, request, timeout):
        url = urlparse(request.full_url)
        if url.hostname == 'external-api.kalshi.com':
            tickers = parse_qs(url.query)['tickers'][0].split(',')
            self.batches.append(tickers)
            if self.fail_second and tickers[0] == 'T100':
                self.failures += 1
                raise HTTPError(request.full_url, 429, 'rate limited', {}, None)
            return Response({'markets': [self.market_payloads.get(ticker, {'ticker': ticker, 'result': 'yes'}) for ticker in tickers]})
        if url.hostname == 'guest.api.arcadia.pinnacle.com':
            sport = int(url.path.split('/')[3])
            self.pinnacle_calls.append((sport, request.get_header('X-api-key')))
            if sport == self.pinnacle_error:
                raise HTTPError(request.full_url, 403, 'forbidden', {}, None)
            return Response(self.pinnacle.get(sport, []))
        key = url.path.rsplit('/', 1)[1].replace('%3A', ':')
        assert request.get_header('Authorization') == 'Bearer private-token'
        if request.get_method() == 'GET':
            if key not in self.values:
                raise HTTPError(request.full_url, 404, 'missing', {}, None)
            return Response(self.values[key])
        if request.get_method() == 'PUT':
            self.values[key] = json.loads(request.data)
        else:
            self.values.pop(key, None)
        return Response({'success': True})


def test_poll_merges_markets_and_keeps_each_fetch_time():
    config = {'date': '2026-10-17', 'legs': [{'ticker': 'T1'}, {'ticker': 'T2'}, {'ticker': 'T1'}]}
    previous = {'date': config['date'], 'fetchedAt': 100, 'markets': {
        'T1': {'ticker': 'T1', 'result': 'old'},
        'T3': {'ticker': 'T3', 'result': 'old', 'fetchedAt': 200},
    }}
    http = FakeHTTP(config, previous)
    with patch.object(poll_live, 'urlopen', http):
        poll_live.poll(poll_live.KV('private-token', 'account', 'namespace'), now=lambda: 500)
    assert http.batches == [['T1', 'T2']]
    markets = http.values['snapshot:current']['markets']
    assert markets['T1']['fetchedAt'] == 500
    assert markets['T2']['fetchedAt'] == 500
    assert markets['T3'] == {'ticker': 'T3', 'result': 'old', 'fetchedAt': 200}
    assert 'poll:error' not in http.values


def test_second_batch_failure_keeps_first_batch_and_old_data():
    config = {'date': '2026-10-17', 'legs': [{'ticker': f'T{i}'} for i in range(101)]}
    previous = {'date': config['date'], 'markets': {'OLD': {'ticker': 'OLD', 'fetchedAt': 100}}}
    http = FakeHTTP(config, previous, fail_second=True)
    with patch.object(poll_live, 'urlopen', http), patch.object(poll_live.time, 'sleep', lambda _: None):
        with pytest.raises(poll_live.PollError, match='Kalshi HTTP 429'):
            poll_live.poll(poll_live.KV('private-token', 'account', 'namespace'), now=lambda: 500)
    assert len(http.batches[0]) == 100
    assert len(http.batches) == 6
    assert len(http.values['snapshot:current']['markets']) == 101
    assert http.values['snapshot:current']['markets']['OLD']['fetchedAt'] == 100
    assert http.values['poll:error']['at'] == 500


def test_new_date_starts_new_snapshot_without_prior_day_markets():
    config = {'date': '2026-10-18', 'legs': [{'ticker': 'T1'}]}
    previous = {'date': '2026-10-17', 'markets': {'OLD': {'ticker': 'OLD', 'fetchedAt': 100}}}
    http = FakeHTTP(config, previous)
    with patch.object(poll_live, 'urlopen', http):
        poll_live.poll(poll_live.KV('private-token', 'account', 'namespace'), now=lambda: 500)
    assert list(http.values['snapshot:current']['markets']) == ['T1']


def test_retry_after_is_honored_before_retrying_429():
    attempts = []
    delays = []

    def open_once_then_succeed(request, timeout):
        attempts.append(request.full_url)
        if len(attempts) == 1:
            raise HTTPError(request.full_url, 429, 'rate limited', {'Retry-After': '3'}, None)
        return Response({'markets': []})

    with patch.object(poll_live, 'urlopen', open_once_then_succeed):
        result = poll_live.request('https://external-api.kalshi.com/trade-api/v2/markets?tickers=T1',
                                   service='Kalshi', sleep=delays.append)
    assert json.loads(result) == {'markets': []}
    assert len(attempts) == 2
    assert delays == [3]


def test_pinnacle_status_requires_both_pinned_names_and_fetches_each_sport_once():
    config = {'date': '2026-10-17', 'legs': [
        {'name': 'South St.', 'ticker': 'T1', 'side': 'yes', 'eventTitle': 'North St. vs South St.'},
        {'name': 'Fighter A', 'ticker': 'T2', 'side': 'yes', 'eventTitle': 'Fighter A vs Fighter B'},
        {'name': 'Unmatched', 'ticker': 'T3', 'side': 'yes', 'eventTitle': 'Alpha vs Beta'},
    ]}
    wrong = {'participants': [{'name': 'North State'}, {'name': 'South'}],
             'isLive': True, 'startTime': '2026-10-17T17:00:00Z'}
    right = {'participants': [{'name': 'South State'}, {'name': 'North State'}],
             'isLive': False, 'startTime': '2026-10-17T19:00:00Z'}
    fight = {'participants': [{'name': 'Fighter B'}, {'name': 'Fighter A'}],
             'isLive': True, 'startTime': '2026-10-17T20:00:00Z'}
    http = FakeHTTP(config, pinnacle={22: [wrong, right], 6: [fight]})
    with patch.object(poll_live, 'urlopen', http), patch.dict(poll_live.os.environ, {'PINNACLE_API_KEY': 'guest-key'}):
        poll_live.poll(poll_live.KV('private-token', 'account', 'namespace'), now=lambda: 500)
    assert http.pinnacle_calls == [(sport, 'guest-key') for sport in poll_live.PINNACLE_SPORTS]
    assert http.values['snapshot:current']['legStatus'] == {
        'South St.': {'live': False, 'start': '2026-10-17T19:00:00Z'},
        'Fighter A': {'live': True, 'start': '2026-10-17T20:00:00Z'},
        'Unmatched': {'live': False, 'start': None},
    }


def test_pinnacle_error_does_not_fail_poll_or_discard_previous_status():
    config = {'date': '2026-10-17', 'legs': [{'name': 'Fight', 'ticker': 'T1', 'side': 'yes',
                                            'eventTitle': 'Fighter A vs Fighter B'}]}
    previous = {'date': config['date'], 'markets': {},
                'legStatus': {'Fight': {'live': True, 'start': '2026-10-17T17:00:00Z'}}}
    http = FakeHTTP(config, previous, pinnacle_error=22)
    with patch.object(poll_live, 'urlopen', http), patch.dict(poll_live.os.environ, {'PINNACLE_API_KEY': 'guest-key'}):
        poll_live.poll(poll_live.KV('private-token', 'account', 'namespace'), now=lambda: 500)
    assert http.values['snapshot:current']['legStatus'] == previous['legStatus']
    assert 'poll:error' not in http.values


def test_malformed_pinnacle_matchup_does_not_fail_kalshi_poll():
    config = {'date': '2026-10-17', 'legs': [{'name': 'Fight', 'ticker': 'T1', 'side': 'yes',
                                            'eventTitle': 'Fighter A vs Fighter B'}]}
    http = FakeHTTP(config, pinnacle={22: [{'participants': 5}]})
    with patch.object(poll_live, 'urlopen', http), patch.dict(poll_live.os.environ, {'PINNACLE_API_KEY': 'guest-key'}):
        poll_live.poll(poll_live.KV('private-token', 'account', 'namespace'), now=lambda: 500)
    assert http.values['snapshot:current']['markets']['T1']['fetchedAt'] == 500
    assert 'poll:error' not in http.values


def test_first_seen_side_price_persists_for_day_and_resets_next_day():
    config = {'date': '2026-10-17', 'legs': [{'name': 'No leg', 'ticker': 'T1', 'side': 'no'}]}
    http = FakeHTTP(config)
    http.market_payloads['T1'] = {'ticker': 'T1', 'no_bid_dollars': '0.40', 'no_ask_dollars': '0.44'}
    with patch.object(poll_live, 'urlopen', http), patch.dict(poll_live.os.environ, {'PINNACLE_API_KEY': ''}):
        kv = poll_live.KV('private-token', 'account', 'namespace')
        poll_live.poll(kv, now=lambda: 500)
        assert http.values['snapshot:current']['firstPrices'] == {'No leg': pytest.approx(0.42)}
        http.market_payloads['T1'] = {'ticker': 'T1', 'no_bid_dollars': '0.55', 'no_ask_dollars': '0.59'}
        poll_live.poll(kv, now=lambda: 600)
        assert http.values['snapshot:current']['firstPrices']['No leg'] == pytest.approx(0.42)
        config['date'] = '2026-10-18'
        poll_live.poll(kv, now=lambda: 700)
    assert http.values['snapshot:current']['firstPrices']['No leg'] == pytest.approx(0.57)


def test_pinnacle_match_must_start_near_the_scheduled_leg():
    leg_start = '2026-10-19T00:20:00+00:00'
    tonight = {'startTime': '2026-10-19T00:20:00Z', 'participants': []}
    yesterday = {'startTime': '2026-10-18T00:15:00Z', 'participants': []}
    assert poll_live.near_start(tonight, leg_start)
    assert not poll_live.near_start(yesterday, leg_start)
    assert poll_live.near_start({'participants': []}, leg_start)


class DemoKV:
    def __init__(self, config=None):
        self.values = {'config:demo': config, 'snapshot:demo': {'date': '2026-10-21', 'markets': {}}} if config is not None else {}
        self.writes = []

    def get(self, key):
        return self.values.get(key)

    def put(self, key, value):
        self.writes.append((key, value))
        self.values[key] = value


def demo_config(as_of='old', slips=None):
    return {'asOf': as_of, 'date': '2026-10-21', 'slips': slips if slips is not None else
            [{'number': 1, 'legIds': ['T1'], 'stake': 25, 'placed': 'placed'}], 'legs': [{'id': 'T1'}]}


def test_demo_build_is_throttled_for_30_minutes():
    kv = DemoKV(demo_config())
    builds = []

    def build(attempted_at):
        builds.append(attempted_at)
        return demo_config('new')

    with patch.object(poll_live, 'build_public_demo', build), patch.object(poll_live, 'fetch_demo_markets', return_value={}):
        poll_live.refresh_public_demo(kv, now=lambda: 0)
        poll_live.refresh_public_demo(kv, now=lambda: 29 * 60 * 1000)
        poll_live.refresh_public_demo(kv, now=lambda: 30 * 60 * 1000)
    assert len(builds) == 2
    assert [key for key, _ in kv.writes] == ['demo:last_attempt', 'demo:last_attempt']


def test_unchanged_demo_does_not_write_config_even_with_new_as_of():
    old = demo_config()
    kv = DemoKV(old)
    with patch.object(poll_live, 'build_public_demo', return_value=demo_config('new')), patch.object(poll_live, 'fetch_demo_markets', return_value={}):
        poll_live.refresh_public_demo(kv, now=lambda: 0)
    assert kv.values['config:demo'] is old
    assert [key for key, _ in kv.writes] == ['demo:last_attempt']


def test_changed_demo_writes_only_demo_config():
    kv = DemoKV(demo_config())
    changed = demo_config('new', slips=[{'number': 1, 'legIds': ['T1'], 'stake': 50, 'placed': 'placed'}])
    with patch.object(poll_live, 'build_public_demo', return_value=changed), patch.object(poll_live, 'fetch_demo_markets', return_value={}):
        poll_live.refresh_public_demo(kv, now=lambda: 0)
    assert kv.values['config:demo'] is changed
    assert [key for key, _ in kv.writes] == ['demo:last_attempt', 'config:demo']


@pytest.mark.parametrize('result', [ValueError('No qualifying slips'), demo_config(slips=[])])
def test_failed_or_empty_demo_keeps_old_config_and_does_not_break_poll(result, capsys):
    old = demo_config()
    kv = DemoKV(old)

    def build(attempted_at):
        if isinstance(result, Exception):
            raise result
        return result

    with patch.object(poll_live, 'build_public_demo', build):
        poll_live.refresh_public_demo(kv, now=lambda: 0)
    assert kv.values['config:demo'] is old
    assert [key for key, _ in kv.writes] == ['demo:last_attempt']
    assert 'Demo refresh failed' in capsys.readouterr().err


def test_main_completes_private_poll_when_demo_build_fails(capsys):
    kv = DemoKV(demo_config())
    private_polls = []
    credentials = {'CF_API_TOKEN': 'token', 'CF_ACCOUNT_ID': 'account',
                   'CF_KV_NAMESPACE_ID': 'namespace'}
    with patch.dict(poll_live.os.environ, credentials), \
            patch.object(poll_live, 'KV', return_value=kv), \
            patch.object(poll_live, 'poll', side_effect=lambda client: private_polls.append(client)), \
            patch.object(poll_live, 'build_public_demo', side_effect=ValueError('No qualifying slips')):
        poll_live.main()
    assert private_polls == [kv]
    assert kv.values['config:demo']['asOf'] == 'old'
    assert 'Live poll completed' in capsys.readouterr().out


def test_demo_snapshot_keeps_results_when_quote_disappears():
    config = demo_config()
    previous = {'date': config['date'], 'markets': {'T1': {'result': 'yes', 'fetchedAt': 10}}}
    snapshot = poll_live.demo_snapshot(config, previous, {}, 20)
    assert snapshot['markets']['T1']['result'] == 'yes'
    assert poll_live.demo_finished(config, snapshot)
    assert not poll_live.demo_finished(config, {'date': config['date'], 'markets': {}})


def test_demo_refresh_updates_quotes_even_when_builder_fails(capsys):
    kv = DemoKV(demo_config())
    market = {'ticker': 'T1', 'yes_bid_dollars': '0.95', 'yes_ask_dollars': '0.96',
              'result': 'yes', 'settlement_ts': '2026-10-21T23:00:00Z'}
    with patch.object(poll_live, 'fetch_demo_markets', return_value={'T1': market}), \
            patch.object(poll_live, 'build_public_demo', side_effect=ValueError('No active markets')):
        poll_live.refresh_public_demo(kv, now=lambda: 1234)
    assert kv.values['snapshot:demo']['markets']['T1']['result'] == 'yes'
    assert kv.values['config:demo']['asOf'] == 'old'
    assert 'Demo refresh failed' in capsys.readouterr().err


def test_finished_demo_archives_only_demo_keys_on_new_day():
    old = demo_config()
    kv = DemoKV(old)
    kv.values['snapshot:demo'] = {'date': old['date'], 'markets': {'T1': {'result': 'yes'}}}
    next_day = demo_config('new')
    next_day['date'] = '2026-10-22'
    with patch.object(poll_live, 'fetch_demo_markets', return_value={}), \
            patch.object(poll_live, 'build_public_demo', return_value=next_day):
        poll_live.refresh_public_demo(kv, now=lambda: 1234)
    assert kv.values['days:demo'] == ['2026-10-21']
    assert kv.values['day:demo:2026-10-21']['snapshot']['markets']['T1']['result'] == 'yes'
    assert kv.values['config:demo']['date'] == '2026-10-22'
    assert all(key.startswith(('demo:', 'snapshot:demo', 'day:demo:', 'days:demo', 'config:demo')) for key, _ in kv.writes)
