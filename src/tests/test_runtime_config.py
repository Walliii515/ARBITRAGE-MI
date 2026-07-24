from pathlib import Path

from common.config import Config


def test_forward_open_reserves_eight_percent_on_binance():
    config_path = Path(__file__).parents[1] / 'config' / 'config.yaml'
    runtime_config = Config(str(config_path))

    assert runtime_config.get_float('trade.open.min_binance_available_ratio') == 0.08
