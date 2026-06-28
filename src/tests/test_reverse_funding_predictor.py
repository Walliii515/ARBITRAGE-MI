# coding: utf-8
from datetime import datetime, timedelta

import pytest

from calc import reverse_funding_predictor as predictor


def test_positive_threshold_is_normalized_to_high_negative_threshold():
    assert predictor._normalize_threshold_rate(0.01) == pytest.approx(-0.01)
    assert predictor._normalize_threshold_rate(-0.02) == pytest.approx(-0.02)


def test_compute_prediction_row_returns_three_horizons():
    base = datetime(2026, 6, 1, 0, 0, 0)
    history = [
        (base + timedelta(hours=i * 8), rate)
        for i, rate in enumerate([-0.002, -0.004, -0.012, -0.011, -0.003, -0.015])
    ]

    row = predictor._compute_prediction_row(
        'ABC_USDT',
        history,
        {'base_asset': 'ABC', 'funding_rate_24h': -0.006, 'funding_next_apply': base},
        -0.01,
    )

    assert row is not None
    assert row['base_asset'] == 'ABC'
    assert row['model_version'] == predictor.MODEL_VERSION
    assert row['high_negative_count'] == 3
    assert row['p_next_1'] is not None
    assert row['p_next_2'] is not None
    assert row['p_next_3'] is not None


def test_attach_follow_metrics_scores_objective_follow_signal():
    base = datetime(2026, 6, 1, 0, 0, 0)
    funding_history = [
        (base + timedelta(hours=i), rate)
        for i, rate in enumerate([-0.001, -0.002, -0.003, -0.004, -0.007])
    ]
    borrow_history = [
        {'snapshot_time': base, 'borrow_capacity_usdt': 1000},
        {'snapshot_time': base + timedelta(hours=4), 'borrow_capacity_usdt': 300},
    ]
    row = {
        'base_asset': 'ABC',
        'current_funding_rate_24h': -0.007,
        'borrowable': 1,
        'borrow_capacity_usdt': 300,
        'borrow_24h_bps': 6,
        'high_negative_frequency': 0.2,
        'high_negative_count': 3,
    }

    predictor._attach_follow_metrics(row, funding_history, borrow_history)

    assert row['funding_change_4h_bps'] == pytest.approx(-60)
    assert row['borrow_capacity_drop_4h_pct'] == pytest.approx(70)
    assert row['borrow_capacity_drop_4h_usdt'] == pytest.approx(700)
    assert row['borrow_capacity_drawdown_24h_pct'] == pytest.approx(70)
    assert row['borrow_pressure_score'] > 50
    assert row['follow_score'] > 50
    assert '资金费下行' in row['follow_reason']
    assert '额度压力' in row['follow_reason']


def test_predict_high_negative_funding_filters_asset(monkeypatch):
    rows = [
        {'base_asset': 'ABC', 'contract': 'ABC_USDT', 'p_next_3': 0.3},
        {'base_asset': 'DEF', 'contract': 'DEF_USDT', 'p_next_3': 0.2},
    ]

    def fake_rows(**kwargs):
        assert kwargs['threshold_rate'] == 0.01
        assert kwargs['lookback_days'] == 30
        return rows

    monkeypatch.setattr(predictor, '_compute_prediction_rows', fake_rows)

    result = predictor.predict_high_negative_funding(
        base_asset='ABC',
        threshold_rate=0.01,
        lookback_days=30,
    )

    assert result == [rows[0]]


def test_compute_prediction_rows_uses_orderbook_universe(monkeypatch):
    base = datetime(2026, 6, 1, 0, 0, 0)
    history_rows = [
        {'contract': 'ABC_USDT', 'funding_rate_24h': -0.012, 'record_time': base},
        {'contract': 'ABC_USDT', 'funding_rate_24h': -0.008, 'record_time': base + timedelta(hours=8)},
        {'contract': 'XYZ_USDT', 'funding_rate_24h': -0.020, 'record_time': base},
        {'contract': 'XYZ_USDT', 'funding_rate_24h': -0.015, 'record_time': base + timedelta(hours=8)},
    ]
    monkeypatch.setattr(predictor, '_load_current_contracts', lambda: {})
    monkeypatch.setattr(predictor, '_load_funding_history', lambda lookback_days: history_rows)
    monkeypatch.setattr(predictor, '_load_prediction_universe', lambda: {'ABC': 'B'})
    monkeypatch.setattr(predictor, '_load_latest_borrow_meta', lambda: {})
    monkeypatch.setattr(predictor, '_load_borrow_history', lambda hours=24: {})

    rows = predictor._compute_prediction_rows(threshold_rate=-0.01, lookback_days=30)

    assert [row['base_asset'] for row in rows] == ['ABC']
    assert rows[0]['strategy_tier'] == 'B'


def test_apply_prediction_filters_tracks_step_counts():
    rows = [
        {
            'base_asset': 'AAA',
            'follow_score': 80,
            'funding_change_1h_bps': -8,
            'funding_change_4h_bps': -12,
            'funding_change_12h_bps': -20,
            'borrow_capacity_drop_max_pct': 35,
            'borrow_pressure_score': 25,
            'borrow_capacity_drawdown_24h_pct': 3,
            'borrow_capacity_drop_4h_usdt': 8,
            'borrow_capacity_change_1h_usdt': -2,
            'high_negative_count': 2,
            'current_funding_rate_24h': -0.002,
            'borrowable': 1,
            'borrow_capacity_usdt': 120,
            'borrow_24h_bps': 5,
            'expected_funding_bps': 10,
        },
        {
            'base_asset': 'BBB',
            'follow_score': 30,
            'funding_change_1h_bps': -1,
            'funding_change_4h_bps': -2,
            'funding_change_12h_bps': -3,
            'borrow_capacity_drop_max_pct': 5,
            'borrow_pressure_score': 3,
            'borrow_capacity_drawdown_24h_pct': 0.5,
            'borrow_capacity_drop_4h_usdt': 1,
            'borrow_capacity_change_1h_usdt': 1,
            'high_negative_count': 0,
            'current_funding_rate_24h': -0.003,
            'borrowable': 1,
            'borrow_capacity_usdt': 120,
            'borrow_24h_bps': 5,
            'expected_funding_bps': 10,
        },
    ]

    filtered, steps, opts = predictor._apply_prediction_filters(
        rows,
        {
            'follow_score_enabled': True,
            'funding_down_enabled': True,
            'borrow_drop_enabled': True,
            'history_high_negative_enabled': True,
            'borrowable_enabled': True,
            'borrow_cost_enabled': True,
            'min_borrow_capacity_usdt': 100,
            'min_borrow_pressure_score': 12,
            'min_capacity_drawdown_pct': 2,
            'min_capacity_drop_usdt': 5,
        },
    )

    assert [row['base_asset'] for row in filtered] == ['AAA']
    assert [step['count'] for step in steps] == [2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
    assert opts['min_follow_score'] == predictor.DEFAULT_MIN_FOLLOW_SCORE
    assert filtered[0]['preborrow_filter_pass'] is True
