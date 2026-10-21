import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fairmark.markets import Quote
from fairmark.pricing import SimulatedSlip, simulate

spec = importlib.util.spec_from_file_location('build_demo', Path(__file__).resolve().parents[1] / 'scripts' / 'build_demo.py')
build_demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_demo)

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
TNF = datetime(2026, 10, 23, 0, 15, tzinfo=timezone.utc)
SAT = datetime(2026, 10, 24, 23, tzinfo=timezone.utc)
FRI = datetime(2026, 10, 24, 2, tzinfo=timezone.utc)


def source_data(friday=False):
    rows = [
        (0, .77, 'New England Patriots vs Chicago Bears', TNF),
        (1, .83, 'New England Patriots vs Chicago Bears', TNF),
        (4, .72, 'New England Patriots vs Chicago Bears', TNF),
        (10, .82, 'Oklahoma State vs Colorado', SAT),
        (13, .82, 'Asaf Chopurov vs Colby Thicknesse', SAT),
        (16, .82, 'Abdul Rakhman Yakhyaev vs Nikita Krylov', SAT),
    ]
    events = [{'category': 'Sports', 'series_ticker': ticker.split('-')[0], 'title': event,
               'markets': [{'ticker': ticker, 'title': name, 'yes_sub_title': name,
                            'yes_ask_dollars': str(ask), 'status': 'active',
                            'occurrence_datetime': start.isoformat()}]}
              for index, ask, event, start in rows
              for ticker, name, *_ in [build_demo.TARGETS[index]]]
    prices = [
        Quote('Pinnacle', 'Over 45.5 Rome Odunze Total Receiving Yards', 'Rome Odunze Total Receiving Yards',
              'Chicago Bears vs New England Patriots', 'Football', TNF, 'prop', 'odunze', probability=.502),
        Quote('Pinnacle', 'Over 4.5 Luther Burden Total Receptions', 'Luther Burden Total Receptions',
              'Chicago Bears vs New England Patriots', 'Football', TNF, 'prop', 'burden', probability=.531),
        Quote('Pinnacle', 'Over 43.5 Rhamondre Stevenson Total Rushing Yards',
              'Rhamondre Stevenson Total Rushing Yards', 'Chicago Bears vs New England Patriots',
              'Football', TNF, 'prop', 'stevenson', probability=.502),
    ]
    for name, event, fair in [('Oklahoma State', 'Colorado vs Oklahoma State', .8077),
                              ('Asaf Chopurov', 'Colby Thicknesse vs Asaf Chopurov', .8127),
                              ('Abdul Rakhman Yakhyaev', 'Nikita Krylov vs Abdul Rakhman Yakhyaev', .8078)]:
        prices.append(Quote('Pinnacle', name, 'moneyline', event,
                            'Mixed Martial Arts' if name != 'Oklahoma State' else 'Football',
                            SAT, 'winner', name, probability=fair))
    if friday:
        events.append({'category': 'Sports', 'series_ticker': 'KXNBAGAME', 'title': 'Toronto vs Washington',
                       'markets': [{'ticker': 'KXNBAGAME-26OCT23TORWAS-TOR', 'title': 'Toronto wins',
                                    'yes_sub_title': 'Toronto', 'yes_ask_dollars': '.70', 'status': 'active',
                                    'occurrence_datetime': FRI.isoformat()}]})
        prices.append(Quote('Pinnacle', 'Toronto Raptors', 'moneyline',
                            'Toronto Raptors vs Washington Wizards', 'Basketball', FRI,
                            'winner', 'toronto', probability=.75))
    return events, prices


