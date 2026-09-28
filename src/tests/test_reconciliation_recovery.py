"""Quantity-risk recovery never trades or clears unverified execution state."""
import copy
import json
import os
import sys
import threading
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.asset_reduction_guard import asset_reduction_guard
from calc.reconciliation import Reconciler


@pytest.fixture
def recovery():
    now = datetime(2026, 9, 21, 10)
    executor = MagicMock()
    executor.contract_meta = {'G': {'quanto_multiplier': 100}}
    executor.fetch_binance_spot_balances.return_value = [
        {'asset': 'G', 'total': 2000, 'free': 2000, 'locked': 0},
    ]
    executor.fetch_gate_futures_positions.return_value = [
        {'base_asset': 'G', 'size': -20},
    ]
    reconciler = Reconciler(executor)
    positions = [dict(
        id=i, base_asset='G', status='holding', spot_open_qty=1000,
        opened_at=now - timedelta(days=3),
        future_open_qty=1000, future_open_contracts=10,
        future_quanto_multiplier=100, exchange_risk_status='desynced',
        exchange_risk_type='qty_mismatch',
        exchange_risk_detail='Gate实仓不匹配|contract=G_USDT|local=40|exchange=20',
        exchange_risk_at=now - timedelta(minutes=5),
    ) for i in (811, 812)]
    current = reconciler._build_combined_exposure_rows(
        now, {'G': 2000}, {'G': 20},
        executor.fetch_binance_spot_balances.return_value,
        executor.fetch_gate_futures_positions.return_value,
    )[0]
    previous = copy.deepcopy(current)
    previous['snapshot_at'] = now - timedelta(seconds=60)
    cursor = MagicMock()
    cursor.fetchall.side_effect = [positions, []]
    cursor.fetchone.side_effect = [previous, None]
    cursor.rowcount = 2
    with patch('calc.reconciliation.db_manager.get_cursor') as db:
        db.return_value.__enter__.return_value = cursor
        yield reconciler, now, positions, current, previous, cursor


def updates(cursor):
    return [c for c in cursor.execute.call_args_list if 'UPDATE mi_trade_position' in c.args[0]]


def test_recovers_only_status_after_two_matches_and_fresh_locked_check(recovery):
    r, now, positions, current, previous, cursor = recovery
    previous['detail'] = json.dumps(previous['detail'])
    assert r._recover_matched_quantity_risks(now, [current]) == 2
    sql, ids = updates(cursor)[0].args
    assert ids[:2] == [811, 812]
    assert "exchange_risk_status = 'resolved'" in sql
    assert 'spot_open_qty =' not in sql
    assert 'realized_pnl' not in sql
    assert 'close_reason =' not in sql
    r.executor.fetch_binance_spot_balances.assert_called_once()
    r.executor.fetch_gate_futures_positions.assert_called_once()
    r.executor.execute.assert_not_called()
    assert asset_reduction_guard.owner('G') is None


@pytest.mark.parametrize('risk_type,prefix', [
    ('qty_mismatch', 'Gate实仓不匹配|'),
    ('missing_gate_position', 'Gate实仓不匹配|'),
    ('extra_gate_position', 'Gate多余实仓|'),
    ('binance_spot_excess', '交易所实仓不一致|'),
    ('gate_short_excess', '交易所实仓不一致|'),
])
def test_all_reconciled_quantity_risks_recover(recovery, risk_type, prefix):
    r, now, positions, current, _, cursor = recovery
    positions[1]['exchange_risk_type'] = risk_type
    positions[1]['exchange_risk_detail'] = prefix + 'asset=G'
    assert r._recover_matched_quantity_risks(now, [current]) == 2
    assert risk_type in updates(cursor)[0].args[1][2:]
    r.executor.execute.assert_not_called()


def test_missing_position_from_delisting_clear_is_not_quantity_recovery(recovery):
    r, now, positions, current, _, cursor = recovery
    positions[1]['exchange_risk_type'] = 'missing_gate_position'
    positions[1]['exchange_risk_detail'] = 'Gate下架清算|contract=G_USDT'
    assert r._recover_matched_quantity_risks(now, [current]) == 0
    assert not updates(cursor)


