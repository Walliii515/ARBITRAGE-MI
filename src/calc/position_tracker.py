"""
持仓管理模块
- PositionTracker: 持仓创建、资金费累加、盈亏计算
"""
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

from common.database import db_manager
from common.config import config
from common.logger import get_logger
from calc.orderbook_enricher import calc_vwap_basis_bps
from calc.position_order_fees import attach_position_order_fee_summary

logger = get_logger(__name__)

# 仅作为旧兼容逻辑的兜底值；真实同步优先使用 Gate 流水时间。
DEFAULT_FUNDING_INTERVAL_SEC = 8 * 3600


class PositionTracker:
    """持仓管理器"""
    
    def __init__(self, contract_meta: Dict = None):
        """
        Args:
            contract_meta: base_asset -> {quanto_multiplier, ...} (可选，用于计算张数)
        """
        self.contract_meta = contract_meta or {}
        self._real_executor = None
    
    def create_position(self, order_group: Dict, exec_result: Dict):
        """
        创建持仓记录(开仓成功后调用)
        
        Args:
            order_group: 订单组(含order_uuid, base_asset, spot_symbol, future_contract等)
            exec_result: 虚拟成交结果(含spot_order和future_order的exec_price/exec_qty/exec_amount)
        """
        spot_exec = exec_result['spot_order']
        future_exec = exec_result['future_order']
        
        spot_price = float(spot_exec['exec_price'])
        future_price = float(future_exec['exec_price'])
        
        # 计算开仓价差(bps)
        open_spread_bps = calc_vwap_basis_bps(spot_price, future_price) or 0
        
        # 计算期货张数
        base_asset = order_group['base_asset']
        quanto = self._get_quanto_multiplier(base_asset)
        future_contracts = int(float(future_exec['exec_qty']) / quanto) if quanto > 0 else 0
        
        # 获取交易所下次资金费结算时间，用于初始化 next_funding_time
        next_funding_time = None
        c_meta = self.contract_meta.get(base_asset, {})
        fna = c_meta.get('funding_next_apply')
        if fna:
            if hasattr(fna, 'strftime'):
                next_funding_time = fna
            else:
                try:
                    next_funding_time = datetime.strptime(str(fna), '%Y-%m-%d %H:%M:%S')
                except (ValueError, TypeError):
                    next_funding_time = datetime.now() + timedelta(hours=8)
        if not next_funding_time:
            next_funding_time = datetime.now() + timedelta(hours=8)
        
        sql = """
            INSERT INTO mi_trade_position (
                order_uuid, base_asset, spot_symbol, future_contract, status, opened_at,
                spot_open_qty, spot_open_price, spot_open_amount,
                future_open_qty, future_open_price, future_open_contracts,
                open_spread_bps, next_funding_time
            ) VALUES (
                %(order_uuid)s, %(base_asset)s, %(spot_symbol)s, %(future_contract)s,
                'holding', NOW(),
                %(spot_open_qty)s, %(spot_open_price)s, %(spot_open_amount)s,
                %(future_open_qty)s, %(future_open_price)s, %(future_open_contracts)s,
                %(open_spread_bps)s, %(next_funding_time)s
            )
        """
        
        params = {
            'order_uuid': order_group['order_uuid'],
            'base_asset': base_asset,
            'spot_symbol': order_group.get('spot_symbol', f"{base_asset}USDT"),
            'future_contract': order_group['future_contract'],
            'spot_open_qty': spot_exec['exec_qty'],
            'spot_open_price': spot_exec['exec_price'],
            'spot_open_amount': spot_exec['exec_amount'],
            'future_open_qty': future_exec['exec_qty'],
            'future_open_price': future_exec['exec_price'],
            'future_open_contracts': future_contracts,
            'open_spread_bps': round(open_spread_bps, 2),
            'next_funding_time': next_funding_time,
        }
        
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, params)
        
        logger.info(
            f"持仓创建 | {base_asset} | "
            f"spot_vwap={spot_price} | future_vwap={future_price} | "
            f"spread_bps={open_spread_bps:.2f} | contracts={future_contracts} | "
            f"next_funding={next_funding_time}"
        )
    
    def update_funding_pnl(self):
        """
        从 Gate 真实账户 fund 流水同步持仓资金费。

        资金费收益以交易所实际入账为准：拉取 Gate futures account_book 中的
        type=fund 记录，按结算时刻本地实际开着的同合约持仓张数占比分摊到
        mi_trade_funding_fee_history，再由明细反向聚合回 mi_trade_position。
        """
        lookback_days = max(config.get_int('trade.position.funding_sync_lookback_days', 7), 1)
        end_time = datetime.now()
        start_time = end_time - timedelta(days=lookback_days)
        fund_rows = self._fetch_gate_fund_rows(start_time, end_time)
        if not fund_rows:
            logger.info("资金费同步：Gate fund 流水为空")
            return

        with db_manager.get_cursor() as cursor:
            positions = self._load_positions_for_funding_sync(cursor, start_time)

        if not positions:
            logger.info("资金费同步：没有可分摊持仓")
            return

        entries_by_position = self._build_exchange_funding_entries(fund_rows, positions)
        if not entries_by_position:
            logger.info("资金费同步：Gate fund 流水未匹配到本地持仓")
            self._sync_next_funding_times()
            return

        affected_ids = sorted(entries_by_position.keys())
        with db_manager.get_connection() as conn:
            with conn.cursor() as cursor:
                self._replace_exchange_funding_history(cursor, affected_ids, start_time, entries_by_position)
                self._refresh_position_funding_summary(cursor, affected_ids)

        self._sync_next_funding_times()
        total_rows = sum(len(rows) for rows in entries_by_position.values())
        logger.info(
            f"资金费真实流水同步完成 | fund_rows={len(fund_rows)} | "
            f"positions={len(affected_ids)} | history_rows={total_rows}"
        )

    def _fetch_gate_fund_rows(self, start_time: datetime, end_time: datetime) -> List[Dict]:
        executor = self._get_real_executor()
        rows = executor.fetch_gate_futures_account_book(
            int(start_time.timestamp()),
            int(end_time.timestamp()),
        )
        result = []
        for row in rows:
            if str(row.get('type') or '').lower() != 'fund':
                continue
            contract = str(row.get('contract') or row.get('text') or '').upper()
            if not contract.endswith('_USDT'):
                continue
            settled_at = datetime.fromtimestamp(int(row.get('time') or 0))
            result.append({
                'contract': contract,
                'base_asset': contract[:-5],
                'settled_at': settled_at.replace(microsecond=0),
                'change': _float(row.get('change')),
                'raw': row,
            })
        result.sort(key=lambda item: (item['settled_at'], item['contract']))
        return result

    def _get_real_executor(self):
        if self._real_executor is None:
            from calc.reconciliation import build_exchange_config
            from calc.real_executor import RealExecutor
            from common.meta_loader import fetch_contract_meta, fetch_spot_meta

            self._real_executor = RealExecutor(
                build_exchange_config(),
                contract_meta=fetch_contract_meta(),
                spot_meta=fetch_spot_meta(),
                leverage=config.get_int('margin.leverage', 2),
            )
        return self._real_executor

    def _load_positions_for_funding_sync(self, cursor, start_time: datetime) -> List[Dict]:
        sql = """
            SELECT p.*, c.funding_interval, c.funding_next_apply
            FROM mi_trade_position p
            LEFT JOIN mi_gate_future_contracts c 
                ON p.future_contract = CONCAT(c.base_asset, '_USDT')
            WHERE p.opened_at <= NOW()
              AND (p.closed_at IS NULL OR p.closed_at >= %s)
        """
        cursor.execute(sql, (start_time,))
        return cursor.fetchall()

    def _build_exchange_funding_entries(self, fund_rows: List[Dict], positions: List[Dict]) -> Dict[int, List[Dict]]:
        rate_map = self._load_funding_rate_map(fund_rows)
        by_position: Dict[int, List[Dict]] = {}
        unmatched: Dict[str, int] = {}

        for fund in fund_rows:
            matched = [
                pos for pos in positions
                if str(pos.get('future_contract') or f"{pos.get('base_asset')}_USDT").upper() == fund['contract']
                and pos.get('opened_at') is not None
                and pos['opened_at'] <= fund['settled_at']
                and (pos.get('closed_at') is None or pos['closed_at'] > fund['settled_at'])
            ]
            if not matched:
                unmatched[fund['contract']] = unmatched.get(fund['contract'], 0) + 1
                continue

            weights = [(pos, self._funding_weight(pos)) for pos in matched]
            total_weight = sum(weight for _, weight in weights)
            if total_weight <= 0:
                continue

            rate_info = rate_map.get((fund['contract'], fund['settled_at']))
            for pos, weight in weights:
                pnl = fund['change'] * weight / total_weight
                notional = self._position_notional(pos)
                single_rate = (pnl / notional) if notional else (rate_info[0] if rate_info else 0.0)
                rate_24h = rate_info[1] if rate_info else self._rate_24h_from_single_rate(pos, single_rate)
                by_position.setdefault(pos['id'], []).append({
                    'base_asset': pos['base_asset'],
                    'funding_rate': single_rate,
                    'funding_rate_24h': rate_24h,
                    'funding_pnl': pnl,
                    'future_notional': notional,
                    'settled_at': fund['settled_at'],
                })

        if unmatched:
            samples = ', '.join(f"{contract}:{count}" for contract, count in sorted(unmatched.items())[:10])
            logger.info(f"Gate资金费未匹配本地持仓汇总 | contracts={len(unmatched)} | samples={samples}")

        for rows in by_position.values():
            rows.sort(key=lambda item: item['settled_at'])
        return by_position

    def _load_funding_rate_map(self, fund_rows: List[Dict]) -> Dict[Tuple[str, datetime], Tuple[float, float]]:
        if not fund_rows:
            return {}
        contracts = sorted({row['contract'] for row in fund_rows})
        min_ts = min(int(row['settled_at'].timestamp()) for row in fund_rows) - 60
        max_ts = max(int(row['settled_at'].timestamp()) for row in fund_rows) + 60
        placeholders = ','.join(['%s'] * len(contracts))
        sql = f"""
            SELECT contract, funding_rate, funding_rate_24h, timestamp
            FROM mi_gate_future_his_funding_rates
            WHERE contract IN ({placeholders})
              AND timestamp BETWEEN %s AND %s
        """
        params = contracts + [min_ts, max_ts]
        result: Dict[Tuple[str, datetime], Tuple[float, float]] = {}
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, params)
            for row in cursor.fetchall():
                settled_at = datetime.fromtimestamp(int(row['timestamp'])).replace(microsecond=0)
                result[(row['contract'], settled_at)] = (
                    float(row['funding_rate'] or 0),
                    float(row['funding_rate_24h'] or 0),
                )
        return result

    def _replace_exchange_funding_history(
        self,
        cursor,
        affected_ids: List[int],
        start_time: datetime,
        entries_by_position: Dict[int, List[Dict]],
    ):
        placeholders = ','.join(['%s'] * len(affected_ids))
        cursor.execute(
            f"""
                DELETE FROM mi_trade_funding_fee_history
                WHERE position_id IN ({placeholders}) AND settled_at >= %s
            """,
            affected_ids + [start_time],
        )

        existing_counts = self._history_counts_before(cursor, affected_ids, start_time)
        insert_sql = """
            INSERT INTO mi_trade_funding_fee_history
                (position_id, base_asset, payment_seq, funding_rate, funding_rate_24h,
                 funding_pnl, future_notional, settled_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        rows = []
        for position_id, entries in entries_by_position.items():
            base_seq = existing_counts.get(position_id, 0)
            for index, entry in enumerate(entries, 1):
                rows.append((
                    position_id,
                    entry['base_asset'],
                    base_seq + index,
                    round(entry['funding_rate'], 10),
                    round(entry['funding_rate_24h'], 10),
                    round(entry['funding_pnl'], 6),
                    round(entry['future_notional'], 6),
                    entry['settled_at'],
                ))
        if rows:
            cursor.executemany(insert_sql, rows)

    def _history_counts_before(self, cursor, position_ids: List[int], start_time: datetime) -> Dict[int, int]:
        placeholders = ','.join(['%s'] * len(position_ids))
        cursor.execute(
            f"""
                SELECT position_id, COUNT(*) AS cnt
                FROM mi_trade_funding_fee_history
                WHERE position_id IN ({placeholders}) AND settled_at < %s
                GROUP BY position_id
            """,
            position_ids + [start_time],
        )
        return {row['position_id']: int(row['cnt'] or 0) for row in cursor.fetchall()}

    def _refresh_position_funding_summary(self, cursor, position_ids: List[int]):
        # funding_rate_sum_bps 专供平仓风控：只累计负 funding_rate_24h 的绝对 bps，正资金费不抵消风险。
        placeholders = ','.join(['%s'] * len(position_ids))
        cursor.execute(
            f"""
                SELECT position_id,
                       COUNT(*) AS cnt,
                       COALESCE(SUM(funding_pnl), 0) AS total_pnl,
                       COALESCE(SUM(
                           CASE
                               WHEN funding_rate_24h < 0 THEN ABS(funding_rate_24h) * 10000
                               ELSE 0
                           END
                       ), 0) AS rate_bps
                FROM mi_trade_funding_fee_history
                WHERE position_id IN ({placeholders})
                GROUP BY position_id
            """,
            position_ids,
        )
        summaries = {
            row['position_id']: {
                'cnt': int(row['cnt'] or 0),
                'total_pnl': float(row['total_pnl'] or 0),
                'rate_bps': float(row['rate_bps'] or 0),
            }
            for row in cursor.fetchall()
        }
        for position_id in position_ids:
            summary = summaries.get(position_id, {'cnt': 0, 'total_pnl': 0.0, 'rate_bps': 0.0})
            cursor.execute(
                """
                    UPDATE mi_trade_position
                    SET funding_payments_count = %s,
                        funding_total_pnl = %s,
                        funding_rate_sum_bps = %s
                    WHERE id = %s
                """,
                (
                    summary['cnt'],
                    round(summary['total_pnl'], 4),
                    round(summary['rate_bps'], 2),
                    position_id,
                ),
            )

    def _sync_next_funding_times(self):
        sql = """
            UPDATE mi_trade_position p
            JOIN mi_gate_future_contracts c
              ON p.future_contract = CONCAT(c.base_asset, '_USDT')
            SET p.next_funding_time = c.funding_next_apply
            WHERE p.status = 'holding'
              AND c.funding_next_apply IS NOT NULL
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)

    def _funding_weight(self, pos: Dict) -> float:
        contracts = abs(_float(pos.get('future_open_contracts')))
        if contracts > 0:
            return contracts
        return abs(_float(pos.get('future_open_qty')))

    def _position_notional(self, pos: Dict) -> float:
        return abs(_float(pos.get('future_open_qty')) * _float(pos.get('future_open_price')))

    def _rate_24h_from_single_rate(self, pos: Dict, single_rate: float) -> float:
        interval = int(pos.get('funding_interval') or self._get_funding_interval(pos.get('base_asset')))
        periods_per_day = 86400 / max(interval, 1)
        return single_rate * periods_per_day

    def _get_funding_interval(self, base_asset: str) -> int:
        meta = self.contract_meta.get(base_asset or '', {})
        return int(meta.get('funding_interval') or DEFAULT_FUNDING_INTERVAL_SEC)
    
    def _set_next_funding_time(self, position_id: int, next_time: datetime):
        """仅设置持仓的 next_funding_time（不累加资金费）"""
        sql = "UPDATE mi_trade_position SET next_funding_time = %s WHERE id = %s"
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, (next_time, position_id))
    
    def _get_historical_rates(self, contract: str, settle_times: List[datetime]) -> List[Optional[float]]:
        """
        从 mi_gate_future_his_funding_rates 查找每个结算时刻对应的历史 24h 费率。
        返回列表长度与 settle_times 一致，找不到对应记录则为 None（由调用方回退到当前快照）。
        """
        if not settle_times:
            return []
        
        # 扩大查询范围（前后各 4h 容差）
        tolerance_sec = DEFAULT_FUNDING_INTERVAL_SEC // 2
        min_ts = int(settle_times[0].timestamp()) - tolerance_sec
        max_ts = int(settle_times[-1].timestamp()) + tolerance_sec
        
        sql = """
            SELECT timestamp, funding_rate_24h
            FROM mi_gate_future_his_funding_rates
            WHERE contract = %s AND timestamp BETWEEN %s AND %s
            ORDER BY timestamp
        """
        
        try:
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, (contract, min_ts, max_ts))
                rows = cursor.fetchall()
        except Exception as e:
            logger.warning(f"查询历史费率失败 {contract}: {e}")
            return [None] * len(settle_times)
        
        if not rows:
            return [None] * len(settle_times)
        
        # 对每个 settle_time 找最近的历史记录
        results = []
        for st in settle_times:
            target_ts = int(st.timestamp())
            best_rate = None
            best_diff = float('inf')
            for row in rows:
                diff = abs(int(row['timestamp']) - target_ts)
                if diff < best_diff:
                    best_diff = diff
                    best_rate = row['funding_rate_24h']
            # 仅在容差范围内才采纳
            if best_diff <= tolerance_sec and best_rate is not None:
                results.append(float(best_rate))
            else:
                results.append(None)
        
        return results
    
    def _insert_funding_history(self, cursor, position_id: int, base_asset: str,
                                current_count: int, period_data: List[tuple],
                                future_notional: float):
        """
        逐期写入资金费结算历史记录。
        
        使用 INSERT IGNORE 防止 (position_id, payment_seq) 唯一约束冲突时
        重复写入（依赖 migration 006 添加的 uk_position_seq）。调用方需
        传入与 UPDATE position 同一事务的 cursor，保证两次写入原子。
        
        Args:
            cursor: 数据库 cursor（与 UPDATE mi_trade_position 共用同一事务）
            period_data: [(rate_24h, single_rate, single_pnl, settle_time), ...]
        """
        insert_sql = """
            INSERT IGNORE INTO mi_trade_funding_fee_history
                (position_id, base_asset, payment_seq, funding_rate, funding_rate_24h,
                 funding_pnl, future_notional, settled_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        rows = []
        for i, (rate_24h, single_rate, single_pnl, settle_time) in enumerate(period_data):
            seq = current_count + i + 1
            rows.append((
                position_id, base_asset, seq,
                round(single_rate, 10), round(rate_24h, 10),
                round(single_pnl, 6), round(future_notional, 6),
                settle_time
            ))
        
        if rows:
            cursor.executemany(insert_sql, rows)
    
    def get_holding_positions(self) -> List[Dict]:
        """获取所有持仓中记录"""
        sql = """
            SELECT * FROM mi_trade_position 
            WHERE status = 'holding' 
            ORDER BY opened_at DESC
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            return attach_position_order_fee_summary(cursor.fetchall())

    def get_all_positions(self) -> List[Dict]:
        """获取全部持仓记录（含已平仓），用于实时推送"""
        sql = """
            SELECT * FROM mi_trade_position
            ORDER BY opened_at DESC
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            return attach_position_order_fee_summary(cursor.fetchall())

    def get_all_funding_histories(self) -> Dict[int, List[Dict]]:
        """
        获取所有持仓的资金费结算历史，按 position_id 分组
        Returns:
            {position_id: [{payment_seq, funding_rate, funding_rate_24h, funding_pnl, settled_at}, ...]}
        """
        sql = """
            SELECT position_id, payment_seq, funding_rate, funding_rate_24h,
                   funding_pnl, future_notional, settled_at
            FROM mi_trade_funding_fee_history
            ORDER BY position_id, payment_seq
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
        
        result: Dict[int, List[Dict]] = {}
        for row in rows:
            pid = row['position_id']
            if pid not in result:
                result[pid] = []
            result[pid].append({
                'seq': row['payment_seq'],
                'rate': float(row['funding_rate']),
                'rate_24h': float(row['funding_rate_24h']) if row['funding_rate_24h'] else None,
                'pnl': float(row['funding_pnl']),
                'notional': float(row['future_notional']) if row['future_notional'] else None,
                'time': row['settled_at'].strftime('%m-%d %H:%M') if row['settled_at'] else None,
            })
        return result
    
    def _get_quanto_multiplier(self, base_asset: str) -> float:
        """获取合约面值"""
        if base_asset in self.contract_meta:
            return float(self.contract_meta[base_asset].get('quanto_multiplier', 1.0))
        return 1.0


def _float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
