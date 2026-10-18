import importlib.util
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

script = Path(__file__).resolve().parents[1] / 'scripts' / 'schedule.py'
spec = importlib.util.spec_from_file_location('schedule_script', script)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
read_overrides = module.read_overrides
read_slips = module.read_slips
read_cashouts = module.read_cashouts

from fairmark.markets import Quote


def test_schedule_accepts_optional_confirmed_payout_and_pinned_fallback(tmp_path):
    slips = tmp_path / 'slips.txt'
    slips.write_text('slip Alpha 10 18.50: Team A, Team B\nslip Beta 5: Team B\n')
    parsed = read_slips(slips)
    assert [slip.payout for slip in parsed] == [Decimal('18.50'), None]
    assert parsed[0].legs == ('Team A', 'Team B')

    overrides = tmp_path / 'slips.tickers'
    overrides.write_text('Team A | KXGAME-EVENT-A | no | 2026-10-17T10:00:00-07:00 | false\n')
    pinned = read_overrides(overrides)
    assert pinned['team a'].ticker == 'KXGAME-EVENT-A'
    assert pinned['team a'].side == 'no'
    assert pinned['team a'].fallback_start == datetime.fromisoformat('2026-10-17T10:00:00-07:00')
    assert pinned['team a'].fallback_estimated is False


def test_sportsbook_match_requires_both_complete_pinned_teams():
    pinned = 'North St. vs South St.'
    assert module.same_matchup(pinned, 'South State Beavers vs North State Cougars')
    assert module.same_matchup(pinned, 'North St. vs South St.')
    assert module.same_matchup('Fighter A vs Fighter B: Method of Victory', 'Fighter B vs Fighter A')
    assert not module.same_matchup(pinned, 'West State vs South City')
    assert not module.same_matchup(pinned, 'North State vs South')


def test_ticker_time_wins_and_unmatched_sportsbook_start_is_ignored(monkeypatch):
    ticker = 'KXNCAAFGAME-EVENT-SOUTH'
    kalshi = Quote('Kalshi', 'South St.', 'winner', 'North St. vs South St.', 'KXNCAAFGAME', datetime.fromisoformat('2026-10-18T02:00:00+00:00'), 'winner', ticker)
    wrong = Quote('Pinnacle', 'South', 'moneyline', 'West State vs South', 'football', datetime.fromisoformat('2026-10-18T03:00:00+00:00'), 'winner', 'wrong')
    right = Quote('Bovada', 'South State', 'moneyline', 'North State vs South State', 'football', datetime.fromisoformat('2026-10-17T22:00:00+00:00'), 'winner', 'right')
    quotes = {'Kalshi': [], 'Pinnacle': [], 'Bovada': []}
    monkeypatch.setattr(module, 'fetch_quotes', lambda *args, **kwargs: (quotes, []))
    monkeypatch.setattr(module, 'pinned_events', lambda *args: ([{'event_ticker': ticker,
        'title': 'North St. vs South St.'}], {}))
    monkeypatch.setattr(module, 'kalshi_quotes', lambda *args: [kalshi])
    matches = {'Pinnacle': wrong, 'Bovada': right, 'Kalshi': kalshi}
    monkeypatch.setattr(module, 'resolve', lambda *args: ({'South St.': matches}, []))
    slips = [module.Slip('sample', Decimal('10'), None, ('South St.',))]
    pinned_time = datetime.fromisoformat('2026-10-17T15:00:00-07:00')
    overrides = {'south st.': module.Override(ticker, 'yes', pinned_time, False)}
    leg = module.build(date(2026, 10, 17), slips, overrides)[0]['legs'][0]
    assert leg['start'] == pinned_time.astimezone(module.timezone.utc).isoformat()
    assert leg['eventTitle'] == 'North St. vs South St.'
    assert leg['startSource'] == 'ticker file'
    assert leg['estimated'] is False

    leg = module.build(date(2026, 10, 17), slips, {'south st.': module.Override(ticker, 'yes')})[0]['legs'][0]
    assert leg['start'] == right.start.isoformat()
    assert leg['startSource'] == 'Bovada'

    quotes['Pinnacle'].append(Quote('Pinnacle', 'South State', 'moneyline', 'South State vs North State', 'football', datetime.fromisoformat('2026-10-17T21:00:00+00:00'), 'winner', 'other'))
    leg = module.build(date(2026, 10, 17), slips, {'south st.': module.Override(ticker, 'yes')})[0]['legs'][0]
    assert leg['startSource'] == 'Pinnacle'
    assert leg['start'] == quotes['Pinnacle'][0].start.isoformat()

    quotes['Pinnacle'].clear()
    matches['Bovada'] = None
    leg = module.build(date(2026, 10, 17), slips, {'south st.': module.Override(ticker, 'yes')})[0]['legs'][0]
    assert leg['estimated'] is True
    assert leg['startSource'] == 'estimated'

    overrides['south st.'] = module.Override(ticker, 'yes', pinned_time, True)
    leg = module.build(date(2026, 10, 17), slips, overrides)[0]['legs'][0]
    assert leg['startSource'] == 'estimated'


