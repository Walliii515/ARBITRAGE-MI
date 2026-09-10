from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from common.market_meta_safety import (
    merge_position_multipliers, require_quanto_multiplier, validate_position_multiplier,
    retain_healthy_contract_meta,
    execution_position_multiplier,
)
from calc.delist_risk_monitor import DelistRiskMonitor
from calc.real_executor import RealExecutor, ExchangeConfig
from calc.reconciliation import Reconciler, ReconciliationConfig
from calc.exchange_desync_remediator import ExchangeDesyncRemediator, ExchangeDesyncRemediationConfig


@pytest.mark.parametrize('multiplier', [0.001, 0.1, 1, 10, 100])
def test_removed_contract_retains_position_units(multiplier):
    positions = [{'base_asset': 'AI', 'future_quanto_multiplier': multiplier}]
    merged = merge_position_multipliers({}, positions)
    assert require_quanto_multiplier(merged, 'AI') == multiplier
    assert merged['AI']['position_only'] is True
    executor = RealExecutor(ExchangeConfig(), contract_meta=merged)
    assert executor._resolve_gate_order_sizing({
        'base_asset': 'AI', 'order_side': 'close', 'target_contracts': 11,
        'target_qty': 11 * multiplier, 'future_quanto_multiplier': multiplier,
    }) == (multiplier, 11, None)
    assert executor._resolve_gate_order_sizing({
        'base_asset': 'AI', 'order_side': 'open', 'target_qty': 11 * multiplier,
    })[2] is not None


@pytest.mark.parametrize('invalid', [None, '', 0, -1, float('nan'), float('inf'), -float('inf')])
def test_invalid_saved_multiplier_never_silently_uses_current(invalid):
    with pytest.raises(ValueError):
        validate_position_multiplier({'AI': {'quanto_multiplier': 1}}, {
            'base_asset': 'AI', 'future_quanto_multiplier': invalid,
        })


def test_conflicting_positions_block_only_the_affected_asset():
    merged = merge_position_multipliers({'BTC': {'quanto_multiplier': 0.001}}, [
        {'base_asset': 'AI', 'future_quanto_multiplier': 1},
        {'base_asset': 'AI', 'future_quanto_multiplier': 10},
        {'base_asset': 'AI', 'future_quanto_multiplier': 1},
    ])
    assert retain_healthy_contract_meta(merged, {}) is merged
    with pytest.raises(ValueError):
        require_quanto_multiplier(merged, 'AI')
    assert require_quanto_multiplier(merged, 'BTC') == 0.001


def test_current_contract_change_is_rejected():
    with pytest.raises(ValueError):
        validate_position_multiplier({'AI': {'quanto_multiplier': 100}}, {
            'base_asset': 'AI', 'future_quanto_multiplier': 1,
        })


@pytest.mark.parametrize('qty,contracts', [(100, 1), (10, 10), (float('inf'), 1), (1, 0), (10, 0.1)])
def test_invalid_execution_cannot_create_position(qty, contracts):
    with pytest.raises(ValueError):
        execution_position_multiplier({'AI': {'quanto_multiplier': 10}}, 'AI', {
            'exec_qty': qty, 'exec_contracts': contracts,
        })


def test_new_execution_saves_validated_unit():
    assert execution_position_multiplier({'AI': {'quanto_multiplier': 100}}, 'AI', {
        'exec_qty': 2100, 'exec_contracts': 21, 'quanto_multiplier': 100,
    }) == 100


@pytest.mark.parametrize('side', ['open', 'close'])
def test_explicit_contracts_cannot_hide_quantity_mismatch(side):
    executor = RealExecutor(ExchangeConfig(), {'TUT': {'quanto_multiplier': 100}})
    assert executor._resolve_gate_order_sizing({
        'base_asset': 'TUT', 'order_side': side, 'target_qty': 205, 'target_contracts': 2,
    })[2] is not None