def test_builder_uses_named_slate_and_shared_leg_once():
    events, prices = source_data()
    config = build_demo.build_demo(events, prices, now=NOW, trials=10_000, seed=17)
    assert len(config['slips']) == 3
    assert [len(slip['legIds']) for slip in config['slips']] == [2, 2, 2]
    assert [slip['stake'] for slip in config['slips']] == [25, 25, 100]
    assert config['slips'][0]['legIds'][0] == config['slips'][2]['legIds'][0]
    assert config['slateDate'] == '2026-10-22'
    assert config['slateEndDate'] == '2026-10-24'
    assert config['missingMarkets'] == [row[1] for row in build_demo.TARGETS if row[0] not in
                                         {market['ticker'] for event in events for market in event['markets']}]
    assert all(leg['fair'] >= .60 for slip in config['slips'] for leg in config['legs']
               if leg['id'] in slip['legIds'])
    assert config['portfolio']['dayProfitChance'] > .55
    assert config['portfolio']['medianGain'] > 0
    assert all(leg['underFair'] == pytest.approx((leg['fair'] - leg['ask'] - leg['fee']) / leg['fair'])
               for leg in config['legs'])
    simulated = [SimulatedSlip(tuple(slip['legIds']), slip['stake'], slip['totalPayout'])
                 for slip in config['slips']]
    expected = simulate(simulated, {leg['id']: leg['fair'] for leg in config['legs']},
                        trials=10_000, seed=17)
    assert config['portfolio']['dayProfitChance'] == expected.any_profit
    assert config == build_demo.build_demo(events, prices, now=NOW, trials=10_000, seed=17)


def test_friday_favorite_requires_price_at_or_below_fair():
    events, prices = source_data(friday=True)
    config = build_demo.build_demo(events, prices, now=NOW, trials=1000)
    assert config['slips'][1]['legIds'][1] == 'KXNBAGAME-26OCT23TORWAS-TOR'
    events[-1]['markets'][0]['yes_ask_dollars'] = '.76'
    config = build_demo.build_demo(events, prices, now=NOW, trials=1000)
    assert all('TORWAS' not in leg['id'] for leg in config['legs'])
    assert config['slips'][1]['legIds'][1] == build_demo.TARGETS[10][0]


def test_missing_market_is_skipped_and_slip_not_fabricated():
    events, prices = source_data()
    events[-1]['markets'][0]['status'] = 'settled'
    config = build_demo.build_demo(events, prices, now=NOW, trials=1000)
    assert len(config['slips']) == 2
    assert build_demo.TARGETS[16][1] in config['missingMarkets']
    for event in events:
        for market in event['markets']:
            market['status'] = 'settled'
    with pytest.raises(ValueError, match='No qualifying slips'):
        build_demo.build_demo(events, prices, now=NOW)


def test_fee_and_derived_prop_fair():
    assert build_demo.fee(.48) == .0175
    assert build_demo.fee(.10) == .0063
    _, prices = source_data()
    assert build_demo.derived_prop_fair(prices, 'Rome Odunze', 'Total Receiving Yards', 25) == pytest.approx(.8487, abs=.0001)
    assert build_demo.derived_prop_fair(prices, 'Luther Burden', 'Total Receptions', 3) == pytest.approx(.8612, abs=.0001)


def test_refresh_writes_review_file_and_only_optional_demo_key(monkeypatch, tmp_path):
    events, prices = source_data()
    monkeypatch.setattr(build_demo.clients, '_kalshi_series_events',
                        lambda series, limiter: [event for event in events if event['series_ticker'] == series])
    monkeypatch.setattr(build_demo.clients, 'pinnacle_sport', lambda sport: (sport, None))
    monkeypatch.setattr(build_demo, 'pinnacle_quotes', lambda sport, ignored: prices if sport == 15 else [])
    output = tmp_path / 'demo.json'
    config = build_demo.refresh_demo(output=output, now=NOW)
    assert json.loads(output.read_text()) == config
    writes = []
    kv = type('KV', (), {'put': lambda self, key, value: writes.append((key, value))})()
    build_demo.refresh_demo(kv, output=output, now=NOW)
    assert writes == [('config:demo', config)]
