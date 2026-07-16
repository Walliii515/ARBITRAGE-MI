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

    def test_account_mmr_danger_closes_every_holding_without_books_or_spread(self):
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
        ce._execute_close = MagicMock(side_effect=[
            {'base_asset': 'TUT', 'success': True, 'close_reason': 'margin_close'},
            {'base_asset': 'AI', 'success': True, 'close_reason': 'margin_close'},
        ])

        results = ce.check_and_close_margin_danger(positions, {})

        self.assertEqual([item['position_id'] for item in results], [11, 12])
        self.assertEqual(ce._execute_close.call_count, 2)
        self.assertEqual(ce._execute_close.call_args_list[0].args[3], {'base_asset': 'TUT'})
        self.assertEqual(ce._execute_close.call_args_list[1].args[3], {'base_asset': 'AI'})

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
