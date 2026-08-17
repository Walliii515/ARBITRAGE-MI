# coding: utf-8
"""Capital snapshot query repository.

Wraps existing parameterized SQL for latest / history / annualized / Gate MMR.
Does not change table schema or result columns.
"""
from __future__ import annotations

from typing import Any, Optional

from common.database import DatabaseManager, db_manager as _default_db_manager

Row = dict[str, Any]


class CapitalQueryRepo:
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self._db = db_manager or _default_db_manager

    def list_latest_snapshots(self) -> list[Row]:
        sql = """
        SELECT
            id,
            snapshot_at,
            exchange,
            equity_usdt,
            available_usdt,
            locked_usdt,
            position_value_usdt,
            margin_used_usdt,
            unrealized_pnl_usdt,
            realized_pnl_usdt,
            funding_pnl_usdt,
            fee_cost_usdt,
            total_pnl_usdt,
            COALESCE(total_pnl_usdt, 0) + COALESCE(unrealized_pnl_usdt, 0) AS gross_total_pnl_usdt,
            CASE
                WHEN exchange = 'gate' THEN COALESCE(
                    CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.raw_total_usdt')), 'null') AS DECIMAL(28,12)),
                    equity_usdt - COALESCE(unrealized_pnl_usdt, 0)
                )
                ELSE equity_usdt - COALESCE(unrealized_pnl_usdt, 0)
            END AS account_balance_usdt,
            CASE
                WHEN exchange = 'gate' THEN COALESCE(
                    CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.account_unrealized_pnl')), 'null') AS DECIMAL(28,12)),
                    unrealized_pnl_usdt,
                    0
                )
                ELSE COALESCE(unrealized_pnl_usdt, 0)
            END AS account_unrealized_pnl_usdt,
            CAST(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.bnb_fee_asset.free')) AS DECIMAL(28,12)) AS bnb_available,
            CAST(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.bnb_fee_asset.free_value_usdt')) AS DECIMAL(28,12)) AS bnb_available_usdt,
            NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.status')), 'null') AS gate_cross_risk_status,
            NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.status_label')), 'null') AS gate_cross_risk_status_label,
            CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.position_count')), 'null') AS UNSIGNED) AS gate_cross_position_count,
            CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.account_mmr_pct')), 'null') AS DECIMAL(28,12)) AS gate_cross_mmr_pct,
            CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.available_ratio_pct')), 'null') AS DECIMAL(28,12)) AS gate_cross_available_ratio_pct,
            CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.margin_usage_pct')), 'null') AS DECIMAL(28,12)) AS gate_cross_margin_usage_pct,
            CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.initial_margin_usdt')), 'null') AS DECIMAL(28,12)) AS gate_cross_initial_margin_usdt,
            CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.maintenance_margin_usdt')), 'null') AS DECIMAL(28,12)) AS gate_cross_maintenance_margin_usdt,
            NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.nearest_liq_contract')), 'null') AS gate_cross_nearest_liq_contract,
            CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.nearest_liq_distance_bps')), 'null') AS DECIMAL(28,12)) AS gate_cross_nearest_liq_distance_bps,
            NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.health_status')), 'null') AS gate_cross_health_status,
            NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.health_label')), 'null') AS gate_cross_health_label,
            NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.observed_status')), 'null') AS gate_cross_observed_status,
            NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.source')), 'null') AS gate_cross_source,
            NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.error')), 'null') AS gate_cross_error,
            NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.fetched_at')), 'null') AS gate_cross_fetched_at,
            CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.account_age_sec')), 'null') AS DECIMAL(28,12)) AS gate_cross_account_age_sec,
            CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.positions_age_sec')), 'null') AS DECIMAL(28,12)) AS gate_cross_positions_age_sec,
            CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.account_latency_ms')), 'null') AS DECIMAL(28,12)) AS gate_cross_account_latency_ms,
            CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.positions_latency_ms')), 'null') AS DECIMAL(28,12)) AS gate_cross_positions_latency_ms,
            CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.latency_ms')), 'null') AS DECIMAL(28,12)) AS gate_cross_latency_ms
        FROM mi_capital_snapshot
        WHERE JSON_UNQUOTE(JSON_EXTRACT(detail, '$.source')) = 'exchange_api'
          AND snapshot_at = (
              SELECT MAX(snapshot_at)
              FROM mi_capital_snapshot
              WHERE JSON_UNQUOTE(JSON_EXTRACT(detail, '$.source')) = 'exchange_api'
          )
        ORDER BY FIELD(exchange, 'binance', 'gate', 'total')
    """
        with self._db.get_cursor() as cursor:
            cursor.execute(sql)
            return list(cursor.fetchall() or [])

    def list_daily_return_rows(
        self,
        days: int,
        exchange: Optional[str],
    ) -> list[Row]:
        where = [
            "snapshot_at >= DATE_SUB(CURDATE(), INTERVAL %s DAY)",
            "JSON_UNQUOTE(JSON_EXTRACT(detail, '$.source')) = 'exchange_api'",
            "total_pnl_usdt IS NOT NULL",
        ]
        params: list[Any] = [days]
        if exchange in ('binance', 'gate', 'total'):
            where.append("exchange = %s")
            params.append(exchange)
        where_sql = " AND ".join(where)
        force_index = 'FORCE INDEX (idx_exchange_snapshot)' if exchange else ''
        sql = f"""
        SELECT
            DATE_FORMAT(grouped.summary_date, '%%Y-%%m-%%d 00:00:00') AS snapshot_at,
            grouped.exchange,
            first_row.equity_usdt,
            last_row.total_pnl_usdt - first_row.total_pnl_usdt AS daily_realized_pnl_usdt,
            CASE
                WHEN first_row.equity_usdt IS NULL OR ABS(first_row.equity_usdt) < 0.000000001
                    THEN NULL
                ELSE (last_row.total_pnl_usdt - first_row.total_pnl_usdt) / first_row.equity_usdt * 100
            END AS daily_return_pct
        FROM (
            SELECT
                DATE(snapshot_at) AS summary_date,
                exchange,
                MIN(id) AS first_id,
                MAX(id) AS last_id
            FROM mi_capital_snapshot {force_index}
            WHERE {where_sql}
            GROUP BY DATE(snapshot_at), exchange
        ) grouped
        INNER JOIN mi_capital_snapshot first_row ON first_row.id = grouped.first_id
        INNER JOIN mi_capital_snapshot last_row ON last_row.id = grouped.last_id
        ORDER BY grouped.summary_date ASC, FIELD(grouped.exchange, 'binance', 'gate', 'total')
        LIMIT 10000
    """
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall() or [])

    def list_history_buckets(
        self,
        *,
        select_columns: str,
        where_sql: str,
        params: list[Any],
        bucket_sec: int,
        force_index: str,
    ) -> list[Row]:
        sql = f"""
        SELECT
            {select_columns}
        FROM mi_capital_snapshot s
        INNER JOIN (
            SELECT
                exchange,
                FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(snapshot_at) / %s) * %s) AS bucket_at,
                MAX(snapshot_at) AS snapshot_at
            FROM mi_capital_snapshot {force_index}
            WHERE {where_sql}
            GROUP BY exchange, bucket_at
        ) latest
          ON latest.exchange = s.exchange
         AND latest.snapshot_at = s.snapshot_at
        WHERE JSON_UNQUOTE(JSON_EXTRACT(s.detail, '$.source')) = 'exchange_api'
        ORDER BY s.snapshot_at ASC, FIELD(s.exchange, 'binance', 'gate', 'total')
        LIMIT 10000
    """
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, [bucket_sec, bucket_sec, *params])
            return list(cursor.fetchall() or [])

    def list_annualized_daily_rows(self, days: int) -> list[Row]:
        sql = """
        SELECT
            d.summary_date,
            d.first_snapshot_at,
            d.last_snapshot_at,
            d.equity_sum_usdt,
            d.sample_count,
            d.first_gross_pnl_usdt,
            d.last_gross_pnl_usdt,
            (
                SELECT s.total_pnl_usdt
                FROM mi_capital_snapshot s
                WHERE s.exchange = 'total'
                  AND s.snapshot_at = d.first_snapshot_at
                ORDER BY s.id ASC
                LIMIT 1
            ) AS first_realized_pnl_usdt,
            (
                SELECT s.total_pnl_usdt
                FROM mi_capital_snapshot s
                WHERE s.exchange = 'total'
                  AND s.snapshot_at = d.last_snapshot_at
                ORDER BY s.id DESC
                LIMIT 1
            ) AS last_realized_pnl_usdt
        FROM mi_capital_daily_summary d
        WHERE d.summary_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
          AND d.summary_date < CURDATE()
        ORDER BY d.summary_date ASC
    """
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, (days,))
            return list(cursor.fetchall() or [])

    def fetch_today_realized_pnl_row(self) -> Optional[Row]:
        sql = """
        SELECT
            first_row.snapshot_at AS first_snapshot_at,
            last_row.snapshot_at AS last_snapshot_at,
            first_row.equity_usdt AS first_equity_usdt,
            first_row.total_pnl_usdt AS first_total_pnl_usdt,
            last_row.total_pnl_usdt AS last_total_pnl_usdt
        FROM (
            SELECT MIN(id) AS first_id, MAX(id) AS last_id
            FROM mi_capital_snapshot
            WHERE exchange = 'total'
              AND snapshot_at >= CURDATE()
              AND JSON_UNQUOTE(JSON_EXTRACT(detail, '$.source')) = 'exchange_api'
              AND total_pnl_usdt IS NOT NULL
        ) ids
        INNER JOIN mi_capital_snapshot first_row ON first_row.id = ids.first_id
        INNER JOIN mi_capital_snapshot last_row ON last_row.id = ids.last_id
        LIMIT 1
    """
        with self._db.get_cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetchone()

    def fetch_gate_cross_risk_minimum(self, days: int) -> Optional[Row]:
        sql = """
        SELECT
            snapshot_at,
            CAST(
                NULLIF(
                    JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.account_mmr_pct')),
                    'null'
                ) AS DECIMAL(28,12)
            ) AS gate_cross_mmr_pct,
            detail
        FROM mi_capital_snapshot
        WHERE exchange = 'gate'
          AND snapshot_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
          AND JSON_UNQUOTE(JSON_EXTRACT(detail, '$.source')) = 'exchange_api'
          AND CAST(
                NULLIF(
                    JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.position_count')),
                    'null'
                ) AS UNSIGNED
              ) > 0
          AND CAST(
                NULLIF(
                    JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.account_mmr_pct')),
                    'null'
                ) AS DECIMAL(28,12)
              ) > 0
          AND COALESCE(
                NULLIF(
                    JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.health_status')),
                    'null'
                ),
                'healthy'
              ) = 'healthy'
          AND COALESCE(
                NULLIF(
                    JSON_UNQUOTE(JSON_EXTRACT(detail, '$.gate_cross_risk.source')),
                    'null'
                ),
                'gate_account_api'
              ) = 'gate_account_api'
        ORDER BY gate_cross_mmr_pct ASC, snapshot_at ASC
        LIMIT 1
    """
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, [days])
            return cursor.fetchone()
