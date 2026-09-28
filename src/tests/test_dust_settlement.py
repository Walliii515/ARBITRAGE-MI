"""Durable conversion fault injection. No exchange orders are sent by these tests."""
from contextlib import nullcontext
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from calc.closed_position_pnl import compute_closed_position_pnl
from calc.dust_conversion import allocate, event_time, number, receipt, value_receipt
from calc.dust_settlement import DustSettlement, PENDING
from calc.real_executor import ExchangeConfig, RealExecutor


def conversion(qty='0.1', at=1788000000000, asset='TST'):
    return receipt({'fromAsset': asset, 'tranId': '123456', 'operateTime': at,
                    'amount': qty, 'transferedAmount': '0.0002', 'serviceChargeAmount': '0.000004'})


def ledger(pid=1, spot_close='0.9'):
    return [dict(position_id=pid, market_type=market, order_side=side,
                 exec_qty=Decimal(qty), exec_amount=Decimal(amount), status='executed',
                 fee_amount_usdt=0, executed_at=datetime(2026, 8, 29, 14))
            for market, side, qty, amount in [
                ('spot', 'open', '1', '1'), ('future', 'open', '1', '1.2'),
                ('spot', 'close', spot_close, spot_close), ('future', 'close', '1', '1.1')]]


@pytest.mark.parametrize('value', [None, '', '-1', 'NaN', 'Infinity', '-Infinity'])
def test_invalid_receipt_numbers_rejected(value):
    with pytest.raises(ValueError):
        number(value)


def test_receipt_time_and_net_receipt_valuation():
    raw = conversion()
    valued = value_receipt(raw, '600')
    assert Decimal(valued['exec_amount_usdt']) == Decimal('0.12')
    assert Decimal(valued['gross_exec_amount_usdt']) == Decimal('0.1224')
    assert Decimal(valued['service_charge_usdt']) == Decimal('0.0024')
    assert event_time(raw) == datetime(2026, 8, 29, 18, 40)
    assert valued['valuation_source'] == 'binance_BNBUSDT_1m_open'
    with pytest.raises(ValueError):
        value_receipt(raw, None)


def test_decimal_allocation_reconciles_exactly_and_rejects_excess():
    valued = value_receipt(conversion(qty='3'), '612.12345678')
    parts = allocate(valued, [dict(id=i, _spot_remaining_qty='1') for i in range(3)])
    for field in ('gross_exec_amount_usdt', 'service_charge_usdt', 'service_charge_bnb'):
        assert sum(p[field] for p in parts) == Decimal(valued[field]).quantize(Decimal('0.00000001'))
    with pytest.raises(ValueError, match='exceeds'):
        allocate(valued, [dict(id=1, _spot_remaining_qty='2.99')])


@pytest.mark.parametrize('closed', ['0', '0.9'])
def test_pending_cost_only_realized_on_actual_conversion(closed):
    pos = {'exchange_risk_type': PENDING}
    before = compute_closed_position_pnl(pos, ledger(spot_close=closed))
    assert before['realized_spot_pnl'] == 0
    assert before['realized_future_pnl'] == 0.1
    assert compute_closed_position_pnl(pos, ledger()[:2]) is None


@pytest.fixture
def service(monkeypatch):
    remediator = MagicMock()
    obj = DustSettlement(remediator)
    monkeypatch.setattr('calc.dust_settlement.database_lock', lambda *a, **k: nullcontext(True))
    monkeypatch.setattr('calc.dust_settlement.asset_reduction_guard.claim', lambda *a, **k: nullcontext(True))
    monkeypatch.setattr('calc.dust_settlement.config.get_bool', lambda *a: True)
    obj._tasks = MagicMock(return_value=[])
    obj._error = MagicMock()
    obj.cooldown_remaining = MagicMock(return_value=0)
    return obj


def task(state='submitted'):
    r = conversion()
    return dict(id=1, base_asset='TST', requested_at=event_time(r), status=state,
                conversion_json=r, expected_qty='0.1', positions_json=[
                    dict(id=1, _spot_remaining_qty='0.1', _future_remaining_qty='0')])


@pytest.mark.parametrize('results', [[], [conversion(), conversion()], [conversion(qty='0.05')]])
def test_unknown_restart_never_resends_or_accounts_ambiguous_history(service, results):
    service._tasks.return_value = [task()]
    service.executor.fetch_binance_dust_history.return_value = results
    service._account = MagicMock()
    service._confirm = MagicMock()
    assert service.recover() == []
    service.executor.convert_binance_spot_dust_to_bnb_batch.assert_not_called()
    service._account.assert_not_called()
    service._error.assert_called_once()


