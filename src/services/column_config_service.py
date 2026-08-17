# coding: utf-8
"""AG Grid column-config service.

Assembles GET/POST /column-config/{page_key} JSON. user_id stays 'default'.
Save failures stay HTTP 200 with success=false. Sync on purpose;
callers should wrap with asyncio.to_thread.
"""
from __future__ import annotations

import json
from typing import Any

from common.database import DatabaseManager
from common.logger import get_logger
from repositories.column_config_repo import ColumnConfigRepo

logger = get_logger(__name__)

DEFAULT_COLUMN_CONFIG_USER_ID = 'default'


class ColumnConfigService:
    def __init__(self, db_manager: DatabaseManager) -> None:
        self._repo = ColumnConfigRepo(db_manager)

    def get(self, page_key: str) -> dict[str, Any]:
        rows = self._repo.list_column_state(DEFAULT_COLUMN_CONFIG_USER_ID, page_key)
        column_state = []
        for row in rows:
            state: dict[str, Any] = {
                'colId': row['col_id'],
                'hide': not bool(row['is_visible']),
            }
            if row['width'] is not None:
                state['width'] = row['width']
            if row['pinned']:
                state['pinned'] = row['pinned']
            if row['sort']:
                state['sort'] = row['sort']
            if row['filter_model']:
                state['filterModel'] = (
                    json.loads(row['filter_model'])
                    if isinstance(row['filter_model'], str)
                    else row['filter_model']
                )
            column_state.append(state)
        return {'columnState': column_state}

    def save(self, page_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        column_state = payload.get('columnState', [])
        if not column_state or not isinstance(column_state, list):
            return {'success': False, 'message': 'columnState 必须是非空数组'}

        rows: list[tuple[Any, ...]] = []
        for idx, item in enumerate(column_state):
            if 'colId' not in item:
                continue
            filter_model_json = None
            if item.get('filterModel'):
                filter_model_json = json.dumps(item['filterModel'])
            rows.append((
                DEFAULT_COLUMN_CONFIG_USER_ID,
                page_key,
                item['colId'],
                idx,
                not item.get('hide', False),
                item.get('width'),
                item.get('pinned'),
                item.get('sort'),
                filter_model_json,
            ))
        error = self._repo.upsert_column_states(rows)
        if error is not None:
            logger.error('保存列配置失败: %s', error, exc_info=error)
            return {'success': False, 'message': f'保存失败: {str(error)}'}
        return {'success': True, 'message': '列配置保存成功'}