def test_push_rejects_schedule_without_start_provenance(tmp_path):
    push_path = script.with_name('push_slips.py')
    push_spec = importlib.util.spec_from_file_location('push_slips_script', push_path)
    push = importlib.util.module_from_spec(push_spec)
    push_spec.loader.exec_module(push)
    payload = tmp_path / 'schedule.json'
    payload.write_text(json.dumps({'legs': [{'start': '2026-10-17T22:00:00+00:00', 'startSource': 'ticker file'}], 'cashouts': {}}))
    push.validate_schedule(payload)
    payload.write_text(json.dumps({'legs': [{'start': '2026-10-17T22:00:00+00:00'}]}))
    import pytest
    with pytest.raises(ValueError, match='resolved start source'):
        push.validate_schedule(payload)


def test_cashouts_parse_full_and_partial_and_reject_bad_entries(tmp_path):
    import pytest
    slips = [module.Slip('alpha', Decimal('12'), Decimal('36'), ('Team A',)),
             module.Slip('beta', Decimal('20'), Decimal('45'), ('Team B',))]
    path = tmp_path / 'slips.cashouts'
    assert read_cashouts(path, slips) == {}
    path.write_text('# cash-outs\nalpha 2.50\nbeta 1.25 0.5\n')
    assert read_cashouts(path, slips) == {
        'alpha': {'amount': '2.50', 'remainingFraction': '0'},
        'beta': {'amount': '1.25', 'remainingFraction': '0.5'}
    }
    for invalid in ('missing 1.00', 'alpha -1', 'alpha 1 1', 'alpha 1 -0.1', 'alpha nope', 'alpha 1\nalpha 2'):
        path.write_text(invalid + '\n')
        with pytest.raises(ValueError):
            read_cashouts(path, slips)


def test_push_requires_cashouts_in_uploaded_config(tmp_path):
    import pytest
    push_path = script.with_name('push_slips.py')
    push_spec = importlib.util.spec_from_file_location('push_slips_script_cashouts', push_path)
    push = importlib.util.module_from_spec(push_spec)
    push_spec.loader.exec_module(push)
    payload = tmp_path / 'schedule.json'
    payload.write_text(json.dumps({'legs': [{'start': '2026-10-17T22:00:00+00:00', 'startSource': 'ticker file'}]}))
    with pytest.raises(ValueError, match='cash-outs'):
        push.validate_schedule(payload)