def test_response_lost_then_restart_uses_history_only(service):
    original = task()
    service._tasks.return_value = [original]
    service.executor.fetch_binance_dust_history.return_value = [conversion()]
    service._confirm = MagicMock(side_effect=lambda t, r: t.update(status='confirmed', conversion_json=r))
    service._account = MagicMock()
    assert service.recover()[0]['success']
    service._account.assert_called_once_with(original)
    service.executor.convert_binance_spot_dust_to_bnb_batch.assert_not_called()


def test_missing_event_price_keeps_confirmed_receipt(service):
    original = task('confirmed')
    service._tasks.return_value = [original]
    service.executor.fetch_binance_bnb_event_price.side_effect = ValueError('no price')
    assert service.recover() == []
    assert original['status'] == 'confirmed'
    service._error.assert_called_once()
    service.executor.fetch_binance_dust_history.assert_not_called()
    service.executor.convert_binance_spot_dust_to_bnb_batch.assert_not_called()


def test_recent_recovery_is_rate_limited(service):
    original = dict(task(), last_checked_at=datetime.now())
    service._tasks.return_value = [original]
    assert service.recover() == []
    service.executor.fetch_binance_dust_history.assert_not_called()


def test_unresolved_previous_request_blocks_batch(service):
    service._tasks.return_value = [task('review')]
    assert service.execute([])['reason'] == 'dust_previous_request_unresolved'
    service.executor.convert_binance_spot_dust_to_bnb_batch.assert_not_called()


def test_cooldown_blocks_submission(service):
    service.cooldown_remaining.return_value = 123
    assert service.execute([])['cooldown_remaining_sec'] == 123
    service.executor.convert_binance_spot_dust_to_bnb_batch.assert_not_called()


def test_confirmation_rejects_changed_asset_quantity_or_time(service, monkeypatch):
    for invalid in [conversion(qty='0.2'), conversion(asset='OTHER'), conversion(at=1788100000000)]:
        with pytest.raises(ValueError, match='mismatch'):
            service._confirm(task(), invalid)


def test_confirmed_request_without_post_can_be_cancelled(service, monkeypatch):
    cursor = MagicMock()
    monkeypatch.setattr('calc.dust_settlement.db_manager.get_cursor', lambda: nullcontext(cursor))
    service._tasks.return_value = [task('pending')]
    assert service.recover() == []
    assert "status='cancelled'" in cursor.execute.call_args.args[0]
    service.executor.convert_binance_spot_dust_to_bnb_batch.assert_not_called()


def test_intent_commits_before_post_and_unknown_response_is_retained(service, monkeypatch):
    from contextlib import contextmanager
    events = []
    cursor = MagicMock(lastrowid=1)
    @contextmanager
    def transaction():
        events.append('begin')
        yield cursor
        events.append('commit')
    monkeypatch.setattr('calc.dust_settlement.db_manager.get_cursor', transaction)
    def post(*args, **kwargs):
        assert events == ['begin', 'commit', 'begin', 'commit']
        assert kwargs['client_id']
        return {'success': False, 'reason': 'timeout'}
    service.executor.convert_binance_spot_dust_to_bnb_batch.side_effect = post
    result = service._submit([{'base_asset': 'TST', 'spot_qty': '0.1', 'positions': []}])
    assert not result['success'] and result['attempted']
    assert service._error.call_args.args[0]['status'] == 'submitted'


def test_batch_one_missing_receipt_does_not_lose_successful_receipt(service, monkeypatch):
    cursor = MagicMock(lastrowid=1)
    monkeypatch.setattr('calc.dust_settlement.db_manager.get_cursor', lambda: nullcontext(cursor))
    service.executor.convert_binance_spot_dust_to_bnb_batch.return_value = {'results': {'TST': conversion()}}
    service._confirm = MagicMock()
    service._account = MagicMock()
    result = service._submit([dict(base_asset=a, spot_qty='0.1', positions=[]) for a in ('TST', 'B')])
    assert [r['success'] for r in result['results']] == [True, False]
    service._account.assert_called_once()
    service.executor.convert_binance_spot_dust_to_bnb_batch.assert_called_once()


def test_history_limit_and_wrong_event_price_fail_closed():
    executor = RealExecutor(ExchangeConfig())
    executor._binance_signed_get = MagicMock(return_value={'userAssetDribblets': [{}] * 100})
    with pytest.raises(ValueError, match='truncated'):
        executor.fetch_binance_dust_history(0, 1000)
    executor._session = MagicMock()
    executor._session.get.return_value.json.return_value = [[0, '600']]
    with pytest.raises(ValueError, match='historical'):
        executor.fetch_binance_bnb_event_price(1788000000000)


