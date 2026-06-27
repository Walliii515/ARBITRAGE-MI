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

    rows = predictor._compute_prediction_rows(threshold_rate=-0.01, lookback_days=30)

    assert [row['base_asset'] for row in rows] == ['ABC']
    assert rows[0]['strategy_tier'] == 'B'


def test_apply_prediction_filters_tracks_step_counts():
    rows = [
        {
            'base_asset': 'AAA',
            'p_next_2': 0.30,
            'p_next_3': 0.40,
            'confidence': 0.70,
            'current_funding_rate_24h': -0.002,
            'borrowable': 1,
            'borrow_capacity_usdt': 120,
            'borrow_24h_bps': 5,
            'expected_funding_bps': 10,
        },
        {
            'base_asset': 'BBB',
            'p_next_2': 0.10,
            'p_next_3': 0.12,
            'confidence': 0.80,
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
            'probability_enabled': True,
            'confidence_enabled': True,
            'negative_funding_enabled': True,
            'borrowable_enabled': True,
            'capacity_enabled': True,
            'borrow_cost_enabled': True,
            'min_borrow_capacity_usdt': 100,
        },
    )

    assert [row['base_asset'] for row in filtered] == ['AAA']
    assert [step['count'] for step in steps] == [2, 1, 1, 1, 1, 1, 1]
    assert opts['min_p_next_2'] == predictor.DEFAULT_MIN_P_NEXT_2
    assert filtered[0]['preborrow_filter_pass'] is True
