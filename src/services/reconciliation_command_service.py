# coding: utf-8
"""Reconciliation command service.

Keeps POST /reconciliation/run and /dust/cleanup JSON. Sync on purpose;
callers should wrap with asyncio.to_thread.
"""
from __future__ import annotations

import math
from typing import Any, Callable

from common.logger import get_logger

logger = get_logger(__name__)

GetTradeMode = Callable[[], str]
GetBool = Callable[..., bool]
BuildReconciler = Callable[[], Any]
IsRunning = Callable[[], bool]
SetRunning = Callable[[bool], None]


def format_dust_cleanup_message(cleanup: dict[str, Any]) -> str:
    if cleanup.get('message'):
        return str(cleanup['message'])

    if cleanup.get('success'):
        return '小额残余清理完成'

    reason = str(cleanup.get('reason') or 'unknown')
    if reason == 'binance_dust_conversion_cooldown':
        remaining = cleanup.get('cooldown_remaining_sec')
        if remaining is not None:
            seconds = max(0, int(math.ceil(float(remaining))))
            return f'小额残余清理失败: Binance 小额兑换冷却中，剩余 {seconds} 秒'
        return '小额残余清理失败: Binance 小额兑换冷却中'

    skipped = cleanup.get('skipped')
    if reason == 'no_safe_dust_found' and isinstance(skipped, list) and skipped:
        summary = '；'.join(
            f"{item.get('base_asset') or '-'}={item.get('reason') or 'unknown'}"
            for item in skipped[:5]
            if isinstance(item, dict)
        )
        if summary:
            return f'未发现可安全兑换的小额残余，已跳过: {summary}'

    return f'小额残余清理失败: {reason}'


class ReconciliationCommandService:
    def __init__(
        self,
        *,
        get_trade_mode: GetTradeMode,
        get_bool: GetBool,
        build_reconciler: BuildReconciler,
        lock: Any,
        is_running: IsRunning,
        set_running: SetRunning,
    ) -> None:
        self._get_trade_mode = get_trade_mode
        self._get_bool = get_bool
        self._build_reconciler = build_reconciler
        self._lock = lock
        self._is_running = is_running
        self._set_running = set_running

    def _acquire(self) -> dict[str, Any] | None:
        with self._lock:
            if self._is_running():
                return {'success': False, 'message': '对账任务正在执行中'}
            self._set_running(True)
        return None

    def _release(self) -> None:
        with self._lock:
            self._set_running(False)

    def run_now(self) -> dict[str, Any]:
        if self._get_trade_mode() == 'virtual':
            return {'success': False, 'message': 'virtual 模式不执行交易所对账'}
        if not self._get_bool('reconciliation.enabled', True):
            return {'success': False, 'message': '对账功能已关闭'}
        busy = self._acquire()
        if busy is not None:
            return busy
        try:
            result = self._build_reconciler().run_with_fast_confirmation()
            return {'success': True, 'message': '对账完成', **result}
        except Exception as exc:
            logger.error('手动对账失败: %s', exc, exc_info=True)
            return {'success': False, 'message': f'对账失败: {exc}'}
        finally:
            self._release()

    def cleanup_dust(self) -> dict[str, Any]:
        if self._get_trade_mode() == 'virtual':
            return {'success': False, 'message': 'virtual 模式不执行小额兑换'}
        if not self._get_bool('reconciliation.enabled', True):
            return {'success': False, 'message': '对账功能已关闭'}
        busy = self._acquire()
        if busy is not None:
            return busy
        try:
            reconciler = self._build_reconciler()
            cleanup = reconciler.cleanup_post_close_dust()
            reconciliation = reconciler.run_once()
            return {
                **cleanup,
                'message': format_dust_cleanup_message(cleanup),
                'reconciliation': reconciliation,
            }
        except Exception as exc:
            logger.error('小额残余清理失败: %s', exc, exc_info=True)
            return {'success': False, 'message': f'小额残余清理失败: {exc}'}
        finally:
            self._release()
