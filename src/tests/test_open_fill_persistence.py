"""BEL maker/fallback regression: exchange fills must survive ledger persistence."""
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest

from calc.real_executor import ExchangeConfig, RealExecutor
from common.market_meta_safety import execution_position_multiplier
from tests.test_open_close_logic import make_trading_executor


def opening(multiplier=1):
    meta = {'BEL': {'quanto_multiplier': multiplier}}
    trader = make_trading_executor(contract_meta=meta)
    trader.executor_client.channel = '实盘'
    group = trader._create_order_group({
        'base_asset': 'BEL', 'contract': 'BEL_USDT',
        'spot_qty': 764 * multiplier, 'open_amount_usdt': 96,
        'funding_rate_24h': 0.00944,
    })
    group['future_order'].update({
        'execution_style': 'maker', 'maker_fallback_ioc_enabled': True,
        'maker_fallback_protective_price': 0.118,
    })
    executor = RealExecutor(ExchangeConfig(), contract_meta=meta)
    executor._session = MagicMock()
    return trader, group, executor


def gate_fill(executor, contracts, price, order_id, multiplier=1, fee=None):
    return executor._parse_gate_response({
        'id': order_id, 'status': 'finished', 'size': -contracts, 'left': 0,
        'fill_price': str(price), 'fee': fee, 'finish_as': 'filled' if contracts else 'ioc',
    }, multiplier)


def executed_open(multiplier=1, maker=235, fallback=529):
    trader, group, executor = opening(multiplier)
    first = gate_fill(executor, maker, 0.119, 'maker-235', multiplier, 0.005593)
    first['execution_stats'] = {'future_maker': {
        'attempted': True, 'filled': maker > 0, 'fill_ratio': maker / 764,
        'requested_contracts': 764, 'filled_contracts': maker,
        'terminal_confirmed': True, 'fill_state_uncertain': False,
    }}
    # The production parser rounds each order's exec_amount to cents.
    second = gate_fill(executor, fallback, 0.11874060491, 'fallback-529', multiplier, 0.03140689)
    executor._place_gate_futures_order = MagicMock(side_effect=[first, second])
    qty = (maker + fallback) * multiplier
    executor._place_binance_spot_order = MagicMock(return_value={
        'success': True, 'exec_price': 0.118805, 'exec_qty': qty,
        'exec_amount': qty * 0.118805, 'exchange_order_id': 'binance-764',
    })
    result = executor.execute(group, {})
    return trader, group, executor, result


@pytest.mark.parametrize('multiplier', [0.001, 0.1, 1, 10, 100])
@pytest.mark.parametrize('maker,fallback', [(235, 529), (235, 100), (764, 0), (0, 764), (235, 0)])
def test_maker_and_fallback_reach_hedge_and_persistence(multiplier, maker, fallback):
    trader, group, executor, result = executed_open(multiplier, maker, fallback)
    assert result['success'], result['message']
    contracts = maker + fallback
    qty = contracts * multiplier
    future = result['future_order']
    assert future['exec_contracts'] == contracts
    assert future['exec_qty'] == pytest.approx(qty)
    assert future['quanto_multiplier'] == multiplier
    assert execution_position_multiplier(trader.contract_meta, 'BEL', future) == multiplier
    assert executor._place_binance_spot_order.call_args.args[0]['target_qty'] == pytest.approx(qty)
    assert executor._place_gate_futures_order.call_count == (1 if maker == 764 else 2)
    if 0 < maker < 764:
        assert executor._place_gate_futures_order.call_args.args[0]['target_contracts'] == 764 - maker

    with patch('calc.trading_executor.db_manager.get_cursor') as get_cursor:
        cursor = get_cursor.return_value.__enter__.return_value
        cursor.lastrowid = 42
        trader._save_orders(group, result)
    get_cursor.assert_called_once()
    assert cursor.execute.call_count == 3
    pos_sql, pos = cursor.execute.call_args_list[0].args
    assert 'INSERT INTO mi_trade_position' in pos_sql
    assert pos['future_open_contracts'] == contracts
    assert pos['future_open_qty'] == pytest.approx(qty)
    assert pos['spot_open_qty'] == pytest.approx(qty)
    assert pos['future_quanto_multiplier'] == multiplier
    assert pos['open_funding_rate_24h'] == 0.00944
    for call in cursor.execute.call_args_list[1:]:
        sql, order = call.args
        assert 'INSERT INTO mi_trade_order' in sql
        assert order['position_id'] == 42
        assert order['status'] == 'executed'
        assert order['exec_qty'] == pytest.approx(qty)
    executor._session.request.assert_not_called()
    executor._session.post.assert_not_called()


def test_bel_vwap_uses_fill_prices_not_cent_rounded_amounts():
    trader, _, _, result = executed_open()
    future = result['future_order']
    expected = (235 * 0.119 + 529 * 0.11874060491) / 764
    assert future['exec_price'] == pytest.approx(expected, abs=1e-12)
    assert abs(future['exec_price'] - future['exec_amount'] / 764) > 1e-7
    assert future['fee_amount_usdt'] == pytest.approx(0.005593 + 0.03140689)
    assert future['fee_amount'] == pytest.approx(future['fee_amount_usdt'])
    assert future['exchange_order_ids'] == ['maker-235', 'fallback-529']
    assert execution_position_multiplier(trader.contract_meta, 'BEL', future) == 1