@pytest.mark.parametrize('field,value', [
    ('exchange_risk_type', 'adl'), ('exchange_risk_type', 'liquidation'),
    ('exchange_risk_type', 'close_persistence_failed'),
    ('exchange_risk_type', 'delisting'), ('exchange_risk_type', 'margin_close'),
    ('exchange_risk_detail', '系统平仓Gate期货已成交但Binance现货失败'),
    ('exchange_risk_at', None),
])
def test_other_risks_are_not_erased(recovery, field, value):
    r, now, positions, current, _, cursor = recovery
    positions[1][field] = value
    assert r._recover_matched_quantity_risks(now, [current]) == 0
    assert not updates(cursor)
    r.executor.fetch_gate_futures_positions.assert_not_called()


@pytest.mark.parametrize('value', [None, 0, -1, float('nan'), float('inf'), 10])
def test_invalid_or_conflicting_persisted_multiplier_blocks_recovery(recovery, value):
    r, now, positions, current, _, cursor = recovery
    positions[0]['future_quanto_multiplier'] = value
    assert r._recover_matched_quantity_risks(now, [current]) == 0
    assert not updates(cursor)


@pytest.mark.parametrize('field,value', [
    ('spot_open_qty', 999), ('future_open_qty', 999),
    ('future_open_contracts', 9), ('spot_open_qty', 0),
    ('future_open_qty', float('nan')),
])
def test_per_position_quantity_must_be_valid_even_if_aggregate_matches(recovery, field, value):
    r, now, positions, current, _, cursor = recovery
    positions[0][field] = value
    assert r._recover_matched_quantity_risks(now, [current]) == 0
    assert not updates(cursor)


@pytest.mark.parametrize('kind', ['missing', 'mismatch', 'stale', 'before_risk', 'same_time'])
def test_requires_latest_previous_healthy_snapshot_after_risk(recovery, kind):
    r, now, positions, current, previous, cursor = recovery
    if kind == 'missing':
        cursor.fetchone.side_effect = [None]
    elif kind == 'mismatch':
        previous['is_match'] = False
    elif kind == 'stale':
        previous['snapshot_at'] = now - timedelta(seconds=121)
    elif kind == 'before_risk':
        previous['snapshot_at'] = positions[0]['exchange_risk_at']
    else:
        previous['snapshot_at'] = now
    assert r._recover_matched_quantity_risks(now, [current]) == 0
    assert not updates(cursor)


@pytest.mark.parametrize('blocker', ['order', 'event'])
def test_pending_or_uncertain_execution_blocks_recovery(recovery, blocker):
    r, now, positions, current, previous, cursor = recovery
    if blocker == 'order':
        cursor.fetchall.side_effect = [positions, [{'status': 'pending'}]]
    else:
        cursor.fetchone.side_effect = [previous, {'id': 123}]
    assert r._recover_matched_quantity_risks(now, [current]) == 0
    assert not updates(cursor)
    r.executor.fetch_gate_futures_positions.assert_not_called()


@pytest.mark.parametrize('change', [
    'one_contract', 'spot_missing', 'gate_missing', 'long', 'locked',
    'nan', 'api_failure', 'slow', 'missing_multiplier',
])
def test_fresh_exchange_validation_is_fail_closed(recovery, change):
    r, now, _, current, _, cursor = recovery
    balances = r.executor.fetch_binance_spot_balances.return_value
    futures = r.executor.fetch_gate_futures_positions.return_value
    if change == 'one_contract':
        futures[0]['size'] = -19  # Display tolerance accepts this; recovery must not.
    elif change == 'spot_missing':
        balances.clear()
    elif change == 'gate_missing':
        futures.clear()
    elif change == 'long':
        futures[0]['size'] = 20
    elif change == 'locked':
        balances[0]['locked'] = 1
    elif change == 'nan':
        balances[0]['total'] = float('nan')
    elif change == 'api_failure':
        r.executor.fetch_gate_futures_positions.side_effect = RuntimeError('offline')
    elif change == 'missing_multiplier':
        r.executor.contract_meta = {}
    with patch('calc.reconciliation.time.monotonic', side_effect=[0, 6 if change == 'slow' else 1]):
        assert r._recover_matched_quantity_risks(now, [current]) == 0
    assert not updates(cursor)