def test_open_is_blocked_while_dust_task_unsettled(monkeypatch):
    from calc.trading_executor import TradingExecutor
    executor = TradingExecutor.__new__(TradingExecutor)
    executor.executor_client = MagicMock()
    monkeypatch.setattr('common.database_lock.database_lock', lambda *a: nullcontext(True))
    monkeypatch.setattr('calc.dust_settlement.has_unsettled_dust', lambda a: True)
    assert not executor._execute_and_save_open({'spot_order': {'base_asset': 'TST'}}, {})['success']
    executor.executor_client.execute.assert_not_called()


def test_capital_collection_discards_stale_cached_realized(monkeypatch):
    from calc.account_capital import AccountCapitalSnapshotter
    snapshotter = AccountCapitalSnapshotter(MagicMock())
    snapshotter._run_once_locked = MagicMock(return_value={'success': True})
    monkeypatch.setattr('common.database_lock.database_lock', lambda *a, **k: nullcontext(True))
    snapshotter.run_once({'realized_pnl': 99, 'fee_cost': -5, 'floating_pnl': 2})
    snapshotter._run_once_locked.assert_called_once_with({'floating_pnl': 2})


def test_database_lock_released_after_exception(monkeypatch):
    from common.database_lock import database_lock
    cursor = MagicMock()
    cursor.fetchone.return_value = {'acquired': 1}
    conn = MagicMock()
    conn.cursor.return_value = nullcontext(cursor)
    monkeypatch.setattr('common.database_lock.db_manager.get_connection', lambda: nullcontext(conn))
    with pytest.raises(RuntimeError):
        with database_lock('test'):
            raise RuntimeError('boom')
    assert 'RELEASE_LOCK' in cursor.execute.call_args.args[0]


def prepare_candidate(service):
    pos = dict(id=1, status='closed', base_asset='TST', exchange_risk_type=PENDING,
               _spot_remaining_qty='0.1', _future_remaining_qty='0')
    candidate = dict(eligible=True, base_asset='TST', positions=[pos], spot_qty='0.1')
    service.remediator._load_holding_positions_with_execution_remainders.return_value = [pos]
    service.remediator._prepare_dust_cleanup_candidate.return_value = candidate
    service.executor.fetch_binance_spot_balances.return_value = [dict(asset='TST', total='0.1', free='0.1')]
    service.executor.fetch_gate_futures_positions.return_value = []
    service.executor._get_binance_usdt_price.return_value = '1'
    service.executor.spot_meta = {'TST': {'min_notional': '5'}}
    service._check_sources = MagicMock()
    service._submit = MagicMock(return_value={'success': True})
    return candidate


def test_verified_candidate_reaches_single_batch_submission(service):
    candidate = prepare_candidate(service)
    assert service.execute([candidate])['success']
    service._submit.assert_called_once()
    service._check_sources.assert_called_once()


@pytest.mark.parametrize('change', ['locked', 'quantity', 'future', 'price'])
def test_final_fresh_verification_blocks_balance_or_price_changes(service, change):
    candidate = prepare_candidate(service)
    original = dict(asset='TST', total='0.1', free='0.1')
    changed = dict(original)
    if change == 'locked':
        changed['locked'] = '0.01'
    elif change == 'quantity':
        changed['total'] = '0.2'
    elif change == 'future':
        service.executor.fetch_gate_futures_positions.side_effect = [[], [dict(base_asset='TST', size=-1)]]
    else:
        service.executor._get_binance_usdt_price.side_effect = ['1', '100']
    service.executor.fetch_binance_spot_balances.side_effect = [[original], [changed]]
    with pytest.raises(ValueError):
        service.execute([candidate])
    service._submit.assert_not_called()


def test_gate_partial_failure_never_converts_spot(service):
    candidate = prepare_candidate(service)
    service.executor.fetch_gate_futures_positions.return_value = [dict(base_asset='TST', size=-1)]
    service.remediator._close_gate_dust_before_conversion.return_value = {'failed': True}
    assert not service.execute([candidate])['success']
    service._submit.assert_not_called()


@pytest.mark.parametrize('source', ['reverse', 'uncertain', 'risk_event'])
def test_uncertain_sources_reject_conversion(service, monkeypatch, source):
    cursor = MagicMock()
    cursor.fetchone.side_effect = [{'id': 1} if source == 'reverse' else None,
                                   {'id': 2} if source == 'risk_event' else None]
    cursor.fetchall.return_value = [{'status': 'pending'}] if source == 'uncertain' else []
    monkeypatch.setattr('calc.dust_settlement.db_manager.get_cursor', lambda: nullcontext(cursor))
    with pytest.raises(ValueError):
        service._check_sources('TST', [dict(opened_at=datetime(2026,8,1))])
