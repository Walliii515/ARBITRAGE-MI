import copy
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from calc.closing_executor import ClosingExecutor
from calc.spot_residual import residual_quantity, proportional_spot_slice, VERIFIED_SPOT_RESIDUAL


@pytest.fixture
def snapshot():
    return {
        'local_spot_qty': '31270.1', 'local_gate_qty': '31270',
        'binance_qty': '31270.1', 'gate_qty': '31270',
        'binance_detail': {'locked': 0},
        'gate_detail': {'size': -3127, 'mark_price': '0.00438'},
    }


def test_rez_residual_preserves_raw_quantities(snapshot):
    original = copy.deepcopy(snapshot)
    assert residual_quantity(snapshot, {'step_size': .1, 'min_notional': 5}) == Decimal('.1')
    assert snapshot == original


@pytest.mark.parametrize('key,value', [
    ('local_spot_qty', '31269.9'), ('binance_qty', 31270),
    ('gate_qty', 31269), ('local_gate_qty', 0),
    ('local_spot_qty', 'NaN'), ('binance_qty', 'Infinity'),
    ('local_spot_qty', None),
])
def test_invalid_quantities_never_receive_permission(snapshot, key, value):
    snapshot[key] = value
    assert residual_quantity(snapshot, {'step_size': .1, 'min_notional': 5}) is None


@pytest.mark.parametrize('case', ['locked', 'long', 'no_price', 'no_step', 'cap', 'bps', 'tradable'])
def test_limits_and_market_conditions(snapshot, case):
    meta = {'step_size': .1, 'min_notional': 5}
    if case == 'locked': snapshot['binance_detail']['locked'] = .1
    if case == 'long': snapshot['gate_detail']['size'] = 3127
    if case == 'no_price': snapshot['gate_detail']['mark_price'] = None
    if case == 'no_step': meta['step_size'] = 0
    if case == 'cap': snapshot['gate_detail']['mark_price'] = 11
    if case == 'bps': snapshot['binance_qty'] = snapshot['local_spot_qty'] = 31274
    if case == 'tradable': meta['min_notional'] = .0001
    assert residual_quantity(snapshot, meta) is None


def test_decimal_slice_avoids_rez_one_step_underfill():
    assert proportional_spot_slice(24340, 336, 2434, .1) == 3360
    assert proportional_spot_slice(20980.1, 336, 2098, .1) == 3360
    for contracts in range(1, 2099):
        qty = Decimal(str(proportional_spot_slice('20980.1', contracts, 2098, '.1')))
        assert qty % Decimal('.1') == 0
        assert qty <= Decimal('20980.1') * contracts / 2098


@pytest.mark.parametrize('args', [(1, 2, 1, .1), (1, 1, 1, 0), ('NaN', 1, 1, .1)])
def test_slice_rejects_invalid_inputs(args):
    with pytest.raises(ValueError): proportional_spot_slice(*args)


@pytest.mark.parametrize('outcome', [0, 1, RuntimeError('offline')])
def test_close_checks_marked_sibling_not_just_current_position(outcome):
    ce = ClosingExecutor.__new__(ClosingExecutor)
    pos = {'id': 658, 'base_asset': 'REZ', 'exchange_risk_type': None}
    with patch('calc.closing_executor.db_manager.get_cursor') as db, \
         patch('calc.reconciliation.build_default_reconciler') as builder:
        db.return_value.__enter__.return_value.fetchone.return_value = {'id': 647}
        builder.return_value._quantity_recovery_note = '复核未通过'
        check = builder.return_value._recover_matched_quantity_risk
        if isinstance(outcome, Exception): check.side_effect = outcome
        else: check.return_value = outcome
        result = ce._verify_residual_close(pos)
        assert (result is None) == (outcome == 1)
        assert check.call_args.kwargs['dry_run'] is True
        assert check.call_args.kwargs['expected_position'] == pos


def test_database_failure_never_bypasses_close_check():
    ce = ClosingExecutor.__new__(ClosingExecutor)
    with patch('calc.closing_executor.db_manager.get_cursor', side_effect=RuntimeError('offline')):
        assert ce._verify_residual_close({'base_asset': 'REZ'}) is not None


def test_new_sibling_risk_blocks_even_when_caller_has_old_normal_snapshot():
    ce = ClosingExecutor.__new__(ClosingExecutor)
    with patch('calc.closing_executor.db_manager.get_cursor') as db:
        db.return_value.__enter__.return_value.fetchone.return_value = {
            'id': 647, 'exchange_risk_status': 'desynced', 'exchange_risk_type': 'adl',
        }
        assert ce._verify_residual_close({'id': 658, 'base_asset': 'REZ'}) is not None


def test_failed_residual_check_never_submits_order():
    ce = ClosingExecutor.__new__(ClosingExecutor)
    ce._check_spot_close_min_notional = MagicMock(return_value=(True, ''))
    ce._verify_residual_close = MagicMock(return_value='changed')
    ce._trigger_reconciliation = MagicMock()
    ce.executor_client = MagicMock()
    result = ce._execute_close({'id': 647, 'base_asset': 'REZ'}, 'margin_close', '', {})
    assert not result['success']
    ce.executor_client.execute.assert_not_called()
