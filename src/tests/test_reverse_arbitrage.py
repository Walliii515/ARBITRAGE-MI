import pytest

from calc.reverse_arbitrage import ReverseArbitrageConfig, enrich_reverse_opportunities
from exchange_apis.get_binance_margin_borrow import BinanceMarginBorrowClient, BinanceMarginBorrowConfig


def test_reverse_opportunity_net_edge_with_borrow_rate():
    row = {
        'base_asset': 'HOME',
        'contract': 'HOME_USDT',
        'spot_qty': 1000,
        'future_qty': 1000,
        'spot_price_bid_1': 0.02849,
        'spot_volume_bid_1': 200000,
        'future_price_ask_1': 0.02849,
        'future_volume_ask_1': 200000,
    }
    for i in range(2, 21):
        row[f'spot_price_bid_{i}'] = 0.02849
        row[f'spot_volume_bid_{i}'] = 0
        row[f'future_price_ask_{i}'] = 0.02849
        row[f'future_volume_ask_{i}'] = 0

    cfg = ReverseArbitrageConfig(
        open_amount_usdt=28.49,
        spot_open_fee=0.00075,
        spot_close_fee=0.00075,
        future_open_fee=0.0002,
        future_close_fee=0.0002,
        orderbook_coverage_threshold=0.6,
    )
    contract_meta = {'HOME': {'funding_rate_24h': -0.051, 'quanto_multiplier': 1}}
    borrow_meta = {
        'HOME': {
            'borrowable': True,
            'hourly_interest_rate': 0.0000994603,
            'borrow_limit': 170000,
        }
    }
    reverse_threshold_meta = {
        'HOME': {
            'reverse_open_basis_p20': -20,
            'reverse_close_basis_p20': -10,
        }
    }

    enrich_reverse_opportunities([row], contract_meta, cfg, borrow_meta, reverse_threshold_meta)

    assert row['reverse_gross_funding_bps'] == 510
    assert row['reverse_funding_2h_rate'] == pytest.approx(-0.051 / 12)
    assert row['reverse_borrow_24h_bps'] == 23.8705
    assert row['reverse_basis_bps'] == 0
    assert row['reverse_borrow_capacity_usdt'] == 4843.3
    assert row['reverse_expected_funding_bps'] == 255
    assert row['reverse_p20_edge_bps'] == -20
    assert row['reverse_fee_bps'] == 19
    assert row['reverse_margin_edge_bps'] == pytest.approx(192.1295)
    assert row['reverse_funding_pass'] is True
    assert row['reverse_borrow_pass'] is True
    assert row['reverse_margin_edge_pass'] is True
    assert row['reverse_coverage_pass'] is True
    assert row['reverse_status'] == 'candidate'


def test_reverse_margin_edge_must_be_non_negative():
    row = {
        'base_asset': 'ABC',
        'contract': 'ABC_USDT',
        'spot_qty': 100,
        'future_qty': 100,
        'spot_price_bid_1': 1.0,
        'spot_volume_bid_1': 100000,
        'future_price_ask_1': 0.9995,
        'future_volume_ask_1': 100000,
    }
    for i in range(2, 21):
        row[f'spot_price_bid_{i}'] = 1.0
        row[f'spot_volume_bid_{i}'] = 0
        row[f'future_price_ask_{i}'] = 0.9995
        row[f'future_volume_ask_{i}'] = 0

    cfg = ReverseArbitrageConfig(
        open_amount_usdt=100,
        spot_open_fee=0.00075,
        spot_close_fee=0.00075,
        future_open_fee=0.0002,
        future_close_fee=0.0002,
        orderbook_coverage_threshold=0.6,
    )
    contract_meta = {'ABC': {'funding_rate_24h': -0.02, 'quanto_multiplier': 1}}
    borrow_meta = {
        'ABC': {
            'borrowable': True,
            'hourly_interest_rate': 0,
            'borrow_limit': 100000,
        }
    }
    reverse_threshold_meta = {
        'ABC': {
            'reverse_open_basis_p20': -100,
            'reverse_close_basis_p20': -90,
        }
    }

    enrich_reverse_opportunities([row], contract_meta, cfg, borrow_meta, reverse_threshold_meta)

    assert row['reverse_basis_bps'] == pytest.approx(-5)
    assert row['reverse_p20_edge_bps'] == -100
    assert row['reverse_margin_edge_bps'] == pytest.approx(-14)
    assert row['reverse_margin_edge_pass'] is False
    assert row['reverse_status'] == 'margin_edge_too_low'


def test_binance_margin_borrow_client_maps_interest_and_borrow_limit(monkeypatch):
    client = BinanceMarginBorrowClient(BinanceMarginBorrowConfig(
        base_url='https://example.test',
        api_key='key',
        api_secret='secret',
    ))

    def fake_signed_get(path, params):
        if path.endswith('next-hourly-interest-rate'):
            assert params['assets'] == 'HOME'
            return [{'asset': 'HOME', 'nextHourlyInterestRate': '0.0000994603'}]
        if path.endswith('crossMarginData'):
            return [{
                'coin': 'HOME',
                'borrowable': True,
                'dailyInterest': '0.0023870472',
                'yearlyInterest': '0.8713',
                'borrowLimit': '170000',
            }]
        if path.endswith('maxBorrowable'):
            assert params['asset'] == 'HOME'
            return {'amount': '170000', 'borrowLimit': '200000'}
        raise AssertionError(path)

    monkeypatch.setattr(client, '_signed_get', fake_signed_get)

    meta = client.get_cross_margin_borrow_meta(['HOME'])

    assert meta['HOME']['borrowable'] is True
    assert meta['HOME']['hourly_interest_rate'] == 0.0000994603
    assert meta['HOME']['borrow_limit'] == 170000
    assert meta['HOME']['account_borrow_limit'] == 200000


def test_binance_margin_borrow_client_uses_fee_data_borrow_limit_when_amount_is_zero(monkeypatch):
    client = BinanceMarginBorrowClient(BinanceMarginBorrowConfig(
        base_url='https://example.test',
        api_key='key',
        api_secret='secret',
    ))

    def fake_signed_get(path, params):
        if path.endswith('next-hourly-interest-rate'):
            return [{'asset': 'HOME', 'nextHourlyInterestRate': '0.0000994603'}]
        if path.endswith('crossMarginData'):
            return [{
                'coin': 'HOME',
                'borrowable': True,
                'dailyInterest': '0.0023870472',
                'yearlyInterest': '0.8713',
                'borrowLimit': '170000',
            }]
        if path.endswith('maxBorrowable'):
            return {'amount': '0', 'borrowLimit': '200000'}
        raise AssertionError(path)

    monkeypatch.setattr(client, '_signed_get', fake_signed_get)

    meta = client.get_cross_margin_borrow_meta(['HOME'])

    assert meta['HOME']['borrowable'] is True
    assert meta['HOME']['hourly_interest_rate'] == 0.0000994603
    assert meta['HOME']['borrow_limit'] == 170000
    assert meta['HOME']['max_borrowable_amount'] == 0
    assert meta['HOME']['account_borrow_limit'] == 200000