def test_generated_schedule_contains_cashouts_for_upload(tmp_path, monkeypatch):
    slips_dir = tmp_path / 'data' / 'slips'
    slips_dir.mkdir(parents=True)
    (slips_dir / '2026-10-17.txt').write_text('slip sample 12 36: Team A\n')
    (slips_dir / '2026-10-17.cashouts').write_text('sample 2.50\n')
    live_dir = tmp_path / 'live'
    live_dir.mkdir()
    (live_dir / 'page.html').write_text('/* LOCAL_DATA */ null')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'build', lambda day, slips, overrides: ({
        'date': day.isoformat(), 'slips': [{'name': 'sample', 'stake': '12', 'payout': '36', 'legs': ['Team A']}],
        'legs': [{'name': 'Team A', 'start': '2026-10-17T22:00:00+00:00', 'startSource': 'ticker file'}]
    }, {}))
    monkeypatch.setattr(sys, 'argv', ['schedule.py', '2026-10-17'])
    module.main()
    payload = tmp_path / 'schedule' / '2026-10-17.json'
    assert json.loads(payload.read_text())['cashouts'] == {'sample': {'amount': '2.50', 'remainingFraction': '0'}}
    assert 'remainingFraction' in payload.with_suffix('.html').read_text()

    push_path = script.with_name('push_slips.py')
    push_spec = importlib.util.spec_from_file_location('push_slips_script_generated', push_path)
    push = importlib.util.module_from_spec(push_spec)
    push_spec.loader.exec_module(push)
    push.validate_schedule(payload)


def test_placed_times_read_as_pacific_on_the_slip_day(tmp_path):
    import pytest
    slips = tmp_path / '2026-10-18.txt'
    slips.write_text('slip 1 12 36 @6:40a: Team A\nslip 2 8 @18:05: Team B\nslip 3 6 @10-17-11:50p: Team C\nslip 4 4: Team D\n')
    parsed = read_slips(slips)
    assert parsed[0].payout == Decimal('36')
    assert [slip.placed.isoformat() if slip.placed else None for slip in parsed] == [
        '2026-10-18T06:40:00-07:00', '2026-10-18T18:05:00-07:00', '2026-10-17T23:50:00-07:00', None]
    slips.write_text('slip 1 12 @13:40p: Team A\n')
    with pytest.raises(ValueError, match='placed time'):
        read_slips(slips)


def test_lists_are_optional_and_validated(tmp_path):
    import pytest
    assert module.read_lists(tmp_path / 'missing.json') == {}
    lists = tmp_path / 'lists.json'
    lists.write_text(json.dumps({'ban': [' Team A '], 'doghouse': ['Team B'], 'hateWatch': []}))
    assert module.read_lists(lists) == {'ban': ['Team A'], 'doghouse': ['Team B'], 'hateWatch': []}
    lists.write_text(json.dumps({'friends': ['Someone']}))
    with pytest.raises(ValueError):
        module.read_lists(lists)


def test_push_archives_the_previous_day_once(tmp_path):
    push_path = script.with_name('push_slips.py')
    push_spec = importlib.util.spec_from_file_location('push_slips_script_archive', push_path)
    push = importlib.util.module_from_spec(push_spec)
    push_spec.loader.exec_module(push)
    store = {'config:current': {'date': '2026-10-17', 'legs': []},
             'snapshot:current': {'date': '2026-10-17', 'markets': {'A': {'result': 'yes'}}},
             'days': ['2026-10-16']}
    writes = []
    put = lambda key, value: (writes.append(key), store.__setitem__(key, value))
    assert push.archive_previous('2026-10-18', store.get, put) == '2026-10-17'
    assert store['day:2026-10-17']['snapshot']['markets'] == {'A': {'result': 'yes'}}
    assert store['days'] == ['2026-10-16', '2026-10-17']
    assert push.archive_previous('2026-10-17', store.get, put) is None
    assert writes == ['day:2026-10-17', 'days']
    store['snapshot:current'] = {'date': '2026-10-16'}
    store['config:current'] = {'date': '2026-10-15'}
    push.archive_previous('2026-10-18', store.get, put)
    assert store['day:2026-10-15']['snapshot'] is None
