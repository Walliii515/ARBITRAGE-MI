# coding: utf-8
import time

from api import orderbook_server as server


def test_successful_margin_topup_result_is_detected_for_cache_refresh():
    assert server._has_successful_margin_topup([
        {'success': False, 'action': 'margin_topup'},
        {'success': True, 'action': 'take_profit'},
    ]) is False

    assert server._has_successful_margin_topup([
        {'success': True, 'action': 'margin_topup'},
    ]) is True


def test_gate_position_risk_cache_invalidation_clears_snapshot():
    server._gate_position_risk_cache = [{'contract': 'OLD_USDT'}]
    server._gate_position_risk_cache_ts = time.time()

    server._invalidate_gate_position_risk_cache('unit_test')

    assert server._gate_position_risk_cache == []
    assert server._gate_position_risk_cache_ts == 0.0


def test_force_refresh_bypasses_gate_position_risk_cache(monkeypatch):
    class FakeExecutor:
        def fetch_gate_futures_positions(self):
            return [{'contract': 'NEW_USDT', 'size': '-1'}]

    class FakeReconciler:
        executor = FakeExecutor()

    monkeypatch.setattr(server.config, 'get_trade_mode', lambda: 'real')
    monkeypatch.setattr(server, 'build_default_reconciler', lambda: FakeReconciler())
    server._gate_position_risk_cache = [{'contract': 'OLD_USDT', 'size': '-1'}]
    server._gate_position_risk_cache_ts = time.time()

    cached = server._get_gate_position_risk_snapshot()
    refreshed = server._get_gate_position_risk_snapshot(force_refresh=True)

    assert cached == [{'contract': 'OLD_USDT', 'size': '-1'}]
    assert refreshed == [{'contract': 'NEW_USDT', 'size': '-1'}]
    assert server._gate_position_risk_cache == refreshed