def test_delisting_exit_reaches_execution_without_orderbooks():
    from calc.closing_executor import ClosingExecutor
    executor = ClosingExecutor({'AI': {'quanto_multiplier': 1}}, {})
    executor.set_delist_risk_report({'items': [{'base_asset': 'AI', 'status': 'delisting'}]})
    pos = dict(id=1, base_asset='AI', status='holding', future_contract='AI_USDT',
               spot_open_qty=100, future_open_qty=100, future_open_contracts=100,
               future_quanto_multiplier=1, spot_open_price=1, future_open_price=1)
    executor._execute_close = MagicMock(return_value={'success': True})
    executor._pre_execution_gate = MagicMock(side_effect=AssertionError('must not need WS'))
    assert executor.check_and_close([pos], {}, {}) == [{'success': True}]
    assert executor._execute_close.call_args.args[1] == 'delist_risk_exit'
    executor._pre_execution_gate.assert_not_called()


def response(data):
    result = MagicMock()
    result.json.return_value = data
    return result


@pytest.mark.parametrize('status', ['delisting', 'delisted'])
def test_held_contract_missing_from_list_is_checked_individually(status):
    monitor = DelistRiskMonitor()
    with patch('calc.delist_risk_monitor.requests.get', side_effect=[response([]), response({
        'name': 'AI_USDT', 'status': status, 'in_delisting': True,
        'delisted_time': int(datetime.now().timestamp()),
    })]) as get:
        risks = monitor._gate_risks({'AI'})
    assert risks[0]['status'] == status
    assert get.call_args.args[0].endswith('/contracts/AI_USDT')


def test_failed_individual_contract_lookup_is_unknown_not_safe():
    monitor = DelistRiskMonitor()
    with patch('calc.delist_risk_monitor.requests.get', side_effect=[response([]), TimeoutError('timeout')]):
        risks = monitor._gate_risks({'AI'})
    assert risks[0]['status'] == 'unknown'
    assert 'gate:AI' in monitor.source_errors


def test_exchange_only_positions_are_part_of_delist_checks():
    executor = MagicMock()
    executor.fetch_gate_futures_positions.return_value = [{'base_asset': 'GATEONLY', 'size': -10}]
    executor.fetch_binance_account_balances.return_value = [
        {'asset': 'SPOTONLY', 'free': 11, 'locked': 0}, {'asset': 'USDT', 'total': 1000},
    ]
    cursor = MagicMock()
    cursor.fetchall.return_value = [{'base_asset': 'LOCAL'}]
    with patch('calc.delist_risk_monitor.db_manager.get_cursor') as get_cursor, \
         patch('common.config.config.get_trade_mode', return_value='live'), \
         patch('calc.reconciliation.build_exchange_config'), \
         patch('calc.real_executor.RealExecutor', return_value=executor):
        get_cursor.return_value.__enter__.return_value = cursor
        assets = DelistRiskMonitor().get_monitored_assets()
    assert assets == {'LOCAL', 'GATEONLY', 'SPOTONLY'}


def clear_risk():
    return {
        'exchange_clear': True, 'type': 'missing_gate_position', 'event_at': datetime.now(),
        'future_close_size': 30, 'future_close_price': 0.02,
        'future_exchange_order_id': 'clear-1', 'future_fee': 0.001,
        'detail': 'Gate clear',
    }


def test_cleared_future_remainder_does_not_zero_closed_amounts_or_pnl():
    from calc.closed_position_pnl import compute_closed_position_pnl, update_closed_position_pnl
    pos = {'future_open_qty': 0, 'future_open_contracts': 0, 'funding_total_pnl': 0.5}
    orders = [dict(order_side=side, market_type=market, status='executed',
                   exec_qty=30, exec_amount=amount, fee_amount_usdt=0.001)
              for side, market, amount in [('open', 'spot', 3), ('open', 'future', 3.1),
                                           ('close', 'spot', 2.4), ('close', 'future', 2.5)]]
    pnl = compute_closed_position_pnl(pos, orders)
    assert pnl['future_close_amount'] == 2.5
    assert pnl['spot_close_amount'] == 2.4
    assert pnl['realized_pnl'] == pytest.approx(0)
    assert pnl['total_pnl'] == pytest.approx(0.496)
    cursor = MagicMock()
    update_closed_position_pnl(cursor, 1, pnl, {'spot_close_amount', 'future_close_amount'})
    assert cursor.execute.call_args.args[1] == [2.4, 2.5, 1]


