"""
持仓管理模块
- PositionTracker: 持仓创建、资金费累加、盈亏计算
"""
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from common.database import db_manager
from common.config import config
from common.logger import get_logger

logger = get_logger(__name__)


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
        open_spread_bps = (future_price - spot_price) / spot_price * 10000
        
        # 计算期货张数
        base_asset = order_group['base_asset']
        quanto = self._get_quanto_multiplier(base_asset)
        future_contracts = int(float(future_exec['exec_qty']) / quanto) if quanto > 0 else 0
        
        sql = """
            INSERT INTO mi_trade_position (
                order_uuid, base_asset, spot_symbol, future_contract, status, opened_at,
                spot_open_qty, spot_open_price, spot_open_amount,
                future_open_qty, future_open_price, future_open_contracts,
                open_spread_bps
            ) VALUES (
                %(order_uuid)s, %(base_asset)s, %(spot_symbol)s, %(future_contract)s,
                'holding', NOW(),
                %(spot_open_qty)s, %(spot_open_price)s, %(spot_open_amount)s,
                %(future_open_qty)s, %(future_open_price)s, %(future_open_contracts)s,
                %(open_spread_bps)s
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
            'open_spread_bps': round(open_spread_bps, 2)
        }
        
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, params)
        
        logger.info(
            f"持仓创建 | {base_asset} | "
            f"spot_vwap={spot_price} | future_vwap={future_price} | "
            f"spread_bps={open_spread_bps:.2f} | contracts={future_contracts}"
        )
    
    def update_funding_pnl(self):
        """
        定时更新资金费收益(每8小时,资金费结算后)
        从mi_gate_future_contracts获取当前资金费率并累加
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
        for pos in positions:
            try:
                funding_rate_24h = pos.get('funding_rate_24h')
                if funding_rate_24h is None:
                    continue
                
                funding_rate_24h = float(funding_rate_24h)
                
                # 检查是否已过结算时间
                next_funding = pos.get('funding_next_apply') or pos.get('next_funding_time')
                if next_funding and next_funding > datetime.now():
                    continue  # 还未到结算时间
                
                # 单次资金费 = 24h费率/3 * 期货名义价值
                # 注: funding_rate_24h是24小时总费率，每8小时结算1/3
                single_rate = funding_rate_24h / 3
                future_notional = float(pos['future_open_qty']) * float(pos['future_open_price'])
                funding_pnl = single_rate * future_notional
                
                # 累加资金费
                update_sql = """
                    UPDATE mi_trade_position SET
                        funding_rate_sum_bps = funding_rate_sum_bps + %(rate_bps)s,
                        funding_payments_count = funding_payments_count + 1,
                        funding_total_pnl = funding_total_pnl + %(funding_pnl)s,
                        next_funding_time = %(next_funding_time)s
                    WHERE id = %(position_id)s
                """
                
                # 下次结算时间+8小时
                next_time = datetime.now() + timedelta(hours=8)
                
                with db_manager.get_cursor() as cursor:
                    cursor.execute(update_sql, {
                        'rate_bps': round(single_rate * 10000, 2),
                        'funding_pnl': round(funding_pnl, 4),
                        'next_funding_time': next_time,
                        'position_id': pos['id']
                    })
                
                updated_count += 1
                logger.info(
                    f"资金费结算 | {pos['base_asset']} | "
                    f"rate_24h={funding_rate_24h:.6f} | "
                    f"pnl={funding_pnl:.4f} | "
                    f"count={pos['funding_payments_count'] + 1}"
                )
                
            except Exception as e:
                logger.error(f"更新资金费收益失败 {pos.get('order_uuid', 'unknown')}: {e}")
        
        if updated_count > 0:
            logger.info(f"资金费批量更新完成，共更新 {updated_count} 条持仓")
    
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
    
    def _get_quanto_multiplier(self, base_asset: str) -> float:
        """获取合约面值"""
        if base_asset in self.contract_meta:
            return float(self.contract_meta[base_asset].get('quanto_multiplier', 1.0))
        return 1.0
