# coding: utf-8
import time
from datetime import datetime

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


def test_auto_risk_close_notification_formats_margin_close_result():
    event_at = datetime(2026, 6, 21, 10, 30, 0)

    item = server._build_auto_risk_close_notification({
        'base_asset': 'AI',
        'success': True,
        'close_reason': 'margin_close',
        'order_uuid': 'order-1',
        'message': '成交成功',
        'pre_gate_basis_bps': 216.2,
        'actual_close_basis_bps': 214.3,
        'close_basis_slip_bps': -1.9,
    }, event_at=event_at)

    assert item['title'] == '系统强平成功: AI'
    assert item['type'] == 'warning'
    assert item['source'] == 'auto_risk_close'
    assert item['dedup_key'] == 'auto_risk_close:margin_close:order-1'
    assert item['event_at'] == event_at
    assert '保证金风控强平成功' in item['message']
    assert '成交基差=214.3bps' in item['message']


def test_auto_risk_close_notification_ignores_non_risk_close_result():
    assert server._build_auto_risk_close_notification({
        'base_asset': 'AI',
        'success': True,
        'close_reason': 'take_profit',
        'order_uuid': 'order-1',
    }) is None


def test_record_auto_risk_close_notifications_continues_after_store_error(monkeypatch):
    calls = []

    def fake_upsert(**item):
        calls.append(item)
        if item['dedup_key'] == 'auto_risk_close:margin_close:bad-order':
            raise RuntimeError('db down')
        return {'id': len(calls)}

    monkeypatch.setattr(server, 'upsert_popup_notification', fake_upsert)

    recorded = server._record_auto_risk_close_notifications([
        {
            'base_asset': 'AI',
            'success': False,
            'close_reason': 'margin_close',
            'order_uuid': 'bad-order',
            'message': '现货失败',
        },
        {
            'base_asset': 'BEL',
            'success': True,
            'close_reason': 'negative_funding_exit',
            'order_uuid': 'ok-order',
            'message': '成交成功',
        },
        {
            'base_asset': 'TUT',
            'success': True,
            'close_reason': 'take_profit',
            'order_uuid': 'ignored-order',
        },
    ])

    assert recorded == 1
    assert len(calls) == 2
    assert calls[0]['type'] == 'error'
    assert calls[1]['dedup_key'] == 'auto_risk_close:negative_funding_exit:ok-order'
