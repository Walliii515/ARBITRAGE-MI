# coding: utf-8
"""Fund-transfer API facade.

Keeps calc.FundTransferService behavior and maps errors to AppError with
the existing public detail strings.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from common.errors import AppError
from common.logger import get_logger

logger = get_logger(__name__)

GetService = Callable[[], Any]
SerializeRow = Callable[[dict[str, Any]], dict[str, Any]]
SerializeRows = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]


class FundTransferApiService:
    def __init__(
        self,
        get_service: GetService,
        *,
        serialize_row: SerializeRow,
        serialize_rows: SerializeRows,
    ) -> None:
        self._get_service = get_service
        self._serialize_row = serialize_row
        self._serialize_rows = serialize_rows

    def list_tasks(self, limit: int) -> dict[str, Any]:
        try:
            service = self._get_service()
            active = service.store.get_active()
            history = service.store.list(limit=limit)
            return {
                'active': self._serialize_row(active) if active else None,
                'history': self._serialize_rows(history),
                'open_locked': service.open_locked,
            }
        except Exception as exc:
            logger.error('读取资金划转任务失败: %s', exc, exc_info=True)
            raise AppError(f'读取资金划转任务失败: {exc}', status_code=500) from exc

    def limits(self) -> dict[str, Any]:
        try:
            result = self._get_service().limits()
            result.pop('_network_info', None)
            return {'success': True, 'limits': self._serialize_row(result)}
        except ValueError as exc:
            raise AppError(str(exc), status_code=409) from exc
        except Exception as exc:
            logger.error('读取资金划转额度失败: %s', exc, exc_info=True)
            raise AppError(f'读取资金划转额度失败: {exc}', status_code=500) from exc

    def preview(self, amount: Decimal) -> dict[str, Any]:
        try:
            result = self._get_service().preview(amount)
            return {'success': True, 'preview': self._serialize_row(result)}
        except ValueError as exc:
            raise AppError(str(exc), status_code=409) from exc
        except Exception as exc:
            logger.error('资金划转预检失败: %s', exc, exc_info=True)
            raise AppError(f'资金划转预检失败: {exc}', status_code=500) from exc

    def create_task(
        self,
        *,
        amount: Decimal,
        user_id: str,
        username: str,
    ) -> dict[str, Any]:
        try:
            service = self._get_service()
            task = service.create_task(
                amount=amount,
                user_id=user_id,
                username=username,
            )
            return {'success': True, 'task': self._serialize_row(task)}
        except ValueError as exc:
            raise AppError(str(exc), status_code=409) from exc
        except Exception as exc:
            logger.error('创建资金划转任务失败: %s', exc, exc_info=True)
            raise AppError(f'创建资金划转任务失败: {exc}', status_code=500) from exc

    def retry_task(self, task_id: int) -> dict[str, Any]:
        try:
            task = self._get_service().request_retry(task_id)
            return {'success': True, 'task': self._serialize_row(task)}
        except ValueError as exc:
            raise AppError(str(exc), status_code=404) from exc
        except Exception as exc:
            logger.error('资金划转恢复失败: task=%s error=%s', task_id, exc, exc_info=True)
            raise AppError(f'资金划转恢复失败: {exc}', status_code=500) from exc
