# coding: utf-8
"""
交易API路由模块
- 订单查询
- 持仓查询
- 持仓汇总统计
- VWAP基差阈值查询与手动执行
"""
import asyncio
from decimal import Decimal
from datetime import datetime, date
from typing import Optional, Any, List, Dict
from fastapi import APIRouter, Query

from common.database import db_manager
from common.config import config
from common.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/trading", tags=["trading"])


def _serialize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """将数据库行中的 Decimal/datetime 转换为 JSON 可序列化类型"""
    result = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            result[key] = float(value)
        elif isinstance(value, datetime):
            result[key] = value.strftime('%Y-%m-%d %H:%M:%S')
        elif isinstance(value, date):
            result[key] = value.strftime('%Y-%m-%d')
        else:
            result[key] = value
    return result


def _serialize_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """批量序列化数据库行"""
    return [_serialize_row(row) for row in rows]


@router.get('/orders')
async def get_orders(
    status: Optional[str] = Query(None, description="订单状态过滤"),
    channel: Optional[str] = Query(None, description="渠道过滤"),
    position_id: Optional[int] = Query(None, description="持仓ID过滤"),
    base_asset: Optional[str] = Query(None, description="标的资产过滤"),
    start_time: Optional[str] = Query(None, description="开始时间"),
    end_time: Optional[str] = Query(None, description="结束时间")
):
    """查询订单列表"""
    sql = "SELECT * FROM mi_trade_order WHERE 1=1"
    params = []
    
    if status:
        sql += " AND status = %s"
        params.append(status)
    if channel:
        sql += " AND channel = %s"
        params.append(channel)
    if position_id is not None:
        sql += " AND position_id = %s"
        params.append(position_id)
    if base_asset:
        sql += " AND base_asset = %s"
        params.append(base_asset)
    if start_time:
        sql += " AND created_at >= %s"
        params.append(start_time)
    if end_time:
        sql += " AND created_at <= %s"
        params.append(end_time)
    
    sql += " ORDER BY created_at DESC LIMIT 1000"
    
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        return _serialize_rows(rows)


@router.get('/orders/grouped')
async def get_orders_grouped():
    """
    返回分组后的订单列表
    结构：[
        {
            position_id: 123,
            base_asset: 'BTC',
            orders: [现货开仓, 期货开仓, 现货平仓, 期货平仓],
            summary: { 汇总信息 }
        }
    ]
    """
    sql = """
        SELECT * FROM mi_trade_order 
        WHERE position_id IS NOT NULL
        ORDER BY position_id, order_side, market_type, created_at DESC
        LIMIT 2000
    """
    
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()
    
    # 按 position_id 分组
    groups = {}
    for row in rows:
        pid = row['position_id']
        if pid not in groups:
            groups[pid] = {
                'position_id': pid,
                'base_asset': row['base_asset'],
                'orders': [],
                'summary': {}
            }
        groups[pid]['orders'].append(_serialize_row(row))
    
    # 计算汇总信息
    result = []
    for pid, group in groups.items():
        orders = group['orders']
        total_exec_amount = sum(float(o['exec_amount'] or 0) for o in orders)
        total_target_amount = sum(float(o['target_amount'] or 0) for o in orders)
        
        group['summary'] = {
            'total_exec_amount': total_exec_amount,
            'total_target_amount': total_target_amount,
            'order_count': len(orders),
        }
        result.append(group)
    
    return result


@router.get('/positions')
async def get_positions(
    status: Optional[str] = Query(None, description="持仓状态过滤"),
    base_asset: Optional[str] = Query(None, description="标的资产过滤")
):
    """查询持仓列表"""
    sql = "SELECT * FROM mi_trade_position WHERE 1=1"
    params = []
    
    if status:
        sql += " AND status = %s"
        params.append(status)
    if base_asset:
        sql += " AND base_asset = %s"
        params.append(base_asset)
    
    sql += " ORDER BY opened_at DESC"
    
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        return _serialize_rows(rows)


