# coding: utf-8
"""反向套利信号持久化。

只记录 reverse scanner 的判断结果，不触碰正向 TradingExecutor 状态机。
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from common.database import db_manager
from common.logger import get_logger

logger = get_logger(__name__)


REVERSE_SIGNAL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mi_reverse_trade_signal (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    base_asset VARCHAR(32) NOT NULL,
    contract VARCHAR(64) DEFAULT NULL,
    signal_time DATETIME NOT NULL,
    resolved_time DATETIME DEFAULT NULL,
    last_seen_time DATETIME DEFAULT NULL,
    status ENUM('candidate', 'rejected', 'conditions_lost') NOT NULL,
    reverse_status VARCHAR(64) NOT NULL,
    trigger_reason VARCHAR(500) DEFAULT NULL,
    reject_reason TEXT DEFAULT NULL,
    funding_rate_24h DECIMAL(18,10) DEFAULT NULL,
    funding_rate_2h DECIMAL(18,10) DEFAULT NULL,
    reverse_gross_funding_bps DECIMAL(12,4) DEFAULT NULL,
    reverse_expected_funding_bps DECIMAL(12,4) DEFAULT NULL,
    reverse_basis_bps DECIMAL(12,4) DEFAULT NULL,
    reverse_close_basis_bps DECIMAL(12,4) DEFAULT NULL,
    reverse_p20_edge_bps DECIMAL(12,4) DEFAULT NULL,
    reverse_margin_edge_bps DECIMAL(12,4) DEFAULT NULL,
    reverse_open_coverage DECIMAL(12,8) DEFAULT NULL,
    reverse_borrow_hourly_rate DECIMAL(18,10) DEFAULT NULL,
    reverse_borrow_24h_bps DECIMAL(12,4) DEFAULT NULL,
    reverse_borrow_limit DECIMAL(24,8) DEFAULT NULL,
    reverse_capacity_usdt DECIMAL(24,8) DEFAULT NULL,
    reverse_open_basis_p20 DECIMAL(12,4) DEFAULT NULL,
    reverse_close_basis_p20 DECIMAL(12,4) DEFAULT NULL,
    funding_next_apply DATETIME DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_signal_time (signal_time),
    INDEX idx_base_asset_time (base_asset, signal_time),
    INDEX idx_status_time (status, signal_time),
    INDEX idx_active_asset (base_asset, resolved_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

REVERSE_SIGNAL_EXTRA_COLUMNS = {
    'reverse_close_basis_bps': 'ADD COLUMN reverse_close_basis_bps DECIMAL(12,4) DEFAULT NULL AFTER reverse_basis_bps',
    'reverse_p20_edge_bps': 'ADD COLUMN reverse_p20_edge_bps DECIMAL(12,4) DEFAULT NULL AFTER reverse_close_basis_bps',
}

REVERSE_STATUS_LABELS = {
    'candidate': '满足反向开仓条件',
    'missing_open_data': '反向开仓盘口数据不完整',
    'funding_too_low': '24h funding rate 非负，反向不可收 funding',
    'missing_borrow_data': '借币数据缺失',
    'borrow_unavailable': '币种不可借',
    'borrow_capacity_low': '借币额度不足',
    'depth_too_thin': '开仓盘口覆盖不足',
    'missing_margin_edge': '边际盈亏无法计算',
    'margin_edge_too_low': '边际盈亏小于 0',
}


def ensure_reverse_signal_table() -> None:
    """幂等创建反向信号表。"""
    with db_manager.get_cursor() as cursor:
        cursor.execute(REVERSE_SIGNAL_TABLE_SQL)
        cursor.execute("SHOW COLUMNS FROM mi_reverse_trade_signal")
        existing = {row['Field'] for row in cursor.fetchall()}
        for column, alter_sql in REVERSE_SIGNAL_EXTRA_COLUMNS.items():
            if column not in existing:
                cursor.execute(f"ALTER TABLE mi_reverse_trade_signal {alter_sql}")


def reverse_signal_status(reverse_status: Optional[str]) -> str:
    return 'candidate' if reverse_status == 'candidate' else 'rejected'


def reverse_signal_reason(row: Dict) -> Tuple[str, Optional[str]]:
    reverse_status = str(row.get('reverse_status') or 'missing_margin_edge')
    label = REVERSE_STATUS_LABELS.get(reverse_status, reverse_status)
    trigger = (
        f"负费率={row.get('funding_rate_24h')}, "
        f"边际盈亏={row.get('reverse_margin_edge_bps')}bps, "
        f"开仓基差={row.get('reverse_basis_bps')}bps"
    )
    if reverse_status == 'candidate':
        return trigger, None
    return trigger, label


def _as_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _funding_2h_rate(row: Dict) -> Optional[float]:
    rate_24h = _as_float(row.get('funding_rate_24h'))
    return rate_24h / 12.0 if rate_24h is not None else None


def _signal_values(row: Dict, scan_time: datetime) -> Dict:
    reverse_status = str(row.get('reverse_status') or 'missing_margin_edge')
    trigger_reason, reject_reason = reverse_signal_reason(row)
    return {
        'base_asset': str(row.get('base_asset') or '').upper(),
        'contract': row.get('contract'),
        'status': reverse_signal_status(reverse_status),
        'reverse_status': reverse_status,
        'trigger_reason': trigger_reason,
        'reject_reason': reject_reason,
        'scan_time': scan_time,
        'funding_rate_24h': _as_float(row.get('funding_rate_24h')),
        'funding_rate_2h': _funding_2h_rate(row),
        'reverse_gross_funding_bps': _as_float(row.get('reverse_gross_funding_bps')),
        'reverse_expected_funding_bps': _as_float(row.get('reverse_expected_funding_bps')),
        'reverse_basis_bps': _as_float(row.get('reverse_basis_bps')),
        'reverse_close_basis_bps': _as_float(row.get('reverse_close_basis_bps')),
        'reverse_p20_edge_bps': _as_float(row.get('reverse_p20_edge_bps')),
        'reverse_margin_edge_bps': _as_float(row.get('reverse_margin_edge_bps')),
        'reverse_open_coverage': _as_float(row.get('reverse_open_coverage')),
        'reverse_borrow_hourly_rate': _as_float(row.get('reverse_borrow_hourly_rate')),
        'reverse_borrow_24h_bps': _as_float(row.get('reverse_borrow_24h_bps')),
        'reverse_borrow_limit': _as_float(row.get('reverse_borrow_limit')),
        'reverse_capacity_usdt': _as_float(row.get('reverse_capacity_usdt')),
        'reverse_open_basis_p20': _as_float(row.get('reverse_open_basis_p20')),
        'reverse_close_basis_p20': _as_float(row.get('reverse_close_basis_p20')),
        'funding_next_apply': row.get('funding_next_apply'),
    }


def _is_reverse_signal_row(row: Dict) -> bool:
    """反向信号只记录负 funding 标的完成开仓门槛判断后的结果。"""
    rate_24h = _as_float(row.get('funding_rate_24h'))
    return bool(row.get('base_asset')) and rate_24h is not None and rate_24h < 0


def _insert_signal(cursor, values: Dict) -> None:
    sql = """
        INSERT INTO mi_reverse_trade_signal (
            base_asset, contract, signal_time, last_seen_time, status, reverse_status,
            trigger_reason, reject_reason, funding_rate_24h, funding_rate_2h,
            reverse_gross_funding_bps, reverse_expected_funding_bps,
            reverse_basis_bps, reverse_close_basis_bps, reverse_p20_edge_bps,
            reverse_margin_edge_bps, reverse_open_coverage,
            reverse_borrow_hourly_rate, reverse_borrow_24h_bps, reverse_borrow_limit,
            reverse_capacity_usdt, reverse_open_basis_p20, reverse_close_basis_p20,
            funding_next_apply
        ) VALUES (
            %(base_asset)s, %(contract)s, %(scan_time)s, %(scan_time)s, %(status)s, %(reverse_status)s,
            %(trigger_reason)s, %(reject_reason)s, %(funding_rate_24h)s, %(funding_rate_2h)s,
            %(reverse_gross_funding_bps)s, %(reverse_expected_funding_bps)s,
            %(reverse_basis_bps)s, %(reverse_close_basis_bps)s, %(reverse_p20_edge_bps)s,
            %(reverse_margin_edge_bps)s, %(reverse_open_coverage)s,
            %(reverse_borrow_hourly_rate)s, %(reverse_borrow_24h_bps)s, %(reverse_borrow_limit)s,
            %(reverse_capacity_usdt)s, %(reverse_open_basis_p20)s, %(reverse_close_basis_p20)s,
            %(funding_next_apply)s
        )
    """
    cursor.execute(sql, values)


def _update_active_signal(cursor, signal_id: int, values: Dict) -> None:
    sql = """
        UPDATE mi_reverse_trade_signal SET
            last_seen_time = %(scan_time)s,
            contract = %(contract)s,
            trigger_reason = %(trigger_reason)s,
            reject_reason = %(reject_reason)s,
            funding_rate_24h = %(funding_rate_24h)s,
            funding_rate_2h = %(funding_rate_2h)s,
            reverse_gross_funding_bps = %(reverse_gross_funding_bps)s,
            reverse_expected_funding_bps = %(reverse_expected_funding_bps)s,
            reverse_basis_bps = %(reverse_basis_bps)s,
            reverse_close_basis_bps = %(reverse_close_basis_bps)s,
            reverse_p20_edge_bps = %(reverse_p20_edge_bps)s,
            reverse_margin_edge_bps = %(reverse_margin_edge_bps)s,
            reverse_open_coverage = %(reverse_open_coverage)s,
            reverse_borrow_hourly_rate = %(reverse_borrow_hourly_rate)s,
            reverse_borrow_24h_bps = %(reverse_borrow_24h_bps)s,
            reverse_borrow_limit = %(reverse_borrow_limit)s,
            reverse_capacity_usdt = %(reverse_capacity_usdt)s,
            reverse_open_basis_p20 = %(reverse_open_basis_p20)s,
            reverse_close_basis_p20 = %(reverse_close_basis_p20)s,
            funding_next_apply = %(funding_next_apply)s
        WHERE id = %(id)s
    """
    params = dict(values)
    params['id'] = signal_id
    cursor.execute(sql, params)


def log_reverse_signals(rows: Iterable[Dict], scan_time: Optional[datetime] = None) -> int:
    """记录本轮反向信号判断结果，返回新增信号数。"""
    scan_time = scan_time or datetime.now()
    current = [_signal_values(row, scan_time) for row in rows if _is_reverse_signal_row(row)]
    current = [row for row in current if row.get('base_asset')]
    seen_assets = {row['base_asset'] for row in current}
    inserted = 0

    try:
        ensure_reverse_signal_table()
        with db_manager.get_cursor() as cursor:
            for values in current:
                cursor.execute(
                    """
                    SELECT id, status, reverse_status
                    FROM mi_reverse_trade_signal
                    WHERE base_asset = %s AND resolved_time IS NULL
                    ORDER BY signal_time DESC
                    LIMIT 1
                    """,
                    (values['base_asset'],),
                )
                active = cursor.fetchone()
                if active and active['status'] == values['status'] and active['reverse_status'] == values['reverse_status']:
                    _update_active_signal(cursor, active['id'], values)
                    continue
                if active:
                    cursor.execute(
                        """
                        UPDATE mi_reverse_trade_signal
                        SET status = 'conditions_lost',
                            resolved_time = %s,
                            last_seen_time = %s,
                            reject_reason = COALESCE(reject_reason, '反向信号状态切换')
                        WHERE id = %s
                        """,
                        (scan_time, scan_time, active['id']),
                    )
                _insert_signal(cursor, values)
                inserted += 1

            if seen_assets:
                placeholders = ','.join(['%s'] * len(seen_assets))
                cursor.execute(
                    f"""
                    UPDATE mi_reverse_trade_signal
                    SET status = 'conditions_lost',
                        resolved_time = %s,
                        last_seen_time = %s,
                        reject_reason = COALESCE(reject_reason, '负费率信号消失')
                    WHERE resolved_time IS NULL
                      AND base_asset NOT IN ({placeholders})
                    """,
                    [scan_time, scan_time, *sorted(seen_assets)],
                )
            else:
                cursor.execute(
                    """
                    UPDATE mi_reverse_trade_signal
                    SET status = 'conditions_lost',
                        resolved_time = %s,
                        last_seen_time = %s,
                        reject_reason = COALESCE(reject_reason, '负费率信号消失')
                    WHERE resolved_time IS NULL
                    """,
                    (scan_time, scan_time),
                )
    except Exception as exc:
        logger.warning(f'反向交易信号记录失败: {exc}', exc_info=True)
        return 0

    return inserted


def query_reverse_signals(
    status: Optional[str],
    reject_reason: Optional[str],
    base_asset: Optional[str],
    days: int,
    page: int,
    page_size: int,
) -> Dict:
    where = ["signal_time >= DATE_SUB(NOW(), INTERVAL %s DAY)"]
    params: List = [days]
    if status:
        where.append("status = %s")
        params.append(status)
    if reject_reason:
        where.append("(reject_reason LIKE %s OR trigger_reason LIKE %s)")
        params.extend([f"%{reject_reason}%", f"%{reject_reason}%"])
    if base_asset:
        where.append("base_asset LIKE %s")
        params.append(f"%{base_asset}%")

    where_sql = " AND ".join(where)
    offset = (page - 1) * page_size
    ensure_reverse_signal_table()

    with db_manager.get_cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS total FROM mi_reverse_trade_signal WHERE {where_sql}", params)
        total_row = cursor.fetchone() or {}
        total = int(total_row.get('total') or 0)

        cursor.execute(
            f"""
            SELECT *
            FROM mi_reverse_trade_signal
            WHERE {where_sql}
            ORDER BY signal_time DESC
            LIMIT %s OFFSET %s
            """,
            [*params, page_size, offset],
        )
        rows = cursor.fetchall()

        cursor.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'candidate' THEN 1 ELSE 0 END) AS candidate,
                SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected,
                SUM(CASE WHEN status = 'conditions_lost' THEN 1 ELSE 0 END) AS conditions_lost,
                MAX(signal_time) AS latest_signal_time
            FROM mi_reverse_trade_signal
            WHERE {where_sql}
            """,
            params,
        )
        summary = cursor.fetchone() or {}

    return {
        'signals': rows,
        'pagination': {
            'page': page,
            'page_size': page_size,
            'total': total,
            'total_pages': (total + page_size - 1) // page_size if page_size else 0,
        },
        'summary': summary,
    }
