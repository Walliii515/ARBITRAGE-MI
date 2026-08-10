# coding: utf-8
"""
分析开仓/平仓决策时基差 vs 实际成交基差的差异

核心问题：
- 开仓信号显示正向大基差（如ACH 97.9bps），但实际成交后计算的基差为负值（-72.16bps）
- 平仓信号显示已止盈，但实际平仓基差并未达到止盈水平

分析思路：
1. 从数据库读取相关交易的持仓和订单记录
2. 对比决策时盘口VWAP vs 实际成交价格
3. 分析VWAP快照表中对应时间点的盘口数据
4. 识别根因（盘口数据偏差 or 滑点 or 其他）
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pymysql
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))


def get_connection():
    return pymysql.connect(
        host=os.getenv('MYSQL_HOST', 'localhost'),
        port=int(os.getenv('MYSQL_PORT', 3306)),
        database=os.getenv('MYSQL_DATABASE', 'crypto_arbitrage'),
        user=os.getenv('MYSQL_USER', 'arb_app'),
        password=os.getenv('MYSQL_PASSWORD', ''),
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor
    )


def calc_basis_bps(spot_price, future_price):
    """计算基差 bps"""
    if spot_price and future_price and float(spot_price) > 0:
        return (float(future_price) - float(spot_price)) / float(spot_price) * 10000
    return None


def analyze_open_trades():
    """分析开仓交易的基差差异"""
    print("=" * 100)
    print("【一、开仓交易分析】")
    print("=" * 100)

    # 目标交易对和时间
    targets = [
        ('ACH', '2026-06-03 11:45:00', '2026-06-03 11:45:05'),
        ('W', '2026-06-03 11:49:05', '2026-06-03 11:49:10'),
        ('RIF', '2026-06-03 13:00:59', '2026-06-03 13:01:05'),
    ]

    conn = get_connection()
    cursor = conn.cursor()

    for base_asset, time_start, time_end in targets:
        print(f"\n{'─' * 80}")
        print(f"  交易对: {base_asset}")
        print(f"{'─' * 80}")

        # 1. 查持仓记录
        cursor.execute("""
            SELECT id, order_uuid, base_asset, spot_symbol, future_contract,
                   opened_at, spot_open_qty, spot_open_price, spot_open_amount,
                   future_open_qty, future_open_price, future_open_contracts,
                   open_spread_bps, open_reason
            FROM mi_trade_position
            WHERE base_asset = %s AND opened_at BETWEEN %s AND %s
            ORDER BY opened_at DESC LIMIT 1
        """, (base_asset, time_start, time_end))
        position = cursor.fetchone()

        if not position:
            print("  ⚠️ 未找到持仓记录")
            continue

        print(f"\n  📌 持仓记录 (position_id={position['id']}):")
        print(f"     开仓时间: {position['opened_at']}")
        print(f"     现货成交价: {position['spot_open_price']}")
        print(f"     期货成交价: {position['future_open_price']}")
        print(f"     实际开仓基差(open_spread_bps): {position['open_spread_bps']} bps")
        print(f"     开仓原因: {position['open_reason']}")

        # 验算实际基差
        actual_basis = calc_basis_bps(position['spot_open_price'], position['future_open_price'])
        print(f"     验算基差: {actual_basis:.2f} bps" if actual_basis else "     验算基差: N/A")

        # 2. 查订单记录（含决策时基差）
        cursor.execute("""
            SELECT order_uuid, market_type, trade_direction, exec_price, exec_qty, exec_amount,
                   open_vwap_basis_bps, reject_reason, executed_at
            FROM mi_trade_order
            WHERE position_id = %s AND order_side = 'open'
            ORDER BY market_type
        """, (position['id'],))
        orders = cursor.fetchall()

        print("\n  📋 订单记录:")
        for order in orders:
            print(f"     [{order['market_type']}] {order['trade_direction']} | "
                  f"exec_price={order['exec_price']} | qty={order['exec_qty']} | "
                  f"amount={order['exec_amount']}")
            print(f"     订单表中的 open_vwap_basis_bps = {order['open_vwap_basis_bps']} bps")

        # 3. 查同一时间段的 VWAP 快照（开仓前后各2分钟）
        snap_start = datetime.strptime(time_start, '%Y-%m-%d %H:%M:%S') - timedelta(minutes=2)
        snap_end = datetime.strptime(time_end, '%Y-%m-%d %H:%M:%S') + timedelta(minutes=2)
        cursor.execute("""
            SELECT snapshot_time, base_asset,
                   spot_open_vwap, future_open_vwap, open_vwap_basis_bps,
                   spot_close_vwap, future_close_vwap, close_vwap_basis_bps,
                   open_coverage
            FROM mi_vwap_basis_snapshot
            WHERE base_asset = %s AND snapshot_time BETWEEN %s AND %s
            ORDER BY snapshot_time
        """, (base_asset, snap_start, snap_end))
        snapshots = cursor.fetchall()

        print("\n  📊 VWAP快照(开仓前后±2min):")
        if snapshots:
            print(f"     {'时间':<22} {'spot_open_vwap':<16} {'future_open_vwap':<16} {'open_basis_bps':<14} {'coverage'}")
            for snap in snapshots:
                print(f"     {str(snap['snapshot_time']):<22} "
                      f"{snap['spot_open_vwap'] or 'N/A':<16} "
                      f"{snap['future_open_vwap'] or 'N/A':<16} "
                      f"{snap['open_vwap_basis_bps'] or 'N/A':<14} "
                      f"{snap['open_coverage'] or 'N/A'}")
        else:
            print("     无快照数据")

        # 4. 分析差异
        print("\n  🔍 差异分析:")
        if orders:
            # 从开仓原因中提取决策时基差
            reason = position['open_reason'] or ''
            # 提取 "基差XX.Xbps" 中的数字
            import re
            basis_match = re.search(r'基差([\d.]+)bps', reason)
            decision_basis = float(basis_match.group(1)) if basis_match else None

            if decision_basis is not None and actual_basis is not None:
                delta = decision_basis - actual_basis
                print(f"     决策时基差: +{decision_basis:.1f} bps (from open_reason)")
                print(f"     实际成交基差: {actual_basis:.2f} bps (from exec_price)")
                print(f"     差异: {delta:.1f} bps !!!")
                print()
                print(f"     ⚠️ 决策时盘口数据显示期货升水{decision_basis:.1f}bps")
                print(f"        但实际成交显示期货贴水{abs(actual_basis):.1f}bps")
                print("        说明本地订单簿中的Gate期货bid价格严重高估!")
                print()

                # 反推决策时的预期价格
                spot_price = float(position['spot_open_price'])
                expected_future_bid = spot_price * (1 + decision_basis / 10000)
                actual_future_price = float(position['future_open_price'])
                price_diff_pct = (expected_future_bid - actual_future_price) / actual_future_price * 100

                print("     按决策时基差反推:")
                print(f"       预期期货bid价: {expected_future_bid:.10f}")
                print(f"       实际期货成交价: {actual_future_price:.10f}")
                print(f"       价格偏差: {price_diff_pct:.3f}%")

    cursor.close()
    conn.close()


def analyze_close_trades():
    """分析平仓交易的基差差异"""
    print("\n\n")
    print("=" * 100)
    print("【二、平仓交易分析】")
    print("=" * 100)

    targets = [
        ('ALLO', '2026-06-03 13:00:30', '2026-06-03 13:01:05'),
        ('EPIC', '2026-06-03 13:01:00', '2026-06-03 13:01:10'),
    ]

    conn = get_connection()
    cursor = conn.cursor()

    for base_asset, time_start, time_end in targets:
        print(f"\n{'─' * 80}")
        print(f"  交易对: {base_asset}")
        print(f"{'─' * 80}")

        # 1. 查最近平仓的持仓记录
        cursor.execute("""
            SELECT id, order_uuid, base_asset, status,
                   opened_at, closed_at,
                   spot_open_price, future_open_price, open_spread_bps,
                   spot_close_price, future_close_price, close_spread_bps,
                   close_reason, open_reason,
                   funding_total_pnl, funding_payments_count
            FROM mi_trade_position
            WHERE base_asset = %s AND status = 'closed' AND closed_at BETWEEN %s AND %s
            ORDER BY closed_at DESC LIMIT 1
        """, (base_asset, time_start, time_end))
        position = cursor.fetchone()

        if not position:
            print("  ⚠️ 未找到已平仓记录")
            continue

        print(f"\n  📌 持仓记录 (position_id={position['id']}):")
        print(f"     开仓时间: {position['opened_at']}")
        print(f"     平仓时间: {position['closed_at']}")
        print("     --- 开仓侧 ---")
        print(f"     现货开仓价: {position['spot_open_price']}")
        print(f"     期货开仓价: {position['future_open_price']}")
        print(f"     开仓基差(DB): {position['open_spread_bps']} bps")
        actual_open_basis = calc_basis_bps(position['spot_open_price'], position['future_open_price'])
        print(f"     开仓基差(验算): {actual_open_basis:.2f} bps" if actual_open_basis else "")
        print("     --- 平仓侧 ---")
        print(f"     现货平仓价: {position['spot_close_price']}")
        print(f"     期货平仓价: {position['future_close_price']}")
        print(f"     平仓基差(DB): {position['close_spread_bps']} bps")
        actual_close_basis = calc_basis_bps(position['spot_close_price'], position['future_close_price'])
        print(f"     平仓基差(验算): {actual_close_basis:.2f} bps" if actual_close_basis else "")
        print("     --- 盈亏 ---")
        if actual_open_basis is not None and actual_close_basis is not None:
            convergence_pnl = actual_open_basis - actual_close_basis
            print(f"     实际基差收敛盈亏: {convergence_pnl:.2f} bps ({'盈利' if convergence_pnl > 0 else '亏损'}!)")
        print(f"     资金费收益: {position['funding_total_pnl']} USDT ({position['funding_payments_count']}次)")
        print(f"     平仓原因: {position['close_reason']}")

        # 2. 查平仓订单
        cursor.execute("""
            SELECT market_type, trade_direction, exec_price, exec_qty, exec_amount, reject_reason
            FROM mi_trade_order
            WHERE position_id = %s AND order_side = 'close' AND status = 'executed'
            ORDER BY market_type
        """, (position['id'],))
        close_orders = cursor.fetchall()

        print("\n  📋 平仓订单:")
        for order in close_orders:
            print(f"     [{order['market_type']}] {order['trade_direction']} | "
                  f"exec_price={order['exec_price']} | qty={order['exec_qty']}")

        # 3. 查VWAP快照
        snap_start = position['closed_at'] - timedelta(minutes=2) if position['closed_at'] else None
        snap_end = position['closed_at'] + timedelta(minutes=2) if position['closed_at'] else None
        if snap_start:
            cursor.execute("""
                SELECT snapshot_time, 
                       spot_open_vwap, future_open_vwap, open_vwap_basis_bps,
                       spot_close_vwap, future_close_vwap, close_vwap_basis_bps
                FROM mi_vwap_basis_snapshot
                WHERE base_asset = %s AND snapshot_time BETWEEN %s AND %s
                ORDER BY snapshot_time
            """, (base_asset, snap_start, snap_end))
            snapshots = cursor.fetchall()

            print("\n  📊 VWAP快照(平仓前后±2min):")
            if snapshots:
                print(f"     {'时间':<22} {'spot_close_vwap':<16} {'future_close_vwap':<16} {'close_basis_bps':<14}")
                for snap in snapshots:
                    print(f"     {str(snap['snapshot_time']):<22} "
                          f"{snap['spot_close_vwap'] or 'N/A':<16} "
                          f"{snap['future_close_vwap'] or 'N/A':<16} "
                          f"{snap['close_vwap_basis_bps'] or 'N/A':<14}")
            else:
                print("     无快照数据")

        # 4. 分析
        print("\n  🔍 差异分析:")
        reason = position['close_reason'] or ''
        # 从 close_reason 提取止盈时判断的总盈亏
        import re
        pnl_match = re.search(r'总盈亏([\d.]+)bps', reason)
        signal_pnl = float(pnl_match.group(1)) if pnl_match else None

        if signal_pnl and actual_open_basis is not None and actual_close_basis is not None:
            actual_convergence = actual_open_basis - actual_close_basis
            print(f"     信号认为的总盈亏: {signal_pnl:.1f} bps")
            print(f"     实际基差收敛: {actual_convergence:.2f} bps")
            print(f"     差异: {signal_pnl - actual_convergence:.1f} bps !!!")
            print()
            print("     ⚠️ 平仓信号使用的 current_spread_bps 来自实时盘口，")
            print("        该值认为基差已大幅收敛（负向），但实际成交价格显示并非如此。")
            print("        说明本地订单簿中的「平仓侧VWAP基差」与真实市场严重偏离！")

    cursor.close()
    conn.close()


def analyze_orderbook_quality():
    """分析盘口数据质量 - 查看快照表中开仓前后的盘口变化"""
    print("\n\n")
    print("=" * 100)
    print("【三、盘口数据质量分析 - 快照趋势】")
    print("=" * 100)

    conn = get_connection()
    cursor = conn.cursor()

    # 查看当天所有出问题的标的在11:40-13:10之间的快照
    for base_asset in ['ACH', 'W', 'RIF', 'ALLO', 'EPIC']:
        cursor.execute("""
            SELECT snapshot_time, 
                   spot_open_vwap, future_open_vwap, open_vwap_basis_bps,
                   spot_close_vwap, future_close_vwap, close_vwap_basis_bps,
                   open_coverage
            FROM mi_vwap_basis_snapshot
            WHERE base_asset = %s 
              AND snapshot_time BETWEEN '2026-06-03 11:40:00' AND '2026-06-03 13:10:00'
            ORDER BY snapshot_time
        """, (base_asset,))
        snapshots = cursor.fetchall()

        print(f"\n  📈 {base_asset} 盘口快照趋势 ({len(snapshots)} 条):")
        if snapshots:
            print(f"     {'时间':<22} {'open_basis(bps)':<16} {'close_basis(bps)':<16} {'spot_ask_vwap':<14} {'fut_bid_vwap':<14}")
            # 只打印关键时间点的快照（每分钟一条或前后各5条）
            for snap in snapshots[:20]:  # 最多显示20条
                open_b = f"{snap['open_vwap_basis_bps']:.2f}" if snap['open_vwap_basis_bps'] else 'N/A'
                close_b = f"{snap['close_vwap_basis_bps']:.2f}" if snap['close_vwap_basis_bps'] else 'N/A'
                spot_v = f"{snap['spot_open_vwap']:.8f}" if snap['spot_open_vwap'] else 'N/A'
                fut_v = f"{snap['future_open_vwap']:.8f}" if snap['future_open_vwap'] else 'N/A'
                print(f"     {str(snap['snapshot_time']):<22} {open_b:<16} {close_b:<16} {spot_v:<14} {fut_v:<14}")
            if len(snapshots) > 20:
                print(f"     ... 还有 {len(snapshots) - 20} 条")
        else:
            print("     无数据")

    cursor.close()
    conn.close()


def analyze_recent_positions_accuracy():
    """分析最近所有开仓的决策准确性"""
    print("\n\n")
    print("=" * 100)
    print("【四、近期所有开仓的决策基差 vs 实际成交基差对比】")
    print("=" * 100)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, base_asset, opened_at,
               spot_open_price, future_open_price, open_spread_bps,
               open_reason
        FROM mi_trade_position
        WHERE opened_at >= '2026-06-03 00:00:00'
        ORDER BY opened_at
    """)
    positions = cursor.fetchall()

    print(f"\n  2026-06-03 所有开仓 ({len(positions)} 笔):\n")
    print(f"  {'标的':<8} {'开仓时间':<20} {'决策基差(bps)':<14} {'实际基差(bps)':<14} {'差异(bps)':<12} {'结论'}")
    print(f"  {'─' * 90}")

    import re
    for pos in positions:
        actual_basis = calc_basis_bps(pos['spot_open_price'], pos['future_open_price'])
        reason = pos['open_reason'] or ''
        basis_match = re.search(r'基差([\d.]+)bps', reason)
        decision_basis = float(basis_match.group(1)) if basis_match else None

        if decision_basis and actual_basis is not None:
            delta = decision_basis - actual_basis
            conclusion = "✓ 正常" if abs(delta) < 10 else "⚠️ 偏离" if abs(delta) < 50 else "❌ 严重偏离"
            print(f"  {pos['base_asset']:<8} {str(pos['opened_at']):<20} "
                  f"{decision_basis:>+10.1f}    {actual_basis:>+10.2f}    {delta:>+10.1f}   {conclusion}")
        else:
            print(f"  {pos['base_asset']:<8} {str(pos['opened_at']):<20} "
                  f"{'N/A':>10}    {actual_basis if actual_basis else 'N/A':>10}    {'N/A':>10}")

    cursor.close()
    conn.close()


