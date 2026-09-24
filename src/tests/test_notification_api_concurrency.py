import asyncio
import inspect
import json
import threading
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI

from api import trading_api as api


CASES = [
    ('GET', '/risk-notifications/recent', None, '_build_recent_risk_notification_items', []),
    ('GET', '/notifications', None, '_sync_recent_popup_notifications', {}),
    ('GET', '/notifications?sync_recent=false', None, 'list_popup_notifications', {'items': [], 'pagination': {}, 'unread_count': 0}),
    ('GET', '/notifications/unread-count', None, 'count_unread_popup_notifications', 0),
    ('POST', '/notifications', {'title': 'test', 'message': 'test'}, 'upsert_popup_notification', {}),
    ('POST', '/notifications/mark-read', {'ids': [1]}, 'mark_popup_notifications_read', 1),
    ('POST', '/notifications/1/read', None, 'mark_popup_notifications_read', 1),
]


async def request(app, method, url, payload=None):
    path, _, query = url.partition('?')
    messages = []

    async def receive():
        return {'type': 'http.request', 'body': json.dumps(payload).encode() if payload is not None else b'', 'more_body': False}

    async def send(message):
        messages.append(message)

    await app({
        'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
        'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
        'query_string': query.encode(), 'headers': [(b'content-type', b'application/json')],
        'server': ('test', 80), 'client': ('test', 1234), 'root_path': '',
    }, receive, send)
    status = next(m['status'] for m in messages if m['type'] == 'http.response.start')
    body = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
    return status, json.loads(body)


def make_app(monkeypatch):
    monkeypatch.setattr(api, '_sync_recent_popup_notifications', MagicMock(return_value={}))
    monkeypatch.setattr(api, 'list_popup_notifications', MagicMock(return_value={
        'items': [], 'pagination': {}, 'unread_count': 0,
    }))
    monkeypatch.setattr(api, 'count_unread_popup_notifications', MagicMock(return_value=0))
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.verify_token_dependency] = lambda: 'test-user'

    @app.get('/probe')
    async def probe():
        return {'ok': True}

    return app


@pytest.mark.parametrize('method,path,payload,helper,value', CASES)
def test_slow_notification_database_does_not_block_event_loop(monkeypatch, method, path, payload, helper, value):
    app = make_app(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    worker_ids = []

    def slow_db(*args, **kwargs):
        worker_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(3), 'notification handler blocked the event loop'
        return value

    monkeypatch.setattr(api, helper, slow_db)

    async def scenario():
        loop_id = threading.get_ident()
        task = asyncio.create_task(request(app, method, '/api/trading' + path, payload))
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            assert worker_ids != [loop_id]
            assert not task.done()
            status, body = await asyncio.wait_for(request(app, 'GET', '/probe'), timeout=0.5)
            assert status == 200 and body == {'ok': True}
        finally:
            release.set()
            status, _ = await task
        assert status == 200

    asyncio.run(scenario())


def test_risk_response_contract_and_parameters(monkeypatch):
    items = [{'source': 'exchange_risk'}, {'source': 'reconciliation'}]
    helper = MagicMock(return_value=items)
    monkeypatch.setattr(api, '_build_recent_risk_notification_items', helper)
    assert api.get_recent_risk_notifications(hours=48, limit=20) == {
        'items': items, 'summary': {'total': 2, 'exchange_risk': 1, 'reconciliation': 1},
        'lookback_hours': 48,
    }
    helper.assert_called_once_with(hours=48, limit=20)


def test_skip_sync_does_not_query_history(monkeypatch):
    make_app(monkeypatch)
    api.get_popup_notifications(read_status='all', source=None, page=2, page_size=10, sync_recent=False)
    api._sync_recent_popup_notifications.assert_not_called()
    api.list_popup_notifications.assert_called_once_with(read_status='all', source=None, page=2, page_size=10)


@pytest.mark.parametrize('path', ['/risk-notifications/recent?hours=0', '/notifications?page=0'])
def test_invalid_request_still_rejected_before_database(monkeypatch, path):
    app = make_app(monkeypatch)
    helper = MagicMock()
    monkeypatch.setattr(api, '_build_recent_risk_notification_items', helper)

    async def scenario():
        assert (await request(app, 'GET', '/api/trading' + path))[0] == 422

    asyncio.run(scenario())
    helper.assert_not_called()
    api._sync_recent_popup_notifications.assert_not_called()


def test_notification_database_error_not_reported_as_success(monkeypatch):
    app = make_app(monkeypatch)
    monkeypatch.setattr(api, 'count_unread_popup_notifications', MagicMock(side_effect=RuntimeError('database unavailable')))

    async def scenario():
        with pytest.raises(RuntimeError, match='database unavailable'):
            await request(app, 'GET', '/api/trading/notifications/unread-count')
        assert (await request(app, 'GET', '/probe'))[0] == 200

    asyncio.run(scenario())


def test_notification_writes_keep_authentication_dependencies():
    writes = [route for route in api.router.routes if 'POST' in route.methods and '/notifications' in route.path]
    assert len(writes) == 3
    for route in writes:
        assert not inspect.iscoroutinefunction(route.endpoint)
        assert any(dep.call is api.verify_token_dependency for dep in route.dependant.dependencies)


def test_previous_snapshot_query_uses_asset_history_index(monkeypatch):
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = {'latest_snapshot_at': None}
    context = MagicMock()
    context.__enter__.return_value = cursor
    monkeypatch.setattr(api.db_manager, 'get_cursor', lambda: context)
    assert api._build_recent_risk_notification_items(hours=24, limit=50) == []
    sql, params = cursor.execute.call_args.args
    assert 'FROM mi_recon_snapshot prev FORCE INDEX (idx_recon_history)' in sql
    assert 'prev.snapshot_at < r.snapshot_at' in sql
    assert params[-1] == 250
