# coding: utf-8
from calc import reverse_account_monitor as monitor


def test_build_reverse_reconciliation_rows_matches_borrow_and_future(monkeypatch):
    def fake_binance_get(path, params=None, cfg=None):
        assert path == '/sapi/v1/margin/account'
        return {
            'marginLevel': '3.5',
            'userAssets': [
                {'asset': 'ABC', 'free': '0.1', 'locked': '0', 'borrowed': '10', 'interest': '0.02', 'netAsset': '-9.92'},
            ],
        }

    def fake_gate_get(path, query_string='', cfg=None):
        assert path == '/api/v4/futures/usdt/positions'
        return [{'contract': 'ABC_USDT', 'size': '10'}]

    monkeypatch.setattr(monitor, '_binance_signed_get', fake_binance_get)
    monkeypatch.setattr(monitor, '_gate_signed_get', fake_gate_get)

    result = monitor.build_reverse_reconciliation_rows([
        {
            'id': 1,
            'base_asset': 'ABC',
            'status': 'holding',
            'future_contract': 'ABC_USDT',
            'borrow_qty': 10,
            'borrow_repaid_qty': 0,
            'future_open_qty': 10,
            'future_close_qty': 0,
        }
    ])

    assert result['summary']['local_holding'] == 1
    assert result['summary']['match_count'] == 1
    assert result['summary']['mismatch_count'] == 0
    assert result['rows'][0]['is_match'] is True
    assert result['rows'][0]['exchange_interest_qty'] == 0.02


def test_get_reverse_capital_snapshot_is_partial_on_exchange_error(monkeypatch):
    def broken_binance_get(path, params=None, cfg=None):
        raise RuntimeError('binance unavailable')

    def fake_gate_get(path, query_string='', cfg=None):
        assert path == '/api/v4/futures/usdt/accounts'
        return {'available': '250', 'total': '251.5', 'unrealised_pnl': '1.5'}

    monkeypatch.setattr(monitor, '_binance_signed_get', broken_binance_get)
    monkeypatch.setattr(monitor, '_gate_signed_get', fake_gate_get)

    result = monitor.get_reverse_capital_snapshot()

    assert result['strategy'] == 'reverse'
    assert 'binance_cross_margin' in result['errors']
    assert result['gate_futures']['available'] == 250
