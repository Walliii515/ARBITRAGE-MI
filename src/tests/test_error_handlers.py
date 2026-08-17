import asyncio
import json
from unittest import TestCase
from unittest.mock import patch

from starlette.requests import Request

from api.error_handlers import handle_app_error
from common.config import config
from common.errors import AppError, ValidationAppError
from common.logger import _SafeTimedRotatingFileHandler


def _request() -> Request:
    return Request(
        {
            'type': 'http',
            'asgi': {'version': '3.0'},
            'http_version': '1.1',
            'method': 'GET',
            'scheme': 'http',
            'path': '/',
            'raw_path': b'/',
            'query_string': b'',
            'headers': [],
            'client': ('test', 123),
            'server': ('test', 80),
        }
    )


class AppErrorHandlerTests(TestCase):
    def test_handler_returns_fastapi_detail_json(self) -> None:
        response = asyncio.run(handle_app_error(_request(), AppError('服务未初始化', status_code=503)))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body.decode('utf-8')), {'detail': '服务未初始化'})
        self.assertIn('application/json', response.headers['content-type'])

    def test_validation_error_is_http_400_with_stable_detail(self) -> None:
        response = asyncio.run(
            handle_app_error(_request(), ValidationAppError('view 必须为 open 或 close'))
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body.decode('utf-8')), {'detail': 'view 必须为 open 或 close'})


class InfraCompatTests(TestCase):
    def test_orderbook_server_port_default_remains_19876(self) -> None:
        self.assertEqual(config.get_int('orderbook.server_port', 19876), 19876)

    def test_log_rollover_lock_does_not_raise(self) -> None:
        handler = _SafeTimedRotatingFileHandler.__new__(_SafeTimedRotatingFileHandler)
        with patch('logging.handlers.TimedRotatingFileHandler.rotate', side_effect=OSError('locked')):
            handler.rotate('app.log', 'app.log.2026-08-16')
