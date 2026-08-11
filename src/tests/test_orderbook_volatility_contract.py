# coding: utf-8
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.orderbook_enricher import EnrichConfig, enrich_snapshot_fields


class TestOrderbookVolatilityContract(unittest.TestCase):
    def test_backend_snapshot_exposes_contract_amplitude(self):
        rows = [{'base_asset': 'TUT', 'contract': 'TUT_USDT'}]
        cfg = EnrichConfig(
            open_amount_usdt=160.0,
            funding_threshold_percentile='percentile_30',
            risk_relief_bps=-5.0,
            spot_open_fee=0.00075,
            spot_close_fee=0.00075,
            future_open_fee=0.0002,
            future_close_fee=0.0005,
        )
        enrich_snapshot_fields(
            rows,
            {'TUT': {'quanto_multiplier': 1, 'range_24h_pct': 55.25}},
            {},
            {},
            {},
            cfg,
            '2026-08-11 12:00:00',
        )
        self.assertEqual(rows[0]['future_range_24h_pct'], 55.25)

    def test_frontend_declares_and_displays_amplitude_column(self):
        repo_root = Path(__file__).parents[2]
        types_source = (repo_root / 'frontend/src/views/orderbookTypes.ts').read_text(encoding='utf-8')
        view_source = (repo_root / 'frontend/src/views/OrderBookMonitor.vue').read_text(encoding='utf-8')

        self.assertIn('future_range_24h_pct?: number | null', types_source)
        self.assertIn("headerName: '合约24h振幅'", view_source)
        self.assertIn("field: 'future_range_24h_pct'", view_source)

    def test_orderbook_actions_link_to_binance_spot_and_gate_futures(self):
        view_source = (
            Path(__file__).parents[2] / 'frontend/src/views/OrderBookMonitor.vue'
        ).read_text(encoding='utf-8')

        self.assertIn('renderOrderbookActions(params.data)', view_source)
        self.assertIn('https://www.binance.com/zh-CN/trade/${pair}?_from=markets&type=spot', view_source)
        self.assertIn('https://www.gate.com/zh/futures/USDT/${pair}', view_source)
        self.assertIn("link.target = '_blank'", view_source)
        self.assertIn("link.rel = 'noopener noreferrer'", view_source)


if __name__ == '__main__':
    unittest.main()