def analyze_recent_closes_accuracy():
    """分析最近所有平仓的决策准确性"""
    print("\n\n")
    print("=" * 100)
    print("【五、近期所有平仓的信号盈亏 vs 实际盈亏对比】")
    print("=" * 100)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, base_asset, opened_at, closed_at,
               spot_open_price, future_open_price, open_spread_bps,
               spot_close_price, future_close_price, close_spread_bps,
               close_reason, funding_total_pnl
        FROM mi_trade_position
        WHERE status = 'closed' AND closed_at >= '2026-06-03 00:00:00'
        ORDER BY closed_at
    """)
    positions = cursor.fetchall()

    print(f"\n  2026-06-03 所有平仓 ({len(positions)} 笔):\n")
    print(f"  {'标的':<8} {'平仓时间':<20} {'开仓基差':<10} {'平仓基差':<10} {'收敛盈亏':<10} {'信号盈亏':<10} {'差异':<10}")
    print(f"  {'─' * 90}")

    import re
    for pos in positions:
        open_basis = calc_basis_bps(pos['spot_open_price'], pos['future_open_price'])
        close_basis = calc_basis_bps(pos['spot_close_price'], pos['future_close_price'])
        convergence = (open_basis - close_basis) if (open_basis is not None and close_basis is not None) else None

        reason = pos['close_reason'] or ''
        pnl_match = re.search(r'总盈亏([\d.]+)bps', reason)
        signal_pnl = float(pnl_match.group(1)) if pnl_match else None

        if convergence is not None:
            print(f"  {pos['base_asset']:<8} {str(pos['closed_at']):<20} "
                  f"{open_basis:>+8.2f}  {close_basis:>+8.2f}  {convergence:>+8.2f}  "
                  f"{signal_pnl if signal_pnl else 'N/A':>8}  "
                  f"{(signal_pnl - convergence) if signal_pnl else 'N/A':>8}")

    cursor.close()
    conn.close()


if __name__ == '__main__':
    print("┌─────────────────────────────────────────────────────────┐")
    print("│   开仓/平仓 VWAP基差差异分析报告                        │")
    print("│   日期: 2026-06-03                                      │")
    print("└─────────────────────────────────────────────────────────┘\n")

    analyze_open_trades()
    analyze_close_trades()
    analyze_orderbook_quality()
    analyze_recent_positions_accuracy()
    analyze_recent_closes_accuracy()

    print("\n\n")
    print("=" * 100)
    print("【结论与根因推测】")
    print("=" * 100)
    print("""
  根据代码分析，差异来源于以下环节：

  1. 决策时基差计算链路:
     ETL管道/旁路风控 → merge_orderbook → calculate_hedge_metrics 
     → spot_open_vwap (spot ask侧) / future_open_vwap (future bid侧)
     → open_vwap_basis_bps = (future_bid_vwap - spot_ask_vwap) / spot_ask_vwap * 10000

  2. 实际成交基差计算链路:
     RealExecutor → Binance现货市价单 / Gate期货市价单 → 交易所返回成交价
     → actual_basis_bps = (future_exec_price - spot_exec_price) / spot_exec_price * 10000

  关键差异点:
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │ 决策时的 future_open_vwap 来自 Gate本地订单簿的bid侧5档加权均价           │
  │ 实际成交的 future_exec_price 来自 Gate交易所返回的真实fill_price           │
  │                                                                             │
  │ 如果本地维护的Gate期货订单簿与真实盘口严重偏离（如快照过期/WS断连后         │
  │ 重建数据不一致），就会出现"决策时看到大基差，实际成交价格完全不同"的现象。 │
  └─────────────────────────────────────────────────────────────────────────────┘

  可能的根因:
  a) Gate期货WS订单簿增量更新丢失/乱序，导致本地盘口价格偏离
  b) 快照重建后WS增量尚未追平，短暂出现"老快照+新增量"的脏数据状态
  c) update_count闸虽然存在，但闸值(sustain_sec×2=6)可能不够严格
  d) 即使lag<200ms，也不能保证盘口内容正确（时间新但数据可能已偏）

  建议排查:
  - 在开仓成交后立即用 REST API 查询Gate盘口，对比本地维护的盘口
  - 分析日志中是否有大量"快照重新加载"发生在开仓附近时间
  - 检查 update_count 增量是否出现跳跃（表示有WS消息丢失）
""")
