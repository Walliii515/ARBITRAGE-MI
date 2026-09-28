from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from calc.reconciliation import Reconciler, ReconciliationConfig
from calc.exchange_desync_remediator import ExchangeDesyncRemediator, ExchangeDesyncRemediationConfig
from tests.test_open_close_logic import make_closing_executor
from tests.test_closing_executor_cross_margin_risk import _risk_position


def reconciler():
    executor = MagicMock(contract_meta={'AI': {'quanto_multiplier': 1}, 'TUT': {'quanto_multiplier': 100}})
    executor.fetch_binance_spot_balances.return_value = [
        {'asset': asset, 'total': 100, 'locked': 0} for asset in ('AI', 'TUT')
    ]
    executor.fetch_gate_futures_positions.return_value = [
        {'base_asset': asset, 'size': -90} for asset in ('AI', 'TUT')
    ]
    rec = Reconciler(executor, ReconciliationConfig(auto_remediate_enabled=True))
    rec._write_reconciliation_risk_event = MagicMock()
    return rec


def combined(asset='AI', **kwargs):
    return dict(base_asset=asset, risk_type='binance_spot_excess', confirmed=True,
                risk={'type': 'binance_spot_excess'}, binance_qty=100, gate_qty=90,
                gate_contracts=90, quanto_multiplier=1, **kwargs)


@pytest.mark.parametrize('error', [TimeoutError('timeout'), ValueError('invalid fill'), RuntimeError('db')])
def test_combined_failure_isolated_and_next_asset_still_reduces(error):
    rec = reconciler()
    rec.remediator.remediate_binance_spot_desync = MagicMock(side_effect=[error, {'success': True}])
    results = rec._auto_remediate_combined_exposure_risks(datetime.now(), [combined(), combined('TUT')])
    assert results[0]['exchange_order_state_unknown']
    assert results[0]['retry_needed']
    assert results[1]['success']
    assert rec.remediator.remediate_binance_spot_desync.call_count == 2


def test_event_write_failure_does_not_lose_snapshot_ownership_or_starve_other_assets():
    rec = reconciler()
    rec._write_reconciliation_risk_event.side_effect = RuntimeError('event table unavailable')
    rec.remediator.remediate_binance_spot_desync = MagicMock(return_value={
        'success': True, 'exchange_order_submitted': True,
    })
    risks = [combined(), combined('TUT')]
    results = rec._auto_remediate_combined_exposure_risks(datetime.now(), risks)
    assert len(results) == 2
    assert rec._gate_remediation_owned_assets(risks, results) == {'AI', 'TUT'}


@pytest.mark.parametrize('marker_name', ['_mark_gate_desync_risks', '_mark_combined_exposure_risks'])
def test_one_broken_risk_row_does_not_drop_other_risks(marker_name):
    rec = reconciler()
    marker = MagicMock(side_effect=[ValueError('bad event'), [{'base_asset': 'TUT'}]])
    setattr(rec, marker_name, marker)
    rows = [{'base_asset': 'AI'}, {'base_asset': 'TUT'}]
    result = rec._collect_asset_risks(getattr(rec, marker_name), datetime.now(), rows)
    assert result == [{'base_asset': 'TUT'}]
    assert rows[0]['detail']['risk_processing_error'] == 'bad event'


@pytest.mark.parametrize('last,confirmed', [
    ({'is_match': 0, 'risk_type': 'binance_spot_excess'}, True),
    ({'is_match': 1, 'risk_type': None}, False),
    ({'is_match': 0, 'risk_type': 'gate_short_excess'}, False),
    ({'is_match': 0, 'risk_type': None}, False),
])
def test_confirmation_requires_most_recent_snapshot_same_direction(last, confirmed):
    rec = reconciler()
    with patch('calc.reconciliation.db_manager.get_cursor') as db:
        cursor = db.return_value.__enter__.return_value
        cursor.fetchall.return_value = [last]
        assert rec._is_combined_exposure_risk_confirmed('AI', 'binance_spot_excess', datetime.now()) == confirmed
        sql = cursor.execute.call_args.args[0]
        assert 'AND is_match = 0' not in sql
        assert 'ORDER BY snapshot_at DESC, id DESC' in sql


