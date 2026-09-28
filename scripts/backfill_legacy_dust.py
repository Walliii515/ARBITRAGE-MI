#!/usr/bin/env python3
"""Approved TLM/SOPH valuation correction only. Never submits exchange orders.

Default is read-only. --apply requires a new backup path on the database host.
"""
import argparse
import gzip
import hashlib
import os
import uuid
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path

from audit_dust_settlement import build_report
from calc.dust_conversion import SCALE, event_time, number
from calc.dust_settlement import DustSettlement, asset_lock_name, decoded, encoded
from calc.real_executor import GATE_CROSS_MARGIN_LEVERAGE, RealExecutor
from calc.reconciliation import build_exchange_config
from common.database import db_manager
from common.database_lock import database_lock

APPROVED = {
    ('TLM', '404801396374'): (7465, 749, Decimal('1.51967962'), Decimal('1.51661994')),
    ('SOPH', '406960050534'): (17083, 697, Decimal('2.90204413'), Decimal('2.89744908')),
}
REPAIR = 'legacy_dust_event_valuation_20260928'


def receipt_amounts(conversion):
    # Preserve the approved net amount; rounding gross and fee separately can differ by 1e-8.
    net = number(conversion['exec_amount_usdt']).quantize(SCALE)
    fee = number(conversion['service_charge_usdt']).quantize(SCALE)
    return net + fee, fee


def repair(cursor, entry, approved):
    oid, pid, old_net, new_net = approved
    asset, transaction = entry['asset'], entry['transaction_id']
    conversion = entry['receipt']
    gross, fee = receipt_amounts(conversion)
    if gross-fee != new_net or entry['order_ids'] != [oid] or entry['position_ids'] != [pid]:
        raise ValueError('approved_correction_changed')
    at = event_time(conversion)
    cursor.execute('SELECT * FROM mi_trade_position WHERE id=%s FOR UPDATE', (pid,))
    pos = cursor.fetchone()
    if not pos or pos['status'] != 'closed' or pos['base_asset'] != asset or pos['exchange_risk_type']:
        raise ValueError('position_not_finally_closed')
    cursor.execute('SELECT * FROM mi_trade_order WHERE id=%s FOR UPDATE', (oid,))
    order = cursor.fetchone()
    if (not order or order['position_id'] != pid or order['base_asset'] != asset
            or order['status'] != 'executed' or order['market_type'] != 'spot'
            or order['order_side'] != 'close' or order['exchange_order_id'] != 'dust:'+transaction
            or order['executed_at'] != at or order['exec_qty'] != number(conversion['source_qty'])):
        raise ValueError('original_order_changed')
    cursor.execute('SELECT * FROM mi_dust_conversion_task WHERE base_asset=%s AND transaction_id=%s FOR UPDATE',
                   (asset, transaction))
    prior = cursor.fetchone()
    if prior:
        audit = decoded(prior['accounting_json']) or {}
        if (prior['status'] != 'accounted' or audit.get('repair') != REPAIR
                or order['exec_amount'] != gross or order['fee_amount_usdt'] != fee):
            raise ValueError('conflicting_existing_receipt')
        return {'asset': asset, 'status': 'already_applied', 'delta': Decimal(0)}
    if order['exec_amount']-order['fee_amount_usdt'] != old_net:
        raise ValueError('original_valuation_changed')
    realized_delta = gross-order['exec_amount']
    fee_delta = fee-order['fee_amount_usdt']
    net_delta = realized_delta-fee_delta
    if net_delta != new_net-old_net:
        raise ValueError('unexpected_net_delta')
    # Limit the repair to valuation deltas, preserving unrelated historical funding/cost corrections.
    updates = {key: pos[key]+realized_delta for key in (
        'spot_close_amount', 'realized_pnl', 'realized_pnl_spot', 'realized_pnl_total')}
    updates['total_pnl'] = pos['total_pnl']+net_delta
    updates['fee_cost'] = pos['fee_cost']-fee_delta
    notional = number(pos['spot_open_amount'], positive=True)
    for bps, amount in [('realized_pnl_bps', 'realized_pnl'), ('total_pnl_bps', 'total_pnl'), ('fee_bps', 'fee_cost')]:
        updates[bps] = (updates[amount]/notional*10000).quantize(Decimal('0.0001'))
    cursor.execute("""UPDATE mi_trade_order SET exec_amount=%s,exec_price=%s,
        fee_amount=%s,fee_amount_usdt=%s,fee_asset='BNB',
        reject_reason=CONCAT(COALESCE(reject_reason,''),'|',%s) WHERE id=%s""",
        (gross,gross/order['exec_qty'],number(conversion['service_charge_bnb']),fee,REPAIR,oid))
    assignments = ','.join(f'{key}=%s' for key in updates)
    cursor.execute(f'UPDATE mi_trade_position SET {assignments} WHERE id=%s', [*updates.values(),pid])
    DustSettlement._correct_snapshots(cursor,at,realized_delta,fee_delta)
    audit = {'repair': REPAIR, 'order_before': order, 'position_before': pos,
             'realized_delta': realized_delta, 'fee_delta': fee_delta, 'net_delta': net_delta}
    cursor.execute("""INSERT INTO mi_dust_conversion_task
        (batch_uuid,base_asset,status,requested_at,positions_json,expected_qty,conversion_json,
         transaction_id,event_at,accounted_at,accounting_json,net_delta_usdt)
        VALUES (%s,%s,'accounted',%s,%s,%s,%s,%s,%s,NOW(3),%s,%s)""",
        (str(uuid.uuid5(uuid.NAMESPACE_URL,REPAIR)),asset,at,encoded([{'id':pid}]),
         order['exec_qty'],encoded(conversion),transaction,at,encoded(audit),net_delta))
    return {'asset':asset,'status':'applied','event_at':at,'delta':net_delta}


