# coding: utf-8
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.update_gate_future_contracts import replace_contracts


class TestGateFutureContractRefresh(unittest.TestCase):
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
