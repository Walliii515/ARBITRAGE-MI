"""Exchange receipts and decimal allocation for spot dust settlement."""
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

from calc.spot_residual import decimal_value

SCALE = Decimal('0.00000001')


def number(value, *, positive=False):
    result = decimal_value(value)
    if result is None or result < 0 or (positive and result <= 0):
        raise ValueError('invalid_dust_receipt_number')
    return result


def receipt(item):
    asset = str(item.get('fromAsset') or '').upper()
    transaction = str(item.get('tranId') or item.get('transId') or '')
    timestamp = int(item.get('operateTime') or 0)
    if not asset or not transaction or timestamp <= 0 or item.get('targetAsset', 'BNB') != 'BNB':
        raise ValueError('incomplete_dust_receipt')
    return {
        'asset': asset, 'transaction_id': transaction, 'operate_time_ms': timestamp,
        'source_qty': str(number(item.get('amount'), positive=True)),
        'bnb_qty': str(number(item.get('transferedAmount'), positive=True)),
        'service_charge_bnb': str(number(item.get('serviceChargeAmount'))),
        'raw_receipt': item,
    }


def event_time(conversion):
    return datetime.fromtimestamp(int(conversion['operate_time_ms']) / 1000,
                                  ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)


def value_receipt(conversion, price):
    result = dict(conversion)
    mark = number(price, positive=True)
    qty = number(result['source_qty'], positive=True)
    net = number(result['bnb_qty'], positive=True) * mark
    fee = number(result['service_charge_bnb']) * mark
    result.update({
        'success': True, 'bnb_price_usdt': str(mark),
        'valuation_source': 'binance_BNBUSDT_1m_open',
        'valuation_time_ms': int(result['operate_time_ms']) // 60000 * 60000,
        'exec_amount_usdt': str(net), 'exec_price_usdt': str(net / qty),
        'service_charge_usdt': str(fee), 'gross_exec_amount_usdt': str(net + fee),
        'gross_exec_price_usdt': str((net + fee) / qty),
    })
    return result


def allocate(conversion, positions):
    """FIFO attribution; last allocation carries decimal rounding remainder."""
    left = number(conversion['source_qty'], positive=True)
    total = left
    result = []
    for pos in positions:
        available = number(pos['_spot_remaining_qty'])
        qty = min(available, left)
        if qty <= 0:
            continue
        result.append({'position_id': int(pos['id']), 'qty': qty})
        left -= qty
    if left > 0:
        raise ValueError('dust_receipt_exceeds_attributable_inventory')
    for field in ('gross_exec_amount_usdt', 'service_charge_usdt', 'service_charge_bnb'):
        amount = number(conversion[field]).quantize(SCALE)
        remaining = amount
        for index, item in enumerate(result):
            value = remaining if index == len(result) - 1 else (
                amount * item['qty'] / total).quantize(SCALE, rounding=ROUND_DOWN)
            item[field] = value
            remaining -= value
    return result