def backup(cursor, path, entries):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    earliest = min(event_time(e['receipt']) for e in entries)
    queries = [
        ('mi_trade_order', 'SELECT * FROM mi_trade_order WHERE position_id IN (749,697)', ()),
        ('mi_trade_position', 'SELECT * FROM mi_trade_position WHERE id IN (749,697)', ()),
        ('mi_dust_conversion_task', "SELECT * FROM mi_dust_conversion_task WHERE base_asset IN ('TLM','SOPH')", ()),
        ('mi_capital_daily_summary', 'SELECT * FROM mi_capital_daily_summary WHERE last_snapshot_at >= %s', (earliest,)),
        ('mi_capital_snapshot', """SELECT id,snapshot_at,exchange,realized_pnl_usdt,fee_cost_usdt,
            total_pnl_usdt,equity_usdt,funding_pnl_usdt FROM mi_capital_snapshot
            WHERE snapshot_at >= %s AND exchange IN ('binance','total')
              AND JSON_UNQUOTE(JSON_EXTRACT(detail,'$.source'))='exchange_api' ORDER BY id""", (earliest,)),
    ]
    counts = {}
    with path.open('xb') as raw:
        os.chmod(path,0o600)
        with gzip.GzipFile(fileobj=raw,mode='wb') as stream:
            stream.write((encoded({'repair':REPAIR,'receipts':entries})+'\n').encode())
            for table, sql, params in queries:
                cursor.execute(sql,params)
                counts[table] = 0
                while rows := cursor.fetchmany(1000):
                    for row in rows:
                        stream.write((encoded({'table':table,'row':row})+'\n').encode())
                    counts[table] += len(rows)
        raw.flush()
        os.fsync(raw.fileno())
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return {'path':str(path),'sha256':digest.hexdigest(),'counts':counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--backup')
    args = parser.parse_args()
    if args.apply and not args.backup:
        parser.error('--apply requires --backup (new file)')
    report = build_report(RealExecutor(build_exchange_config(),leverage=GATE_CROSS_MARGIN_LEVERAGE))
    entries = [e for e in report['conversions'] if (e['asset'],e['transaction_id']) in APPROVED]
    if len(entries) != 2 or any(e['status'] != 'matched' for e in entries):
        raise ValueError('both_original_receipts_required')
    if not args.apply:
        print(encoded({'dry_run':True,'corrections':entries}))
        return
    with ExitStack() as stack:
        for name in ['mi_dust_account',asset_lock_name('SOPH'),asset_lock_name('TLM'),'mi_capital_accounting']:
            if not stack.enter_context(database_lock(name,timeout=10)):
                raise RuntimeError('repair_lock_busy:'+name)
        with db_manager.get_cursor() as cursor:
            saved = backup(cursor,args.backup,entries)
            print(encoded({'backup':saved}),flush=True)
            results = [repair(cursor,e,APPROVED[(e['asset'],e['transaction_id'])]) for e in entries]
        print(encoded({'committed':True,'results':results}),flush=True)


if __name__ == '__main__':
    main()
