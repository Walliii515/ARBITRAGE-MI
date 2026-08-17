import asyncio
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from api.trading_api import (
    get_reverse_orders,
    get_reverse_position_orders,
    get_reverse_positions,
    get_reverse_signals,
)
from calc import reverse_trade_store as store


def _query_reverse_signals(
    *,
    status=None,
    base_asset=None,
    days=3,
    page=1,
    page_size=100,
):
    cursor = MagicMock()
    cursor.fetchone.side_effect = [
        {'total': 4},
        {
            'total': 4,
            'monitoring': 1,
            'opened': 2,
            'conditions_lost': 0,
            'rejected': 1,
            'monitor_timeout': 0,
            'latest_signal_time': '2026-08-17 10:00:00',
        },
    ]
    cursor.fetchall.return_value = [{
        'id': 9,
        'base_asset': 'AI',
        'status': 'opened',
        'funding_rate_2h': 0.01,
        'funding_rate_24h': 0.02,
    }]
    context = MagicMock()
    context.__enter__.return_value = cursor
    context.__exit__.return_value = False

    with patch('api.trading_api.db_manager.get_cursor', return_value=context):
        result = asyncio.run(get_reverse_signals(
            status=status,
            base_asset=base_asset,
            days=days,
            page=page,
            page_size=page_size,
        ))
    return result, cursor.execute.call_args_list


class _FakeCursor:
    def __init__(self, fetchone_values, fetchall_values):
        self.calls = []
        self._fetchone_values = list(fetchone_values)
        self._fetchall_values = list(fetchall_values)

    def execute(self, sql, params=None):
        self.calls.append((sql, list(params or [])))

    def fetchone(self):
        return self._fetchone_values.pop(0)

    def fetchall(self):
        return self._fetchall_values.pop(0)


class _FakeDbManager:
    def __init__(self, cursor):
        self.cursor = cursor

    @contextmanager
    def get_cursor(self):
        yield self.cursor


def test_reverse_signals_filter_by_days_status_and_base_asset():
    result, calls = _query_reverse_signals(status='opened', base_asset='AI', days=7, page=2, page_size=50)

    assert result['pagination'] == {
        'page': 2,
        'page_size': 50,
        'total': 4,
        'total_pages': 1,
    }
    assert result['summary']['opened'] == 2
    assert result['summary']['conversion_rate'] == 50.0
    assert result['signals'][0]['funding_rate_24h'] == 0.02
    assert 'funding_rate_2h' not in result['signals'][0]

    count_sql, count_params = calls[0].args
    data_sql, data_params = calls[1].args
    summary_sql, summary_params = calls[2].args
    assert 'mi_reverse_trade_signal' in count_sql
    assert 'signal_basis_bps IS NOT NULL' in count_sql
    assert 'status = %s' in count_sql
    assert 'base_asset LIKE %s' in count_sql
    assert count_params == [7, 'opened', '%AI%']
    assert 's.status = %s' in data_sql
    assert 'LEFT JOIN mi_base_asset' in data_sql
    assert 'ORDER BY s.signal_time DESC' in data_sql
    assert data_params == [7, 'opened', '%AI%', 50, 50]
    assert 'monitor_timeout' in summary_sql
    assert summary_params == [7, 'opened', '%AI%']
    assert len(calls) == 3
    for call in calls:
        sql = call.args[0].strip().upper()
        assert sql.startswith('SELECT')
        assert 'UPDATE ' not in sql
        assert 'INSERT ' not in sql
        assert 'DELETE ' not in sql


def test_reverse_positions_query_uses_store_and_serializes_rows(monkeypatch):
    cursor = _FakeCursor(
        fetchone_values=[
            {'total': 3, 'open_count': 2, 'close_count': 1, 'exchange_risk_count': 0},
            {'total': 1},
        ],
        fetchall_values=[[{
            'id': 11,
            'base_asset': 'HOME',
            'status': 'holding',
            'open_funding_rate_24h': None,
            'signal_open_funding_rate_24h': 0.03,
            'open_borrow_24h_bps': 1.2,
            'signal_open_borrow_24h_bps': 9.9,
            'reverse_open_basis_p20': None,
            'signal_reverse_open_basis_p20': 12.0,
            'reverse_close_basis_p20': 8.0,
            'signal_reverse_close_basis_p20': 7.0,
        }]],
    )
    monkeypatch.setattr(store, '_tables_ready', True)
    monkeypatch.setattr(store, 'db_manager', _FakeDbManager(cursor))

    result = asyncio.run(get_reverse_positions(
        status='holding',
        order_side=None,
        exchange_risk=False,
        base_asset='home',
        days=7,
        page=2,
        page_size=50,
    ))

    assert result['summary'] == {'total': 3, 'open': 2, 'close': 1, 'exchange_risk': 0}
    assert result['pagination']['total'] == 1
    assert result['pagination']['page'] == 2
    row = result['positions'][0]
    assert row['open_funding_rate_24h'] == 0.03
    assert row['open_borrow_24h_bps'] == 1.2
    assert row['reverse_open_basis_p20'] == 12.0
    assert row['reverse_close_basis_p20'] == 8.0
    assert 'signal_open_funding_rate_24h' not in row
    assert 'mi_reverse_trade_position' in cursor.calls[0][0]
    assert 'mi_trade_position' not in cursor.calls[0][0]


def test_reverse_position_orders_are_serialized(monkeypatch):
    cursor = _FakeCursor(
        fetchone_values=[],
        fetchall_values=[[{'id': 21, 'position_id': 11, 'order_side': 'open'}]],
    )
    monkeypatch.setattr(store, '_tables_ready', True)
    monkeypatch.setattr(store, 'db_manager', _FakeDbManager(cursor))

    result = asyncio.run(get_reverse_position_orders(11))

    assert result == {'orders': [{'id': 21, 'position_id': 11, 'order_side': 'open'}]}
    sql, params = cursor.calls[0]
    assert 'mi_reverse_trade_order' in sql
    assert params == [11]


def test_reverse_orders_filter_and_paginate(monkeypatch):
    cursor = _FakeCursor(
        fetchone_values=[{'total': 1}],
        fetchall_values=[[{
            'id': 21,
            'base_asset': 'HOME',
            'order_side': 'open',
            'market_type': 'margin_spot',
        }]],
    )
    monkeypatch.setattr(store, '_tables_ready', True)
    monkeypatch.setattr(store, 'db_manager', _FakeDbManager(cursor))

    result = asyncio.run(get_reverse_orders(
        position_id=8,
        order_uuid='uuid-home',
        order_side='open',
        status='filled',
        market_type='margin_spot',
        base_asset='home',
        days=14,
        page=1,
        page_size=20,
    ))

    assert result['pagination'] == {
        'page': 1,
        'page_size': 20,
        'total': 1,
        'total_pages': 1,
    }
    assert result['orders'][0]['market_type'] == 'margin_spot'
    count_sql, count_params = cursor.calls[0]
    page_sql, page_params = cursor.calls[1]
    assert 'mi_reverse_trade_order' in count_sql
    assert 'mi_trade_order' not in count_sql
    assert count_params == [14, 8, 'uuid-home', 'open', 'filled', 'margin_spot', '%HOME%']
    assert page_params == [14, 8, 'uuid-home', 'open', 'filled', 'margin_spot', '%HOME%', 20, 0]
