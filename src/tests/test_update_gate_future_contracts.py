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
        contracts = [{'name': 'TUT_USDT'}]

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
        cursor.execute.assert_called_once_with('DELETE FROM mi_gate_future_contracts')
        insert_contracts.assert_called_once_with(cursor, contracts)


if __name__ == '__main__':
    unittest.main()
