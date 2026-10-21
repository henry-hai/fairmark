import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('poll_live_cashouts', SCRIPTS / 'poll_live.py')
poll_live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poll_live)
from cashout_detection import detect_cashouts, persist_cashouts, update_cashouts


CONFIG = {'date': '2026-11-14', 'slips': [
    {'name': 'sample', 'stake': '20.00', 'payout': '40', 'placed': '2026-11-14T12:00:00+00:00'}],
    'legs': [{'ticker': 'LEG'}], 'cashouts': {}}


def buy():
    return {'order_id': 'buy-1', 'ticker': 'COMBO', 'action': 'buy', 'side': 'yes',
            'count_fp': '40', 'yes_price_dollars': '0.45', 'fee_cost': '2',
            'created_time': '2026-11-14T12:00:15Z'}


def sell(count='40', price='0.20', fee='1.00'):
    return {'order_id': 'sell-1', 'ticker': 'COMBO', 'action': 'sell', 'side': 'no',
            'count_fp': count, 'yes_price_dollars': price, 'fee_cost': fee,
            'created_time': '2026-11-14T20:00:00Z'}


class FakeClient:
    def __init__(self, fills):
        self.items = fills

    def fills(self, *, min_ts):
        assert min_ts > 0
        return self.items


class FakeKV:
    def __init__(self, config):
        self.config = config
        self.writes = []

    def get(self, key):
        return self.config if key == 'config:current' else None

    def put(self, key, value):
        self.writes.append((key, value))

    def delete(self, key):
        pass


def test_full_sale_and_fee_math():
    assert detect_cashouts(CONFIG, [buy(), sell()]) == {
        'sample': {'amount': '7.00', 'remainingFraction': '0'}}


def test_partial_sale_and_no_sale():
    assert detect_cashouts(CONFIG, [buy(), sell('20', '0.20', '0.50')]) == {
        'sample': {'amount': '3.50', 'remainingFraction': '0.5'}}
    assert detect_cashouts(CONFIG, [buy()]) == {}


def test_slip_without_placed_time_uses_unique_purchase():
    config = {**CONFIG, 'slips': [{key: value for key, value in CONFIG['slips'][0].items() if key != 'placed'}]}
    assert detect_cashouts(config, [buy(), sell()])['sample']['amount'] == '7.00'


def test_manual_amount_is_preserved_and_warned(tmp_path, capsys):
    path = tmp_path / '2026-11-14.cashouts'
    path.write_text('sample 8.00\n')
    accepted, _ = persist_cashouts(path, detect_cashouts(CONFIG, [buy(), sell()]))
    assert accepted == {}
    assert path.read_text() == 'sample 8.00\n'
    assert 'manual cash-out differs' in capsys.readouterr().err


def test_automatic_partial_sale_can_advance_to_full(tmp_path):
    path = tmp_path / '2026-11-14.cashouts'
    partial = detect_cashouts(CONFIG, [buy(), sell('20', '0.20', '0.50')])
    accepted, automatic = persist_cashouts(path, partial)
    assert accepted == partial and automatic == {'sample'}
    full = detect_cashouts(CONFIG, [buy(), sell()])
    accepted, automatic = persist_cashouts(path, full)
    assert accepted == full and automatic == {'sample'}
    assert path.read_text().splitlines() == ['# automatic cash-out: sample', 'sample 7.00 0']


def test_unchanged_cashout_does_not_write_config_to_kv(tmp_path):
    path = tmp_path / 'data' / 'slips' / '2026-11-14.cashouts'
    path.parent.mkdir(parents=True)
    path.write_text('sample 7.00\n')
    config = {**CONFIG, 'cashouts': {'sample': {'amount': '7.00', 'remainingFraction': '0'}}}
    kv = FakeKV(config)
    update_cashouts(kv, config, root=tmp_path, client=FakeClient([buy(), sell()]))
    assert kv.writes == []
    assert path.read_text() == 'sample 7.00\n'


def test_different_manual_kv_amount_blocks_file_and_kv_write(tmp_path, capsys):
    config = {**CONFIG, 'cashouts': {'sample': {'amount': '8.00', 'remainingFraction': '0'}}}
    kv = FakeKV(config)
    update_cashouts(kv, config, root=tmp_path, client=FakeClient([buy(), sell()]))
    assert kv.writes == []
    assert not (tmp_path / 'data' / 'slips' / '2026-11-14.cashouts').exists()
    assert 'KV cash-out differs' in capsys.readouterr().err


def test_new_cashout_writes_file_and_config_once(tmp_path):
    kv = FakeKV(CONFIG)
    update_cashouts(kv, CONFIG, root=tmp_path, client=FakeClient([buy(), sell()]))
    assert [key for key, _ in kv.writes] == ['config:current']
    assert kv.writes[0][1]['cashouts'] == {'sample': {'amount': '7.00', 'remainingFraction': '0'}}
    assert (tmp_path / 'data' / 'slips' / '2026-11-14.cashouts').read_text().endswith('sample 7.00 0\n')


def test_missing_key_and_api_error_do_not_stop_poll(tmp_path, capsys):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return b'{"markets": [{"ticker": "LEG"}]}'

    kv = FakeKV(CONFIG)
    with patch.dict(poll_live.os.environ, {'KALSHI_API_KEY_ID': '', 'KALSHI_PRIVATE_KEY_PATH': ''}), \
            patch.object(poll_live, 'urlopen', return_value=Response()):
        poll_live.poll(kv, now=lambda: 100)
    assert any(key == 'snapshot:current' for key, _ in kv.writes)
    assert 'credentials missing' in capsys.readouterr().err

    kv.writes.clear()
    class BrokenClient:
        def fills(self, *, min_ts):
            raise RuntimeError('API error')

    with patch.dict(poll_live.os.environ, {'KALSHI_API_KEY_ID': 'test', 'KALSHI_PRIVATE_KEY_PATH': 'unused'}), \
            patch('kalshi_fills.KalshiFills', return_value=BrokenClient()), \
            patch.object(poll_live, 'urlopen', return_value=Response()):
        poll_live.poll(kv, now=lambda: 200)
    assert any(key == 'snapshot:current' for key, _ in kv.writes)
    assert 'API error' in capsys.readouterr().err