def test_exchange_clear_fills_are_aggregated_and_identified():
    executor = MagicMock()
    executor.fetch_gate_futures_my_trades.return_value = [
        {'contract': 'AI_USDT', 'text': 'clear', 'close_size': qty, 'price': '0.02',
         'order_id': 'clear-1', 'create_time': datetime.now().timestamp(), 'fee': '0.001'}
        for qty in (11, 19)
    ]
    reconciler = Reconciler(executor, ReconciliationConfig())
    risk = reconciler._detect_gate_desync_risk('AI', datetime.now(), 30, 0)
    assert risk['exchange_clear'] is True
    assert risk['future_close_size'] == 30
    assert risk['future_close_price'] == pytest.approx(0.02)
    assert risk['future_fee'] == pytest.approx(0.002)


def test_old_clear_with_different_quantity_is_not_applied():
    executor = MagicMock()
    executor.fetch_gate_futures_my_trades.return_value = [{
        'contract': 'AI_USDT', 'text': 'clear', 'close_size': 300, 'price': '0.02',
        'order_id': 'old-clear', 'create_time': datetime.now().timestamp(),
    }]
    risk = Reconciler(executor, ReconciliationConfig())._detect_gate_desync_risk('AI', datetime.now(), 30, 0)
    assert not risk.get('exchange_clear')


def remediation_fixture():
    executor = MagicMock()
    executor.contract_meta = {'AI': {'quanto_multiplier': 1}}
    executor.fetch_gate_futures_positions.return_value = []
    remediation = ExchangeDesyncRemediator(executor, ExchangeDesyncRemediationConfig())
    remediation._insert_synthetic_future_adl_order = MagicMock()
    remediation._load_binance_available_qty = MagicMock(return_value=30)
    remediation.remediate_binance_spot_desync = MagicMock(return_value={'success': True})
    positions = [dict(id=i, base_asset='AI', spot_open_qty=qty, future_open_qty=qty, future_open_contracts=qty,
                      future_quanto_multiplier=1, opened_at=datetime.now()-timedelta(days=1))
                 for i, qty in enumerate((11, 19), 1)]
    cursor = MagicMock()
    cursor.fetchall.return_value = positions
    return remediation, cursor, positions


def test_clear_records_future_before_aggregate_spot_sale_and_is_idempotent():
    remediation, cursor, positions = remediation_fixture()
    with patch('calc.exchange_desync_remediator.db_manager.get_cursor') as db:
        db.return_value.__enter__.return_value = cursor
        assert remediation.remediate_gate_clear('AI', clear_risk())['success']
        assert remediation._insert_synthetic_future_adl_order.call_count == 2
        assert remediation.remediate_binance_spot_desync.call_args.args[2] == 30
        for pos in positions:
            pos['future_open_qty'] = pos['future_open_contracts'] = 0
        assert remediation.remediate_gate_clear('AI', clear_risk())['success']
        assert remediation._insert_synthetic_future_adl_order.call_count == 2


@pytest.mark.parametrize('failure', ['new_gate_position', 'wrong_size', 'newer_position', 'bad_multiplier', 'db_failure'])
def test_clear_never_sells_spot_when_settlement_cannot_be_verified(failure):
    remediation, cursor, positions = remediation_fixture()
    risk = clear_risk()
    if failure == 'new_gate_position':
        remediation.executor.fetch_gate_futures_positions.return_value = [{'base_asset': 'AI', 'size': -5}]
    elif failure == 'wrong_size':
        risk['future_close_size'] = 31
    elif failure == 'newer_position':
        positions[0]['opened_at'] = datetime.now()+timedelta(days=1)
    elif failure == 'bad_multiplier':
        positions[0]['future_quanto_multiplier'] = 100
    else:
        remediation._insert_synthetic_future_adl_order.side_effect = RuntimeError('db failure')
    with patch('calc.exchange_desync_remediator.db_manager.get_cursor') as db:
        db.return_value.__enter__.return_value = cursor
        if failure in {'newer_position', 'bad_multiplier', 'db_failure'}:
            with pytest.raises((ValueError, RuntimeError)):
                remediation.remediate_gate_clear('AI', risk)
        else:
            assert not remediation.remediate_gate_clear('AI', risk).get('attempted')
    remediation.remediate_binance_spot_desync.assert_not_called()
