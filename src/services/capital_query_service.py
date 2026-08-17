# coding: utf-8
"""Capital snapshot query service.

Assembles latest / history / annualized / Gate MMR JSON. Sync on purpose;
callers should wrap with asyncio.to_thread.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Optional

from calc.gate_cross_risk import gate_cross_risk_pressure
from common.database import DatabaseManager
from common.errors import ValidationAppError
from repositories.capital_query_repo import CapitalQueryRepo

Row = dict[str, Any]
SerializeRow = Callable[[Row], Row]
SerializeRows = Callable[[list[Row]], list[Row]]

CAPITAL_HISTORY_INTERVALS = {
    '1m': 60,
    '10m': 600,
    '1h': 3600,
}
CAPITAL_HISTORY_METRIC_COLUMNS = {
    'equity_usdt': (),
    'unrealized_pnl_usdt': (
        's.unrealized_pnl_usdt',
    ),
    'realized_breakdown': (
        's.realized_pnl_usdt',
        's.funding_pnl_usdt',
        's.total_pnl_usdt',
    ),
    'gross_total_pnl_usdt': (
        (
            'COALESCE(s.total_pnl_usdt, 0) + '
            'COALESCE(s.unrealized_pnl_usdt, 0) AS gross_total_pnl_usdt'
        ),
    ),
    'daily_return': (
        's.total_pnl_usdt',
    ),
    'gate_cross_risk': (
        (
            "CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT("
            "s.detail, '$.gate_cross_risk.account_mmr_pct')), 'null') "
            "AS DECIMAL(28,12)) AS gate_cross_mmr_pct"
        ),
    ),
}
CAPITAL_ANNUALIZED_PERIODS = {1, 3, 7, 30, 90, 180, 365}
_CAPITAL_TRANSFER_OUTLIER_RELATIVE_CHANGE = 0.05
_CAPITAL_TRANSFER_RECOVERY_TOLERANCE = 0.015
_CAPITAL_TRANSFER_MAX_RECOVERY_SECONDS = 30 * 60


def capital_history_interval(hours: Optional[int], days: int) -> tuple[str, int]:
    if hours is not None or days <= 1:
        return '1m', CAPITAL_HISTORY_INTERVALS['1m']
    if days <= 7:
        return '10m', CAPITAL_HISTORY_INTERVALS['10m']
    return '1h', CAPITAL_HISTORY_INTERVALS['1h']


def capital_history_select_columns(metric: str) -> str:
    metric_columns = CAPITAL_HISTORY_METRIC_COLUMNS.get(metric)
    if metric_columns is None:
        raise ValidationAppError('不支持的资金趋势指标')
    columns = [
        's.snapshot_at',
        's.exchange',
        's.equity_usdt',
        *metric_columns,
    ]
    return ',\n            '.join(columns)


def calculate_capital_annualized_return(
    rows: list[dict[str, Any]],
    period_days: int,
) -> dict[str, Any]:
    valid_rows = []
    for row in rows:
        sample_count = int(row.get('sample_count') or 0)
        equity_sum = float(row.get('equity_sum_usdt') or 0)
        first_pnl = row.get('first_gross_pnl_usdt')
        last_pnl = row.get('last_gross_pnl_usdt')
        first_realized_pnl = row.get('first_realized_pnl_usdt')
        last_realized_pnl = row.get('last_realized_pnl_usdt')
        if sample_count <= 0 or equity_sum <= 0 or first_pnl is None or last_pnl is None:
            continue
        valid_rows.append({
            **row,
            'average_equity_usdt': equity_sum / sample_count,
            'gross_pnl_delta_usdt': float(last_pnl) - float(first_pnl),
            'realized_pnl_delta_usdt': (
                float(last_realized_pnl) - float(first_realized_pnl)
                if first_realized_pnl is not None and last_realized_pnl is not None
                else None
            ),
        })

    available_days = len(valid_rows)
    sufficient = available_days >= period_days
    total_samples = sum(int(row.get('sample_count') or 0) for row in valid_rows)
    total_equity = sum(float(row.get('equity_sum_usdt') or 0) for row in valid_rows)
    average_equity = total_equity / total_samples if total_samples > 0 else None

    def _metric_result(delta_key: str) -> dict[str, Any]:
        metric_rows = [
            row for row in valid_rows
            if row.get(delta_key) is not None and row.get('average_equity_usdt')
        ]
        period_pnl = sum(float(row[delta_key]) for row in metric_rows)
        compound_factor = 1.0
        factor_valid = bool(metric_rows)
        for row in metric_rows:
            daily_factor = 1.0 + (float(row[delta_key]) / row['average_equity_usdt'])
            if daily_factor <= 0:
                factor_valid = False
                break
            compound_factor *= daily_factor
        period_return_pct = (
            (compound_factor - 1.0) * 100.0
            if factor_valid
            else None
        )
        annualized_return_pct = (
            (compound_factor ** (365.0 / period_days) - 1.0) * 100.0
            if sufficient and len(metric_rows) >= period_days and factor_valid
            else None
        )
        return {
            'available_days': len(metric_rows),
            'annualized_return_pct': annualized_return_pct,
            'period_return_pct': period_return_pct,
            'period_pnl_usdt': period_pnl if metric_rows else None,
        }

    strategy = _metric_result('gross_pnl_delta_usdt')
    realized = _metric_result('realized_pnl_delta_usdt')
    realized_sufficient = realized['available_days'] >= period_days
    realized_data_available = realized['available_days'] > 0
    realized_supported = all(
        row.get('first_realized_pnl_usdt') is not None and row.get('last_realized_pnl_usdt') is not None
        for row in valid_rows
    )
    return {
        'period_days': period_days,
        'available_days': available_days,
        'sufficient_data': sufficient,
        'annualized_return_pct': strategy['annualized_return_pct'],
        'period_return_pct': strategy['period_return_pct'],
        'period_pnl_usdt': strategy['period_pnl_usdt'],
        'realized_available_days': realized['available_days'],
        'realized_sufficient_data': realized_sufficient,
        'realized_data_available': realized_data_available,
        'realized_annualized_return_pct': realized['annualized_return_pct'],
        'realized_period_return_pct': realized['period_return_pct'],
        'realized_period_pnl_usdt': realized['period_pnl_usdt'],
        'average_equity_usdt': average_equity,
        'start_date': str(valid_rows[0].get('summary_date')) if valid_rows else None,
        'end_date': str(valid_rows[-1].get('summary_date')) if valid_rows else None,
        'window_end_policy': 'previous_calendar_day',
        'realized_formula_supported': realized_supported,
        'formula': 'daily_gross_pnl_delta_over_daily_average_equity_compounded',
        'realized_formula': 'daily_realized_pnl_delta_over_daily_average_equity_compounded',
    }


def aggregate_capital_latest_account_rows(
    rows: list[dict[str, Any]],
    serialize_rows: SerializeRows,
) -> list[dict[str, Any]]:
    serialized = serialize_rows(rows)
    by_exchange = {str(row.get('exchange') or ''): row for row in serialized}
    binance = by_exchange.get('binance')
    gate = by_exchange.get('gate')
    total = by_exchange.get('total')
    if not binance or not gate or not total:
        return serialized

    for key in (
        'equity_usdt',
        'available_usdt',
        'locked_usdt',
        'position_value_usdt',
        'margin_used_usdt',
    ):
        if binance.get(key) is None or gate.get(key) is None:
            continue
        total[key] = float(binance[key]) + float(gate[key])

    account_balance_values = (
        binance.get('account_balance_usdt'),
        gate.get('account_balance_usdt'),
    )
    account_unrealized_values = (
        binance.get('account_unrealized_pnl_usdt'),
        gate.get('account_unrealized_pnl_usdt'),
    )
    if all(value is not None for value in account_balance_values):
        total['account_balance_usdt'] = sum(float(value) for value in account_balance_values)
    if all(value is not None for value in account_unrealized_values):
        total['account_unrealized_pnl_usdt'] = sum(
            float(value) for value in account_unrealized_values
        )
    return serialized


def _capital_row_time(row: dict[str, Any]) -> Optional[datetime]:
    value = row.get('snapshot_at')
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return None
    return None


def _capital_row_equity(row: dict[str, Any]) -> Optional[float]:
    try:
        value = float(row.get('equity_usdt'))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _relative_gap(value: float, reference: float) -> float:
    if abs(reference) <= 1e-9:
        return 0.0
    return abs(value - reference) / abs(reference)


def filter_capital_transfer_transient_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total_rows = [(idx, row) for idx, row in enumerate(rows) if row.get('exchange') == 'total']
    if len(total_rows) < 3:
        return rows

    remove_indexes: set[int] = set()
    pos = 0
    while pos < len(total_rows) - 1:
        prev_idx, prev = total_rows[pos]
        prev_equity = _capital_row_equity(prev)
        prev_time = _capital_row_time(prev)
        next_idx, next_row = total_rows[pos + 1]
        next_equity = _capital_row_equity(next_row)
        if prev_equity is None or prev_time is None or next_equity is None:
            pos += 1
            continue

        if _relative_gap(next_equity, prev_equity) < _CAPITAL_TRANSFER_OUTLIER_RELATIVE_CHANGE:
            pos += 1
            continue

        recovery_pos: Optional[int] = None
        for scan_pos in range(pos + 2, len(total_rows)):
            _, candidate = total_rows[scan_pos]
            candidate_time = _capital_row_time(candidate)
            candidate_equity = _capital_row_equity(candidate)
            if candidate_time is None or candidate_equity is None:
                continue
            if (candidate_time - prev_time).total_seconds() > _CAPITAL_TRANSFER_MAX_RECOVERY_SECONDS:
                break
            if _relative_gap(candidate_equity, prev_equity) <= _CAPITAL_TRANSFER_RECOVERY_TOLERANCE:
                recovery_pos = scan_pos
                break

        if recovery_pos is None:
            pos += 1
            continue

        for outlier_pos in range(pos + 1, recovery_pos):
            remove_indexes.add(total_rows[outlier_pos][0])
        pos = recovery_pos

    if not remove_indexes:
        return rows
    return [row for idx, row in enumerate(rows) if idx not in remove_indexes]


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gate_cross_primary_risk(risk: dict[str, Any]) -> tuple[Optional[dict], str]:
    primary = risk.get('primary_risk')
    if isinstance(primary, dict) and primary.get('contract'):
        return primary, 'full_snapshot'

    candidates = [
        item
        for item in (risk.get('top_risks') or [])
        if isinstance(item, dict) and item.get('contract')
    ]
    if candidates:
        return max(
            candidates,
            key=lambda item: (
                gate_cross_risk_pressure(item),
                _optional_float(item.get('maintenance_margin_usdt')) or 0.0,
                str(item.get('contract') or ''),
            ),
        ), 'legacy_top_risks'

    contract = risk.get('primary_risk_contract')
    if contract:
        return {
            'contract': contract,
            'risk_pressure_usdt': risk.get('primary_risk_pressure_usdt'),
        }, 'snapshot_contract'
    return None, 'unavailable'


def build_gate_cross_minimum_summary(
    row: Optional[dict[str, Any]],
    *,
    serialize_row: SerializeRow,
) -> Optional[dict[str, Any]]:
    if not row:
        return None

    detail = row.get('detail')
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except (TypeError, ValueError):
            detail = {}
    detail = detail if isinstance(detail, dict) else {}
    risk = detail.get('gate_cross_risk')
    risk = risk if isinstance(risk, dict) else {}
    primary, attribution = _gate_cross_primary_risk(risk)
    primary = primary or {}
    contract = str(primary.get('contract') or '').strip().upper() or None
    base_asset = contract
    if base_asset and base_asset.endswith('_USDT'):
        base_asset = base_asset[:-5]

    mmr_pct = _optional_float(row.get('gate_cross_mmr_pct'))
    if mmr_pct is None:
        mmr_pct = _optional_float(risk.get('account_mmr_pct'))
    pressure = _optional_float(primary.get('risk_pressure_usdt'))
    if primary and pressure is None:
        pressure = gate_cross_risk_pressure(primary)

    serialized = serialize_row({'snapshot_at': row.get('snapshot_at')})
    return {
        'account_mmr_pct': mmr_pct,
        'snapshot_at': serialized.get('snapshot_at'),
        'primary_risk_contract': contract,
        'primary_risk_asset': base_asset,
        'primary_risk_pressure_usdt': pressure,
        'maintenance_margin_usdt': _optional_float(
            primary.get('maintenance_margin_usdt')
        ),
        'unrealized_pnl_usdt': _optional_float(
            primary.get('unrealized_pnl_usdt')
        ),
        'liq_distance_bps': _optional_float(primary.get('liq_distance_bps')),
        'attribution': attribution,
    }


def _empty_today_realized_pnl_summary() -> dict[str, Any]:
    return {
        'today_realized_pnl_usdt': None,
        'today_return_pct': None,
        'today_first_snapshot_at': None,
        'today_last_snapshot_at': None,
    }


class CapitalQueryService:
    def __init__(
        self,
        db_manager: DatabaseManager,
        *,
        serialize_row: SerializeRow,
        serialize_rows: SerializeRows,
    ) -> None:
        self._repo = CapitalQueryRepo(db_manager)
        self._serialize_row = serialize_row
        self._serialize_rows = serialize_rows

    def latest(self) -> dict[str, Any]:
        rows = self._repo.list_latest_snapshots()
        return {'rows': aggregate_capital_latest_account_rows(rows, self._serialize_rows)}

    def history(
        self,
        *,
        days: int,
        hours: Optional[int],
        exchange: Optional[str],
        metric: str,
    ) -> dict[str, Any]:
        if metric == 'daily_return':
            if hours is not None:
                days = 1
            rows = self._repo.list_daily_return_rows(days, exchange)
            return {
                'rows': self._serialize_rows(rows),
                'metric': metric,
                'interval': '1d',
                'window': {'hours': hours} if hours is not None else {'days': days},
            }

        interval, bucket_sec = capital_history_interval(hours, days)
        select_columns = capital_history_select_columns(metric)
        if metric == 'gate_cross_risk':
            exchange = 'gate'
        if hours is not None:
            window_clause = "snapshot_at >= DATE_SUB(NOW(), INTERVAL %s HOUR)"
            window_value = hours
        else:
            window_clause = "snapshot_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
            window_value = days
        where = [
            window_clause,
            "JSON_UNQUOTE(JSON_EXTRACT(detail, '$.source')) = 'exchange_api'",
        ]
        params: list[Any] = [window_value]
        if exchange in ('binance', 'gate', 'total'):
            where.append("exchange = %s")
            params.append(exchange)
        where_sql = " AND ".join(where)
        force_index = 'FORCE INDEX (idx_exchange_snapshot)' if exchange else ''
        rows = self._repo.list_history_buckets(
            select_columns=select_columns,
            where_sql=where_sql,
            params=params,
            bucket_sec=bucket_sec,
            force_index=force_index,
        )
        serialized_rows = filter_capital_transfer_transient_rows(self._serialize_rows(rows))
        return {
            'rows': serialized_rows,
            'metric': metric,
            'interval': interval,
            'window': {'hours': hours} if hours is not None else {'days': days},
        }

    def annualized_return(self, days: int) -> dict[str, Any]:
        rows = self._repo.list_annualized_daily_rows(days)
        result = calculate_capital_annualized_return(rows, days)
        result.update(self._today_realized_pnl_summary())
        return result

    def gate_cross_risk_summary(self, days: int) -> dict[str, Any]:
        row = self._repo.fetch_gate_cross_risk_minimum(days)
        return {
            'period_days': days,
            'minimum': build_gate_cross_minimum_summary(row, serialize_row=self._serialize_row),
        }

    def _today_realized_pnl_summary(self) -> dict[str, Any]:
        row = self._repo.fetch_today_realized_pnl_row()
        if not isinstance(row, dict):
            return _empty_today_realized_pnl_summary()
        first_pnl = row.get('first_total_pnl_usdt')
        last_pnl = row.get('last_total_pnl_usdt')
        if first_pnl is None or last_pnl is None:
            return _empty_today_realized_pnl_summary()
        pnl_delta = float(last_pnl) - float(first_pnl)
        first_equity = float(row.get('first_equity_usdt') or 0)
        return {
            'today_realized_pnl_usdt': pnl_delta,
            'today_return_pct': (
                pnl_delta / first_equity * 100
                if abs(first_equity) > 0.000000001
                else None
            ),
            'today_first_snapshot_at': (
                str(row.get('first_snapshot_at')) if row.get('first_snapshot_at') else None
            ),
            'today_last_snapshot_at': (
                str(row.get('last_snapshot_at')) if row.get('last_snapshot_at') else None
            ),
        }
