#!/usr/bin/env python3
"""Read-only audit of legacy dust receipts. Never sends orders or changes rows."""
import json
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from calc.dust_conversion import event_time, number, value_receipt
from calc.real_executor import RealExecutor, GATE_CROSS_MARGIN_LEVERAGE
from calc.reconciliation import build_exchange_config
from common.database import db_manager


def build_report(executor):
    with db_manager.get_cursor() as cursor:
        cursor.execute("""SELECT * FROM mi_trade_order WHERE status='executed'
            AND exchange_order_id LIKE 'dust:%%' ORDER BY executed_at,id""")
        orders = list(cursor.fetchall())
    grouped = defaultdict(list)
    for order in orders:
        grouped[(order['base_asset'], order['exchange_order_id'][5:])].append(order)
    report = []
    for (asset, transaction), rows in grouped.items():
        entry = {'asset': asset, 'transaction_id': transaction, 'order_ids': [r['id'] for r in rows],
                 'position_ids': sorted({r['position_id'] for r in rows}), 'status': 'review'}
        try:
            lower = min(r['executed_at'] for r in rows) - timedelta(days=1)
            upper = max(r['executed_at'] for r in rows) + timedelta(days=1)
            history = executor.fetch_binance_dust_history(
                int(lower.replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1000),
                int(upper.replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1000))
            matches = [r for r in history if r['asset'] == asset and r['transaction_id'] == transaction]
            if len(matches) != 1:
                raise ValueError('receipt_missing_or_not_unique')
            conversion = matches[0]
            if sum(number(r['exec_qty']) for r in rows) != number(conversion['source_qty']):
                raise ValueError('receipt_quantity_mismatch')
            valued = value_receipt(conversion, executor.fetch_binance_bnb_event_price(conversion['operate_time_ms']))
            old_net = sum(number(r['exec_amount']) - number(r['fee_amount_usdt']) for r in rows)
            new_net = Decimal(valued['exec_amount_usdt']).quantize(Decimal('0.00000001'))
            entry.update(status='matched', event_at=event_time(conversion), old_net_usdt=old_net,
                         valued_net_usdt=new_net, difference_usdt=new_net-old_net, receipt=valued)
        except Exception as exc:
            entry['error'] = str(exc)
        report.append(entry)
    return {'dry_run': True, 'orders': len(orders), 'conversions': report,
            'valuation_note': 'BNB valued at Binance BNBUSDT event-minute open; not a USDT exchange fill.'}


if __name__ == '__main__':
    executor = RealExecutor(build_exchange_config(), leverage=GATE_CROSS_MARGIN_LEVERAGE)
    print(json.dumps(build_report(executor), ensure_ascii=False, indent=2, default=str))
