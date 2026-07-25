# coding: utf-8
"""Persistent state for the single active cross-exchange fund transfer."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from common.database import db_manager


CREATE_FUND_TRANSFER_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mi_fund_transfer_task (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_key VARCHAR(64) NOT NULL,
    active_slot TINYINT NULL DEFAULT 1,
    user_id VARCHAR(64) NOT NULL DEFAULT 'default',
    username VARCHAR(128) NOT NULL,
    status VARCHAR(48) NOT NULL,
    step VARCHAR(48) NOT NULL,
    status_message VARCHAR(500) NULL,
    coin VARCHAR(32) NOT NULL,
    network VARCHAR(32) NOT NULL,
    destination_masked VARCHAR(128) NOT NULL,
    requested_amount DECIMAL(24,8) NOT NULL,
    expected_fee DECIMAL(24,8) NOT NULL,
    withdraw_amount DECIMAL(24,8) NOT NULL,
    actual_fee DECIMAL(24,8) NULL,
    received_amount DECIMAL(24,8) NULL,
    binance_transfer_client_id VARCHAR(64) NOT NULL,
    binance_transfer_id VARCHAR(128) NULL,
    binance_rollback_client_id VARCHAR(64) NOT NULL,
    binance_rollback_id VARCHAR(128) NULL,
    binance_withdraw_order_id VARCHAR(64) NOT NULL,
    binance_withdraw_id VARCHAR(128) NULL,
    binance_tx_id VARCHAR(255) NULL,
    gate_deposit_id VARCHAR(128) NULL,
    gate_transfer_client_id VARCHAR(64) NOT NULL,
    gate_transfer_id VARCHAR(128) NULL,
    attention_required TINYINT(1) NOT NULL DEFAULT 0,
    last_error TEXT NULL,
    detail JSON NULL,
    delayed_notified_at DATETIME NULL,
    attention_notified_at DATETIME NULL,
    last_checked_at DATETIME NULL,
    started_at DATETIME NULL,
    completed_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_fund_transfer_task_key (task_key),
    UNIQUE KEY uk_fund_transfer_active_slot (active_slot),
    UNIQUE KEY uk_fund_transfer_binance_client (binance_transfer_client_id),
    UNIQUE KEY uk_fund_transfer_binance_rollback_client (binance_rollback_client_id),
    UNIQUE KEY uk_fund_transfer_withdraw_order (binance_withdraw_order_id),
    UNIQUE KEY uk_fund_transfer_gate_client (gate_transfer_client_id),
    INDEX idx_fund_transfer_created (created_at),
    INDEX idx_fund_transfer_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

TERMINAL_STATUSES = {
    'completed',
    'rolled_back',
    'failed_before_transfer',
    'manually_reconciled',
}

_tables_ready = False


def ensure_fund_transfer_table() -> None:
    global _tables_ready
    if _tables_ready:
        return
    with db_manager.get_cursor() as cursor:
        cursor.execute(CREATE_FUND_TRANSFER_TABLE_SQL)
    _tables_ready = True


def _json_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _normalize_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return row
    result = dict(row)
    detail = result.get('detail')
    if isinstance(detail, str):
        try:
            result['detail'] = json.loads(detail)
        except (TypeError, ValueError):
            result['detail'] = {}
    return result


class FundTransferStore:
    UPDATE_FIELDS = {
        'status',
        'step',
        'status_message',
        'expected_fee',
        'withdraw_amount',
        'actual_fee',
        'received_amount',
        'binance_transfer_id',
        'binance_rollback_id',
        'binance_withdraw_id',
        'binance_tx_id',
        'gate_deposit_id',
        'gate_transfer_id',
        'attention_required',
        'last_error',
        'detail',
        'delayed_notified_at',
        'attention_notified_at',
        'last_checked_at',
        'started_at',
        'completed_at',
    }

    def __init__(self):
        ensure_fund_transfer_table()

    def create(self, values: Dict[str, Any]) -> Dict[str, Any]:
        columns = [
            'task_key', 'user_id', 'username', 'status', 'step',
            'status_message', 'coin', 'network', 'destination_masked',
            'requested_amount', 'expected_fee', 'withdraw_amount',
            'binance_transfer_client_id', 'binance_withdraw_order_id',
            'binance_rollback_client_id', 'gate_transfer_client_id',
        ]
        params = [values.get(column) for column in columns]
        placeholders = ', '.join(['%s'] * len(columns))
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"INSERT INTO mi_fund_transfer_task ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                params,
            )
            task_id = cursor.lastrowid
            cursor.execute(
                'SELECT * FROM mi_fund_transfer_task WHERE id = %s', (task_id,)
            )
            return _normalize_row(cursor.fetchone()) or {}

    def get(self, task_id: int) -> Optional[Dict[str, Any]]:
        with db_manager.get_cursor() as cursor:
            cursor.execute(
                'SELECT * FROM mi_fund_transfer_task WHERE id = %s', (task_id,)
            )
            return _normalize_row(cursor.fetchone())

    def get_active(self) -> Optional[Dict[str, Any]]:
        with db_manager.get_cursor() as cursor:
            cursor.execute(
                'SELECT * FROM mi_fund_transfer_task '
                'WHERE active_slot = 1 ORDER BY id DESC LIMIT 1'
            )
            return _normalize_row(cursor.fetchone())

    def list(self, limit: int = 30) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 30), 200))
        with db_manager.get_cursor() as cursor:
            cursor.execute(
                'SELECT * FROM mi_fund_transfer_task ORDER BY id DESC LIMIT %s',
                (limit,),
            )
            return [
                _normalize_row(row) or {}
                for row in (cursor.fetchall() or [])
            ]

    def get_unnotified_terminal(self) -> Optional[Dict[str, Any]]:
        statuses = sorted(TERMINAL_STATUSES)
        placeholders = ', '.join(['%s'] * len(statuses))
        with db_manager.get_cursor() as cursor:
            cursor.execute(
                f"""
                SELECT *
                FROM mi_fund_transfer_task
                WHERE status IN ({placeholders})
                  AND (
                    detail IS NULL
                    OR COALESCE(
                      JSON_UNQUOTE(JSON_EXTRACT(detail, '$.terminal_notified')),
                      'false'
                    ) <> 'true'
                  )
                ORDER BY id ASC
                LIMIT 1
                """,
                statuses,
            )
            return _normalize_row(cursor.fetchone())

    def update(self, task_id: int, **values: Any) -> Dict[str, Any]:
        clean = {
            key: (_json_value(value) if key == 'detail' else value)
            for key, value in values.items()
            if key in self.UPDATE_FIELDS
        }
        if not clean:
            return self.get(task_id) or {}
        terminal = clean.get('status') in TERMINAL_STATUSES
        assignments = [f'{key} = %s' for key in clean]
        params = list(clean.values())
        if terminal:
            assignments.append('active_slot = NULL')
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE mi_fund_transfer_task SET {', '.join(assignments)} "
                'WHERE id = %s',
                [*params, task_id],
            )
            cursor.execute(
                'SELECT * FROM mi_fund_transfer_task WHERE id = %s', (task_id,)
            )
            return _normalize_row(cursor.fetchone()) or {}
