import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from calc.position_tracker import PositionTracker
from calc.orderbook_resiliency import ResiliencyResult
from tests.test_open_close_logic import make_closing_executor


def test_close_resiliency_isolates_hold_and_clear_by_position():
    ce = make_closing_executor()
    row = {'spot_close_coverage': 0.2, 'future_close_coverage': 0.2}
    first = {'id': 1, 'open_spread_bps': 100}
    second = {'id': 2, 'open_spread_bps': 10}
    for _ in range(3):
        assert not ce._pass_close_resiliency_check('AI', row, 20, first)
    state = ce._close_resiliency._state[1]
    state['passed_since'] = datetime.now() - timedelta(seconds=1)
    assert not ce._pass_close_resiliency_check('AI', row, 20, second)
    assert ce._close_resiliency._state[1] is state
    assert ce._pass_close_resiliency_check('AI', row, 20, first)
    ce._clear_position_close_state('AI', second)
    assert ce._close_resiliency._state[1] is state


def test_repeated_terminal_cycles_are_throttled_without_changing_decisions():
    ce = make_closing_executor()
    pos = {'id': 1, 'open_spread_bps': 10}
    result = ResiliencyResult(False, False, True, 'basis_above_max(20>=10)', {})
    with patch.object(ce._close_resiliency, 'check', return_value=result), \
            patch('calc.closing_executor.logger.info') as log, \
            patch('calc.closing_executor.time.monotonic', return_value=100):
        for _ in range(50):
            assert not ce._pass_valley_check('AI', 20, pos)
            assert ce._pass_valley_check('AI', 20, pos)
            assert not ce._pass_close_resiliency_check('AI', {}, 20, pos)
        assert log.call_count == 3
    with patch('calc.closing_executor.logger.info') as log, \
            patch('calc.closing_executor.time.monotonic', return_value=161):
        ce._pass_valley_check('AI', 20, pos)
        assert log.call_count == 1


def funding_row():
    return {'position_id': 1, 'base_asset': 'AI', 'payment_seq': 1,
            'funding_rate': 0.001, 'funding_rate_24h': 0.003,
            'funding_pnl': 2, 'future_notional': 100,
            'settled_at': datetime(2026, 9, 14, 8)}


def test_funding_cache_reused_across_trackers_and_results_are_detached():
    cache = {}
    positions = [{'id': 1, 'base_asset': 'AI', 'funding_payments_count': 1}]
    with patch('calc.position_tracker.db_manager.get_cursor') as db:
        cursor = db.return_value.__enter__.return_value
        cursor.fetchall.return_value = [funding_row()]
        PositionTracker(funding_history_cache=cache).attach_funding_histories(positions)
        positions[0]['funding_history'][0]['pnl'] = 999
        PositionTracker(funding_history_cache=cache).attach_funding_histories(positions)
        assert cursor.execute.call_count == 1
        assert positions[0]['funding_history'][0]['pnl'] == 2
        positions[0]['funding_payments_count'] = 2
        PositionTracker(funding_history_cache=cache).attach_funding_histories(positions)
        assert cursor.execute.call_count == 2
        cache['at'] -= 6
        PositionTracker(funding_history_cache=cache).attach_funding_histories(positions)
        assert cursor.execute.call_count == 3
        positions.append({'id': 2, 'base_asset': 'FF'})
        PositionTracker(funding_history_cache=cache).attach_funding_histories(positions)
        assert cursor.execute.call_count == 4


def test_expired_funding_cache_does_not_hide_database_failure():
    cache = {}
    pos = [{'id': 1, 'base_asset': 'AI'}]
    tracker = PositionTracker(funding_history_cache=cache)
    with patch('calc.position_tracker.db_manager.get_cursor') as db:
        db.return_value.__enter__.return_value.fetchall.return_value = [funding_row()]
        tracker.attach_funding_histories(pos)
    cache['at'] -= 6
    with patch('calc.position_tracker.db_manager.get_cursor', side_effect=RuntimeError('offline')):
        with pytest.raises(RuntimeError, match='offline'):
            tracker.attach_funding_histories(pos)


def test_stdout_logging_has_no_file_and_rotating_files_are_bounded(tmp_path, monkeypatch):
    import logging
    from common import logger as module
    monkeypatch.setenv('LOG_OUTPUT', 'file')
    monkeypatch.setenv('LOG_MAX_BYTES', '512')
    monkeypatch.setenv('LOG_BACKUP_COUNT', '2')
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    for handler in original_handlers:
        root.removeHandler(handler)
    original_initialized = module._INITIALIZED
    original_level = root.level
    try:
        module.setup_logging(log_dir=str(tmp_path), filename='test.log', force=True)
        for _ in range(100):
            root.info('x' * 100)
        files = list(tmp_path.iterdir())
        assert len(files) == 3
        assert all(path.stat().st_size <= 512 for path in files)
        file_handler = next(h for h in root.handlers if getattr(h, '_arb_file', False))
        monkeypatch.setenv('LOG_OUTPUT', 'stdout')
        module.setup_logging(log_dir=str(tmp_path / 'unused'), force=True)
        assert file_handler.stream is None
        assert not (tmp_path / 'unused').exists()
        assert not any(getattr(h, '_arb_file', False) for h in root.handlers)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            root.addHandler(handler)
        root.setLevel(original_level)
        module._INITIALIZED = original_initialized
