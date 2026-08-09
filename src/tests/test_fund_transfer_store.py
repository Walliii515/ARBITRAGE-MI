from calc.fund_transfer_store import (
    CREATE_FUND_TRANSFER_TABLE_SQL,
    TERMINAL_STATUSES,
    FundTransferStore,
)
from unittest.mock import MagicMock, patch


def test_table_enforces_single_active_task_at_database_level():
    assert 'UNIQUE KEY uk_fund_transfer_active_slot (active_slot)' in CREATE_FUND_TRANSFER_TABLE_SQL
    assert 'active_slot TINYINT NULL DEFAULT 1' in CREATE_FUND_TRANSFER_TABLE_SQL


def test_terminal_statuses_release_active_slot():
    assert {
        'completed',
        'rolled_back',
        'failed_before_transfer',
        'manually_reconciled',
    } == TERMINAL_STATUSES


def test_store_update_fields_do_not_allow_idempotency_identifiers_to_change():
    assert 'binance_transfer_client_id' not in FundTransferStore.UPDATE_FIELDS
    assert 'binance_rollback_client_id' not in FundTransferStore.UPDATE_FIELDS
    assert 'binance_withdraw_order_id' not in FundTransferStore.UPDATE_FIELDS
    assert 'gate_transfer_client_id' not in FundTransferStore.UPDATE_FIELDS


def test_store_reads_latest_automatic_task_without_a_fixed_history_window():
    store = object.__new__(FundTransferStore)
    cursor = MagicMock()
    cursor.fetchone.return_value = {
        'id': 81,
        'detail': '{"initiator": "auto_mmr"}',
    }
    context = MagicMock()
    context.__enter__.return_value = cursor
    context.__exit__.return_value = False

    with patch(
        'calc.fund_transfer_store.db_manager.get_cursor',
        return_value=context,
    ):
        task = store.get_latest_by_initiator('auto_mmr')

    sql, params = cursor.execute.call_args.args
    assert "JSON_EXTRACT(detail, '$.initiator')" in sql
    assert 'ORDER BY id DESC' in sql
    assert params == ('auto_mmr',)
    assert task['id'] == 81
    assert task['detail']['initiator'] == 'auto_mmr'