@router.get('/positions/summary')
async def get_positions_summary():
    """持仓汇总统计"""
    sql = """
        SELECT 
            COUNT(*) as total_positions,
            SUM(CASE WHEN status = 'holding' THEN 1 ELSE 0 END) as holding_count,
            SUM(CASE WHEN status = 'holding' THEN spot_open_amount ELSE 0 END) as total_holding_amount,
            SUM(funding_total_pnl) as total_funding_pnl,
            SUM(total_pnl) as total_pnl
        FROM mi_trade_position
    """
    
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
        return _serialize_row(row) if row else {}


# ─── VWAP 基差阈值 ────────────────────────────────────────────────────────────

@router.get('/threshold/latest-date')
async def get_threshold_latest_date():
    """获取 BTC 的最新数据写入时间（用于页面展示，避免全表扫描）"""
    sql = """
        SELECT updated_at
        FROM mi_vwap_basis_threshold
        WHERE base_asset = 'BTC'
          AND updated_at IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 1
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
        if row and row.get('updated_at'):
            d = row['updated_at']
            return {"latest_date": d.strftime('%Y-%m-%d %H:%M') if hasattr(d, 'strftime') else str(d)}
        return {"latest_date": None}


@router.get('/threshold/dates')
async def get_threshold_dates():
    """获取 mi_vwap_basis_threshold 表中所有可用日期（降序）"""
    sql = "SELECT DISTINCT calc_date FROM mi_vwap_basis_threshold ORDER BY calc_date DESC LIMIT 365"
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()
        return [row['calc_date'].strftime('%Y-%m-%d') if hasattr(row['calc_date'], 'strftime') else str(row['calc_date']) for row in rows]


@router.get('/threshold/assets')
async def get_threshold_assets():
    """获取 mi_vwap_basis_threshold 表中所有标的（按 open_basis_p20 降序）"""
    sql = """
        SELECT base_asset
        FROM mi_vwap_basis_threshold
        WHERE calc_date = (SELECT MAX(calc_date) FROM mi_vwap_basis_threshold)
        ORDER BY open_basis_p20 DESC
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()
        return [row['base_asset'] for row in rows]


@router.get('/threshold/data')
async def get_threshold_data(
    calc_date: Optional[str] = Query(None, description="计算日期，格式 YYYY-MM-DD，默认最新日期"),
    base_asset: Optional[str] = Query(None, description="标的资产过滤"),
):
    """查询 mi_vwap_basis_threshold 表数据"""
    sql = "SELECT * FROM mi_vwap_basis_threshold WHERE 1=1"
    params: list = []

    if calc_date:
        sql += " AND calc_date = %s"
        params.append(calc_date)
    else:
        sql += " AND calc_date = (SELECT MAX(calc_date) FROM mi_vwap_basis_threshold)"

    if base_asset:
        sql += " AND base_asset = %s"
        params.append(base_asset)

    sql += " ORDER BY open_basis_p20 DESC"

    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        return _serialize_rows(rows)


# 手动执行状态（避免并发重复触发）
_threshold_calc_running = False


@router.post('/threshold/calculate')
async def trigger_threshold_calculate():
    """手动触发 VWAP 基差分位阈值计算"""
    global _threshold_calc_running
    if _threshold_calc_running:
        return {"success": False, "message": "计算任务正在执行中，请稍后再试"}

    _threshold_calc_running = True
    try:
        lookback_days = config.get_int('trade.vwap_threshold_lookback_days', 7)
        # 在线程池中执行，避免阻塞事件循环
        loop = asyncio.get_event_loop()
        from calc.calculate_vwap_basis_threshold import run_analysis
        await loop.run_in_executor(None, run_analysis, lookback_days)
        return {"success": True, "message": f"计算完成（回溯 {lookback_days} 天）"}
    except Exception as e:
        logger.error(f"手动执行阈值计算失败: {e}", exc_info=True)
        return {"success": False, "message": f"计算失败: {e}"}
    finally:
        _threshold_calc_running = False