@pytest.mark.parametrize('multiplier', [0.1, 1, 10, 100])
@pytest.mark.parametrize('spot_contract_equivalent,action', [(100, 'none'), (90, 'gate'), (110, 'spot')])
def test_gate_extra_remediates_only_real_unhedged_quantity(multiplier, spot_contract_equivalent, action):
    rec = reconciler()
    rec.executor.contract_meta = {'AI': {'quanto_multiplier': multiplier}}
    item = dict(base_asset='AI', confirmed=True, local_contracts=80, exchange_contracts=100,
                extra_contracts=20, risk={'type': 'extra_gate_position', 'exchange_size': -100})
    spot = {'AI': {'exchange_value': spot_contract_equivalent * multiplier}}
    rec.executor.fetch_binance_spot_balances.return_value = [{'asset': 'AI', 'total': spot_contract_equivalent * multiplier}]
    rec.executor.fetch_gate_futures_positions.return_value = [{'base_asset': 'AI', 'size': -100}]
    rec.remediator.remediate_gate_extra_position = MagicMock(return_value={'success': True})
    rec.remediator.remediate_binance_spot_desync = MagicMock(return_value={'success': True})
    result = rec._remediate_confirmed_gate_risk(item, item['risk'], spot)
    if action == 'gate':
        assert rec.remediator.remediate_gate_extra_position.call_args.kwargs['extra_contracts'] == pytest.approx(10)
        rec.remediator.remediate_binance_spot_desync.assert_not_called()
    elif action == 'spot':
        call = rec.remediator.remediate_binance_spot_desync.call_args.kwargs
        assert call['exchange_qty'] - call['local_qty'] == pytest.approx(10 * multiplier)
        rec.remediator.remediate_gate_extra_position.assert_not_called()
    else:
        assert result['reason'] == 'exchange_legs_balanced_local_ledger_stale'
        rec.remediator.remediate_gate_extra_position.assert_not_called()
        rec.remediator.remediate_binance_spot_desync.assert_not_called()


def test_gate_extra_never_trades_without_binance_snapshot_or_with_gate_long():
    rec = reconciler()
    rec.remediator.remediate_gate_extra_position = MagicMock()
    item = dict(base_asset='AI', exchange_contracts=100, local_contracts=80)
    result = rec._remediate_confirmed_gate_risk(item, {'type': 'extra_gate_position', 'exchange_size': -100}, {})
    assert result['reason'] == 'remediation_snapshot_unavailable'
    rec.executor.fetch_binance_spot_balances.return_value = []
    rec.executor.fetch_gate_futures_positions.return_value = [{'base_asset': 'AI', 'size': 100}]
    result = rec._remediate_confirmed_gate_risk(item, {'type': 'extra_gate_position', 'exchange_size': 100}, {'AI': {'exchange_value': 0}})
    assert result['reason'] == 'remediation_snapshot_unsettled'
    rec.remediator.remediate_gate_extra_position.assert_not_called()


@pytest.mark.parametrize('gate_submitted,binance_failed', [(False, True), (True, False)])
def test_dust_does_not_use_failed_or_consumed_exchange_snapshots(gate_submitted, binance_failed):
    rec = reconciler()
    rec._load_local_spot_positions = MagicMock(return_value={})
    rec._load_local_gate_positions = MagicMock(return_value={})
    rec.executor.fetch_binance_spot_balances.return_value = []
    if binance_failed:
        rec.executor.fetch_binance_spot_balances.side_effect = TimeoutError('binance unavailable')
    rec.executor.fetch_gate_futures_positions.return_value = []
    rec._collect_asset_risks = MagicMock(return_value=[{'base_asset': 'AI'}])
    rec._auto_remediate_gate_risks = MagicMock(return_value=[{'exchange_order_submitted': gate_submitted}])
    rec._auto_remediate_combined_exposure_risks = MagicMock(return_value=[])
    rec._auto_cleanup_completed_asset_dust = MagicMock(return_value=[])
    rec._insert_rows = MagicMock()
    rec.cleanup_old_snapshots = MagicMock()
    rec.run_once()
    if binance_failed:
        rec._auto_cleanup_completed_asset_dust.assert_not_called()
    else:
        assert rec._auto_cleanup_completed_asset_dust.call_args.kwargs['skip_assets'] == {'AI'}


def test_dust_skips_only_consumed_asset_even_after_reloading_positions(monkeypatch):
    monkeypatch.setattr('calc.dust_settlement.DustSettlement.recover', lambda self: [])
    rem = ExchangeDesyncRemediator(MagicMock(), ExchangeDesyncRemediationConfig())
    rows = [dict(base_asset=asset) for asset in ('AI', 'TUT')]
    rem._load_holding_positions_with_execution_remainders = MagicMock(return_value=rows)
    rem._settle_spot_only_dust_positions = MagicMock(return_value=[{'base_asset': 'TUT'}])
    rem._recover_completed_dust_cleanup = MagicMock(return_value={'success': True})
    rem.cleanup_post_close_dust([], [], skip_assets={'AI'})
    assert rem._settle_spot_only_dust_positions.call_args.args[0] == [rows[1]]
    assert set(rem._recover_completed_dust_cleanup.call_args.args[0]) == {'TUT'}


