# coding: utf-8
from datetime import datetime

from calc.gate_risk_event_monitor import normalize_gate_risk_event


def test_normalize_gate_auto_deleverage_event():
    event = normalize_gate_risk_event('futures.auto_deleverages', {
        'contract': 'SAHARA_USDT',
        'order_id': '246290605044183631',
        'trade_size': 18,
        'fill_price': '0.02295',
        'entry_price': '0.0165',
        'time_ms': 1780971548000,
    })

    assert event['type'] == 'adl'
    assert event['base_asset'] == 'SAHARA'
    assert event['future_close_size'] == 18
    assert event['future_close_price'] == 0.02295
    assert event['future_exchange_order_id'] == '246290605044183631'
    assert event['event_key'] == 'gate:adl:SAHARA_USDT:order:246290605044183631'
    assert isinstance(event['event_at'], datetime)


def test_normalize_gate_liquidation_event():
    event = normalize_gate_risk_event('futures.liquidates', {
        'contract': 'BANK_USDT',
        'order_id': 'liq-1',
        'size': '-5',
        'order_price': '0.044',
        'mark_price': '0.0439',
        'liq_price': '0.0438',
        'time': 1780971548,
    })

    assert event['type'] == 'liquidation'
    assert event['base_asset'] == 'BANK'
    assert event['future_close_size'] == 5
    assert event['future_close_price'] == 0.044
    assert event['mark_price'] == 0.0439
    assert event['liq_price'] == 0.0438