def test_current_mismatch_never_starts_recovery(recovery):
    r, now, _, current, _, cursor = recovery
    current['is_match'] = False
    assert r._recover_matched_quantity_risks(now, [current]) == 0
    cursor.execute.assert_not_called()


def test_active_reduction_keeps_isolation(recovery):
    r, now, _, current, _, cursor = recovery
    ready = threading.Event()
    release = threading.Event()

    def owner():
        with asset_reduction_guard.claim('G', 'closing'):
            ready.set()
            release.wait(5)

    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert ready.wait(2)
        assert r._recover_matched_quantity_risks(now, [current]) == 0
        cursor.execute.assert_not_called()
    finally:
        release.set()
        thread.join(5)


def test_recovery_holds_execution_guard_while_refreshing_exchange(recovery):
    r, now, _, current, _, cursor = recovery
    balances = r.executor.fetch_binance_spot_balances.return_value

    def refresh():
        assert asset_reduction_guard.owner('G') == 'reconciliation_recovery'
        return balances

    r.executor.fetch_binance_spot_balances.side_effect = refresh
    assert r._recover_matched_quantity_risks(now, [current]) == 2


def test_already_resolved_is_idempotent(recovery):
    r, now, positions, current, _, cursor = recovery
    for p in positions:
        p['exchange_risk_status'] = 'resolved'
    assert r._recover_matched_quantity_risks(now, [current]) == 0
    assert not updates(cursor)


def test_real_g_valley_timeout_fok_rejection_does_not_block_recovery(recovery):
    r, now, positions, current, _, cursor = recovery
    cursor.fetchall.side_effect = [positions, [{
        'status': 'rejected',
        'reject_reason': '动态止盈|谷底超时(谷-167.7,62s) | 拒单: '
                         '期货拒单(未执行现货): HTTP 400: '
                         '{"label":"ORDER_FOK","message":"order can not be filled"}',
    }]]
    assert r._recover_matched_quantity_risks(now, [current]) == 2


@pytest.mark.parametrize('reason', [
    '动态止盈|谷底超时 | 拒单: Gate 请求超时(10s)',
    '动态止盈 | 拒单: 现货拒单(期货已成交): Binance 状态未知',
    '成交引擎服务返回错误: 500', 'ConnectionError', 'TIMEOUT', None,
])
def test_real_execution_uncertainty_is_not_confused_with_strategy_reason(reason):
    assert Reconciler._order_execution_uncertain({'status': 'rejected', 'reject_reason': reason})


def test_run_once_invokes_recovery_and_reports_count(recovery):
    r, now, _, current, _, cursor = recovery
    r.cfg.auto_remediate_enabled = False
    r._load_local_spot_positions = MagicMock(return_value={'G': 2000})
    r._load_local_gate_positions = MagicMock(return_value={'G': 20})
    r._mark_gate_desync_risks = MagicMock(return_value=[])
    r._mark_combined_exposure_risks = MagicMock(return_value=[])
    r._recover_matched_quantity_risks = MagicMock(return_value=2)
    r._insert_rows = MagicMock()
    r.cleanup_old_snapshots = MagicMock()
    assert r.run_once()['recovered_position_count'] == 2
    r._recover_matched_quantity_risks.assert_called_once()


def test_same_thread_active_reduction_also_blocks_recovery(recovery):
    r, now, _, current, _, cursor = recovery
    with asset_reduction_guard.claim('G', 'closing'):
        assert r._recover_matched_quantity_risks(now, [current]) == 0
    cursor.execute.assert_not_called()


