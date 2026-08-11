# coding: utf-8
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.update_gate_future_contracts import (
    calculate_24h_range_metrics,
    merge_contracts_with_tickers,
    replace_contracts,
)


class TestGateFutureContractRefresh(unittest.TestCase):
    def test_merge_contracts_adds_24h_range_metrics(self):
        merged = merge_contracts_with_tickers(
            [{'name': 'TUT_USDT', 'base_asset': 'TUT'}],
            [{
                'contract': 'TUT_USDT',
                'volume_24h_settle': '1234.5',
                'high_24h': '1.5',
                'low_24h': '1.0',
                'last': '1.4',
            }],
        )

        self.assertEqual(merged[0]['volume_24h_settle'], 1234.5)
        self.assertAlmostEqual(merged[0]['range_24h_pct'], 50.0)
        self.assertAlmostEqual(merged[0]['range_position_24h'], 0.8)
        self.assertEqual(merged[0]['last_price'], 1.4)

    def test_merge_contracts_keeps_missing_range_unknown(self):
        merged = merge_contracts_with_tickers(
            [{'name': 'TUT_USDT', 'base_asset': 'TUT'}],
            [],
        )

        self.assertIsNone(merged[0]['range_24h_pct'])
        self.assertIsNone(merged[0]['range_position_24h'])

    def test_range_metrics_reject_invalid_prices(self):
        self.assertEqual(calculate_24h_range_metrics(0.9, 1.0, 0.95), (None, None))
        self.assertEqual(calculate_24h_range_metrics('nan', 1.0, 1.0), (None, None))
        self.assertEqual(calculate_24h_range_metrics(1.0, 0.0, 1.0), (None, None))

    def test_flat_range_is_zero_amplitude_and_position(self):
        self.assertEqual(calculate_24h_range_metrics(1.0, 1.0, 1.0), (0.0, 0.0))

    def test_replace_contracts_deletes_and_inserts_in_one_transaction(self):
        cursor = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False
        cursor.fetchone.return_value = {'cnt': 1}
        contracts = [{
            'name': 'TUT_USDT',
            'base_asset': 'TUT',
            'quanto_multiplier': '100',
        }]

        with patch(
            'calc.update_gate_future_contracts.db_manager.get_cursor',
            return_value=context,
        ) as get_cursor, patch(
            'calc.update_gate_future_contracts._insert_contracts',
            return_value=1,
        ) as insert_contracts:
            count = replace_contracts(contracts)

        self.assertEqual(count, 1)
        get_cursor.assert_called_once_with()
        self.assertEqual(cursor.execute.call_args_list[0].args[0], 'SELECT COUNT(*) AS cnt FROM mi_gate_future_contracts')
        self.assertEqual(cursor.execute.call_args_list[1].args[0], 'DELETE FROM mi_gate_future_contracts')
        insert_contracts.assert_called_once_with(cursor, contracts)

    def test_replace_contracts_rejects_invalid_snapshot_before_delete(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {'cnt': 200}
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False

        with patch(
            'calc.update_gate_future_contracts.db_manager.get_cursor',
            return_value=context,
        ), self.assertRaises(ValueError):
            replace_contracts([{
                'name': 'TUT_USDT',
                'base_asset': 'TUT',
                'quanto_multiplier': None,
            }])

        self.assertEqual(cursor.execute.call_count, 1)


if __name__ == '__main__':
    unittest.main()
