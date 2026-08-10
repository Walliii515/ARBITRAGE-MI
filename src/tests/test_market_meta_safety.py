# coding: utf-8
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.calculate_funding_rate_threshold import replace_funding_thresholds
from calc.update_binance_spot_info import replace_spot_info
from common.market_meta_safety import (
    retain_healthy_contract_meta,
    retain_healthy_spot_meta,
    validate_contract_records,
)


class TestMarketMetaSafety(unittest.TestCase):
    def test_contract_cache_keeps_last_good_on_empty_refresh(self):
        current = {'TUT': {'quanto_multiplier': 100.0}}
        self.assertIs(retain_healthy_contract_meta({}, current), current)

    def test_contract_cache_keeps_last_good_on_invalid_multiplier(self):
        current = {'TUT': {'quanto_multiplier': 100.0}}
        candidate = {'TUT': {'quanto_multiplier': 0}}
        self.assertIs(retain_healthy_contract_meta(candidate, current), current)

    def test_contract_cache_rejects_large_count_drop(self):
        current = {f'A{i}': {'quanto_multiplier': 1.0} for i in range(100)}
        candidate = {f'A{i}': {'quanto_multiplier': 1.0} for i in range(10)}
        self.assertIs(retain_healthy_contract_meta(candidate, current), current)

    def test_contract_cache_accepts_healthy_refresh(self):
        current = {'OLD': {'quanto_multiplier': 1.0}}
        candidate = {'TUT': {'quanto_multiplier': 100.0}}
        self.assertIs(retain_healthy_contract_meta(candidate, current), candidate)

    def test_spot_cache_keeps_last_good_on_empty_or_invalid_refresh(self):
        current = {'TUT': {'step_size': 1.0}}
        self.assertIs(retain_healthy_spot_meta({}, current), current)
        self.assertIs(
            retain_healthy_spot_meta({'TUT': {'step_size': 0}}, current),
            current,
        )

    def test_contract_record_validation_rejects_partial_snapshot(self):
        rows = [{
            'name': f'A{i}_USDT',
            'base_asset': f'A{i}',
            'quanto_multiplier': 1,
        } for i in range(10)]
        with self.assertRaisesRegex(ValueError, '数量异常下降'):
            validate_contract_records(rows, previous_count=100)

    def test_binance_replace_is_atomic(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {'cnt': 1}
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False
        rows = [{
            'symbol': 'TUTUSDT',
            'base_asset': 'TUT',
            'step_size': '1',
            'tick_size': '0.00001',
        }]

        with patch(
            'calc.update_binance_spot_info.db_manager.get_cursor',
            return_value=context,
        ), patch(
            'calc.update_binance_spot_info._insert_spot_info',
            return_value=1,
        ) as insert_rows:
            count = replace_spot_info(rows)

        self.assertEqual(count, 1)
        self.assertEqual(cursor.execute.call_args_list[0].args[0], 'SELECT COUNT(*) AS cnt FROM mi_binance_spot_info')
        self.assertEqual(cursor.execute.call_args_list[1].args[0], 'DELETE FROM mi_binance_spot_info')
        insert_rows.assert_called_once_with(cursor, rows)

    def test_binance_invalid_snapshot_is_not_deleted(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {'cnt': 1}
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False

        with patch(
            'calc.update_binance_spot_info.db_manager.get_cursor',
            return_value=context,
        ), self.assertRaises(ValueError):
            replace_spot_info([{
                'symbol': 'TUTUSDT',
                'base_asset': 'TUT',
                'step_size': 0,
                'tick_size': '0.00001',
            }])

        self.assertEqual(cursor.execute.call_count, 1)

    def test_funding_threshold_empty_result_keeps_existing_table(self):
        cursor = MagicMock()
        self.assertEqual(replace_funding_thresholds(cursor, []), 0)
        cursor.execute.assert_not_called()
        cursor.executemany.assert_not_called()

    def test_funding_threshold_replace_uses_delete_not_truncate(self):
        cursor = MagicMock()
        row = {
            'contract': 'TUT_USDT',
            'total_records': 10,
            'positive_count': 8,
            'percentile_20': 0.001,
            'percentile_30': 0.002,
            'percentile_40': 0.003,
            'min_rate': 0.0001,
            'max_rate': 0.01,
        }

        self.assertEqual(replace_funding_thresholds(cursor, [row]), 1)
        self.assertEqual(
            cursor.execute.call_args.args[0],
            'DELETE FROM mi_gate_future_funding_rate_threshold',
        )
        self.assertNotIn('TRUNCATE', cursor.execute.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
