"""
持仓管理模块
- PositionTracker: 持仓创建、资金费累加、盈亏计算
"""
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from common.database import db_manager
from common.config import config
from common.logger import get_logger
from calc.orderbook_enricher import calc_vwap_basis_bps

logger = get_logger(__name__)

# Gate 资金费结算间隔（秒）
FUNDING_INTERVAL_SEC = 8 * 3600


class PositionTracker:
    """持仓管理器"""
    
    def __init__(self, contract_meta: Dict = None):
        """
        Args:
            contract_meta: base_asset -> {quanto_multiplier, ...} (可选，用于计算张数)
        """
        self.contract_meta = contract_meta or {}
    
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
        定时更新资金费收益
        逻辑：
        1. 仅使用持仓自身的 next_funding_time 判断是否到期
        2. 支持追补：若 next_funding_time 为 NULL 或已过多个结算周期，一次性追补所有缺失的结算
        3. 每次结算查历史费率表取当期真实费率，写入 mi_trade_funding_fee_history 记录明细
        4. settled_at 基于 next_funding_time 正向推算，确保时间戳与真实结算周期对齐
        """
        sql = """
            SELECT p.*, c.funding_rate_24h, c.funding_next_apply
            FROM mi_trade_position p
            LEFT JOIN mi_gate_future_contracts c 
                ON p.future_contract = CONCAT(c.base_asset, '_USDT')
            WHERE p.status = 'holding'
        """
        
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            positions = cursor.fetchall()
        
        if not positions:
            return
        
        updated_count = 0
        now = datetime.now()
        
        for pos in positions:
            try:
                funding_rate_24h = pos.get('funding_rate_24h')
                if funding_rate_24h is None:
                    continue
                
                funding_rate_24h = float(funding_rate_24h)
                
                # ── 判断是否需要结算 ──
                next_funding = pos.get('next_funding_time')
                base_settle_time = None  # 第一笔待结算的真实时间点
                
                if next_funding is None:
                    # 从未设置过 next_funding_time（历史遗留数据）
                    opened_at = pos.get('opened_at')
                    if not opened_at:
                        continue
                    
                    elapsed_sec = (now - opened_at).total_seconds()
                    # 守卫：持仓时长不足 8h，不可能产生任何结算
                    if elapsed_sec < FUNDING_INTERVAL_SEC:
                        exchange_next = pos.get('funding_next_apply')
                        init_time = exchange_next if exchange_next and exchange_next > now else now + timedelta(hours=8)
                        self._set_next_funding_time(pos['id'], init_time)
                        continue
                    
                    expected_count = int(elapsed_sec / FUNDING_INTERVAL_SEC)
                    actual_count = int(pos.get('funding_payments_count') or 0)
                    payments_to_credit = max(expected_count - actual_count, 0)
                    if payments_to_credit == 0:
                        # 已全部结完，初始化 next_funding_time 后跳过
                        exchange_next = pos.get('funding_next_apply')
                        init_time = exchange_next if exchange_next and exchange_next > now else now + timedelta(hours=8)
                        self._set_next_funding_time(pos['id'], init_time)
                        continue
                    # 基于 opened_at 推算第一笔未结算时间
                    base_settle_time = opened_at + timedelta(seconds=FUNDING_INTERVAL_SEC * (actual_count + 1))
                elif next_funding > now:
                    continue  # 还未到结算时间
                else:
                    # next_funding_time <= now，可能有一次或多次结算到期
                    elapsed_since_due = (now - next_funding).total_seconds()
                    payments_to_credit = 1 + int(elapsed_since_due / FUNDING_INTERVAL_SEC)
                    base_settle_time = next_funding
                
                # ── 计算每期真实结算时间 ──
                settle_times = [
                    base_settle_time + timedelta(seconds=FUNDING_INTERVAL_SEC * i)
                    for i in range(payments_to_credit)
                ]
                
                # ── 查历史费率表获取每期真实费率 ──
                contract = pos.get('future_contract', f"{pos['base_asset']}_USDT")
                historical_rates = self._get_historical_rates(contract, settle_times)
                
                # ── 逐期计算并汇总 ──
                future_notional = float(pos['future_open_qty']) * float(pos['future_open_price'])
                current_count = int(pos.get('funding_payments_count') or 0)
                
                period_data = []  # 每期详情：(rate_24h, single_rate, single_pnl, settle_time)
                total_pnl = 0.0
                total_rate_bps = 0.0
                
                for i in range(payments_to_credit):
                    # 优先使用历史真实费率，回退到当前快照
                    period_rate_24h = historical_rates[i] if historical_rates[i] is not None else funding_rate_24h
                    period_single_rate = period_rate_24h / 3
                    period_pnl = period_single_rate * future_notional
                    
                    total_pnl += period_pnl
                    total_rate_bps += period_single_rate * 10000
                    period_data.append((period_rate_24h, period_single_rate, period_pnl, settle_times[i]))
                
                total_pnl = round(total_pnl, 4)
                total_rate_bps = round(total_rate_bps, 2)
                
                # 确定下次结算时间：基于最后一笔结算时间 + 8h
                next_time = settle_times[-1] + timedelta(seconds=FUNDING_INTERVAL_SEC)
                
                # 累加资金费到持仓 + 写入历史明细：合并到同一事务，确保原子性。
                # 任一失败则两边都回滚，避免出现 funding_payments_count 与 history 行数不一致。
                update_sql = """
                    UPDATE mi_trade_position SET
                        funding_rate_sum_bps = funding_rate_sum_bps + %(rate_bps)s,
                        funding_payments_count = funding_payments_count + %(credit_count)s,
                        funding_total_pnl = funding_total_pnl + %(funding_pnl)s,
                        next_funding_time = %(next_funding_time)s
                    WHERE id = %(position_id)s
                """
                
                with db_manager.get_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(update_sql, {
                            'rate_bps': total_rate_bps,
                            'credit_count': payments_to_credit,
                            'funding_pnl': total_pnl,
                            'next_funding_time': next_time,
                            'position_id': pos['id']
                        })
                        
                        # 写入结算历史明细（逐期写入真实费率和时间）
                        # 使用同一连接以保证与上面的 UPDATE 在同一事务
                        self._insert_funding_history(
                            cursor=cursor,
                            position_id=pos['id'],
                            base_asset=pos['base_asset'],
                            current_count=current_count,
                            period_data=period_data,
                            future_notional=future_notional,
                        )
                
                updated_count += 1
                logger.info(
                    f"资金费结算 | {pos['base_asset']} | "
                    f"rate_24h(current)={funding_rate_24h:.6f} | "
                    f"total_pnl={total_pnl:.4f} | "
                    f"credited={payments_to_credit}次 | "
                    f"total_count={current_count + payments_to_credit} | "
                    f"settle_range=[{settle_times[0].strftime('%m-%d %H:%M')}~{settle_times[-1].strftime('%m-%d %H:%M')}] | "
                    f"next={next_time}"
                )
                
            except Exception as e:
                logger.error(f"更新资金费收益失败 {pos.get('order_uuid', 'unknown')}: {e}")
        
        if updated_count > 0:
            logger.info(f"资金费批量更新完成，共更新 {updated_count} 条持仓")
    
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
        tolerance_sec = FUNDING_INTERVAL_SEC // 2
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
            return cursor.fetchall()

    def get_all_positions(self) -> List[Dict]:
        """获取全部持仓记录（含已平仓），用于实时推送"""
        sql = """
            SELECT * FROM mi_trade_position
            ORDER BY opened_at DESC
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetchall()

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
