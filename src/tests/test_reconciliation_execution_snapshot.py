import threading
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from calc.asset_reduction_guard import asset_reduction_guard
from calc.reconciliation import Reconciler


def make_reconciler(spot=37900, contracts=347):
    executor = MagicMock(contract_meta={'G': {'quanto_multiplier': 100}})
    executor.fetch_binance_spot_balances.return_value = [{'asset': 'G', 'total': spot, 'locked': 0}]
    executor.fetch_gate_futures_positions.return_value = [{'base_asset': 'G', 'size': -contracts}]
    reconciler = Reconciler(executor)
    reconciler.remediator = MagicMock()
    return reconciler


def dispatch(reconciler, route, spot=37900, contracts=347):
    if route == 'gate':
        return reconciler._remediate_confirmed_gate_risk(
            {'base_asset': 'G', 'local_contracts': 411, 'exchange_contracts': contracts},
            {'type': 'qty_mismatch'}, {'G': {'exchange_value': spot}},
        )
    return reconciler._remediate_confirmed_combined_risk({
        'base_asset': 'G', 'risk_type': 'binance_spot_excess' if spot > contracts * 100 else 'gate_short_excess',
        'binance_qty': spot, 'gate_qty': contracts * 100, 'gate_contracts': contracts,
        'quanto_multiplier': 100, 'risk': {'type': 'binance_spot_excess'},
    })


@pytest.mark.parametrize('route', ['gate', 'combined'])
@pytest.mark.parametrize('spot,contracts', [(34700, 347), (31500, 347), (37900, 315), (0, 0)])
def test_discard_changed_snapshot_without_switching_trade_direction(route, spot, contracts):
    reconciler = make_reconciler(spot, contracts)
    result = dispatch(reconciler, route)
    assert result['reason'] == 'remediation_snapshot_changed'
    assert result['retry_needed'] and not result['attempted']
    assert not reconciler.remediator.mock_calls
    assert asset_reduction_guard.owner('G') is None


@pytest.mark.parametrize('route', ['gate', 'combined'])
def test_verified_excess_keeps_lock_until_remediator_returns(route):
    reconciler = make_reconciler()
    def execute(**kwargs):
        assert asset_reduction_guard.owner('G') == 'reconciliation_revalidation'
        with asset_reduction_guard.claim('G', 'nested') as acquired:
            assert acquired
        return {'success': True, 'attempted': True}
    reconciler.remediator.remediate_binance_spot_desync.side_effect = execute
    assert dispatch(reconciler, route)['success']
    kwargs = reconciler.remediator.remediate_binance_spot_desync.call_args.kwargs
    assert kwargs['exchange_qty'] - kwargs['local_qty'] == 3200
    assert asset_reduction_guard.owner('G') is None


@pytest.mark.parametrize('route', ['gate', 'combined'])
def test_repeated_reconciliation_cannot_sell_same_excess_twice(route):
    reconciler = make_reconciler()
    def execute(**kwargs):
        reconciler.executor.fetch_binance_spot_balances.return_value[0]['total'] = 34700
        return {'success': True}
    reconciler.remediator.remediate_binance_spot_desync.side_effect = execute
    assert dispatch(reconciler, route)['success']
    assert dispatch(reconciler, route)['reason'] == 'remediation_snapshot_changed'
    reconciler.remediator.remediate_binance_spot_desync.assert_called_once()


@pytest.mark.parametrize('route', ['gate', 'combined'])
def test_concurrent_normal_close_blocks_even_snapshot_fetch(route):
    reconciler = make_reconciler()
    acquired = threading.Event()
    release = threading.Event()
    def closing():
        with asset_reduction_guard.claim('G', 'normal_close') as owned:
            assert owned
            acquired.set()
            release.wait(5)
    worker = threading.Thread(target=closing)
    worker.start()
    try:
        assert acquired.wait(2)
        assert dispatch(reconciler, route)['reason'] == 'asset_reduction_inflight'
        reconciler.executor.fetch_binance_spot_balances.assert_not_called()
        assert not reconciler.remediator.mock_calls
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()


@pytest.mark.parametrize('route', ['gate', 'combined'])
@pytest.mark.parametrize('failure', ['spot_error', 'gate_error', 'slow', 'invalid_list', 'nan', 'negative', 'long', 'locked', 'duplicate', 'missing_total'])
def test_untrustworthy_snapshot_never_submits_order(route, failure):
    reconciler = make_reconciler()
    ex = reconciler.executor
    if failure == 'spot_error':
        ex.fetch_binance_spot_balances.side_effect = TimeoutError('timeout')
    elif failure == 'gate_error':
        ex.fetch_gate_futures_positions.side_effect = TimeoutError('timeout')
    elif failure == 'invalid_list':
        ex.fetch_gate_futures_positions.return_value = None
    elif failure == 'nan':
        ex.fetch_gate_futures_positions.return_value[0]['size'] = float('nan')
    elif failure == 'negative':
        ex.fetch_binance_spot_balances.return_value[0]['total'] = -1
    elif failure == 'long':
        ex.fetch_gate_futures_positions.return_value[0]['size'] = 347
    elif failure == 'locked':
        ex.fetch_binance_spot_balances.return_value[0]['locked'] = 1
    elif failure == 'duplicate':
        ex.fetch_gate_futures_positions.return_value *= 2
    elif failure == 'missing_total':
        del ex.fetch_binance_spot_balances.return_value[0]['total']
    with patch('calc.reconciliation.time.monotonic', side_effect=[0, 6 if failure == 'slow' else 1]):
        result = dispatch(reconciler, route)
    assert not result['attempted'] and result['retry_needed']
    assert not reconciler.remediator.mock_calls
    assert asset_reduction_guard.owner('G') is None


@pytest.mark.parametrize('route', ['gate', 'combined'])
def test_confirmed_real_gate_excess_reduces_contracts_using_multiplier(route):
    reconciler = make_reconciler(31500, 347)
    reconciler.remediator.remediate_gate_extra_position.return_value = {'success': True}
    assert dispatch(reconciler, route, 31500, 347)['success']
    assert reconciler.remediator.remediate_gate_extra_position.call_args.kwargs['extra_contracts'] == 32


@pytest.mark.parametrize('route', ['gate', 'combined'])
def test_lock_released_on_execution_exception(route):
    reconciler = make_reconciler()
    reconciler.remediator.remediate_binance_spot_desync.side_effect = RuntimeError('persistence_failure')
    with pytest.raises(RuntimeError):
        dispatch(reconciler, route)
    assert asset_reduction_guard.owner('G') is None


@pytest.mark.parametrize('previous,confirmed', [
    ({'local_value': 411, 'exchange_value': 347, 'is_match': 0}, True),
    ({'local_value': 347, 'exchange_value': 347, 'is_match': 1}, False),
    ({'local_value': 315, 'exchange_value': 347, 'is_match': 0}, False),
])
def test_gate_confirmation_does_not_skip_intervening_recovery(previous, confirmed):
    reconciler = make_reconciler()
    reconciler._has_self_reported_gate_desync = MagicMock(return_value=False)
    with patch('calc.reconciliation.db_manager.get_cursor') as db:
        cursor = db.return_value.__enter__.return_value
        cursor.fetchall.return_value = [previous]
        assert reconciler._is_gate_risk_confirmed('G', 'qty_mismatch', datetime.now()) == confirmed
        sql = cursor.execute.call_args.args[0]
        assert 'AND is_match = 0' not in sql
        assert 'ORDER BY snapshot_at DESC, id DESC' in sql
