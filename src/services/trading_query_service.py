# coding: utf-8
"""Forward trading query service.

Assembles repository rows into the existing API JSON. Sync on purpose;
callers should wrap with asyncio.to_thread.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from calc.gate_position_risk import attach_gate_position_risk
from calc.position_order_fees import attach_position_order_fee_summary
from calc.position_pnl_calculator import PnlConfig, calculate_realtime_pnl
from calc.reconciliation import build_default_reconciler
from common.config import config
from common.database import DatabaseManager
from common.logger import get_logger
from common.meta_loader import fetch_contract_meta
from repositories.trading_query_repo import (
    ALLOWED_CLOSE_THRESHOLD_COLS,
    TradingQueryRepo,
    build_forward_signal_filters,
    build_order_view_where,
)

logger = get_logger(__name__)

Row = dict[str, Any]
SerializeRow = Callable[[Row], Row]
SerializeRows = Callable[[list[Row]], list[Row]]
AttachDelist = Callable[[list[Row]], None]
DelistAssetSet = Callable[..., set[str]]
InjectFunding = Callable[..., None]
PnlConfigFactory = Callable[[], PnlConfig]


class TradingQueryService:
    def __init__(
        self,
        db_manager: DatabaseManager,
        *,
        serialize_row: SerializeRow,
        serialize_rows: SerializeRows,
        attach_delist_risks: AttachDelist,
        delist_risk_asset_set: DelistAssetSet,
        inject_current_funding_fields: InjectFunding,
        position_pnl_config: PnlConfigFactory,
    ) -> None:
        self._repo = TradingQueryRepo(db_manager)
        self._serialize_row = serialize_row
        self._serialize_rows = serialize_rows
        self._attach_delist_risks = attach_delist_risks
        self._delist_risk_asset_set = delist_risk_asset_set
        self._inject_current_funding_fields = inject_current_funding_fields
        self._position_pnl_config = position_pnl_config

    def _close_threshold_col(self) -> str:
        close_threshold_col = config.get_str(
            'trade.vwap.close_threshold_percentile',
            'close_basis_p20',
        ).strip()
        if close_threshold_col not in ALLOWED_CLOSE_THRESHOLD_COLS:
            logger.warning(f'无效平仓VWAP阈值字段 {close_threshold_col}，回退 close_basis_p20')
            return 'close_basis_p20'
        return close_threshold_col

    def list_order_view(
        self,
        *,
        view: str,
        channel: Optional[str],
        exchange_risk: bool,
        position_id: Optional[int],
        base_asset: Optional[str],
        days: int,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        close_threshold_col = self._close_threshold_col()
        time_column = 'p.opened_at' if view == 'open' else 'p.closed_at'
        delist_risk_assets: list[str] = (
            sorted(self._delist_risk_asset_set()) if exchange_risk else []
        )
        where_sql, params = build_order_view_where(
            normalized_view=view,
            days=days,
            base_asset=base_asset,
            position_id=position_id,
            channel=channel,
            delist_risk_assets=delist_risk_assets,
            exchange_risk=exchange_risk,
        )
        total = self._repo.count_positions_by_where(where_sql, params)
        offset = (page - 1) * page_size
        rows = self._repo.list_order_view_positions(
            where_sql=where_sql,
            params=params,
            time_column=time_column,
            close_threshold_col=close_threshold_col,
            page_size=page_size,
            offset=offset,
        )
        self._attach_delist_risks(rows)
        tab_summary_row = self._repo.fetch_order_tab_summary()
        return {
            'orders': self._serialize_rows(rows),
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total': total,
                'total_pages': (total + page_size - 1) // page_size if total else 0,
            },
            'view': view,
            'summary': {
                'current_open': int(tab_summary_row.get('current_open') or 0),
                'today_closed': int(tab_summary_row.get('today_closed') or 0),
            },
        }

    def list_position_orders(self, position_id: int) -> dict[str, Any]:
        rows = self._repo.list_orders_by_position_id(position_id)
        return {'orders': self._serialize_rows(rows)}

    def list_grouped_orders(self) -> list[dict[str, Any]]:
        rows = self._repo.list_grouped_orders()
        groups: dict[Any, dict[str, Any]] = {}
        for row in rows:
            pid = row['position_id']
            if pid not in groups:
                groups[pid] = {
                    'position_id': pid,
                    'base_asset': row['base_asset'],
                    'orders': [],
                    'summary': {},
                }
            groups[pid]['orders'].append(self._serialize_row(row))

        result: list[dict[str, Any]] = []
        for _pid, group in groups.items():
            orders = group['orders']
            total_exec_amount = sum(float(o['exec_amount'] or 0) for o in orders)
            total_target_amount = sum(float(o['target_amount'] or 0) for o in orders)
            group['summary'] = {
                'total_exec_amount': total_exec_amount,
                'total_target_amount': total_target_amount,
                'order_count': len(orders),
            }
            result.append(group)
        return result

    def list_positions(
        self,
        *,
        status: Optional[str],
        base_asset: Optional[str],
        days: int,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        try:
            summary_row = self._repo.fetch_positions_status_summary(days, base_asset)
            summary = {
                'total': int(summary_row.get('total') or 0),
                'holding': int(summary_row.get('holding_count') or 0),
                'closed': int(summary_row.get('closed_count') or 0),
            }
            total = self._repo.count_positions(days, status, base_asset)
            offset = (page - 1) * page_size
            rows = self._repo.list_positions(days, status, base_asset, page_size, offset)
            if not rows:
                return {
                    'positions': [],
                    'pagination': {
                        'page': page,
                        'page_size': page_size,
                        'total': 0,
                        'total_pages': 0,
                    },
                    'summary': summary,
                }

            position_ids = [r['id'] for r in rows]
            history_rows = self._repo.list_funding_fee_history(position_ids)
            histories: dict[Any, list[dict[str, Any]]] = {}
            for history in history_rows:
                pid = history['position_id']
                if pid not in histories:
                    histories[pid] = []
                histories[pid].append({
                    'seq': history['payment_seq'],
                    'rate': float(history['funding_rate']) if history.get('funding_rate') is not None else None,
                    'rate_24h': float(history['funding_rate_24h']) if history.get('funding_rate_24h') else None,
                    'pnl': float(history['funding_pnl']) if history.get('funding_pnl') is not None else 0,
                    'notional': float(history['future_notional']) if history.get('future_notional') else None,
                    'time': history['settled_at'].strftime('%m-%d %H:%M') if history.get('settled_at') else None,
                })

            attach_position_order_fee_summary(rows)
            if any(row.get('status') == 'holding' for row in rows):
                try:
                    gate_positions = build_default_reconciler().executor.fetch_gate_futures_positions()
                    attach_gate_position_risk(rows, gate_positions)
                except Exception as e:
                    logger.warning(f'Gate维持保证金率拉取失败: {e}')
            contract_meta = fetch_contract_meta()
            calculate_realtime_pnl(rows, {}, contract_meta, self._position_pnl_config())
            self._attach_delist_risks(rows)
            serialized = self._serialize_rows(rows)
            self._inject_current_funding_fields(serialized, contract_meta)
            for row in serialized:
                row['funding_history'] = histories.get(row.get('id'), [])
            return {
                'positions': serialized,
                'pagination': {
                    'page': page,
                    'page_size': page_size,
                    'total': total,
                    'total_pages': (total + page_size - 1) // page_size,
                },
                'summary': summary,
                'open_amount_usdt': config.get_float('trade.open.amount_usdt', 10.0),
            }
        except Exception as e:
            logger.error(f'查询持仓失败: {e}', exc_info=True)
            raise

    def positions_summary(self) -> dict[str, Any]:
        row = self._repo.fetch_positions_aggregate_summary()
        return self._serialize_row(row) if row else {}

    def list_signals(
        self,
        *,
        status: Optional[str],
        exit_reason: Optional[str],
        base_asset: Optional[str],
        time_range: Optional[str],
        days: int,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        where_sql, where_params = build_forward_signal_filters(
            status=status,
            exit_reason=exit_reason,
            base_asset=base_asset,
            time_range=time_range,
            days=days,
        )
        aliased_where_sql, aliased_where_params = build_forward_signal_filters(
            status=status,
            exit_reason=exit_reason,
            base_asset=base_asset,
            time_range=time_range,
            days=days,
            prefix='s.',
        )
        offset = (page - 1) * page_size
        rows = self._repo.list_forward_signals(
            aliased_where_sql, aliased_where_params, page_size, offset
        )
        data = self._serialize_rows(rows)
        summary_row = self._repo.fetch_forward_signal_summary(where_sql, where_params)
        summary_data = self._serialize_row(summary_row) if summary_row else {}
        total_count = summary_data.get('total', 0)
        opened_count = summary_data.get('opened', 0)
        rejected_count = summary_data.get('rejected', 0)
        conditions_lost_count = summary_data.get('conditions_lost', 0)
        monitoring_count = summary_data.get('monitoring', 0)
        latest_signal_time = summary_data.get('latest_signal_time')
        return {
            'signals': data,
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total': total_count,
                'total_pages': (total_count + page_size - 1) // page_size,
            },
            'summary': {
                'total': total_count,
                'opened': opened_count,
                'rejected': rejected_count,
                'conditions_lost': conditions_lost_count,
                'monitoring': monitoring_count,
                'conversion_rate': round(opened_count / total_count * 100, 1) if total_count > 0 else 0,
                'latest_signal_time': latest_signal_time,
            },
        }
