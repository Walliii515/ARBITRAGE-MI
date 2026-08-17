# coding: utf-8
"""Capital snapshot command service.

Keeps POST /capital/run, /clear-range, and /binance-bnb/buy JSON.
Sync on purpose; callers should wrap with asyncio.to_thread.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from common.database import DatabaseManager
from common.errors import AppError
from common.logger import get_logger
from repositories.capital_command_repo import CapitalCommandRepo

logger = get_logger(__name__)

GetTradeMode = Callable[[], str]
GetBool = Callable[..., bool]
BuildSnapshotter = Callable[[], Any]
BuildBnbBuyer = Callable[[], Any]
GetProvider = Callable[[], Optional[Callable[[], Any]]]
IsRunning = Callable[[], bool]
SetRunning = Callable[[bool], None]
SerializeRow = Callable[[dict[str, Any]], dict[str, Any]]


def parse_capital_range_datetime(value: str, field_name: str) -> datetime:
    text = (value or '').strip()
    if not text:
        raise AppError(f'{field_name} 不能为空', status_code=400)
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise AppError(f'{field_name} 格式必须为 YYYY-MM-DD HH:mm:ss', status_code=400)


class CapitalCommandService:
    def __init__(
        self,
        db_manager: DatabaseManager,
        *,
        get_trade_mode: GetTradeMode,
        get_bool: GetBool,
        build_snapshotter: BuildSnapshotter,
        build_bnb_buyer: BuildBnbBuyer,
        get_pnl_provider: GetProvider,
        lock: Any,
        is_running: IsRunning,
        set_running: SetRunning,
        serialize_row: SerializeRow,
    ) -> None:
        self._repo = CapitalCommandRepo(db_manager)
        self._get_trade_mode = get_trade_mode
        self._get_bool = get_bool
        self._build_snapshotter = build_snapshotter
        self._build_bnb_buyer = build_bnb_buyer
        self._get_pnl_provider = get_pnl_provider
        self._lock = lock
        self._is_running = is_running
        self._set_running = set_running
        self._serialize_row = serialize_row

    def run_snapshot(self) -> dict[str, Any]:
        if self._get_trade_mode() == 'virtual':
            return {'success': False, 'message': 'virtual 模式不采集交易所真实资金'}
        if not self._get_bool('account_capital.enabled', True):
            return {'success': False, 'message': '真实资金采集已关闭'}
        with self._lock:
            if self._is_running():
                return {'success': False, 'message': '资金采集正在执行中'}
            self._set_running(True)
        try:
            provider = self._get_pnl_provider()
            if provider is None:
                return {
                    'success': False,
                    'message': '实时策略盈亏尚未就绪，本次未写入资金快照',
                }
            strategy_pnl = provider()
            if not isinstance(strategy_pnl, dict):
                raise RuntimeError('实时策略盈亏返回格式无效')
            result = self._build_snapshotter().run_once(strategy_pnl)
            return {'success': True, 'message': '资金采集完成', **result}
        except Exception as exc:
            logger.error('手动资金采集失败: %s', exc, exc_info=True)
            return {'success': False, 'message': f'资金采集失败: {exc}'}
        finally:
            with self._lock:
                self._set_running(False)

    def clear_range(self, start_at: str, end_at: str) -> dict[str, Any]:
        start_dt = parse_capital_range_datetime(start_at, 'start_at')
        end_dt = parse_capital_range_datetime(end_at, 'end_at')
        if start_dt > end_dt:
            raise AppError('开始时间不能晚于结束时间', status_code=400)
        backup_table = (
            'mi_capital_snapshot_backup_clear_range_'
            f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        )
        try:
            result = self._repo.clear_range(start_dt, end_dt, backup_table)
            if result['empty']:
                return {
                    'success': True,
                    'deleted': 0,
                    'backup_table': None,
                    'message': '指定时间段没有资金监控数据',
                }
            summary = result['summary']
            deleted = result['deleted']
            logger.warning(
                '资金监控数据已按时间段清理: start=%s end=%s deleted=%s backup=%s',
                start_dt.strftime('%Y-%m-%d %H:%M:%S'),
                end_dt.strftime('%Y-%m-%d %H:%M:%S'),
                deleted,
                result['backup_table'],
            )
            return {
                'success': True,
                'deleted': deleted,
                'backup_table': result['backup_table'],
                'first_snapshot_at': self._serialize_row(
                    {'value': summary.get('first_snapshot_at')}
                )['value'],
                'last_snapshot_at': self._serialize_row(
                    {'value': summary.get('last_snapshot_at')}
                )['value'],
                'message': f'已清理 {deleted} 条资金监控数据',
            }
        except AppError:
            raise
        except Exception as exc:
            logger.error('资金监控数据按时间段清理失败: %s', exc, exc_info=True)
            raise AppError(f'清理失败: {exc}', status_code=500) from exc

    def buy_bnb(self, amount_usdt: float) -> dict[str, Any]:
        if self._get_trade_mode() == 'virtual':
            return {'success': False, 'message': 'virtual 模式不执行 Binance 真实买入'}
        try:
            result = self._build_bnb_buyer().buy_with_usdt(amount_usdt)
        except ValueError as exc:
            return {'success': False, 'message': str(exc)}
        except Exception as exc:
            logger.error('FORWARD Binance BNB 手续费余额买入失败: %s', exc, exc_info=True)
            return {'success': False, 'message': f'买入失败: {exc}'}

        payload = {
            'success': result.success,
            'message': result.message,
            'amount_usdt': result.amount_usdt,
            'result': result.result,
        }
        if result.success:
            try:
                snapshot = self._build_snapshotter().run_once()
                payload['capital_snapshot'] = snapshot
            except Exception as exc:
                logger.warning(
                    'FORWARD Binance BNB 买入后资金快照刷新失败: %s',
                    exc,
                    exc_info=True,
                )
                payload['snapshot_error'] = str(exc)
        return payload
