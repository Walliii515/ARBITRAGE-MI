# coding: utf-8
"""VWAP threshold calculate job.

Keeps POST /threshold/calculate and GET /status JSON. The background
thread and running flag stay owned by the API module.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable

from common.logger import get_logger

logger = get_logger(__name__)

GetBool = Callable[..., bool]
GetInt = Callable[..., int]
IsRunning = Callable[[], bool]
SetRunning = Callable[[bool], None]
SetStatus = Callable[..., None]
GetStatus = Callable[[], dict[str, Any]]


class ThresholdCalcService:
    def __init__(
        self,
        *,
        get_bool: GetBool,
        get_int: GetInt,
        lock: threading.Lock,
        status: dict[str, Any],
        is_running: IsRunning,
        set_running: SetRunning,
        set_status: SetStatus,
        get_status: GetStatus,
        run_analysis: Callable[..., Any],
    ) -> None:
        self._get_bool = get_bool
        self._get_int = get_int
        self._lock = lock
        self._status = status
        self._is_running = is_running
        self._set_running = set_running
        self._set_status = set_status
        self._get_status = get_status
        self._run_analysis = run_analysis

    def status(self) -> dict[str, Any]:
        return self._get_status()

    def trigger(self) -> dict[str, Any]:
        if not self._get_bool('trade.vwap.update_threshold_enabled', True):
            return {
                'success': False,
                'message': 'VWAP基差分位阈值更新已关闭，仅保留 mi_vwap_basis_snapshot 快照',
            }

        with self._lock:
            if self._is_running():
                return {
                    'success': False,
                    'message': '计算任务正在执行中',
                    'status': dict(self._status),
                }

            lookback_days = self._get_int('trade.vwap.threshold_lookback_days', 7)
            self._set_running(True)
            self._status.update({
                'running': True,
                'processed': 0,
                'total': 0,
                'current_asset': None,
                'success_count': 0,
                'skip_count': 0,
                'fail_count': 0,
                'message': '准备中',
                'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'finished_at': None,
                'error': None,
            })

        thread = threading.Thread(
            target=self.run_job,
            args=(lookback_days,),
            name='vwap-threshold-calculate',
            daemon=True,
        )
        thread.start()
        return {
            'success': True,
            'message': f'计算已启动（回溯 {lookback_days} 天）',
            'status': self._get_status(),
        }

    def run_job(self, lookback_days: int) -> None:
        def on_progress(progress: dict[str, Any]) -> None:
            processed = int(progress.get('processed') or 0)
            total = int(progress.get('total') or 0)
            self._set_status(
                processed=processed,
                total=total,
                current_asset=progress.get('current_asset'),
                success_count=progress.get('success_count', 0),
                skip_count=progress.get('skip_count', 0),
                fail_count=progress.get('fail_count', 0),
                message=f'{processed}/{total}' if total else '准备中',
            )

        try:
            self._run_analysis(lookback_days, progress_callback=on_progress)
            status = self._get_status()
            total = int(status.get('total') or 0)
            self._set_status(
                running=False,
                processed=total,
                message=f'{total}/{total} 计算完成' if total else '计算完成',
                finished_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                error=None,
            )
        except Exception as exc:
            logger.error('手动执行阈值计算失败: %s', exc, exc_info=True)
            self._set_status(
                running=False,
                message=f'计算失败: {exc}',
                finished_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                error=str(exc),
            )
        finally:
            with self._lock:
                self._set_running(False)
