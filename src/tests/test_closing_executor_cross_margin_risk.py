# coding: utf-8
import os
import sys
import time
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tests.test_open_close_logic import (
    make_closing_executor,
    make_gate_cross_risk,
)


def _risk_position(**overrides):
    pos = {
        'id': 11,
        'base_asset': 'TUT',
        'future_contract': 'TUT_USDT',
        'spot_symbol': 'TUTUSDT',
        'status': 'holding',
        'spot_open_qty': 10.0,
        'future_open_qty': 10.0,
        'future_open_contracts': 10,
        'spot_open_amount': 10.0,
        'future_open_price': 1.0,
        'open_spread_bps': 100.0,
        'current_spread_bps': 100.0,
        'gate_contract_maintenance_margin': 1.0,
        'gate_liq_price': 1.05,
        'gate_mark_price': 1.03,
    }
    pos.update(overrides)
    return pos


class TestClosingExecutorGateCrossRisk(unittest.TestCase):
    def test_350_stage_closes_only_profitable_position_and_uses_one_snapshot_once(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='warning', account_mmr_pct=340.0),
        }
        losing = _risk_position(
            open_spread_bps=100.0,
            current_spread_bps=600.0,
            gate_liq_price=2.0,
            gate_mark_price=1.0,
        )
        profitable = _risk_position(
            id=12,
            base_asset='AI',
            future_contract='AI_USDT',
            spot_symbol='AIUSDT',
            spot_open_amount=100.0,
            open_spread_bps=800.0,
            current_spread_bps=100.0,
            gate_liq_price=2.0,
            gate_mark_price=1.0,
        )
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'AI', 'success': True, 'close_reason': 'margin_close',
        })

        first = ce.check_and_close_margin_danger([losing, profitable], {})
        second = ce.check_and_close_margin_danger([losing, profitable], {})

        self.assertEqual([item['position_id'] for item in first], [12])
        self.assertEqual(first[0]['margin_risk_stage'], 'profit_release_350')
        self.assertIn('保证金盈利释放', ce._execute_close.call_args.args[2])
        self.assertEqual(second, [])
        ce._execute_close.assert_called_once()

    def test_350_stage_is_disabled_with_auto_funding_switch(self):
        ce = make_closing_executor()
        ce.set_auto_fund_transfer_enabled_provider(lambda: False)
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='warning', account_mmr_pct=340.0),
        }
        ce._execute_close = MagicMock()

        result = ce.check_and_close_margin_danger([
            _risk_position(open_spread_bps=800.0, current_spread_bps=100.0),
        ], {})

        self.assertEqual(result, [])
        ce._execute_close.assert_not_called()

    def test_350_stage_failed_attempt_waits_for_a_new_official_snapshot(self):
        ce = make_closing_executor()
        risk = make_gate_cross_risk(status='warning', account_mmr_pct=350.0)
        ce._gate_cross_risk_cache = {'ts': time.time(), 'risk': risk}
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'TUT',
            'success': False,
            'close_reason': 'margin_close',
            'future_exec_qty': 0.0,
            'spot_exec_qty': 0.0,
            'gate_reduction_consumed': False,
            'message': 'Gate明确拒单',
        })
        position = _risk_position(
            open_spread_bps=800.0,
            current_spread_bps=100.0,
            gate_liq_price=2.0,
            gate_mark_price=1.0,
        )

        first = ce.check_and_close_margin_danger([position], {})
        same_snapshot = ce.check_and_close_margin_danger([position], {})

        self.assertEqual(len(first), 1)
        self.assertFalse(first[0]['success'])
        self.assertEqual(same_snapshot, [])
        ce._execute_close.assert_called_once()

    def test_350_stage_exception_waits_for_a_new_official_snapshot(self):
        ce = make_closing_executor()
        risk = make_gate_cross_risk(status='warning', account_mmr_pct=340.0)
        ce._gate_cross_risk_cache = {'ts': time.time(), 'risk': risk}
        ce._execute_close = MagicMock(side_effect=RuntimeError('executor unavailable'))
        position = _risk_position(
            open_spread_bps=800.0,
            current_spread_bps=100.0,
            gate_liq_price=2.0,
            gate_mark_price=1.0,
        )

        first = ce.check_and_close_margin_danger([position], {})
        same_snapshot = ce.check_and_close_margin_danger([position], {})

        self.assertEqual(len(first), 1)
        self.assertFalse(first[0]['success'])
        self.assertEqual(same_snapshot, [])
        ce._execute_close.assert_called_once()

    def test_350_stage_requires_live_economic_data_and_positive_net_result(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='warning', account_mmr_pct=350.0),
        }
        ce._execute_close = MagicMock()

        missing = _risk_position(
            current_spread_bps=None,
            gate_liq_price=2.0,
            gate_mark_price=1.0,
        )
        breakeven_or_loss = _risk_position(
            id=12,
            base_asset='AI',
            future_contract='AI_USDT',
            spot_symbol='AIUSDT',
            open_spread_bps=100.0,
            current_spread_bps=100.0,
            gate_liq_price=2.0,
            gate_mark_price=1.0,
        )

        result = ce.check_and_close_margin_danger([missing, breakeven_or_loss], {})

        self.assertEqual(result, [])
        ce._execute_close.assert_not_called()

    def test_mmr_just_above_350_does_not_release_a_profitable_position(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='warning', account_mmr_pct=350.01),
        }
        ce._execute_close = MagicMock()

        result = ce.check_and_close_margin_danger([
            _risk_position(
                open_spread_bps=800.0,
                current_spread_bps=100.0,
                gate_liq_price=2.0,
                gate_mark_price=1.0,
            ),
        ], {})

        self.assertEqual(result, [])
        ce._execute_close.assert_not_called()

    def test_liquidation_distance_danger_ignores_profit_release_switch(self):
        ce = make_closing_executor()
        ce.set_auto_fund_transfer_enabled_provider(lambda: False)
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='danger', account_mmr_pct=340.0),
        }
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'TUT',
            'success': True,
            'close_reason': 'margin_close',
        })

        result = ce.check_and_close_margin_danger([
            _risk_position(gate_liq_price=1.05, gate_mark_price=1.03),
        ], {})

        self.assertEqual(result[0]['margin_risk_stage'], 'liquidation_distance')
        ce._execute_close.assert_called_once()

    def test_exact_300_and_200_boundaries_use_the_expected_safety_stage(self):
        for mmr, expected_stage in ((300.0, 'controlled_300'), (200.0, 'critical_200')):
            ce = make_closing_executor()
            ce._gate_cross_risk_cache = {
                'ts': time.time(),
                'risk': make_gate_cross_risk(
                    status='danger',
                    account_mmr_pct=mmr,
                    close_priority=[{
                        'contract': 'TUT_USDT',
                        'maintenance_margin_usdt': 100.0,
                    }],
                ),
            }
            ce._execute_close = MagicMock(return_value={
                'base_asset': 'TUT',
                'success': True,
                'close_reason': 'margin_close',
            })

            result = ce.check_and_close_margin_danger([
                _risk_position(gate_liq_price=2.0, gate_mark_price=1.0),
            ], {})

            self.assertEqual(result[0]['margin_risk_stage'], expected_stage)

    def test_active_transfer_permission_block_does_not_block_300_safety_close(self):
        ce = make_closing_executor()
        ce.set_auto_fund_transfer_enabled_provider(lambda: False)
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(
                status='danger',
                account_mmr_pct=290.0,
                close_priority=[{
                    'contract': 'TUT_USDT',
                    'maintenance_margin_usdt': 100.0,
                }],
            ),
        }
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'TUT',
            'success': True,
            'close_reason': 'margin_close',
        })

        result = ce.check_and_close_margin_danger([
            _risk_position(gate_liq_price=2.0, gate_mark_price=1.0),
        ], {})

        self.assertEqual(result[0]['margin_risk_stage'], 'controlled_300')
        ce._execute_close.assert_called_once()

    def test_350_waits_for_a_new_insufficient_decision_after_successful_release(self):
        permission = {'allowed': True}
        ce = make_closing_executor()
        ce.set_auto_fund_transfer_enabled_provider(lambda: permission['allowed'])
        first_risk = make_gate_cross_risk(status='warning', account_mmr_pct=340.0)
        ce._gate_cross_risk_cache = {'ts': time.time(), 'risk': first_risk}
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'TUT',
            'success': True,
            'close_reason': 'margin_close',
        })
        first_position = _risk_position(
            open_spread_bps=800.0,
            current_spread_bps=100.0,
            gate_liq_price=2.0,
            gate_mark_price=1.0,
        )

        first = ce.check_and_close_margin_danger([first_position], {})
        permission['allowed'] = False
        second_risk = make_gate_cross_risk(status='warning', account_mmr_pct=340.0)
        second_risk['account_fetched_at_ts'] = first_risk['account_fetched_at_ts'] + 1
        second_risk['positions_fetched_at_ts'] = first_risk['positions_fetched_at_ts'] + 1
        ce._gate_cross_risk_cache = {'ts': time.time(), 'risk': second_risk}
        blocked = ce.check_and_close_margin_danger([
            _risk_position(
                id=12,
                base_asset='AI',
                future_contract='AI_USDT',
                spot_symbol='AIUSDT',
                open_spread_bps=800.0,
                current_spread_bps=100.0,
                gate_liq_price=2.0,
                gate_mark_price=1.0,
            ),
        ], {})

        self.assertEqual(first[0]['margin_risk_stage'], 'profit_release_350')
        self.assertEqual(blocked, [])
        ce._execute_close.assert_called_once()

    def test_350_allows_next_release_after_auto_funding_reports_insufficient_again(self):
        permission = {'allowed': True}
        ce = make_closing_executor()
        ce.set_auto_fund_transfer_enabled_provider(lambda: permission['allowed'])
        first_risk = make_gate_cross_risk(status='warning', account_mmr_pct=340.0)
        ce._gate_cross_risk_cache = {'ts': time.time(), 'risk': first_risk}
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'TUT',
            'success': True,
            'close_reason': 'margin_close',
        })
        ce.check_and_close_margin_danger([
            _risk_position(
                open_spread_bps=800.0,
                current_spread_bps=100.0,
                gate_liq_price=2.0,
                gate_mark_price=1.0,
            ),
        ], {})

        permission['allowed'] = False
        permission['allowed'] = True
        next_risk = make_gate_cross_risk(status='warning', account_mmr_pct=340.0)
        next_risk['account_fetched_at_ts'] = first_risk['account_fetched_at_ts'] + 1
        next_risk['positions_fetched_at_ts'] = first_risk['positions_fetched_at_ts'] + 1
        ce._gate_cross_risk_cache = {'ts': time.time(), 'risk': next_risk}
        second = ce.check_and_close_margin_danger([
            _risk_position(
                id=12,
                base_asset='AI',
                future_contract='AI_USDT',
                spot_symbol='AIUSDT',
                open_spread_bps=800.0,
                current_spread_bps=100.0,
                gate_liq_price=2.0,
                gate_mark_price=1.0,
            ),
        ], {})

        self.assertEqual(second[0]['margin_risk_stage'], 'profit_release_350')
        self.assertEqual(ce._execute_close.call_count, 2)

    def test_300_without_live_economics_falls_back_to_gate_risk_priority(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(
                status='danger',
                account_mmr_pct=290.0,
                close_priority=[
                    {'contract': 'AI_USDT', 'maintenance_margin_usdt': 90.0},
                    {'contract': 'TUT_USDT', 'maintenance_margin_usdt': 80.0},
                ],
            ),
        }
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'AI',
            'success': True,
            'close_reason': 'margin_close',
        })

        result = ce.check_and_close_margin_danger([
            _risk_position(
                current_spread_bps=None,
                gate_liq_price=2.0,
                gate_mark_price=1.0,
            ),
            _risk_position(
                id=12,
                base_asset='AI',
                future_contract='AI_USDT',
                spot_symbol='AIUSDT',
                current_spread_bps=None,
                gate_liq_price=2.0,
                gate_mark_price=1.0,
            ),
        ], {})

        self.assertEqual(result[0]['position_id'], 12)
        self.assertEqual(result[0]['margin_risk_stage'], 'controlled_300')

    def test_300_stage_prefers_lower_loss_with_comparable_mmr_relief(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(
                status='danger',
                account_mmr_pct=290.0,
                account_equity_usdt=290.0,
                maintenance_margin_usdt=100.0,
                close_priority=[
                    {
                        'contract': 'TUT_USDT',
                        'maintenance_margin_usdt': 100.0,
                        'liq_distance_bps': 1000.0,
                    },
                    {
                        'contract': 'AI_USDT',
                        'maintenance_margin_usdt': 90.0,
                        'liq_distance_bps': 1000.0,
                    },
                ],
            ),
        }
        losing = _risk_position(
            open_spread_bps=100.0,
            current_spread_bps=600.0,
            gate_liq_price=2.0,
            gate_mark_price=1.0,
        )
        profitable = _risk_position(
            id=12,
            base_asset='AI',
            future_contract='AI_USDT',
            spot_symbol='AIUSDT',
            open_spread_bps=800.0,
            current_spread_bps=100.0,
            gate_liq_price=2.0,
            gate_mark_price=1.0,
        )
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'AI', 'success': True, 'close_reason': 'margin_close',
        })

        result = ce.check_and_close_margin_danger([losing, profitable], {})

        self.assertEqual([item['position_id'] for item in result], [12])
        self.assertEqual(result[0]['margin_risk_stage'], 'controlled_300')

    def test_200_stage_uses_pure_gate_priority_but_still_closes_one_position(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(
                status='danger',
                account_mmr_pct=190.0,
                close_priority=[
                    {'contract': 'TUT_USDT', 'maintenance_margin_usdt': 100.0},
                    {'contract': 'AI_USDT', 'maintenance_margin_usdt': 90.0},
                ],
            ),
        }
        losing = _risk_position(
            open_spread_bps=100.0,
            current_spread_bps=600.0,
            gate_liq_price=2.0,
            gate_mark_price=1.0,
        )
        profitable = _risk_position(
            id=12,
            base_asset='AI',
            future_contract='AI_USDT',
            spot_symbol='AIUSDT',
            open_spread_bps=800.0,
            current_spread_bps=100.0,
            gate_liq_price=2.0,
            gate_mark_price=1.0,
        )
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'TUT', 'success': True, 'close_reason': 'margin_close',
        })

        result = ce.check_and_close_margin_danger([losing, profitable], {})

        self.assertEqual([item['position_id'] for item in result], [11])
        self.assertEqual(result[0]['margin_risk_stage'], 'critical_200')
        ce._execute_close.assert_called_once()

    def test_risk_close_failure_stops_current_round_before_next_asset(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(
                status='danger',
                account_mmr_pct=190.0,
                close_priority=[
                    {'contract': 'TUT_USDT'},
                    {'contract': 'AI_USDT'},
                ],
            ),
        }
        positions = [
            _risk_position(gate_liq_price=2.0, gate_mark_price=1.0),
            _risk_position(
                id=12,
                base_asset='AI',
                future_contract='AI_USDT',
                spot_symbol='AIUSDT',
                gate_liq_price=2.0,
                gate_mark_price=1.0,
            ),
        ]
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'TUT',
            'success': False,
            'close_reason': 'margin_close',
            'message': 'Binance现货拒单',
        })

        result = ce.check_and_close_margin_danger(positions, {})

        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]['success'])
        ce._execute_close.assert_called_once()

    def test_cross_margin_missing_refresh_uses_account_mmr_not_contract_mmr(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='safe', account_mmr_pct=3349.5),
        }

        self.assertFalse(ce.needs_fresh_margin_risk([
            _risk_position(gate_liq_price=1.20, gate_mark_price=1.00),
        ]))

    def test_cross_margin_missing_refresh_requires_account_mmr_and_liq_price(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {'ts': time.time(), 'risk': None}

        self.assertTrue(ce.needs_fresh_margin_risk([_risk_position()]))

        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='safe', account_mmr_pct=3349.5),
        }
        self.assertTrue(ce.needs_fresh_margin_risk([
            _risk_position(gate_liq_price=None),
        ]))

    def test_cross_margin_does_not_fallback_to_contract_fields(self):
        ce = make_closing_executor()
        ce.margin_danger_mmr_pct = 300.0
        ce._gate_cross_risk_cache = {'ts': time.time(), 'risk': None}

        pos = _risk_position(gate_contract_maintenance_margin=0.6)
        state = ce._margin_danger_state(pos)

        self.assertIsNone(ce._maintenance_margin_rate(pos))
        self.assertFalse(state['active'])
        self.assertNotIn('MMR0.60%', ';'.join(state['reasons']))

    def test_account_mmr_danger_closes_only_one_holding_per_risk_snapshot(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='danger', account_mmr_pct=250.0),
        }
        ce._close_cooldown['TUT'] = datetime.now()
        positions = [
            _risk_position(current_spread_bps=None, gate_liq_price=1.50),
            _risk_position(
                id=12,
                base_asset='AI',
                future_contract='AI_USDT',
                spot_symbol='AIUSDT',
                current_spread_bps=None,
                gate_liq_price=2.0,
                gate_mark_price=1.0,
            ),
        ]
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'TUT', 'success': True, 'close_reason': 'margin_close',
        })

        results = ce.check_and_close_margin_danger(positions, {})

        self.assertEqual([item['position_id'] for item in results], [11])
        self.assertEqual(ce._execute_close.call_count, 1)
        self.assertEqual(ce._execute_close.call_args_list[0].args[3], {'base_asset': 'TUT'})

        second = ce.check_and_close_margin_danger(positions, {})

        self.assertEqual(second, [])
        self.assertEqual(ce._execute_close.call_count, 1)

    def test_account_mmr_danger_uses_gate_risk_close_priority(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(
                status='danger',
                account_mmr_pct=250.0,
                close_priority=[
                    {'contract': 'AI_USDT', 'reason': 'maintenance_margin'},
                    {'contract': 'TUT_USDT', 'reason': 'maintenance_margin'},
                ],
            ),
        }
        positions = [
            _risk_position(gate_liq_price=1.50),
            _risk_position(
                id=12,
                base_asset='AI',
                future_contract='AI_USDT',
                spot_symbol='AIUSDT',
                gate_liq_price=2.0,
                gate_mark_price=1.0,
            ),
        ]
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'AI', 'success': True, 'close_reason': 'margin_close',
        })

        results = ce.check_and_close_margin_danger(positions, {})

        self.assertEqual([item['position_id'] for item in results], [12])
        self.assertEqual(
            [
                call.args[0]['future_contract']
                for call in ce._execute_close.call_args_list
            ],
            ['AI_USDT'],
        )

    def test_account_mmr_recovery_continues_until_fresh_snapshot_reaches_target(self):
        ce = make_closing_executor()
        ce.margin_danger_mmr_pct = 300.0
        ce.margin_recovery_target_mmr_pct = 500.0
        first_risk = make_gate_cross_risk(
            status='danger',
            account_mmr_pct=250.0,
            close_priority=[
                {'contract': 'AI_USDT', 'reason': 'maintenance_margin'},
                {'contract': 'TUT_USDT', 'reason': 'maintenance_margin'},
            ],
        )
        ce._gate_cross_risk_cache = {'ts': time.time(), 'risk': first_risk}
        ai = _risk_position(
            id=12,
            base_asset='AI',
            future_contract='AI_USDT',
            spot_symbol='AIUSDT',
            gate_liq_price=2.0,
            gate_mark_price=1.0,
        )
        tut = _risk_position(gate_liq_price=2.0, gate_mark_price=1.0)
        ce._execute_close = MagicMock(side_effect=[
            {'base_asset': 'AI', 'success': True, 'close_reason': 'margin_close'},
            {'base_asset': 'TUT', 'success': True, 'close_reason': 'margin_close'},
        ])

        first = ce.check_and_close_margin_danger([tut, ai], {})
        same_snapshot = ce.check_and_close_margin_danger([tut], {})

        self.assertEqual([item['position_id'] for item in first], [12])
        self.assertEqual(same_snapshot, [])
        self.assertTrue(ce._margin_recovery_active)
        self.assertEqual(ce._execute_close.call_count, 1)

        second_risk = make_gate_cross_risk(
            status='warning',
            account_mmr_pct=420.0,
            close_priority=[{'contract': 'TUT_USDT', 'reason': 'maintenance_margin'}],
        )
        second_risk['account_fetched_at_ts'] = first_risk['account_fetched_at_ts'] + 1.0
        ce._gate_cross_risk_cache = {'ts': time.time(), 'risk': second_risk}
        second = ce.check_and_close_margin_danger([tut], {})

        self.assertEqual([item['position_id'] for item in second], [11])
        self.assertEqual(ce._execute_close.call_count, 2)

        recovered_risk = make_gate_cross_risk(status='safe', account_mmr_pct=510.0)
        recovered_risk['account_fetched_at_ts'] = second_risk['account_fetched_at_ts'] + 1.0
        ce._gate_cross_risk_cache = {'ts': time.time(), 'risk': recovered_risk}
        recovered = ce.check_and_close_margin_danger([tut], {})

        self.assertEqual(recovered, [])
        self.assertFalse(ce._margin_recovery_active)
        self.assertEqual(ce._execute_close.call_count, 2)

    def test_recreated_executor_resumes_published_mmr_recovery_episode(self):
        ce = make_closing_executor()
        risk = make_gate_cross_risk(
            status='warning',
            account_mmr_pct=420.0,
            mmr_recovery_active=True,
            close_priority=[{'contract': 'TUT_USDT', 'reason': 'maintenance_margin'}],
        )
        ce._gate_cross_risk_cache = {'ts': time.time(), 'risk': risk}
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'TUT',
            'success': True,
            'close_reason': 'margin_close',
        })

        results = ce.check_and_close_margin_danger([
            _risk_position(gate_liq_price=2.0, gate_mark_price=1.0),
        ], {})

        self.assertEqual([item['position_id'] for item in results], [11])
        self.assertTrue(ce._margin_recovery_active)
        ce._execute_close.assert_called_once()

    def test_liquidation_distance_danger_only_closes_affected_contract(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='danger', account_mmr_pct=1000.0),
        }
        positions = [
            _risk_position(gate_liq_price=1.05, gate_mark_price=1.03),
            _risk_position(
                id=12,
                base_asset='AI',
                future_contract='AI_USDT',
                spot_symbol='AIUSDT',
                gate_liq_price=1.50,
                gate_mark_price=1.00,
            ),
        ]
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'TUT',
            'success': True,
            'close_reason': 'margin_close',
        })

        results = ce.check_and_close_margin_danger(positions, {})

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['position_id'], 11)
        self.assertEqual(ce._execute_close.call_args.args[0]['future_contract'], 'TUT_USDT')

    def test_unknown_or_idle_zero_mmr_never_triggers_close(self):
        for status in ('unknown', 'idle'):
            with self.subTest(status=status):
                ce = make_closing_executor()
                ce._gate_cross_risk_cache = {
                    'ts': time.time(),
                    'risk': make_gate_cross_risk(status=status, account_mmr_pct=0.0),
                }
                ce._execute_close = MagicMock()

                results = ce.check_and_close_margin_danger([_risk_position()], {})

                self.assertEqual(results, [])
                ce._execute_close.assert_not_called()

    def test_stale_danger_snapshot_never_triggers_close(self):
        ce = make_closing_executor()
        ce.gate_cross_risk_max_age_sec = 5.0
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(
                status='danger',
                account_mmr_pct=250.0,
                age_sec=6.0,
            ),
        }
        ce._execute_close = MagicMock()

        results = ce.check_and_close_margin_danger([_risk_position()], {})

        self.assertEqual(results, [])
        ce._execute_close.assert_not_called()

    def test_gate_mark_price_has_priority_for_liquidation_distance(self):
        ce = make_closing_executor()
        ce.margin_danger_liq_distance_bps = 250.0
        risk = make_gate_cross_risk(status='danger', account_mmr_pct=1000.0)
        pos = _risk_position(
            gate_mark_price=1.00,
            current_future_price=1.04,
            gate_liq_price=1.03,
        )

        state = ce._margin_danger_state(pos, cross_risk=risk)

        self.assertEqual(state['ref_price'], 1.0)
        self.assertAlmostEqual(state['liq_distance_bps'], 300.0)
        self.assertFalse(state['active'])

    def test_failed_emergency_close_retries_without_ordinary_cooldown(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='danger', account_mmr_pct=250.0),
        }
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'TUT',
            'success': False,
            'close_reason': 'margin_close',
            'message': 'temporary exchange error',
        })
        pos = _risk_position(current_spread_bps=None)

        first = ce.check_and_close_margin_danger([pos], {})
        second = ce.check_and_close_margin_danger([pos], {})

        self.assertFalse(first[0]['success'])
        self.assertFalse(second[0]['success'])
        self.assertEqual(ce._execute_close.call_count, 2)
        self.assertNotIn('TUT', ce._close_cooldown)

    def test_ordinary_close_path_contains_no_margin_risk_branch(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='danger', account_mmr_pct=250.0),
        }
        ce._execute_close = MagicMock()
        pos = _risk_position(current_spread_bps=10.0)

        with (
            patch.object(ce, '_check_delist_risk_exit', return_value=False),
            patch.object(ce, '_check_negative_funding_exit', return_value=False),
            patch.object(ce, '_check_take_profit', return_value=False),
        ):
            results = ce.check_and_close([pos], {}, {'TUT': {'base_asset': 'TUT'}})

        self.assertEqual(results, [])
        ce._execute_close.assert_not_called()

    def test_inflight_guard_prevents_duplicate_emergency_submission(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='danger', account_mmr_pct=250.0),
        }
        pos = _risk_position()
        key = ce._margin_close_key(pos)
        self.assertTrue(ce._claim_margin_close(key))
        ce._execute_close = MagicMock()
        try:
            results = ce.check_and_close_margin_danger([pos], {})
        finally:
            ce._release_margin_close(key)

        self.assertEqual(results, [])
        ce._execute_close.assert_not_called()


if __name__ == '__main__':
    unittest.main()
