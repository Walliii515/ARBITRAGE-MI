# coding: utf-8
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.gate_position_risk import attach_gate_position_risk


class TestGatePositionRisk(unittest.TestCase):
    def test_cross_position_uses_exchange_initial_margin(self):
        positions = [{
            'status': 'holding',
            'base_asset': 'AI',
            'future_contract': 'AI_USDT',
            'open_notional_usdt': 100.0,
        }]
        gate_positions = [{
            'contract': 'AI_USDT',
            'size': '-100',
            'margin': '99',
            'initial_margin': '12.5',
            'unrealised_pnl': '0.21',
            'maintenance_margin': '3.9',
            'mark_price': '0.138',
            'liq_price': '9.9',
        }]

        attach_gate_position_risk(positions, gate_positions)

        self.assertEqual(positions[0]['gate_contract_initial_margin'], 12.5)
        self.assertEqual(positions[0]['gate_initial_margin'], 12.5)
        self.assertEqual(positions[0]['gate_contract_unrealised_pnl'], 0.21)
        self.assertEqual(positions[0]['gate_contract_maintenance_margin'], 3.9)

    def test_missing_initial_margin_is_not_backfilled_from_margin(self):
        positions = [{
            'status': 'holding',
            'base_asset': 'AI',
            'future_contract': 'AI_USDT',
            'open_notional_usdt': 100.0,
        }]
        gate_positions = [{
            'contract': 'AI_USDT',
            'size': '-100',
            'margin': '10',
            'unrealised_pnl': '-1',
            'maintenance_margin': '3',
        }]

        attach_gate_position_risk(positions, gate_positions)

        self.assertIsNone(positions[0]['gate_contract_initial_margin'])
        self.assertIsNone(positions[0]['gate_initial_margin'])
        self.assertEqual(positions[0]['gate_contract_unrealised_pnl'], -1.0)
        self.assertEqual(positions[0]['gate_contract_maintenance_margin'], 3.0)


if __name__ == '__main__':
    unittest.main()
