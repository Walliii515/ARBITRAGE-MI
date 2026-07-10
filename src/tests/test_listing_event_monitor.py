from unittest.mock import MagicMock, patch

from calc.listing_event_monitor import calculate_strategy_tier_from_volumes, list_listing_events


def test_calculate_strategy_tier_from_volumes():
    assert calculate_strategy_tier_from_volumes(10_000_000, 5_000_000) == 'A'
    assert calculate_strategy_tier_from_volumes(56_800_000, 4_370_000) == 'B'
    assert calculate_strategy_tier_from_volumes(1_000_000, 500_000) == 'B'
    assert calculate_strategy_tier_from_volumes(999_999, 500_000) == 'C'
    assert calculate_strategy_tier_from_volumes(1_000_000, 499_999) == 'C'


def _cursor_context(cursor):
    ctx = MagicMock()
    ctx.__enter__.return_value = cursor
    return ctx


def test_list_listing_events_filters_not_added_monitor_status_independent_of_read_status():
    cursor = MagicMock()
    cursor.fetchall.return_value = [{
        'base_asset': 'GRAM',
        'candidate_status': 'matched',
        'action_status': 'acknowledged',
        'base_asset_is_valid': None,
        'binance_quote_volume': 2_000_000,
        'gate_volume_24h_settle': 1_000_000,
    }]

    with patch('calc.listing_event_monitor.ensure_listing_event_table'), \
         patch('calc.listing_event_monitor.db_manager.get_cursor', return_value=_cursor_context(cursor)):
        rows = list_listing_events(
            action_status='all',
            candidate_status='matched',
            monitor_status='not_added',
            limit=20,
        )

    sql = cursor.execute.call_args.args[0]
    params = cursor.execute.call_args.args[1]
    assert 'e.candidate_status = %s' in sql
    assert "NOT (e.action_status = 'added_to_monitor'" in sql
    assert 'e.action_status = %s' not in sql
    assert params == ['matched', 20]
    assert rows[0]['base_asset'] == 'GRAM'
    assert rows[0]['strategy_tier'] == 'B'


def test_list_listing_events_filters_added_monitor_status_from_base_asset_state():
    cursor = MagicMock()
    cursor.fetchall.return_value = []

    with patch('calc.listing_event_monitor.ensure_listing_event_table'), \
         patch('calc.listing_event_monitor.db_manager.get_cursor', return_value=_cursor_context(cursor)):
        list_listing_events(candidate_status='matched', monitor_status='added', limit=20)

    sql = cursor.execute.call_args.args[0]
    params = cursor.execute.call_args.args[1]
    assert "(e.action_status = 'added_to_monitor' OR COALESCE(b.is_valid, '') = 'Y')" in sql
    assert params == ['matched', 20]
