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
        3. 每次结算写入 mi_funding_fee_history 记录明细
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
                
                if next_funding is None:
                    # 从未设置过 next_funding_time（历史遗留数据）
                    # 根据 opened_at 计算应该已经结算了多少次
                    opened_at = pos.get('opened_at')
                    if not opened_at:
                        continue
                    elapsed_sec = (now - opened_at).total_seconds()
                    expected_count = int(elapsed_sec / FUNDING_INTERVAL_SEC)
                    actual_count = int(pos.get('funding_payments_count') or 0)
                    payments_to_credit = max(expected_count - actual_count, 0)
                    if payments_to_credit == 0:
                        # 还不够8小时，初始化 next_funding_time 后跳过
                        exchange_next = pos.get('funding_next_apply')
                        init_time = exchange_next if exchange_next and exchange_next > now else now + timedelta(hours=8)
                        self._set_next_funding_time(pos['id'], init_time)
                        continue
                elif next_funding > now:
                    continue  # 还未到结算时间
                else:
                    # next_funding_time <= now，可能有一次或多次结算到期
                    elapsed_since_due = (now - next_funding).total_seconds()
                    payments_to_credit = 1 + int(elapsed_since_due / FUNDING_INTERVAL_SEC)
                
                # ── 执行结算 ──
                single_rate = funding_rate_24h / 3
                future_notional = float(pos['future_open_qty']) * float(pos['future_open_price'])
                single_pnl = single_rate * future_notional
                
                total_rate_bps = round(single_rate * 10000 * payments_to_credit, 2)
                total_pnl = round(single_pnl * payments_to_credit, 4)
                current_count = int(pos.get('funding_payments_count') or 0)
                
                # 确定下次结算时间：使用交易所的 funding_next_apply
                exchange_next = pos.get('funding_next_apply')
                next_time = exchange_next if exchange_next and exchange_next > now else now + timedelta(hours=8)
                
                # 累加资金费到持仓
                update_sql = """
                    UPDATE mi_trade_position SET
                        funding_rate_sum_bps = funding_rate_sum_bps + %(rate_bps)s,
                        funding_payments_count = funding_payments_count + %(credit_count)s,
                        funding_total_pnl = funding_total_pnl + %(funding_pnl)s,
                        next_funding_time = %(next_funding_time)s
                    WHERE id = %(position_id)s
                """
                
                with db_manager.get_cursor() as cursor:
                    cursor.execute(update_sql, {
                        'rate_bps': total_rate_bps,
                        'credit_count': payments_to_credit,
                        'funding_pnl': total_pnl,
                        'next_funding_time': next_time,
                        'position_id': pos['id']
                    })
                
                # 写入结算历史明细
                self._insert_funding_history(
                    position_id=pos['id'],
                    base_asset=pos['base_asset'],
                    current_count=current_count,
                    payments_to_credit=payments_to_credit,
                    single_rate=single_rate,
                    funding_rate_24h=funding_rate_24h,
                    single_pnl=single_pnl,
                    future_notional=future_notional,
                )
                
                updated_count += 1
                logger.info(
                    f"资金费结算 | {pos['base_asset']} | "
                    f"rate_24h={funding_rate_24h:.6f} | "
                    f"single_pnl={single_pnl:.4f} | "
                    f"credited={payments_to_credit}次 | "
                    f"total_count={current_count + payments_to_credit} | "
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
    
    def _insert_funding_history(self, position_id: int, base_asset: str,
                                current_count: int, payments_to_credit: int,
                                single_rate: float, funding_rate_24h: float,
                                single_pnl: float, future_notional: float):
        """批量写入资金费结算历史记录"""
        insert_sql = """
            INSERT INTO mi_funding_fee_history
                (position_id, base_asset, payment_seq, funding_rate, funding_rate_24h,
                 funding_pnl, future_notional, settled_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        now = datetime.now()
        rows = []
        for i in range(payments_to_credit):
            seq = current_count + i + 1
            # 推算每笔的结算时间（从当前往回推）
            settle_time = now - timedelta(seconds=FUNDING_INTERVAL_SEC * (payments_to_credit - 1 - i))
            rows.append((
                position_id, base_asset, seq,
                round(single_rate, 10), round(funding_rate_24h, 10),
                round(single_pnl, 6), round(future_notional, 6),
                settle_time
            ))
        
        if rows:
            with db_manager.get_cursor() as cursor:
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
            FROM mi_funding_fee_history
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
