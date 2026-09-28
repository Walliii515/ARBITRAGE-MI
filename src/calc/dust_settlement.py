"""Durable, fail-closed Binance dust requests and atomic order-ledger settlement."""
import json
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from calc.asset_reduction_guard import asset_reduction_guard
from calc.closed_position_pnl import compute_closed_position_pnl, update_closed_position_pnl
from calc.dust_conversion import allocate, event_time, number, value_receipt
from calc.popup_notification_store import upsert_popup_notification
from common.config import config
from common.database import db_manager
from common.database_lock import database_lock
from common.logger import get_logger

logger = get_logger(__name__)
PENDING = 'post_close_spot_dust_pending'
ACTIVE = "('pending','submitted','confirmed','review')"


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def encoded(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def asset_lock_name(asset):
    return 'mi_dust_asset_' + str(asset).upper()


def has_unsettled_dust(asset):
    with db_manager.get_cursor() as cursor:
        cursor.execute(f"SELECT id FROM mi_dust_conversion_task WHERE base_asset=%s AND status IN {ACTIVE} LIMIT 1", (asset,))
        return bool(cursor.fetchone())


class DustSettlement:
    def __init__(self, remediator):
        self.remediator = remediator
        self.executor = remediator.executor

    def _tasks(self):
        with db_manager.get_cursor() as cursor:
            cursor.execute(f"SELECT * FROM mi_dust_conversion_task WHERE status IN {ACTIVE} ORDER BY id")
            return list(cursor.fetchall())

    def _error(self, task, reason):
        message = str(reason)[:500]
        with db_manager.get_cursor() as cursor:
            cursor.execute('UPDATE mi_dust_conversion_task SET last_error=%s, last_checked_at=NOW() WHERE id=%s',
                           (message, task['id']))
        if task.get('last_error') != message:
            logger.warning('尘埃核销待核实 | %s | task=%s | %s', task['base_asset'], task['id'], message)
            try:
                upsert_popup_notification(
                    title='小额兑换待核实', type='warning', source='dust_settlement',
                    dedup_key=f"dust-task:{task['id']}",
                    message=f"{task['base_asset']} 兑换任务 {task['id']}：{message}；未重复兑换，未使用零收益替代。",
                    payload={'task_id': task['id'], 'reason': message},
                )
            except Exception:
                logger.exception('尘埃任务已保留，通知写入失败 | task=%s', task['id'])

    def recover(self):
        """Resume receipts/accounting only. Never submit a conversion on recovery."""
        with database_lock('mi_dust_account') as acquired:
            if not acquired:
                return []
            return self._recover_locked()

    def _recover_locked(self):
        results = []
        for task in self._tasks():
            checked = task.get('last_checked_at')
            if checked and (datetime.now() - checked).total_seconds() < 60:
                continue
            with asset_reduction_guard.claim(task['base_asset'], 'dust_recovery') as acquired:
                if not acquired:
                    continue
                with database_lock(asset_lock_name(task['base_asset'])) as owned:
                    if not owned:
                        continue
                    try:
                        if task['status'] == 'pending':
                            # A committed pending intent is proof POST was not reached.
                            with db_manager.get_cursor() as cursor:
                                cursor.execute("UPDATE mi_dust_conversion_task SET status='cancelled', last_error='not_submitted' WHERE id=%s AND status='pending'", (task['id'],))
                            continue
                        if task['status'] == 'review':
                            continue
                        if task['status'] == 'submitted':
                            start = int(task['requested_at'].replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1000)
                            history = self.executor.fetch_binance_dust_history(start - 5000, min(start + 300000, int(time.time() * 1000)))
                            candidates = [item for item in history if item['asset'] == task['base_asset']
                                          and number(item['source_qty']) == number(task['expected_qty'])
                                          and start - 5000 <= int(item['operate_time_ms']) <= start + 300000]
                            if len(candidates) != 1:
                                raise ValueError('兑换结果未知或历史记录不唯一，需要核实；禁止自动重发')
                            self._confirm(task, candidates[0])
                        self._account(task)
                        results.append({'base_asset': task['base_asset'], 'task_id': task['id'], 'success': True})
                    except Exception as exc:
                        self._error(task, exc)
        return results

    def execute(self, prepared_items):
        if not config.get_bool('reconciliation.dust_settlement.enabled', False):
            return {'success': False, 'attempted': False, 'reason': 'dust_settlement_disabled'}
        with database_lock('mi_dust_account') as acquired:
            if not acquired:
                return {'success': False, 'attempted': False, 'reason': 'dust_conversion_inflight'}
            self._recover_locked()
            if self._tasks():
                return {'success': False, 'attempted': False, 'reason': 'dust_previous_request_unresolved'}
            remaining = self.cooldown_remaining()
            if remaining > 0:
                return {'success': False, 'attempted': False, 'reason': 'binance_dust_conversion_cooldown',
                        'cooldown_remaining_sec': round(remaining, 1)}
            with ExitStack() as stack:
                ready = []
                for item in sorted(prepared_items, key=lambda row: row['base_asset']):
                    asset = item['base_asset']
                    if not stack.enter_context(asset_reduction_guard.claim(asset, 'dust_conversion')):
                        continue
                    if not stack.enter_context(database_lock(asset_lock_name(asset))):
                        continue
                    # Never convert from the pre-Gate-close snapshot or old local remainders.
                    started = time.monotonic()
                    balances = self.executor.fetch_binance_spot_balances()
                    futures = self.executor.fetch_gate_futures_positions()
                    positions = self.remediator._load_holding_positions_with_execution_remainders(asset)
                    if not positions:
                        continue
                    balance = next((r for r in balances if r.get('asset') == asset), {})
                    gate = next((r for r in futures if r.get('base_asset') == asset), {})
                    candidate = self.remediator._prepare_dust_cleanup_candidate(asset, positions, balance, gate)
                    if not candidate.get('eligible'):
                        continue
                    self._check_sources(asset, positions)
                    if abs(float(gate.get('size') or 0)) > 0:
                        closed = self.remediator._close_gate_dust_before_conversion(candidate)
                        if closed.get('failed'):
                            continue
                        # Discard the old quantities after real Gate execution.
                        started = time.monotonic()
                        positions = self.remediator._load_holding_positions_with_execution_remainders(asset)
                        balances = self.executor.fetch_binance_spot_balances()
                        futures = self.executor.fetch_gate_futures_positions()
                        balance = next((r for r in balances if r.get('asset') == asset), {})
                        gate = next((r for r in futures if r.get('base_asset') == asset), {})
                        candidate = self.remediator._prepare_dust_cleanup_candidate(asset, positions, balance, gate)
                        if not candidate.get('eligible') or abs(float(gate.get('size') or 0)) > 0:
                            continue
                    if any(number(pos['_future_remaining_qty']) > 0 for pos in positions):
                        continue
                    price = self.executor._get_binance_usdt_price(asset, max_age_sec=0)
                    if number(price, positive=True) * number(candidate['spot_qty']) >= number(self.executor.spot_meta[asset]['min_notional'], positive=True):
                        continue
                    if time.monotonic() - started > 5:
                        continue
                    with database_lock('mi_capital_accounting', timeout=10) as accounting:
                        if not accounting:
                            continue
                        for pos in positions:
                            if pos['status'] == 'holding' and not self.remediator._mark_spot_dust_pending_locked(
                                pos, float(pos['_spot_remaining_qty']), float(price),
                            ):
                                raise ValueError('dust_pending_cost_basis_incomplete')
                    positions = self.remediator._load_holding_positions_with_execution_remainders(asset)
                    candidate = self.remediator._prepare_dust_cleanup_candidate(asset, positions, balance, gate)
                    if not candidate.get('eligible'):
                        continue
                    ready.append(candidate)
                if not ready:
                    return {'success': False, 'attempted': False, 'reason': 'dust_fresh_verification_failed'}
                # A batch can take longer than one asset's freshness budget.
                checked = time.monotonic()
                balances = self.executor.fetch_binance_spot_balances()
                futures = self.executor.fetch_gate_futures_positions()
                for item in ready:
                    asset = item['base_asset']
                    balance = next((r for r in balances if r.get('asset') == asset), {})
                    gate = next((r for r in futures if r.get('base_asset') == asset), {})
                    if number(balance.get('locked', 0)) != 0 or number(balance.get('total')) != number(item['spot_qty']):
                        raise ValueError('dust_inventory_changed_before_submission')
                    if float(gate.get('size') or 0) != 0:
                        raise ValueError('dust_future_changed_before_submission')
                    price = number(self.executor._get_binance_usdt_price(asset, max_age_sec=0), positive=True)
                    if price * number(item['spot_qty']) >= number(self.executor.spot_meta[asset]['min_notional'], positive=True):
                        raise ValueError('dust_notional_changed_before_submission')
                if time.monotonic() - checked > 5:
                    raise ValueError('dust_batch_snapshot_stale')
                return self._submit(ready)

    def _check_sources(self, asset, positions):
        from calc.reconciliation import Reconciler
        with db_manager.get_cursor() as cursor:
            cursor.execute("SELECT id FROM mi_reverse_trade_position WHERE base_asset=%s AND status='holding' LIMIT 1", (asset,))
            if cursor.fetchone():
                raise ValueError('dust_contains_reverse_position')
            cursor.execute("SELECT status,reject_reason FROM mi_trade_order WHERE base_asset=%s AND created_at >= %s AND status <> 'executed'",
                           (asset, min(p['opened_at'] for p in positions)))
            if any(Reconciler._order_execution_uncertain(r) for r in cursor.fetchall()):
                raise ValueError('dust_contains_uncertain_order')
            cursor.execute("SELECT id FROM mi_exchange_risk_event WHERE base_asset=%s AND status IN ('received','failed') LIMIT 1", (asset,))
            if cursor.fetchone():
                raise ValueError('dust_contains_unresolved_exchange_event')

    def cooldown_remaining(self):
        with db_manager.get_cursor() as cursor:
            cursor.execute("SELECT MAX(requested_at) AS last_at FROM mi_dust_conversion_task WHERE status <> 'cancelled'")
            last = (cursor.fetchone() or {}).get('last_at')
        local_remaining = max(0.0, 3600 - (datetime.now() - last).total_seconds()) if last else 0.0
        if local_remaining:
            return local_remaining
        now_ms = int(time.time() * 1000)
        history = self.executor.fetch_binance_dust_history(now_ms - 3600000, now_ms)
        return max((max(0.0, (int(row['operate_time_ms']) + 3600000 - now_ms) / 1000)
                    for row in history), default=0.0)

    def _submit(self, ready):
        batch = str(uuid.uuid4())
        requested = datetime.now()
        tasks = []
        with db_manager.get_cursor() as cursor:
            for item in ready:
                cursor.execute("""INSERT INTO mi_dust_conversion_task
                    (batch_uuid,base_asset,status,requested_at,positions_json,expected_qty)
                    VALUES (%s,%s,'pending',%s,%s,%s)""",
                    (batch, item['base_asset'], requested, encoded(item['positions']), item['spot_qty']))
                tasks.append({'id': cursor.lastrowid, 'batch_uuid': batch, 'base_asset': item['base_asset'],
                              'requested_at': requested, 'positions_json': item['positions'],
                              'expected_qty': item['spot_qty'], 'status': 'submitted'})
        # Commit this state before POST, including a crash before receiving its response.
        with db_manager.get_cursor() as cursor:
            cursor.execute("UPDATE mi_dust_conversion_task SET status='submitted' WHERE batch_uuid=%s AND status='pending'", (batch,))
        response = self.executor.convert_binance_spot_dust_to_bnb_batch(
            [item['base_asset'] for item in ready], client_id=batch)
        results = []
        for task in tasks:
            conversion = (response.get('results') or {}).get(task['base_asset'])
            try:
                if not conversion:
                    raise ValueError(response.get('reason') or 'dust_receipt_missing')
                self._confirm(task, conversion)
                self._account(task)
                results.append({'base_asset': task['base_asset'], 'task_id': task['id'], 'success': True,
                                'positions': len(task['positions_json'])})
            except Exception as exc:
                self._error(task, exc)
                results.append({'base_asset': task['base_asset'], 'task_id': task['id'], 'success': False, 'reason': str(exc)})
        success = all(r['success'] for r in results)
        completed = [r for r in results if r['success']]
        positions = sum(r.get('positions', 0) for r in completed)
        return {'success': success, 'attempted': True, 'action': 'cleanup_post_close_dust_batch',
                'results': results, 'base_assets': [r['base_asset'] for r in results],
                'positions': positions, 'success_count': positions, 'asset_count': len(completed),
                'asset_success_count': len(completed), 'failure_count': len(results)-len(completed),
                'reason': None if success else 'dust_settlement_pending',
                'message': '小额兑换已核销' if success else '兑换结果或估值待核实；不会重复兑换'}

    def _confirm(self, task, conversion):
        qty = number(conversion['source_qty'], positive=True)
        if conversion['asset'] != task['base_asset'] or qty > number(task['expected_qty']):
            raise ValueError('dust_conversion_qty_mismatch')
        at = event_time(conversion)
        if at < task['requested_at'] - timedelta(seconds=5) or at > task['requested_at'] + timedelta(minutes=5):
            raise ValueError('dust_conversion_time_mismatch')
        with db_manager.get_cursor() as cursor:
            cursor.execute("""UPDATE mi_dust_conversion_task SET status='confirmed',conversion_json=%s,
                transaction_id=%s,event_at=%s,last_error=NULL WHERE id=%s AND status='submitted'""",
                (encoded(conversion), conversion['transaction_id'], at, task['id']))
        task.update(status='confirmed', conversion_json=conversion, event_at=at)

    def _account(self, task):
        conversion = decoded(task['conversion_json'])
        valued = value_receipt(conversion, self.executor.fetch_binance_bnb_event_price(conversion['operate_time_ms']))
        with database_lock('mi_capital_accounting', timeout=10) as acquired:
            if not acquired:
                raise ValueError('capital_accounting_busy')
            with db_manager.get_cursor() as cursor:
                cursor.execute('SELECT * FROM mi_dust_conversion_task WHERE id=%s FOR UPDATE', (task['id'],))
                current = cursor.fetchone()
                if current['status'] == 'accounted':
                    return
                if current['status'] != 'confirmed':
                    raise ValueError('dust_task_not_confirmed')
                source = decoded(current['positions_json'])
                ids = [int(p['id']) for p in source]
                placeholders = ','.join(['%s'] * len(ids))
                cursor.execute(f'SELECT * FROM mi_trade_position WHERE id IN ({placeholders}) ORDER BY id FOR UPDATE', ids)
                positions = list(cursor.fetchall())
                if len(positions) != len(ids):
                    raise ValueError('dust_source_position_missing')
                cursor.execute(f"SELECT * FROM mi_trade_order WHERE position_id IN ({placeholders}) AND status='executed' ORDER BY id", ids)
                orders = list(cursor.fetchall())
                by_id = {p['id']: p for p in positions}
                source_by_id = {int(p['id']): p for p in source}
                for pos in positions:
                    ledger = [o for o in orders if o['position_id'] == pos['id']]
                    for market, key in [('spot', '_spot_remaining_qty'), ('future', '_future_remaining_qty')]:
                        remaining = sum((number(o['exec_qty']) * (1 if o['order_side'] == 'open' else -1)
                                         for o in ledger if o['market_type'] == market), Decimal(0))
                        pos[key] = remaining
                        if abs(remaining - number(source_by_id[pos['id']][key])) > Decimal('0.00000001'):
                            raise ValueError('dust_inventory_changed_since_submission')
                    if (pos['_future_remaining_qty'] != 0 or pos.get('status') != 'closed'
                            or pos.get('exchange_risk_type') != PENDING):
                        raise ValueError('dust_source_has_future_exposure')
                    for market in ('spot', 'future'):
                        opens = [o for o in ledger if o['market_type'] == market and o['order_side'] == 'open']
                        if not opens or any(number(o['exec_amount'], positive=True) <= 0 for o in opens):
                            raise ValueError('dust_cost_basis_missing')
                allocations = allocate(valued, [by_id[i] for i in ids])
                delta = Decimal(0)
                reason = f"平仓残余尘埃处置|Binance小额资产转BNB|task={task['id']}|tran_id={valued['transaction_id']}|估值=BNBUSDT事件分钟开盘价"
                cursor.execute('SHOW COLUMNS FROM mi_trade_position')
                columns = {r['Field'] for r in cursor.fetchall()}
                for item in allocations:
                    pos = by_id[item['position_id']]
                    ledger = [o for o in orders if o['position_id'] == pos['id']]
                    before = compute_closed_position_pnl(pos, ledger)
                    if before is None:
                        raise ValueError('dust_pnl_basis_incomplete')
                    order = {
                        'order_uuid': str(uuid.uuid5(uuid.NAMESPACE_URL, f"dust:{task['id']}:{pos['id']}")),
                        'position_id': pos['id'], 'base_asset': pos['base_asset'],
                        'spot_symbol': pos.get('spot_symbol'), 'future_contract': pos.get('future_contract'),
                        'order_side': 'close', 'market_type': 'spot', 'trade_direction': 'sell',
                        'leverage': 1, 'status': 'executed', 'channel': 'Live', 'reject_reason': reason,
                        'target_qty': item['qty'], 'target_amount': item['gross_exec_amount_usdt'],
                        'exec_qty': item['qty'], 'exec_amount': item['gross_exec_amount_usdt'],
                        'exec_price': item['gross_exec_amount_usdt'] / item['qty'],
                        'coverage_ratio': 0, 'liquidity_role': 'unknown', 'fee_rate': None,
                        'fee_amount': item['service_charge_bnb'], 'fee_amount_usdt': item['service_charge_usdt'],
                        'fee_asset': 'BNB', 'exchange_order_id': 'dust:' + valued['transaction_id'],
                        'executed_at': event_time(valued),
                    }
                    self.remediator._insert_allocated_close_order(order, cursor=cursor)
                    ledger.append(order)
                    remaining = pos['_spot_remaining_qty'] - item['qty']
                    updated = dict(pos, exchange_risk_type=PENDING if remaining > 0 else None)
                    pnl = compute_closed_position_pnl(updated, ledger)
                    if pnl is None:
                        raise ValueError('dust_pnl_incomplete')
                    values = self.remediator._close_execution_values(ledger, pos['base_asset'])
                    cursor.execute("""UPDATE mi_trade_position SET status='closed',
                        closed_at=COALESCE(closed_at,%s), exchange_risk_status='resolved',exchange_risk_type=%s,
                        close_reason=CONCAT(COALESCE(close_reason,''),'|',%s),
                        spot_open_qty=%s,spot_open_amount=%s,future_open_qty=%s,future_open_contracts=%s,
                        spot_close_price=%s,future_close_price=%s,spot_close_amount=%s,future_close_amount=%s
                        WHERE id=%s""", (event_time(valued), updated['exchange_risk_type'], reason,
                        values['spot_open_qty'], values['spot_open_amount'], values['future_open_qty'], values['future_open_contracts'],
                        values['spot_close_price'], values['future_close_price'], values['spot_close_amount'], values['future_close_amount'], pos['id']))
                    update_closed_position_pnl(cursor, pos['id'], pnl, columns)
                    item['realized_delta_usdt'] = Decimal(str(pnl['realized_pnl'])) - Decimal(str(before['realized_pnl']))
                    delta += item['realized_delta_usdt']
                fee = sum((r['service_charge_usdt'] for r in allocations), Decimal(0))
                self._correct_snapshots(cursor, event_time(valued), delta, fee)
                cursor.execute("""UPDATE mi_dust_conversion_task SET status='accounted',accounted_at=NOW(3),
                    conversion_json=%s,accounting_json=%s,net_delta_usdt=%s,last_error=NULL WHERE id=%s""",
                    (encoded(valued), encoded(allocations), delta-fee, task['id']))

    @staticmethod
    def _correct_snapshots(cursor, at, realized, fee, future_realized=0):
        # Snapshot collection holds the same lock. Future snapshots read the updated ledger.
        for exchange, pnl, charge in [('binance', realized, fee), ('gate', future_realized, 0),
                                     ('total', realized+future_realized, fee)]:
            cursor.execute("""UPDATE mi_capital_snapshot SET realized_pnl_usdt=realized_pnl_usdt+%s,
                fee_cost_usdt=fee_cost_usdt-%s,total_pnl_usdt=total_pnl_usdt+%s
                WHERE exchange=%s AND snapshot_at >= %s
                  AND JSON_UNQUOTE(JSON_EXTRACT(detail,'$.source'))='exchange_api'""",
                (pnl, charge, pnl-charge, exchange, at))
        net = realized + future_realized - fee
        cursor.execute("""UPDATE mi_capital_daily_summary SET
            first_gross_pnl_usdt=first_gross_pnl_usdt+IF(first_snapshot_at >= %s,%s,0),
            last_gross_pnl_usdt=last_gross_pnl_usdt+IF(last_snapshot_at >= %s,%s,0)
            WHERE last_snapshot_at >= %s""", (at,net,at,net,at))