@pytest.mark.parametrize('future_result', [{'success': True}, {'success': False, 'retry_needed': True}])
def test_spot_only_tiny_lots_use_shared_aggregate_and_preserve_result(future_result):
    executor = MagicMock(contract_meta={'AI': {'quanto_multiplier': 1}})
    rem = ExchangeDesyncRemediator(executor, ExchangeDesyncRemediationConfig())
    positions = [dict(id=i, spot_open_qty=3, future_open_qty=0) for i in (1, 2, 3)]
    rem._load_spot_only_positions_to_remediate = MagicMock(return_value=positions)
    rem._mark_positions_exchange_risk = MagicMock()
    rem.remediate_binance_spot_desync = MagicMock(return_value=future_result)
    result = rem.remediate_binance_spot_only_exposure('AI', 9, {'type': 'missing_gate_position'})
    assert result is future_result
    args = rem.remediate_binance_spot_desync.call_args.kwargs
    assert args['exchange_qty'] == 9
    assert args['risk']['type'] == 'binance_spot_excess'
    assert args['local_qty'] == 0


@pytest.mark.parametrize('db_row', [None, {'status': 'holding', 'exchange_risk_status': 'normal'}, 'error'])
def test_partial_position_list_cannot_release_execution_quarantine(db_row):
    ce = make_closing_executor()
    ce._quarantine_executed_asset(_risk_position(id=11))
    with patch('calc.closing_executor.db_manager.get_cursor') as db:
        if db_row == 'error':
            db.side_effect = RuntimeError('db unavailable')
        else:
            db.return_value.__enter__.return_value.fetchone.return_value = db_row
        assert 'TUT' in ce._desynced_assets([_risk_position(id=12, base_asset='AI')])
        assert 'TUT' in ce._desynced_assets([])


@pytest.mark.parametrize('success', [True, False])
def test_delist_bypasses_ordinary_cooldown_but_stops_after_failed_asset(success):
    ce = make_closing_executor()
    ce.set_delist_risk_report({'items': [{'base_asset': 'TUT', 'status': 'delisting'}]})
    ce._close_cooldown['TUT'] = datetime.now()
    ce._execute_close = MagicMock(return_value={'success': success})
    positions = [_risk_position(id=i, current_spread_bps=None) for i in (11, 12)]
    ce.check_and_close(positions, {}, {})
    assert ce._execute_close.call_count == (2 if success else 1)


@pytest.mark.parametrize('failure', ['books', 'funding', 'pnl'])
def test_optional_economics_failure_cannot_block_emergency_close(failure):
    from api import orderbook_server as srv
    ce = MagicMock()
    ce.margin_risk_refresh_summary.return_value = {'danger': [], 'missing': []}
    ce.check_and_close_margin_danger.return_value = [{'success': True}]
    tracker = MagicMock()
    pos = _risk_position()
    tracker.get_holding_positions.return_value = [pos]
    if failure == 'funding':
        tracker.attach_funding_histories.side_effect = RuntimeError('funding failed')
    service = MagicMock(state=srv.SERVICE_RUNNING)
    with (
        patch.object(srv, 'svc', service), patch.object(srv, '_closing_executor', ce),
        patch.object(srv, '_configure_closing_executor'),
        patch.object(srv, 'PositionTracker', return_value=tracker),
        patch.object(srv, 'attach_gate_position_risk'),
        patch.object(srv, '_get_gate_position_risk_snapshot', return_value=[]),
        patch.object(srv, '_get_live_gate_cross_risk_snapshot', return_value={'account_mmr_pct': 250}),
        patch.object(srv, '_get_merged_rows', return_value=[], side_effect=RuntimeError('books failed') if failure == 'books' else None),
        patch.object(srv, 'calculate_realtime_pnl', side_effect=RuntimeError('pnl failed') if failure == 'pnl' else None),
        patch.object(srv, '_publish_close_position_results'),
    ):
        srv._run_close_position_check_once()
    ce.check_and_close_margin_danger.assert_called_once_with([pos], {})
    assert 'current_spread_bps' not in pos
