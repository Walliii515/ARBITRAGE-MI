# coding: utf-8
"""Detect newly available spot/futures pairs for manual monitoring decisions."""
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from common.database import db_manager
from common.logger import get_logger

logger = get_logger(__name__)


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mi_listing_event (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
    base_asset VARCHAR(64) NOT NULL COMMENT '标的资产',
    gate_contract VARCHAR(80) NULL COMMENT 'Gate 永续合约名',
    binance_symbol VARCHAR(80) NULL COMMENT 'Binance 现货交易对',
    candidate_status ENUM('matched','gate_only','binance_only') NOT NULL COMMENT '上新配对状态',
    action_status ENUM('pending','acknowledged','ignored','disabled','added_to_monitor') NOT NULL DEFAULT 'pending' COMMENT '处理状态',
    is_actionable TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否值得弹窗提醒',
    gate_status VARCHAR(40) NULL COMMENT 'Gate 合约状态',
    binance_status VARCHAR(40) NULL COMMENT 'Binance 现货状态',
    gate_volume_24h_settle DECIMAL(30,10) NULL COMMENT 'Gate 24h 成交额',
    binance_quote_volume DECIMAL(30,10) NULL COMMENT 'Binance 24h 报价成交额',
    gate_funding_rate_24h DECIMAL(18,10) NULL COMMENT 'Gate 24h 资金费率',
    first_seen_at DATETIME NOT NULL COMMENT '首次发现时间',
    last_seen_at DATETIME NOT NULL COMMENT '最近仍存在时间',
    acknowledged_at DATETIME NULL COMMENT '确认时间',
    action_at DATETIME NULL COMMENT '处理时间',
    action_reason VARCHAR(255) NULL COMMENT '处理原因',
    source_payload JSON NULL COMMENT '原始来源摘要',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_listing_event_asset (base_asset),
    INDEX idx_listing_action (action_status, is_actionable, last_seen_at),
    INDEX idx_listing_candidate (candidate_status, last_seen_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='交易对上新事件'
"""


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return str(value)


def ensure_listing_event_table() -> None:
    with db_manager.get_cursor() as cursor:
        cursor.execute(_CREATE_TABLE_SQL)


def _load_active_base_assets(cursor) -> Dict[str, str]:
    cursor.execute(
        """
        SELECT UPPER(TRIM(base_asset)) AS base_asset, COALESCE(is_valid, 'Y') AS is_valid
        FROM mi_base_asset
        WHERE base_asset IS NOT NULL AND TRIM(base_asset) <> ''
        """
    )
    return {
        str(row.get('base_asset') or '').upper(): str(row.get('is_valid') or 'Y').upper()
        for row in cursor.fetchall() or []
        if row.get('base_asset')
    }


def _load_gate_contracts(cursor) -> Dict[str, Dict[str, Any]]:
    cursor.execute(
        """
        SELECT
            UPPER(TRIM(base_asset)) AS base_asset,
            name,
            status,
            volume_24h_settle,
            funding_rate_24h
        FROM mi_gate_future_contracts
        WHERE name LIKE %s
          AND UPPER(COALESCE(status, '')) = 'TRADING'
          AND base_asset IS NOT NULL
          AND TRIM(base_asset) <> ''
        """,
        ['%\\_USDT'],
    )
    return {
        str(row.get('base_asset') or '').upper(): row
        for row in cursor.fetchall() or []
        if row.get('base_asset')
    }


def _load_binance_spots(cursor) -> Dict[str, Dict[str, Any]]:
    cursor.execute(
        """
        SELECT
            UPPER(TRIM(base_asset)) AS base_asset,
            symbol,
            status,
            quote_volume
        FROM mi_binance_spot_info
        WHERE quote_asset = 'USDT'
          AND UPPER(COALESCE(status, '')) = 'TRADING'
          AND COALESCE(is_spot_trading_allowed, 1) = 1
          AND base_asset IS NOT NULL
          AND TRIM(base_asset) <> ''
        """
    )
    return {
        str(row.get('base_asset') or '').upper(): row
        for row in cursor.fetchall() or []
        if row.get('base_asset')
    }


def _build_event_rows(
    gate_by_asset: Dict[str, Dict[str, Any]],
    spot_by_asset: Dict[str, Dict[str, Any]],
    base_asset_status: Dict[str, str],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for asset in sorted(set(gate_by_asset) | set(spot_by_asset)):
        gate = gate_by_asset.get(asset)
        spot = spot_by_asset.get(asset)
        if gate and spot:
            candidate_status = 'matched'
        elif gate:
            candidate_status = 'gate_only'
        else:
            candidate_status = 'binance_only'

        is_valid = base_asset_status.get(asset)
        already_known = is_valid == 'Y'
        already_disabled = is_valid == 'N'
        if already_known:
            continue

        is_actionable = candidate_status == 'matched' and not already_disabled
        action_status = 'disabled' if already_disabled else 'pending'
        source_payload = {
            'gate': {
                'name': gate.get('name') if gate else None,
                'status': gate.get('status') if gate else None,
                'volume_24h_settle': _to_float(gate.get('volume_24h_settle')) if gate else None,
                'funding_rate_24h': _to_float(gate.get('funding_rate_24h')) if gate else None,
            },
            'binance': {
                'symbol': spot.get('symbol') if spot else None,
                'status': spot.get('status') if spot else None,
                'quote_volume': _to_float(spot.get('quote_volume')) if spot else None,
            },
        }
        rows.append({
            'base_asset': asset,
            'gate_contract': gate.get('name') if gate else None,
            'binance_symbol': spot.get('symbol') if spot else None,
            'candidate_status': candidate_status,
            'action_status': action_status,
            'is_actionable': 1 if is_actionable else 0,
            'gate_status': gate.get('status') if gate else None,
            'binance_status': spot.get('status') if spot else None,
            'gate_volume_24h_settle': _to_float(gate.get('volume_24h_settle')) if gate else None,
            'binance_quote_volume': _to_float(spot.get('quote_volume')) if spot else None,
            'gate_funding_rate_24h': _to_float(gate.get('funding_rate_24h')) if gate else None,
            'source_payload': json.dumps(source_payload, ensure_ascii=False, default=_json_default),
        })
    return rows


def refresh_listing_events() -> Dict[str, Any]:
    """Refresh newly listed pair candidates from current metadata tables."""
    ensure_listing_event_table()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with db_manager.get_connection() as conn:
        with conn.cursor() as cursor:
            base_asset_status = _load_active_base_assets(cursor)
            gate_by_asset = _load_gate_contracts(cursor)
            spot_by_asset = _load_binance_spots(cursor)
            rows = _build_event_rows(gate_by_asset, spot_by_asset, base_asset_status)
            cursor.execute("SELECT COUNT(*) AS cnt FROM mi_listing_event")
            is_initial_baseline = int((cursor.fetchone() or {}).get('cnt') or 0) == 0

            insert_sql = """
            INSERT INTO mi_listing_event (
                base_asset, gate_contract, binance_symbol, candidate_status,
                action_status, is_actionable, gate_status, binance_status,
                gate_volume_24h_settle, binance_quote_volume, gate_funding_rate_24h,
                first_seen_at, last_seen_at, acknowledged_at, action_reason, source_payload
            ) VALUES (
                %(base_asset)s, %(gate_contract)s, %(binance_symbol)s, %(candidate_status)s,
                %(action_status)s, %(is_actionable)s, %(gate_status)s, %(binance_status)s,
                %(gate_volume_24h_settle)s, %(binance_quote_volume)s, %(gate_funding_rate_24h)s,
                %(now)s, %(now)s, %(acknowledged_at)s, %(action_reason)s, %(source_payload)s
            )
            ON DUPLICATE KEY UPDATE
                gate_contract = VALUES(gate_contract),
                binance_symbol = VALUES(binance_symbol),
                candidate_status = VALUES(candidate_status),
                is_actionable = IF(action_status IN ('pending','acknowledged'), VALUES(is_actionable), 0),
                gate_status = VALUES(gate_status),
                binance_status = VALUES(binance_status),
                gate_volume_24h_settle = VALUES(gate_volume_24h_settle),
                binance_quote_volume = VALUES(binance_quote_volume),
                gate_funding_rate_24h = VALUES(gate_funding_rate_24h),
                last_seen_at = VALUES(last_seen_at),
                source_payload = VALUES(source_payload)
            """
            for row in rows:
                if is_initial_baseline and row['action_status'] == 'pending':
                    row = {
                        **row,
                        'action_status': 'acknowledged',
                        'is_actionable': 0,
                        'acknowledged_at': now,
                        'action_reason': 'initial_baseline',
                    }
                else:
                    row = {**row, 'acknowledged_at': None, 'action_reason': None}
                cursor.execute(insert_sql, {**row, 'now': now})

            cursor.execute(
                """
                UPDATE mi_listing_event e
                INNER JOIN mi_base_asset b
                   ON UPPER(TRIM(b.base_asset)) COLLATE utf8mb4_unicode_ci = e.base_asset
                SET
                    e.action_status = CASE
                        WHEN b.is_valid = 'Y' THEN 'added_to_monitor'
                        WHEN b.is_valid = 'N' THEN 'disabled'
                        ELSE e.action_status
                    END,
                    e.is_actionable = 0,
                    e.action_at = COALESCE(e.action_at, %s),
                    e.action_reason = COALESCE(e.action_reason, 'base_asset_state')
                WHERE e.action_status IN ('pending','acknowledged')
                """,
                [now],
            )

            cursor.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(action_status = 'pending' AND is_actionable = 1) AS pending_actionable,
                    SUM(candidate_status = 'matched') AS matched,
                    SUM(candidate_status = 'gate_only') AS gate_only,
                    SUM(candidate_status = 'binance_only') AS binance_only
                FROM mi_listing_event
                """
            )
            summary = cursor.fetchone() or {}

    logger.info(
        '交易对上新事件刷新完成: scanned=%s inserted_or_updated=%s pending_actionable=%s',
        len(set(gate_by_asset) | set(spot_by_asset)),
        len(rows),
        int(summary.get('pending_actionable') or 0),
    )
    return {
        'success': True,
        'scanned': len(set(gate_by_asset) | set(spot_by_asset)),
        'events_seen': len(rows),
        'summary': summary,
        'checked_at': now,
    }


def list_listing_events(
    *,
    action_status: Optional[str] = None,
    candidate_status: Optional[str] = None,
    actionable_only: bool = False,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    ensure_listing_event_table()
    conditions: List[str] = []
    params: List[Any] = []
    if action_status and action_status != 'all':
        conditions.append('e.action_status = %s')
        params.append(action_status)
    if candidate_status and candidate_status != 'all':
        if candidate_status == 'added_to_monitor':
            conditions.append("e.action_status = 'added_to_monitor'")
        else:
            conditions.append('e.candidate_status = %s')
            params.append(candidate_status)
            if candidate_status == 'matched':
                conditions.append("e.action_status <> 'added_to_monitor'")
    if actionable_only:
        conditions.append('e.is_actionable = 1')
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ''
    sql = f"""
        SELECT
            e.*,
            b.is_valid AS base_asset_is_valid,
            COALESCE(b.strategy_tier, 'C') AS strategy_tier
        FROM mi_listing_event e
        LEFT JOIN mi_base_asset b
          ON UPPER(TRIM(b.base_asset)) COLLATE utf8mb4_unicode_ci = e.base_asset
        {where}
        ORDER BY
            FIELD(e.action_status, 'pending', 'acknowledged', 'ignored', 'disabled', 'added_to_monitor'),
            e.is_actionable DESC,
            e.last_seen_at DESC
        LIMIT %s
    """
    params.append(max(1, min(int(limit or 200), 1000)))
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall() or []


def listing_event_summary() -> Dict[str, Any]:
    ensure_listing_event_table()
    with db_manager.get_cursor() as cursor:
        cursor.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(action_status = 'pending') AS pending,
                SUM(action_status = 'pending' AND is_actionable = 1) AS pending_actionable,
                SUM(action_status = 'acknowledged') AS acknowledged,
                SUM(action_status = 'ignored') AS ignored,
                SUM(action_status = 'disabled') AS disabled,
                SUM(action_status = 'added_to_monitor') AS added_to_monitor,
                MAX(last_seen_at) AS latest_seen_at
            FROM mi_listing_event
            """
        )
        return cursor.fetchone() or {}


def mark_listing_events(base_assets: Iterable[str], action_status: str, reason: str = '') -> int:
    ensure_listing_event_table()
    assets = [str(asset or '').strip().upper() for asset in base_assets if str(asset or '').strip()]
    if not assets:
        return 0
    placeholders = ','.join(['%s'] * len(assets))
    with db_manager.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE mi_listing_event
                SET action_status = %s,
                    is_actionable = 0,
                    acknowledged_at = COALESCE(acknowledged_at, NOW()),
                    action_at = NOW(),
                    action_reason = %s
                WHERE base_asset IN ({placeholders})
                """,
                [action_status, reason[:255], *assets],
            )
            return cursor.rowcount


def _set_base_asset_state(cursor, asset: str, is_valid: str, reason: str) -> int:
    cursor.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM mi_base_asset
        WHERE UPPER(TRIM(base_asset)) = %s
        """,
        [asset],
    )
    exists = int((cursor.fetchone() or {}).get('cnt') or 0) > 0
    if exists:
        cursor.execute(
            """
            UPDATE mi_base_asset
            SET is_valid = %s,
                tier_reason = %s,
                tier_updated_at = NOW()
            WHERE UPPER(TRIM(base_asset)) = %s
            """,
            [is_valid, reason[:255], asset],
        )
        return cursor.rowcount

    cursor.execute(
        """
        INSERT INTO mi_base_asset (
            base_asset, spot_symbol, future_name, is_valid, strategy_tier,
            tier_reason, tier_updated_at
        ) VALUES (
            %s, %s, %s, %s, 'C', %s, NOW()
        )
        """,
        [asset, f'{asset}USDT', f'{asset}_USDT', is_valid, reason[:255]],
    )
    return cursor.rowcount


def add_listing_asset_to_monitor(base_asset: str) -> Dict[str, Any]:
    ensure_listing_event_table()
    asset = str(base_asset or '').strip().upper()
    if not asset or not asset.replace('_', '').replace('-', '').isalnum():
        raise ValueError('无效标的资产')
    with db_manager.get_connection() as conn:
        with conn.cursor() as cursor:
            affected_asset = _set_base_asset_state(cursor, asset, 'Y', 'listing_event_add_to_monitor')
            cursor.execute(
                """
                UPDATE mi_listing_event
                SET action_status = 'added_to_monitor',
                    is_actionable = 0,
                    acknowledged_at = COALESCE(acknowledged_at, NOW()),
                    action_at = NOW(),
                    action_reason = 'add_to_monitor'
                WHERE base_asset = %s
                """,
                [asset],
            )
    return {'success': True, 'base_asset': asset, 'affected': affected_asset, 'message': f'{asset} 已加入监控，默认 C 层'}


def disable_listing_asset(base_asset: str, reason: str = 'listing_event_disabled') -> Dict[str, Any]:
    ensure_listing_event_table()
    asset = str(base_asset or '').strip().upper()
    if not asset or not asset.replace('_', '').replace('-', '').isalnum():
        raise ValueError('无效标的资产')
    with db_manager.get_connection() as conn:
        with conn.cursor() as cursor:
            affected_asset = _set_base_asset_state(cursor, asset, 'N', reason)
            cursor.execute(
                """
                UPDATE mi_listing_event
                SET action_status = 'disabled',
                    is_actionable = 0,
                    acknowledged_at = COALESCE(acknowledged_at, NOW()),
                    action_at = NOW(),
                    action_reason = %s
                WHERE base_asset = %s
                """,
                [reason[:255], asset],
            )
    return {'success': True, 'base_asset': asset, 'affected': affected_asset, 'message': f'{asset} 已设为失效，不再提示'}
