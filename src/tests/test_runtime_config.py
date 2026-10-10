from pathlib import Path

from common.config import Config


def test_forward_open_reserves_fifteen_percent_on_binance():
    config_path = Path(__file__).parents[1] / 'config' / 'config.yaml'
    runtime_config = Config(str(config_path))

    assert runtime_config.get_float('trade.open.min_binance_available_ratio') == 0.15


def test_forward_open_reserves_fifteen_percent_on_gate():
    config_path = Path(__file__).parents[1] / 'config' / 'config.yaml'
    runtime_config = Config(str(config_path))

    assert runtime_config.get_float('trade.open.min_gate_available_ratio') == 0.15


def test_forward_entry_buffer_and_freshness_preserve_other_risk_limits():
    config_path = Path(__file__).parents[1] / 'config' / 'config.yaml'
    runtime_config = Config(str(config_path))

    assert runtime_config.get_float('trade.funding_adjusted_entry.slippage_buffer_bps') == 5
    assert runtime_config.get_float('trade.open.max_orderbook_lag_ms') == 800
    assert runtime_config.get_float('trade.market_profile.thin_bursty.max_orderbook_lag_ms') == 1500
    assert runtime_config.get_float('trade.close.max_orderbook_lag_ms') == 500
    assert runtime_config.get_float('trade.execution.future_maker_open.fallback_slippage_bps') == 5
