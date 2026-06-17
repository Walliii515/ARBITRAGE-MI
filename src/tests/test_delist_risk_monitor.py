# coding: utf-8
from datetime import datetime, timedelta
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.delist_risk_monitor import DelistRiskConfig, DelistRiskMonitor


class TestDelistRiskMonitor(unittest.TestCase):
    def test_gate_in_delisting_contract_is_reported(self):
        monitor = DelistRiskMonitor(DelistRiskConfig(lookahead_days=30))
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = [
            {'name': 'BANK_USDT', 'status': 'trading', 'in_delisting': True},
            {'name': 'TUT_USDT', 'status': 'trading', 'in_delisting': False},
        ]

        with patch('calc.delist_risk_monitor.requests.get', return_value=resp):
            risks = monitor._gate_risks({'BANK', 'TUT'})

        self.assertEqual(len(risks), 1)
        self.assertEqual(risks[0]['base_asset'], 'BANK')
        self.assertEqual(risks[0]['exchange'], 'gate')
        self.assertEqual(risks[0]['risk_level'], 'critical')

    def test_binance_schedule_respects_lookahead_window(self):
        monitor = DelistRiskMonitor(DelistRiskConfig(lookahead_days=30))
        delist_at = datetime.now() + timedelta(days=10)
        ignored_at = datetime.now() + timedelta(days=60)
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.return_value = [
            {'delistTime': int(delist_at.timestamp() * 1000), 'symbols': ['BANKUSDT']},
            {'delistTime': int(ignored_at.timestamp() * 1000), 'symbols': ['TUTUSDT']},
        ]
        creds = MagicMock(api_key='key', api_secret='secret')

        with patch('calc.delist_risk_monitor.get_binance_credentials', return_value=creds), \
             patch('calc.delist_risk_monitor.requests.get', return_value=resp):
            risks = monitor._binance_schedule_risks({'BANK', 'TUT'})

        self.assertEqual(len(risks), 1)
        self.assertEqual(risks[0]['base_asset'], 'BANK')
        self.assertEqual(risks[0]['exchange'], 'binance')
        self.assertEqual(risks[0]['risk_type'], 'delist_schedule')

    def test_build_report_dedupes_risks(self):
        monitor = DelistRiskMonitor()
        duplicate = {
            'risk_key': 'gate:BANK:delisting',
            'base_asset': 'BANK',
            'exchange': 'gate',
            'risk_level': 'warning',
        }
        critical = dict(duplicate, risk_level='critical')

        deduped = monitor._dedupe_risks([duplicate, critical])

        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]['risk_level'], 'critical')


if __name__ == '__main__':
    unittest.main(verbosity=2)
