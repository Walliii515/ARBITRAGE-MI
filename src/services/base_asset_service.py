# coding: utf-8
"""Base-asset API service.

Keeps POST /base-assets/{asset}/disable JSON and status codes.
Sync on purpose; callers should wrap with asyncio.to_thread.
"""
from __future__ import annotations

from typing import Any, Optional

from common.database import DatabaseManager
from common.errors import AppError
from common.logger import get_logger
from repositories.base_asset_repo import BaseAssetRepo

logger = get_logger(__name__)


class BaseAssetService:
    def __init__(self, db_manager: DatabaseManager) -> None:
        self._repo = BaseAssetRepo(db_manager)

    def disable(self, base_asset: str, reason: Optional[str] = None) -> dict[str, Any]:
        asset = (base_asset or '').strip().upper()
        if not asset or not asset.replace('_', '').replace('-', '').isalnum():
            raise AppError('无效标的资产', status_code=400)

        holding_count, affected = self._repo.disable_asset(asset)
        if affected <= 0:
            raise AppError(f'{asset} 不存在于 mi_base_asset', status_code=404)

        logger.warning(
            '标的资产已设为失效: asset=%s holding_count=%s reason=%s',
            asset,
            holding_count,
            reason or '-',
        )
        return {
            'success': True,
            'base_asset': asset,
            'affected': affected,
            'holding_count': holding_count,
            'requires_service_reload': True,
            'message': (
                f'{asset} 已设为失效；当前仍有持仓，系统会保留必要持仓风险监控直到平仓，'
                '常规新订阅/新监控候选将在重启订单簿服务后排除。'
                if holding_count > 0
                else f'{asset} 已设为失效；重启订单簿服务后不再进入常规订阅/监控。'
            ),
        }