@pytest.mark.parametrize('failure', ['pending', 'failed', 'rejected'])
def test_uncertain_order_query_covers_both_legs_and_current_holding_lifetime(recovery, failure):
    r, now, positions, current, previous, cursor = recovery
    cursor.fetchall.side_effect = [positions, [{'status': failure, 'reject_reason': '请求超时'}]]
    assert r._recover_matched_quantity_risks(now, [current]) == 0
    sql, params = next(c.args for c in cursor.execute.call_args_list if 'FROM mi_trade_order' in c.args[0])
    assert "status <> 'executed'" in sql
    assert 'market_type =' not in sql
    assert params == ('G', positions[0]['opened_at'])


@pytest.mark.parametrize('unavailable', ['binance', 'gate'])
def test_run_once_does_not_recover_on_incomplete_exchange_snapshots(recovery, unavailable):
    r, *_ = recovery
    r.cfg.auto_remediate_enabled = False
    r._load_local_spot_positions = MagicMock(return_value={'G': 2000})
    r._load_local_gate_positions = MagicMock(return_value={'G': 20})
    r._mark_gate_desync_risks = MagicMock(return_value=[])
    r._recover_matched_quantity_risks = MagicMock()
    r._insert_rows = MagicMock()
    r.cleanup_old_snapshots = MagicMock()
    method = 'fetch_binance_spot_balances' if unavailable == 'binance' else 'fetch_gate_futures_positions'
    getattr(r.executor, method).side_effect = RuntimeError('unavailable')
    assert r.run_once()['recovered_position_count'] == 0
    r._recover_matched_quantity_risks.assert_not_called()


@pytest.mark.parametrize('result', [
    {'exchange_order_submitted': True},
    {'exchange_order_state_unknown': True},
    {'results': [{'exchange_order_state_unknown': True}]},
])
def test_same_run_remediation_consumes_snapshot_before_recovery(recovery, result):
    r, *_ = recovery
    r._load_local_spot_positions = MagicMock(return_value={'G': 2000})
    r._load_local_gate_positions = MagicMock(return_value={'G': 20})
    r._mark_gate_desync_risks = MagicMock(return_value=[])
    r._auto_remediate_gate_risks = MagicMock(return_value=[result])
    r._auto_cleanup_completed_asset_dust = MagicMock(return_value=[])
    r._mark_combined_exposure_risks = MagicMock(return_value=[])
    r._auto_remediate_combined_exposure_risks = MagicMock(return_value=[])
    r._recover_matched_quantity_risks = MagicMock()
    r._insert_rows = MagicMock()
    r.cleanup_old_snapshots = MagicMock()
    assert r.run_once()['recovered_position_count'] == 0
    r._recover_matched_quantity_risks.assert_not_called()


def test_resolved_positions_resume_negative_funding_evaluation_not_forced_execution(recovery):
    from calc.closing_executor import ClosingExecutor

    r, now, positions, current, _, cursor = recovery
    ce = ClosingExecutor.__new__(ClosingExecutor)
    ce._execution_quarantine_lock = threading.Lock()
    ce._execution_quarantine_by_asset = {}
    ce._observe_negative_funding_extremes = MagicMock()
    ce._mark_incomplete_holding_desync = MagicMock(return_value=False)
    ce._check_delist_risk_exit = MagicMock(return_value=False)
    ce._close_cooldown = {}
    ce._negative_funding_exit_mode = MagicMock(return_value=None)
    ce._check_take_profit = MagicMock(return_value=False)
    ce._clear_position_close_state = MagicMock()
    ce._execute_close = MagicMock()
    for pos in positions:
        pos['current_spread_bps'] = 10
    assert ce.check_and_close(positions, {}, {}) == []
    ce._negative_funding_exit_mode.assert_not_called()

    assert r._recover_matched_quantity_risks(now, [current]) == 2
    # Simulate the next normal position refresh observing the committed status.
    for pos in positions:
        pos['exchange_risk_status'] = 'resolved'
        pos['exchange_risk_type'] = None
    assert ce.check_and_close(positions, {}, {}) == []
    assert ce._negative_funding_exit_mode.call_count == 2
    ce._execute_close.assert_not_called()