@pytest.mark.parametrize('fees,expected', [
    ((None, 0.02), None), ((0.02, None), None), ((None, None), None),
    ((0, 0), 0), ((-0.001, 0.02), 0.019),
])
def test_merged_fee_is_complete_or_unknown(fees, expected):
    _, _, executor = opening()
    first = gate_fill(executor, 235, 0.119, 'maker', fee=fees[0])
    second = gate_fill(executor, 529, 0.11874, 'fallback', fee=fees[1])
    merged = executor._merge_future_execution_results(first, second)
    for field in ('fee_amount', 'fee_amount_usdt'):
        assert merged[field] == (pytest.approx(expected) if expected is not None else None)


def test_invalid_fill_still_blocks_ledger_without_losing_receipt():
    trader, group, _, result = executed_open()
    result['future_order']['exec_contracts'] = 529
    receipt = deepcopy(result)
    with patch('calc.trading_executor.db_manager.get_cursor') as get_cursor, \
         patch('calc.trading_executor.upsert_popup_notification') as notify, \
         pytest.raises(ValueError, match='Gate成交数量与持仓乘数不一致'):
        trader._save_orders(group, result)
    get_cursor.return_value.__enter__.return_value.execute.assert_not_called()
    assert trader._open_persistence_failed_assets == {'BEL'}
    payload = notify.call_args.kwargs['payload']
    assert payload['exec_result'] == receipt
    assert notify.call_args.kwargs['dedup_key'] == f"open_persistence:{group['order_uuid']}"


@pytest.mark.parametrize('fail_at', ['position', 'spot', 'future', 'commit'])
def test_open_ledger_is_atomic_and_failed_write_blocks_next_open(fail_at):
    trader, group, _, result = executed_open()
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.lastrowid = 42
    failure = RuntimeError('simulated database failure')
    if fail_at == 'commit':
        connection.commit.side_effect = failure
    else:
        index = ['position', 'spot', 'future'].index(fail_at)
        cursor.execute.side_effect = [None] * index + [failure]
    with patch('common.database.pymysql.connect', return_value=connection), \
         patch('calc.trading_executor.upsert_popup_notification') as notify, \
         pytest.raises(RuntimeError, match='simulated database failure'):
        trader._save_orders(group, result)
    connection.rollback.assert_called_once()
    connection.close.assert_called_once()
    if fail_at != 'commit':
        connection.commit.assert_not_called()
    notify.assert_called_once()
    assert trader._open_persistence_failed_assets == {'BEL'}

    trader.executor_client.execute = MagicMock()
    trader._resolve_signal = MagicMock()
    trader._peak_state['BEL'] = {'peak_bps': 90}
    with patch.object(trader, '_refresh_holding_exposure_from_db'), \
         patch.object(trader, '_load_exchange_risk_blocked_assets', return_value=set()), \
         patch.object(trader, '_load_open_cooldown_from_db'):
        assert trader.check_and_open([{'base_asset': 'BEL'}, {'base_asset': 'ETH'}]) == []
        assert trader.check_and_open([{'base_asset': 'BEL'}]) == []
    trader.executor_client.execute.assert_not_called()
    assert 'BEL' not in trader._peak_state
    # An unrelated symbol still reaches its normal missing-market-data guard.
    assert trader._resolve_signal.call_args.args[0] == 'ETH'


def test_notification_failure_does_not_remove_quarantine_or_mask_original_error():
    trader, group, _, result = executed_open()
    with patch('calc.trading_executor.db_manager.get_cursor', side_effect=RuntimeError('ledger error')), \
         patch('calc.trading_executor.upsert_popup_notification', side_effect=RuntimeError('notify error')), \
         patch('calc.trading_executor.logger') as logger, \
         pytest.raises(RuntimeError, match='ledger error'):
        trader._save_orders(group, result)
    assert trader._open_persistence_failed_assets == {'BEL'}
    assert 'maker-235' in logger.critical.call_args.args[3]
    assert 'fallback-529' in logger.critical.call_args.args[3]


def test_rejected_order_persistence_failure_does_not_claim_a_fill():
    trader, group, _ = opening()
    with patch('calc.trading_executor.db_manager.get_cursor', side_effect=RuntimeError('db error')), \
         patch('calc.trading_executor.upsert_popup_notification') as notify, \
         pytest.raises(RuntimeError):
        trader._save_orders(group, {'success': False, 'message': 'FOK not filled'})
    assert trader._open_persistence_failed_assets == set()
    notify.assert_not_called()


def test_partial_maker_spot_failure_unwinds_all_filled_contracts():
    _, group, executor = opening()
    first = gate_fill(executor, 235, 0.119, 'maker')
    first['execution_stats'] = {'future_maker': {
        'requested_contracts': 764, 'filled_contracts': 235, 'terminal_confirmed': True,
    }}
    second = gate_fill(executor, 529, 0.11874, 'fallback')
    unwind = gate_fill(executor, 764, 0.119, 'unwind')
    executor._place_gate_futures_order = MagicMock(side_effect=[first, second, unwind])
    executor._place_binance_spot_order = MagicMock(return_value={'success': False, 'reason': 'rejected'})
    result = executor.execute(group, {})
    assert not result['success']
    assert executor._place_gate_futures_order.call_count == 3
    unwind_order = executor._place_gate_futures_order.call_args.args[0]
    assert unwind_order['target_contracts'] == 764
    assert unwind_order['target_qty'] == 764
    # The Gate request builder derives reduce_only from order_side='close'.
    assert unwind_order['order_side'] == 'close'
    assert unwind_order['trade_direction'] == 'buy'
