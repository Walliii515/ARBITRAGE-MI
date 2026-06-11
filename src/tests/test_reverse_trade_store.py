from contextlib import contextmanager

from calc import reverse_trade_store as store


class FakeCursor:
    def __init__(self):
        self.calls = []
        self._fetch_queue = [
            {'total': 2},
            [
                {
                    'id': 11,
                    'base_asset': 'HOME',
                    'status': 'holding',
                    'order_uuid': 'uuid-home',
                }
            ],
        ]

    def execute(self, sql, params=None):
        self.calls.append((sql, list(params or [])))

    def fetchone(self):
        value = self._fetch_queue.pop(0)
        assert isinstance(value, dict)
        return value

    def fetchall(self):
        value = self._fetch_queue.pop(0)
        assert isinstance(value, list)
        return value


class FakeDbManager:
    def __init__(self, cursor):
        self.cursor = cursor

    @contextmanager
    def get_cursor(self):
        yield self.cursor


def test_list_reverse_positions_uses_reverse_table_and_filters(monkeypatch):
    cursor = FakeCursor()
    monkeypatch.setattr(store, '_tables_ready', True)
    monkeypatch.setattr(store, 'db_manager', FakeDbManager(cursor))

    result = store.list_reverse_positions(
        status='holding',
        base_asset='home',
        days=7,
        page=2,
        page_size=50,
    )

    assert result.total == 2
    assert result.page == 2
    assert result.page_size == 50
    assert result.total_pages == 1
    assert result.rows[0]['base_asset'] == 'HOME'

    count_sql, count_params = cursor.calls[0]
    page_sql, page_params = cursor.calls[1]
    assert 'mi_reverse_trade_position' in count_sql
    assert 'mi_trade_position' not in count_sql
    assert 'mi_reverse_trade_position' in page_sql
    assert count_params == [7, 'holding', '%HOME%']
    assert page_params == [7, 'holding', '%HOME%', 50, 50]


def test_list_reverse_orders_uses_reverse_table_and_filters(monkeypatch):
    cursor = FakeCursor()
    cursor._fetch_queue = [
        {'total': 1},
        [
            {
                'id': 21,
                'base_asset': 'HOME',
                'order_side': 'open',
                'market_type': 'margin_spot',
            }
        ],
    ]
    monkeypatch.setattr(store, '_tables_ready', True)
    monkeypatch.setattr(store, 'db_manager', FakeDbManager(cursor))

    result = store.list_reverse_orders(
        position_id=8,
        order_uuid='uuid-home',
        order_side='open',
        status='filled',
        market_type='margin_spot',
        base_asset='home',
        days=14,
        page=1,
        page_size=20,
    )

    assert result.total == 1
    assert result.rows[0]['market_type'] == 'margin_spot'

    count_sql, count_params = cursor.calls[0]
    page_sql, page_params = cursor.calls[1]
    assert 'mi_reverse_trade_order' in count_sql
    assert 'mi_trade_order' not in count_sql
    assert 'mi_reverse_trade_order' in page_sql
    assert count_params == [14, 8, 'uuid-home', 'open', 'filled', 'margin_spot', '%HOME%']
    assert page_params == [14, 8, 'uuid-home', 'open', 'filled', 'margin_spot', '%HOME%', 20, 0]


def test_ensure_reverse_trade_tables_executes_only_reverse_ddl(monkeypatch):
    cursor = FakeCursor()
    monkeypatch.setattr(store, '_tables_ready', False)
    monkeypatch.setattr(store, 'db_manager', FakeDbManager(cursor))

    store.ensure_reverse_trade_tables()
    store.ensure_reverse_trade_tables()

    ddl_sql = '\n'.join(sql for sql, _params in cursor.calls)
    assert cursor.calls and len(cursor.calls) == 2
    assert 'mi_reverse_trade_position' in ddl_sql
    assert 'mi_reverse_trade_order' in ddl_sql
    assert 'mi_trade_position' not in ddl_sql
    assert 'mi_trade_order' not in ddl_sql
