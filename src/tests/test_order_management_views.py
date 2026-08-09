import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from api.trading_api import get_orders


def _query_orders(
    view: str,
    *,
    channel=None,
    exchange_risk=False,
    position_id=None,
    base_asset=None,
    days=7,
    page=1,
    page_size=50,
):
    cursor = MagicMock()
    cursor.fetchone.return_value = {'total': 1}
    cursor.fetchall.return_value = [{
        'id': 7,
        'base_asset': 'AI',
        'status': 'holding' if view == 'open' else 'closed',
    }]
    context = MagicMock()
    context.__enter__.return_value = cursor
    context.__exit__.return_value = False

    with patch('api.trading_api.db_manager.get_cursor', return_value=context), \
            patch('api.trading_api._attach_delist_risks'):
        result = asyncio.run(get_orders(
            view=view,
            channel=channel,
            exchange_risk=exchange_risk,
            position_id=position_id,
            base_asset=base_asset,
            days=days,
            page=page,
            page_size=page_size,
        ))
    return result, cursor.execute.call_args_list


def test_open_view_filters_holding_by_open_time_and_sorts_in_backend():
    result, calls = _query_orders('open')

    assert result['view'] == 'open'
    assert result['orders'][0]['status'] == 'holding'
    count_sql, count_params = calls[0].args
    data_sql, data_params = calls[1].args
    assert 'p.status = %s' in count_sql
    assert 'p.opened_at >= DATE_SUB' in count_sql
    assert 'p.closed_at >= DATE_SUB' not in count_sql
    assert 'ORDER BY p.opened_at DESC, p.id DESC' in data_sql
    assert count_params == ['holding', 7]
    assert data_params == ['holding', 7, 50, 0]


def test_close_view_filters_closed_by_close_time_and_sorts_in_backend():
    result, calls = _query_orders('close')

    assert result['view'] == 'close'
    assert result['orders'][0]['status'] == 'closed'
    count_sql, count_params = calls[0].args
    data_sql, data_params = calls[1].args
    assert 'p.status = %s' in count_sql
    assert 'p.closed_at >= DATE_SUB' in count_sql
    assert 'p.opened_at >= DATE_SUB' not in count_sql
    assert 'ORDER BY p.closed_at DESC, p.id DESC' in data_sql
    assert count_params == ['closed', 7]
    assert data_params == ['closed', 7, 50, 0]


def test_order_view_keeps_all_filters_and_pagination_on_server_side():
    result, calls = _query_orders(
        'open',
        channel='Live',
        position_id=99,
        base_asset='AI',
        days=30,
        page=2,
        page_size=100,
    )

    assert result['pagination']['page'] == 2
    count_sql, count_params = calls[0].args
    data_sql, data_params = calls[1].args
    assert 'p.base_asset = %s' in count_sql
    assert 'p.id = %s' in count_sql
    assert 'o.channel = %s' in count_sql
    assert count_params == ['holding', 30, 'AI', 99, 'Live']
    assert data_params == ['holding', 30, 'AI', 99, 'Live', 100, 100]
    assert 'LIMIT %s OFFSET %s' in data_sql


def test_order_views_are_read_only_and_do_not_touch_execution_state():
    _, open_calls = _query_orders('open')
    _, close_calls = _query_orders('close')

    for call in [*open_calls, *close_calls]:
        sql = call.args[0].strip().upper()
        assert sql.startswith('SELECT')
        assert not any(word in sql for word in ('UPDATE ', 'INSERT ', 'DELETE '))


def test_order_view_rejects_unknown_scope_before_querying_database():
    with patch('api.trading_api.db_manager.get_cursor') as get_cursor:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(get_orders(
                view='all',
                channel=None,
                exchange_risk=False,
                position_id=None,
                base_asset=None,
                days=7,
                page=1,
                page_size=50,
            ))

    assert raised.value.status_code == 400
    get_cursor.assert_not_called()


def test_order_management_uses_two_grids_and_independent_column_configs():
    source = (
        Path(__file__).parents[2] / 'frontend' / 'src' / 'views' / 'OrderManagement.vue'
    ).read_text(encoding='utf-8')

    assert source.count('<ag-grid-vue') == 2
    assert "open: 'order_management_open'" in source
    assert "close: 'order_management_close'" in source
    assert "params.set('view', view)" in source
    assert "field: 'open_funding_rate_24h'" in source
    assert "field: 'close_funding_rate_24h'" in source
    assert 'paginationByView.open.currentPage = 1' in source
    assert 'paginationByView.close.currentPage = 1' in source
    assert 'autoRefreshTimer = setInterval(fetchOrders, 2000)' in source


def test_order_management_uses_compact_single_row_toolbar_and_tab_actions():
    source = (
        Path(__file__).parents[2] / 'frontend' / 'src' / 'views' / 'OrderManagement.vue'
    ).read_text(encoding='utf-8')

    assert source.count('class="filter-row"') == 1
    assert '<span>订单管理</span>' not in source
    assert '<template #header>' not in source
    assert 'class="tab-actions"' in source
    assert '.grid-card :deep(.el-card__body)' in source
    assert 'height: calc(100vh - 156px)' in source


def test_funding_snapshot_migration_backfills_both_sides_and_adds_query_indexes():
    migration = (
        Path(__file__).parents[1] / 'migrations' / '035_add_forward_position_funding_snapshots.sql'
    ).read_text(encoding='utf-8')

    assert 'open_funding_rate_24h' in migration
    assert 'close_funding_rate_24h' in migration
    assert "o.order_side = 'open'" in migration
    assert "o.order_side = 'close'" in migration
    assert 'INFORMATION_SCHEMA.COLUMNS' in migration
    assert 'COALESCE(o.executed_at, o.created_at)' in migration
    assert 'idx_trade_position_status_opened' in migration
    assert 'idx_trade_position_status_closed' in migration
