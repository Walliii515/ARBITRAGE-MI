# coding: utf-8
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.gate_position_risk import attach_gate_position_risk


class TestGatePositionRisk(unittest.TestCase):
    def test_cross_margin_zero_position_margin_does_not_create_pseudo_mmr(self):
        positions = [{
            'status': 'holding',
            'base_asset': 'AI',
            'future_contract': 'AI_USDT',
            'open_notional_usdt': 100.0,
        }]
        gate_positions = [{
            'contract': 'AI_USDT',
            'size': '-100',
            'margin': '0',
            'unrealised_pnl': '0.21',
            'maintenance_margin': '3.9',
            'mark_price': '0.138',
            'liq_price': '9.9',
        }]

        attach_gate_position_risk(positions, gate_positions)

        self.assertEqual(positions[0]['gate_contract_position_margin'], 0.0)
        self.assertIsNone(positions[0]['gate_contract_position_margin_equity'])
        self.assertEqual(positions[0]['gate_contract_unrealised_pnl'], 0.21)
        self.assertEqual(positions[0]['gate_contract_maintenance_margin'], 3.9)
        self.assertIsNone(positions[0]['gate_maintenance_margin_rate'])

    def test_isolated_position_margin_keeps_contract_mmr(self):
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

        self.assertEqual(positions[0]['gate_contract_position_margin_equity'], 9.0)
        self.assertEqual(positions[0]['gate_maintenance_margin_rate'], 300.0)


if __name__ == '__main__':
    unittest.main()
