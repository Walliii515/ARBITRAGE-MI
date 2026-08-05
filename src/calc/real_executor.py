# coding: utf-8
"""
真实成交引擎模块

对接 Binance 现货 + Gate 期货的真实下单 API。
与 VirtualExecutor 暴露完全相同的 execute() 接口契约，
TradingExecutor 通过 ExecutorClient 调用时无需关心底层实现。

设计要点：
1. 默认并发双腿下单，降低基差滑点
2. 开仓可切换为 Gate future post-only maker → Binance spot taker hedge
3. 支持 Testnet / Mainnet 通过配置切换
"""
import time
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from common.logger import get_logger
from common.tools import truncate_to_precision

logger = get_logger(__name__)

GATE_CROSS_MARGIN_LEVERAGE = 0.0


@dataclass
class ExchangeConfig:
    """交易所 API 配置（通过 dataclass 注入，符合 calc 模块配置注入规范）"""
    # Binance
    binance_base_url: str = 'https://testnet.binance.vision'
    binance_api_key: str = ''
    binance_api_secret: str = ''
    # Gate
    gate_base_url: str = 'https://fx-api-testnet.gateio.ws'
    gate_api_key: str = ''
    gate_api_secret: str = ''
    # 通用
    timeout_sec: int = 10
    # 环境标识
    env: str = 'testnet'  # testnet / mainnet


class RealExecutor:
    """
    真实成交引擎

    职责：接收订单组 + 盘口数据，向交易所发送真实市价单并返回成交结果。
    接口契约与 VirtualExecutor.execute() 完全一致。
    """

    def __init__(self, exchange_config: ExchangeConfig, contract_meta: Dict = None,
                 spot_meta: Dict = None, leverage: int = 2):
        """
        Args:
            exchange_config: 交易所 API 配置
            contract_meta: base_asset -> {quanto_multiplier, ...} 用于期货张数换算
            spot_meta: base_asset -> {step_size, min_qty, ...} 用于现货精度截断
            leverage: 期货杠杆倍数（>0 为逐仓模式，0 为全仓模式）
        """
        self.config = exchange_config
        self.contract_meta = contract_meta or {}
        self.spot_meta = spot_meta or {}
        self.leverage = leverage
        self._leverage_set: set = set()  # 已设置过杠杆的合约（避免重复调用）
        self._session = requests.Session()
        self._binance_price_cache: Dict[str, tuple[float, float]] = {}
        logger.info(
            f'RealExecutor 已初始化: env={self.config.env}, '
            f'binance={self.config.binance_base_url}, '
            f'gate={self.config.gate_base_url}, '
            f'gate_margin_mode={self._gate_margin_mode_label()}'
        )

    def execute(self, order_group: Dict, orderbook_row: Dict) -> Dict:
        """
        执行真实成交

        流程：
        1. 并发下 Binance 现货单 和 Gate 期货单（降低基差滑点）
        2. 两边都成功则整体成功
        3. 一边失败则标记单边成交风险（需人工处理）

        Args:
            order_group: 订单组，包含 spot_order 和 future_order
            orderbook_row: 当前盘口数据行（用于日志记录，实际成交由交易所决定）

        Returns:
            与 VirtualExecutor 完全一致的格式:
            {
                'success': bool,
                'message': str,
                'spot_order': {exec_price, exec_qty, exec_amount, coverage_ratio} | None,
                'future_order': {exec_price, exec_qty, exec_amount, coverage_ratio} | None
            }
        """
        result = {'success': False, 'spot_order': None, 'future_order': None, 'message': ''}

        try:
            spot_order = order_group.get('spot_order', {})
            future_order = order_group.get('future_order', {})

            if order_group.get('execution_sequence') == 'future_then_spot':
                return self._execute_future_then_spot(order_group, orderbook_row)

            if self._is_future_maker_order(future_order):
                return self._execute_future_maker_then_spot(order_group, orderbook_row)

            leverage_ok, leverage_reason = self._ensure_open_leverage(future_order)
            if not leverage_ok:
                result['message'] = leverage_reason
                logger.warning(
                    f"真实开仓预检失败 | {future_order.get('future_contract')} | {leverage_reason}"
                )
                return result

            # ── 并发下单：同时向 Binance 和 Gate 发送市价单 ──
            with ThreadPoolExecutor(max_workers=2) as pool:
                spot_future = pool.submit(self._place_binance_spot_order, spot_order)
                gate_future = pool.submit(self._place_gate_futures_order, future_order)

                spot_result = spot_future.result()
                future_result = gate_future.result()

            # ── 判定结果 ──
            if spot_result['success'] and future_result['success']:
                # 两边都成功
                result['spot_order'] = spot_result
                result['future_order'] = future_result
                result['success'] = True
                result['message'] = '成交成功'
                logger.info(
                    f"真实成交成功(并发) | {spot_order.get('base_asset')} | "
                    f"spot: price={spot_result['exec_price']}, qty={spot_result['exec_qty']} | "
                    f"future: price={future_result['exec_price']}, qty={future_result['exec_qty']}"
                )
            elif spot_result['success'] and not future_result['success']:
                # 现货成功但期货失败 — 严重情况
                result['spot_order'] = spot_result
                result['message'] = (
                    f"期货拒单(现货已成交,需人工处理): {future_result['reason']} | "
                    f"spot_exec: price={spot_result.get('exec_price')}, "
                    f"qty={spot_result.get('exec_qty')}"
                )
                logger.critical(
                    f"⚠️ 单边成交风险 | {spot_order.get('base_asset')} | "
                    f"现货已成交但期货失败: {future_result['reason']}"
                )
            elif not spot_result['success'] and future_result['success']:
                # 期货成功但现货失败 — 严重情况
                result['future_order'] = future_result
                result['message'] = (
                    f"现货拒单(期货已成交,需人工处理): {spot_result['reason']} | "
                    f"future_exec: price={future_result.get('exec_price')}, "
                    f"qty={future_result.get('exec_qty')}"
                )
                logger.critical(
                    f"⚠️ 单边成交风险 | {spot_order.get('base_asset')} | "
                    f"期货已成交但现货失败: {spot_result['reason']}"
                )
            else:
                # 两边都失败
                result['message'] = (
                    f"双边拒单: 现货({spot_result['reason']}), "
                    f"期货({future_result['reason']})"
                )

        except Exception as e:
            result['message'] = f"系统异常: {str(e)}"
            logger.error(f"RealExecutor 异常: {e}", exc_info=True)

        return result

    def _execute_future_then_spot(self, order_group: Dict, orderbook_row: Dict) -> Dict:
        """先平 Gate 期货腿，再按实际成交量卖 Binance 现货。"""
        result = {'success': False, 'spot_order': None, 'future_order': None, 'message': ''}
        spot_order = dict(order_group.get('spot_order', {}) or {})
        future_order = dict(order_group.get('future_order', {}) or {})
        close_reason = str(order_group.get('execution_reason') or '')
        if close_reason != 'take_profit' and not order_group.get('allow_protective_close'):
            future_order.pop('protective_price', None)
        future_order.pop('execution_style', None)

        future_result = self._place_gate_futures_order(future_order)
        if not future_result.get('success'):
            result['message'] = (
                f"期货拒单(未执行现货): {future_result.get('reason')}"
            )
            logger.critical(
                "⚠️ 平仓期货腿失败，已跳过现货腿 | %s | reason=%s",
                future_order.get('base_asset'), future_result.get('reason'),
            )
            return result

        result['future_order'] = future_result
        future_target_qty = float(future_order.get('target_qty') or 0)
        spot_target_qty = float(spot_order.get('target_qty') or 0)
        hedge_ratio = spot_target_qty / future_target_qty if future_target_qty > 0 else 1.0
        spot_order['target_qty'] = float(future_result.get('exec_qty') or 0) * hedge_ratio
        spot_order['target_amount'] = future_result.get('exec_amount')
        spot_order['quantity_mode'] = 'base'
        spot_result = self._place_binance_spot_order(spot_order)
        if spot_result.get('success'):
            target_qty = float(spot_order.get('target_qty') or 0)
            exec_qty = float(spot_result.get('exec_qty') or 0)
            qty_tolerance = self._spot_close_qty_tolerance(
                spot_order.get('base_asset'),
            )
            if target_qty > 0 and exec_qty + qty_tolerance < target_qty:
                result['spot_order'] = spot_result
                result['message'] = (
                    f"现货部分成交(期货已成交,需人工处理): "
                    f"filled={exec_qty}, target={target_qty} | "
                    f"future_exec: price={future_result.get('exec_price')}, "
                    f"qty={future_result.get('exec_qty')}"
                )
                logger.critical(
                    "⚠️ 平仓现货腿部分成交 | %s | Gate期货已减仓但Binance现货剩余需处理: "
                    "filled=%s target=%s",
                    future_order.get('base_asset'), exec_qty, target_qty,
                )
                return result
            result['spot_order'] = spot_result
            result['success'] = True
            result['message'] = '成交成功'
            execution_style = (
                '保护IOC' if future_order.get('protective_price') is not None else '市价'
            )
            logger.warning(
                "平仓成功(Gate%s优先) | %s | future: price=%s, qty=%s | "
                "spot: price=%s, qty=%s",
                execution_style,
                future_order.get('base_asset'),
                future_result.get('exec_price'), future_result.get('exec_qty'),
                spot_result.get('exec_price'), spot_result.get('exec_qty'),
            )
            return result

        result['message'] = (
            f"现货拒单(期货已成交,需人工处理): {spot_result.get('reason')} | "
            f"future_exec: price={future_result.get('exec_price')}, "
            f"qty={future_result.get('exec_qty')}"
        )
        logger.critical(
            "⚠️ 平仓现货腿失败 | %s | Gate期货已减仓但Binance现货仍需处理: %s",
            future_order.get('base_asset'), spot_result.get('reason'),
        )
        return result

    def _spot_close_qty_tolerance(self, base_asset: str) -> float:
        """允许 Binance 按 step_size 向下截断，不把不可成交尘埃误判为断腿。"""
        step_size = float((self.spot_meta.get(base_asset) or {}).get('step_size') or 0)
        return max(step_size, 1e-12)

    def execute_reverse_open(self, order_group: Dict, orderbook_row: Dict) -> Dict:
        """
        执行反向套利真实开仓。

        方向：
        - Binance Cross Margin 借入 base asset 后卖出
        - Gate USDT 永续买入开多

        本方法只服务反向策略，避免复用正向 spot buy + future sell 的语义。
        """
        result = {
            'success': False,
            'borrow_order': None,
            'spot_order': None,
            'future_order': None,
            'repay_order': None,
            'unwind_order': None,
            'message': '',
        }
        spot_order = dict(order_group.get('spot_order') or {})
        future_order = dict(order_group.get('future_order') or {})
        base_asset = str(spot_order.get('base_asset') or future_order.get('base_asset') or '').upper()
        target_qty = float(spot_order.get('target_qty') or future_order.get('target_qty') or 0)
        if not base_asset or target_qty <= 0:
            result['message'] = f'反向开仓参数无效(base={base_asset}, qty={target_qty})'
            return result

        leverage_ok, leverage_reason = self._ensure_open_leverage(future_order)
        if not leverage_ok:
            result['message'] = leverage_reason
            logger.warning(
                f"反向真实开仓预检失败 | {future_order.get('future_contract')} | {leverage_reason}"
            )
            return result

        borrow_result = self._place_binance_margin_borrow(base_asset, target_qty)
        result['borrow_order'] = borrow_result
        if not borrow_result.get('success'):
            result['message'] = f"借币失败: {borrow_result.get('reason')}"
            return result

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                spot_future = pool.submit(self._place_binance_margin_order, spot_order)
                gate_future = pool.submit(self._place_gate_futures_order, future_order)
                spot_result = spot_future.result()
                future_result = gate_future.result()

            result['spot_order'] = spot_result
            result['future_order'] = future_result

            if spot_result.get('success') and future_result.get('success'):
                result['success'] = True
                result['message'] = '反向开仓成交成功'
                logger.info(
                    f"反向真实开仓成功 | {base_asset} | "
                    f"margin_sell: price={spot_result.get('exec_price')}, qty={spot_result.get('exec_qty')} | "
                    f"future_long: price={future_result.get('exec_price')}, qty={future_result.get('exec_qty')}"
                )
                return result

            if spot_result.get('success') and not future_result.get('success'):
                buyback = self._unwind_margin_spot_leg(spot_order, spot_result)
                result['unwind_order'] = buyback
                if buyback.get('success'):
                    repay = self._place_binance_margin_repay(base_asset, min(
                        float(borrow_result.get('amount') or 0),
                        float(buyback.get('exec_qty') or 0),
                    ))
                    result['repay_order'] = repay
                result['message'] = (
                    f"期货拒单，margin现货已成交并尝试买回: {future_result.get('reason')} | "
                    f"buyback={buyback.get('success')}"
                )
                logger.critical(f"⚠️ 反向单边风险 | {base_asset} | {result['message']}")
                return result

            if future_result.get('success') and not spot_result.get('success'):
                unwind = self._unwind_filled_future_leg(future_order, future_result)
                repay = self._place_binance_margin_repay(base_asset, float(borrow_result.get('amount') or 0))
                result['unwind_order'] = unwind
                result['repay_order'] = repay
                result['message'] = (
                    f"margin现货拒单，future已成交并尝试撤腿: {spot_result.get('reason')} | "
                    f"future_unwind={unwind.get('success')}"
                )
                logger.critical(f"⚠️ 反向单边风险 | {base_asset} | {result['message']}")
                return result

            repay = self._place_binance_margin_repay(base_asset, float(borrow_result.get('amount') or 0))
            result['repay_order'] = repay
            result['message'] = (
                f"双边拒单: margin现货({spot_result.get('reason')}), "
                f"期货({future_result.get('reason')}); repay={repay.get('success')}"
            )
            return result
        except Exception as e:
            result['message'] = f"反向开仓系统异常: {str(e)}"
            logger.error(f"反向 RealExecutor 异常: {e}", exc_info=True)
            return result

    @staticmethod
    def _is_future_maker_order(future_order: Dict) -> bool:
        return (
            future_order.get('market_type') == 'future'
            and future_order.get('execution_style') == 'maker'
        )

    def _execute_future_maker_then_spot(self, order_group: Dict, orderbook_row: Dict) -> Dict:
        """
        Gate future post-only maker 先成交，成交多少就用 Binance spot taker 对冲多少。
        未成交时不动现货，直接放弃本轮开/平仓。
        """
        result = {'success': False, 'spot_order': None, 'future_order': None, 'message': ''}
        spot_order = dict(order_group.get('spot_order') or {})
        future_order = dict(order_group.get('future_order') or {})
        base_asset = future_order.get('base_asset') or spot_order.get('base_asset')
        order_side = future_order.get('order_side', '')

        future_result = self._place_gate_futures_order(future_order)
        stats = future_result.get('execution_stats') or {}
        if not future_result.get('success'):
            fallback_result = self._try_future_maker_fallback_ioc(
                future_order, future_result, stats
            )
            if not fallback_result.get('success'):
                result['future_order'] = future_result if future_result.get('exchange_order_id') else None
                result['message'] = f"future maker未成交/拒单: {fallback_result.get('reason', 'unknown')}"
                result['execution_stats'] = stats
                logger.info(f"future maker 放弃{order_side} | {base_asset} | {result['message']}")
                return result
            future_result = fallback_result
        else:
            maker_stats = stats.setdefault('future_maker', {})
            requested_contracts = int(maker_stats.get('requested_contracts') or 0)
            filled_contracts = int(maker_stats.get('filled_contracts') or 0)
            remaining_contracts = max(requested_contracts - filled_contracts, 0)
            terminal_confirmed = maker_stats.get('terminal_confirmed', True)
            if remaining_contracts > 0 and terminal_confirmed:
                remaining_order = self._build_future_maker_remaining_order(
                    future_order,
                    requested_contracts=requested_contracts,
                    remaining_contracts=remaining_contracts,
                )
                maker_stats['fallback_remaining_contracts'] = remaining_contracts
                fallback_result = self._try_future_maker_fallback_ioc(
                    remaining_order, future_result, stats
                )
                if fallback_result.get('success'):
                    future_result = self._merge_future_execution_results(
                        future_result, fallback_result
                    )

        maker_stats = stats.setdefault('future_maker', {})
        hedge_order = dict(spot_order)
        future_target_qty = float(future_order.get('target_qty') or 0)
        spot_target_qty = float(spot_order.get('target_qty') or 0)
        hedge_ratio = spot_target_qty / future_target_qty if future_target_qty > 0 else 1.0
        hedge_order['target_qty'] = future_result['exec_qty'] * hedge_ratio
        hedge_order['target_amount'] = future_result['exec_amount']
        hedge_order['quantity_mode'] = 'base'
        self._apply_spot_hedge_protection(hedge_order, future_order, future_result, maker_stats)
        spot_result = self._place_binance_spot_order(hedge_order)

        maker_stats['future_exec_price'] = future_result.get('exec_price')
        if spot_result.get('success'):
            maker_stats['spot_exec_price'] = spot_result.get('exec_price')
            maker_stats['spot_protective_ioc_filled'] = bool(hedge_order.get('protective_price'))
            shortfall_qty = self._spot_hedge_shortfall(hedge_order, spot_result)
            if shortfall_qty > 0:
                maker_stats['spot_partial_qty'] = spot_result.get('exec_qty')
                maker_stats['spot_shortfall_qty'] = shortfall_qty
                recovery_order = dict(hedge_order)
                recovery_order['target_qty'] = shortfall_qty
                recovery_order['target_amount'] = (
                    float(future_result.get('exec_price') or 0) * shortfall_qty
                )
                recovery_result = self._recover_failed_spot_hedge(
                    base_asset=base_asset,
                    order_side=order_side,
                    hedge_order=recovery_order,
                    future_order=future_order,
                    future_result=future_result,
                    failed_spot_result={
                        'reason': (
                            f"spot保护IOC部分成交("
                            f"filled={spot_result.get('exec_qty')},target={hedge_order.get('target_qty')})"
                        )
                    },
                    maker_stats=maker_stats,
                    allow_future_unwind=False,
                )
                if recovery_result.get('success'):
                    spot_result = self._merge_spot_hedge_results(spot_result, recovery_result)
                    maker_stats['spot_exec_price'] = spot_result.get('exec_price')
                else:
                    self._neutralize_partial_spot_and_future(
                        spot_order=hedge_order,
                        spot_result=spot_result,
                        future_order=future_order,
                        future_result=future_result,
                        maker_stats=maker_stats,
                    )
                    spot_result = recovery_result
        elif future_result.get('success'):
            spot_result = self._recover_failed_spot_hedge(
                base_asset=base_asset,
                order_side=order_side,
                hedge_order=hedge_order,
                future_order=future_order,
                future_result=future_result,
                failed_spot_result=spot_result,
                maker_stats=maker_stats,
            )

        if spot_result.get('success'):
            future_style = (
                'future maker/fallback IOC'
                if maker_stats.get('fallback_filled')
                else 'future maker'
            )
            result.update({
                'success': True,
                'spot_order': spot_result,
                'future_order': future_result,
                'message': f'成交成功({order_side} {future_style} + spot hedge)',
                'execution_stats': stats,
            })
            logger.info(
                f"真实成交成功({order_side} {future_style} + spot hedge) | {base_asset} | "
                f"fill_ratio={maker_stats.get('fill_ratio', 0):.2f} | "
                f"wait={maker_stats.get('wait_ms', 0):.0f}ms | "
                f"spot: price={spot_result['exec_price']}, qty={spot_result['exec_qty']} | "
                f"future: price={future_result['exec_price']}, qty={future_result['exec_qty']}"
            )
        else:
            unwind_result = maker_stats.get('future_unwind_result') or {}
            spot_unwind_safe = (
                not maker_stats.get('spot_unwind_attempted')
                or maker_stats.get('spot_unwind_filled')
            )
            if maker_stats.get('future_unwind_filled') and spot_unwind_safe:
                result.update({
                    'future_order': future_result,
                    'message': (
                        f"现货对冲失败，future已自动撤腿: {spot_result.get('reason')} | "
                        f"future_exec: price={future_result.get('exec_price')}, "
                        f"qty={future_result.get('exec_qty')} | "
                        f"unwind: price={unwind_result.get('exec_price')}, "
                        f"qty={unwind_result.get('exec_qty')}"
                    ),
                    'execution_stats': stats,
                })
                logger.error(
                    f"future maker 成交后 spot 对冲失败，已自动撤腿 | {base_asset} | "
                    f"spot_reason={spot_result.get('reason')} | unwind={unwind_result}"
                )
                return result
            result.update({
                'future_order': future_result,
                'message': (
                    f"现货拒单(期货maker已成交,自动补救失败需人工处理): {spot_result.get('reason')} | "
                    f"future_exec: price={future_result.get('exec_price')}, "
                    f"qty={future_result.get('exec_qty')} | "
                    f"unwind_reason={unwind_result.get('reason')}"
                ),
                'execution_stats': stats,
            })
            logger.critical(
                f"⚠️ 单边成交风险 | {base_asset} | "
                f"期货maker已成交但现货对冲和future撤腿均失败: "
                f"spot={spot_result.get('reason')} | unwind={unwind_result.get('reason')}"
            )
        return result

    def _recover_failed_spot_hedge(
        self,
        base_asset: str,
        order_side: str,
        hedge_order: Dict,
        future_order: Dict,
        future_result: Dict,
        failed_spot_result: Dict,
        maker_stats: Dict,
        allow_future_unwind: bool = True,
    ) -> Dict:
        """
        Future maker 已成交后，spot 保护 IOC 若未成交，必须立即补救。

        顺序：
        1. 去掉保护价，用 Binance spot 市价单按已成交 base 数量强制补对冲。
        2. 若 spot 仍失败，立刻反向下 Gate futures IOC，把刚成交的 future 腿撤回。
        """
        original_reason = failed_spot_result.get('reason')
        market_hedge_order = dict(hedge_order)
        market_hedge_order.pop('protective_price', None)
        market_hedge_order.pop('order_type', None)
        market_hedge_order['quantity_mode'] = 'base'
        maker_stats['spot_retry_market_attempted'] = True

        retry_result = self._place_binance_spot_order(market_hedge_order)
        maker_stats['spot_retry_market_filled'] = bool(retry_result.get('success'))
        maker_stats['spot_retry_market_price'] = retry_result.get('exec_price')
        maker_stats['spot_retry_market_reason'] = retry_result.get('reason')
        if retry_result.get('success'):
            maker_stats['spot_exec_price'] = retry_result.get('exec_price')
            maker_stats['spot_hedge_recovered'] = True
            logger.warning(
                f"future maker 成交后 spot保护IOC失败，已用spot市价补对冲 | {base_asset} | "
                f"side={order_side} | price={retry_result.get('exec_price')} | "
                f"qty={retry_result.get('exec_qty')}"
            )
            return retry_result

        if not allow_future_unwind:
            retry_result['reason'] = (
                f"{original_reason or retry_result.get('reason')}; "
                f"spot市价补对冲失败: {retry_result.get('reason')}"
            )
            return retry_result

        unwind_result = self._unwind_filled_future_leg(
            future_order=future_order,
            future_result=future_result,
        )
        maker_stats['future_unwind_attempted'] = True
        maker_stats['future_unwind_filled'] = bool(unwind_result.get('success'))
        maker_stats['future_unwind_price'] = unwind_result.get('exec_price')
        maker_stats['future_unwind_qty'] = unwind_result.get('exec_qty')
        maker_stats['future_unwind_reason'] = unwind_result.get('reason')
        maker_stats['future_unwind_result'] = unwind_result
        logger.error(
            f"future maker 成交后 spot市价补对冲失败，尝试future撤腿 | {base_asset} | "
            f"spot_reason={retry_result.get('reason')} | unwind={unwind_result}"
        )
        retry_result['reason'] = (
            f"{original_reason or retry_result.get('reason')}; "
            f"spot市价补对冲失败: {retry_result.get('reason')}"
        )
        return retry_result

    def _neutralize_partial_spot_and_future(
        self,
        spot_order: Dict,
        spot_result: Dict,
        future_order: Dict,
        future_result: Dict,
        maker_stats: Dict,
    ) -> None:
        """spot 部分成交但剩余补不上时，撤回本轮已经成交的 spot 和 future。"""
        spot_unwind_result = self._unwind_spot_leg(spot_order, spot_result)
        maker_stats['spot_unwind_attempted'] = True
        maker_stats['spot_unwind_filled'] = bool(spot_unwind_result.get('success'))
        maker_stats['spot_unwind_price'] = spot_unwind_result.get('exec_price')
        maker_stats['spot_unwind_qty'] = spot_unwind_result.get('exec_qty')
        maker_stats['spot_unwind_reason'] = spot_unwind_result.get('reason')
        maker_stats['spot_unwind_result'] = spot_unwind_result

        unwind_result = self._unwind_filled_future_leg(
            future_order=future_order,
            future_result=future_result,
        )
        maker_stats['future_unwind_attempted'] = True
        maker_stats['future_unwind_filled'] = bool(unwind_result.get('success'))
        maker_stats['future_unwind_price'] = unwind_result.get('exec_price')
        maker_stats['future_unwind_qty'] = unwind_result.get('exec_qty')
        maker_stats['future_unwind_reason'] = unwind_result.get('reason')
        maker_stats['future_unwind_result'] = unwind_result

    def _unwind_spot_leg(self, spot_order: Dict, spot_result: Dict) -> Dict:
        exec_qty = float(spot_result.get('exec_qty') or 0)
        if exec_qty <= 0:
            return {'success': True, 'reason': 'spot无成交无需撤腿'}
        original_direction = spot_order.get('trade_direction')
        reverse_order = dict(spot_order)
        reverse_order.pop('protective_price', None)
        reverse_order.pop('order_type', None)
        reverse_order['trade_direction'] = 'sell' if original_direction == 'buy' else 'buy'
        reverse_order['quantity_mode'] = 'base'
        reverse_order['target_qty'] = exec_qty
        reverse_order['target_amount'] = spot_result.get('exec_amount')
        return self._place_binance_spot_order(reverse_order)

    def _spot_hedge_shortfall(self, hedge_order: Dict, spot_result: Dict) -> float:
        target_qty = float(hedge_order.get('target_qty') or 0)
        exec_qty = float(spot_result.get('exec_qty') or 0)
        shortfall = target_qty - exec_qty
        if shortfall <= 0:
            return 0.0
        tolerance = self._spot_hedge_qty_tolerance(hedge_order.get('base_asset'), target_qty)
        return shortfall if shortfall > tolerance else 0.0

    def _spot_hedge_qty_tolerance(self, base_asset: str, target_qty: float) -> float:
        step_size = float((self.spot_meta.get(base_asset) or {}).get('step_size') or 0)
        # BUY 手续费可能从 base 扣除；给 0.2% 容忍，避免为了手续费尘埃反复补单。
        return max(step_size, abs(float(target_qty or 0)) * 0.002, 1e-12)

    @staticmethod
    def _merge_spot_hedge_results(first: Dict, second: Dict) -> Dict:
        qty1 = float(first.get('exec_qty') or 0)
        qty2 = float(second.get('exec_qty') or 0)
        amount1 = float(first.get('exec_amount') or 0)
        amount2 = float(second.get('exec_amount') or 0)
        total_qty = qty1 + qty2
        total_amount = amount1 + amount2
        merged = dict(second)
        merged['success'] = True
        merged['exec_qty'] = total_qty
        merged['exec_amount'] = total_amount
        merged['exec_price'] = total_amount / total_qty if total_qty > 0 else 0
        ids = [str(v) for v in (first.get('exchange_order_id'), second.get('exchange_order_id')) if v]
        if ids:
            merged['exchange_order_id'] = ','.join(ids)
        fee1 = first.get('fee_amount_usdt')
        fee2 = second.get('fee_amount_usdt')
        if fee1 is not None and fee2 is not None:
            merged['fee_amount_usdt'] = float(fee1) + float(fee2)
        return merged

    @staticmethod
    def _merge_future_execution_results(first: Dict, second: Dict) -> Dict:
        """Merge a confirmed maker fill and its residual fallback fill."""
        qty1 = float(first.get('exec_qty') or 0)
        qty2 = float(second.get('exec_qty') or 0)
        amount1 = float(first.get('exec_amount') or 0)
        amount2 = float(second.get('exec_amount') or 0)
        total_qty = qty1 + qty2
        total_amount = amount1 + amount2
        merged = dict(second)
        merged['success'] = True
        merged['exec_qty'] = total_qty
        merged['exec_amount'] = total_amount
        merged['exec_price'] = total_amount / total_qty if total_qty > 0 else 0
        ids = [
            str(value)
            for value in (first.get('exchange_order_id'), second.get('exchange_order_id'))
            if value
        ]
        if ids:
            merged['exchange_order_ids'] = ids
        stats = first.get('execution_stats') or second.get('execution_stats') or {}
        maker_stats = stats.setdefault('future_maker', {})
        maker_stats['fallback_exchange_order_id'] = second.get('exchange_order_id')
        merged['execution_stats'] = stats
        fees = [
            float(value)
            for value in (first.get('fee_amount_usdt'), second.get('fee_amount_usdt'))
            if value is not None
        ]
        if fees:
            merged['fee_amount_usdt'] = sum(fees)
        return merged

    @staticmethod
    def _build_future_maker_remaining_order(
        maker_order: Dict,
        requested_contracts: int,
        remaining_contracts: int,
    ) -> Dict:
        remaining_order = dict(maker_order)
        ratio = remaining_contracts / requested_contracts if requested_contracts > 0 else 0
        remaining_order['target_qty'] = float(maker_order.get('target_qty') or 0) * ratio
        remaining_order['target_amount'] = float(maker_order.get('target_amount') or 0) * ratio
        return remaining_order

    def _unwind_filled_future_leg(self, future_order: Dict, future_result: Dict) -> Dict:
        """反向 IOC 撤回已成交的 future 腿，避免留下裸仓。"""
        exec_qty = float(future_result.get('exec_qty') or 0)
        if exec_qty <= 0:
            return {'success': False, 'reason': 'future撤腿数量无效'}

        original_direction = future_order.get('trade_direction')
        reverse_direction = 'buy' if original_direction == 'sell' else 'sell'
        original_order_side = future_order.get('order_side')
        reverse_order_side = 'close' if original_order_side == 'open' else 'open'

        unwind_order = {
            key: value
            for key, value in future_order.items()
            if not str(key).startswith('maker_')
        }
        unwind_order.pop('execution_style', None)
        unwind_order.pop('protective_price', None)
        unwind_order['trade_direction'] = reverse_direction
        unwind_order['order_side'] = reverse_order_side
        unwind_order['target_qty'] = exec_qty
        unwind_order['target_amount'] = future_result.get('exec_amount')
        return self._place_gate_futures_order(unwind_order)

    def _apply_spot_hedge_protection(
        self,
        hedge_order: Dict,
        future_order: Dict,
        future_result: Dict,
        maker_stats: Dict,
    ) -> None:
        """Future maker 开仓成交后，用真实 future 成交价反推 spot BUY 最高IOC价。"""
        if not future_order.get('maker_spot_hedge_protective_ioc_enabled'):
            return
        if future_order.get('order_side') != 'open':
            return
        if hedge_order.get('trade_direction') != 'buy':
            return

        try:
            future_exec_price = float(future_result.get('exec_price') or 0)
            min_basis_bps = float(future_order.get('maker_spot_hedge_min_basis_bps') or 0)
        except (TypeError, ValueError):
            return
        if future_exec_price <= 0:
            return

        max_spot_price = future_exec_price / (1 + min_basis_bps / 10000.0)
        if max_spot_price <= 0:
            return
        hedge_order['protective_price'] = max_spot_price
        hedge_order['order_type'] = 'LIMIT_IOC'
        maker_stats['spot_protective_ioc'] = True
        maker_stats['spot_protective_price'] = max_spot_price
        maker_stats['spot_protective_min_basis_bps'] = min_basis_bps

    def _try_future_maker_fallback_ioc(
        self,
        maker_order: Dict,
        maker_result: Dict,
        stats: Dict,
    ) -> Dict:
        """Future maker 未成交后的兜底。

        开仓仍使用保护价 IOC，避免反向滑点吃掉入场边际；平仓 fallback
        使用 Gate 市价语义（price=0 + tif=ioc），优先完整退出目标数量。
        """
        maker_stats = stats.setdefault('future_maker', {})
        maker_stats['fallback_ioc_enabled'] = bool(
            maker_order.get('maker_fallback_ioc_enabled')
        )
        maker_stats['fallback_protective_price'] = maker_order.get(
            'maker_fallback_protective_price'
        )
        maker_stats['fallback_min_basis_bps'] = maker_order.get(
            'maker_fallback_min_basis_bps'
        )
        maker_stats['fallback_current_basis_bps'] = maker_order.get(
            'maker_fallback_current_basis_bps'
        )
        maker_stats['fallback_attempted'] = False
        maker_stats['fallback_filled'] = False

        if maker_stats.get('fill_state_uncertain'):
            maker_stats['fallback_reason'] = 'maker_fill_state_uncertain'
            return {
                'success': False,
                'reason': (
                    f"{maker_result.get('reason', 'future maker状态未知')}; "
                    '为防止重复成交，已禁止fallback'
                ),
            }

        use_market_fallback = maker_order.get('order_side') == 'close'
        maker_stats['fallback_market'] = bool(use_market_fallback)

        protective_price = maker_order.get('maker_fallback_protective_price')
        if not maker_order.get('maker_fallback_ioc_enabled'):
            maker_stats['fallback_reason'] = 'disabled_or_no_protective_price'
            return {'success': False, 'reason': maker_result.get('reason', 'future maker未成交')}
        if not use_market_fallback and protective_price is None:
            maker_stats['fallback_reason'] = 'disabled_or_no_protective_price'
            return {'success': False, 'reason': maker_result.get('reason', 'future maker未成交')}

        fallback_order = dict(maker_order)
        fallback_order.pop('execution_style', None)
        fallback_order.pop('maker_ttl_ms', None)
        fallback_order.pop('maker_price', None)
        fallback_order.pop('maker_price_source', None)
        if use_market_fallback:
            fallback_order.pop('protective_price', None)
            fallback_order.pop('maker_fallback_protective_price', None)
        else:
            fallback_order['protective_price'] = protective_price
        maker_stats['fallback_attempted'] = True

        fallback_result = self._place_gate_futures_order(fallback_order)
        maker_stats['fallback_filled'] = bool(fallback_result.get('success'))
        maker_stats['fallback_future_exec_price'] = fallback_result.get('exec_price')
        if not fallback_result.get('success'):
            maker_stats['fallback_reason'] = fallback_result.get('reason')
            fallback_result['reason'] = (
                f"{maker_result.get('reason', 'future maker未成交')}; "
                f"{self._format_fallback_ioc_fail_reason(fallback_result.get('reason'))}"
            )
            return fallback_result

        fallback_stats = fallback_result.setdefault('execution_stats', {})
        fallback_stats['future_maker'] = maker_stats
        return fallback_result

    @staticmethod
    def _format_fallback_ioc_fail_reason(reason: Optional[str]) -> str:
        reason = str(reason or 'unknown')
        if 'IOC未成交' in reason:
            return f'fallback_ioc未成交: {reason}'
        if '成交数据异常' in reason and ('price=0' in reason or 'size=0' in reason):
            return 'fallback_ioc未成交(fill=0)'
        return f'fallback_ioc失败: {reason}'

    # ──────────────────────────────────────────────────────────────────
    # Binance 现货
    # ──────────────────────────────────────────────────────────────────

    def _place_binance_spot_order(self, order: Dict) -> Dict:
        """
        向 Binance 发送现货市价单

        API: POST /api/v3/order
        Auth: HMAC SHA256

        下单模式:
        - BUY（开仓）: 使用 quoteOrderQty（指定花费 USDT 金额），Binance 自动计算可买数量
        - SELL（平仓）: 使用 quantity（指定卖出币数量）
        """
        base_asset = order.get('base_asset', '')
        symbol = f"{base_asset}USDT"
        side = 'BUY' if order.get('trade_direction') == 'buy' else 'SELL'

        # 构造参数
        timestamp = int(time.time() * 1000)
        order_type = 'LIMIT' if order.get('protective_price') is not None else 'MARKET'
        params = {
            'symbol': symbol,
            'side': side,
            'type': order_type,
            'timestamp': timestamp,
            'newClientOrderId': f"arb_{order.get('order_uuid', '')[:8]}_spot",
        }
        if order_type == 'LIMIT':
            protective_price = float(order.get('protective_price') or 0)
            if protective_price <= 0:
                return {'success': False, 'reason': 'spot保护IOC缺少有效保护价'}
            price_precision = self._get_spot_price_precision(base_asset)
            price = truncate_to_precision(protective_price, price_precision)
            if price is None or price <= 0:
                return {'success': False, 'reason': f'spot保护IOC保护价无效({protective_price})'}
            params['price'] = f"{price:.{price_precision}f}"
            params['timeInForce'] = 'IOC'
            params['newOrderRespType'] = 'FULL'

        if order_type == 'LIMIT':
            quantity = float(order.get('target_qty', 0))
            qty_precision = self._get_spot_qty_precision(base_asset)
            quantity = truncate_to_precision(quantity, qty_precision)
            if quantity is None or quantity <= 0:
                return {'success': False, 'reason': f'spot保护IOC数量无效({order.get("target_qty")})'}
            params['quantity'] = str(quantity)
        elif side == 'BUY' and order.get('quantity_mode') == 'base':
            quantity = float(order.get('target_qty', 0))
            qty_precision = self._get_spot_qty_precision(base_asset)
            quantity = truncate_to_precision(quantity, qty_precision)
            params['quantity'] = str(quantity)
        elif side == 'BUY':
            # 开仓: 使用 quoteOrderQty（花费固定 USDT 金额买入）
            # 优势: 精确控制花费、避免 NOTIONAL 和余额不足问题
            quote_amount = order.get('target_amount', 10)
            params['quoteOrderQty'] = str(quote_amount)
        else:
            # 平仓: 使用 quantity（卖出指定数量的币）
            # 必须按 step_size 精度截断，避免 LOT_SIZE 拒单
            quantity = float(order.get('target_qty', 0))
            qty_precision = self._get_spot_qty_precision(base_asset)
            quantity = truncate_to_precision(quantity, qty_precision)
            params['quantity'] = str(quantity)

        # 签名
        query_string = urlencode(params)
        signature = self._binance_sign(query_string)
        params['signature'] = signature

        # 请求
        url = f"{self.config.binance_base_url}/api/v3/order"
        headers = {'X-MBX-APIKEY': self.config.binance_api_key}

        try:
            resp = self._session.post(
                url, params=params, headers=headers,
                timeout=self.config.timeout_sec
            )

            if resp.status_code != 200:
                error_msg = resp.text[:200]
                logger.warning(f"Binance 下单失败 | {symbol} | HTTP {resp.status_code}: {error_msg}")
                return {'success': False, 'reason': f"HTTP {resp.status_code}: {error_msg}"}

            data = resp.json()
            return self._parse_binance_response(data)

        except requests.exceptions.Timeout:
            return {'success': False, 'reason': f'Binance 请求超时({self.config.timeout_sec}s)'}
        except requests.exceptions.ConnectionError as e:
            return {'success': False, 'reason': f'Binance 连接失败: {str(e)[:100]}'}
        except Exception as e:
            return {'success': False, 'reason': f'Binance 异常: {str(e)[:100]}'}

    def place_binance_spot_order(self, order: Dict) -> Dict:
        """公开的 Binance 现货单腿执行入口，用于交易所断腿自动处置。"""
        return self._place_binance_spot_order(order)

    def place_gate_futures_order(self, order: Dict) -> Dict:
        """公开的 Gate 合约单腿执行入口，用于交易所断腿 reduce-only 处置。"""
        return self._place_gate_futures_order(order)

    def _binance_signed_post(self, path: str, params: Dict) -> Dict:
        payload = dict(params)
        payload.setdefault('recvWindow', 5000)
        payload['timestamp'] = int(time.time() * 1000)
        query_string = urlencode(payload)
        payload['signature'] = self._binance_sign(query_string)
        headers = {'X-MBX-APIKEY': self.config.binance_api_key}
        resp = self._session.post(
            f"{self.config.binance_base_url}{path}",
            params=payload,
            headers=headers,
            timeout=self.config.timeout_sec,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Binance {path} HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def _binance_signed_get(self, path: str, params: Optional[Dict] = None) -> Dict:
        payload = dict(params or {})
        payload.setdefault('recvWindow', 5000)
        payload['timestamp'] = int(time.time() * 1000)
        query_string = urlencode(payload)
        payload['signature'] = self._binance_sign(query_string)
        headers = {'X-MBX-APIKEY': self.config.binance_api_key}
        resp = self._session.get(
            f"{self.config.binance_base_url}{path}",
            params=payload,
            headers=headers,
            timeout=self.config.timeout_sec,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Binance {path} HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return data if isinstance(data, dict) else {}

    def _place_binance_margin_borrow(self, asset: str, amount: float) -> Dict:
        """Binance Cross Margin 借币。"""
        asset = str(asset or '').upper()
        qty = self._format_decimal(amount, 12)
        try:
            data = self._binance_signed_post('/sapi/v1/margin/borrow-repay', {
                'asset': asset,
                'amount': qty,
                'type': 'BORROW',
            })
            return {
                'success': True,
                'asset': asset,
                'amount': float(qty),
                'exchange_order_id': str(data.get('tranId') or ''),
                'raw': data,
            }
        except Exception as e:
            logger.warning(f"Binance margin 借币失败 | {asset} | amount={qty} | {e}")
            return {'success': False, 'asset': asset, 'amount': float(amount or 0), 'reason': str(e)[:200]}

    def _place_binance_margin_repay(self, asset: str, amount: float) -> Dict:
        """Binance Cross Margin 还币。失败不抛出，交由上层记录风险。"""
        asset = str(asset or '').upper()
        if float(amount or 0) <= 0:
            return {'success': True, 'asset': asset, 'amount': 0.0, 'reason': '无需还币'}
        qty = self._format_decimal(amount, 12)
        try:
            data = self._binance_signed_post('/sapi/v1/margin/borrow-repay', {
                'asset': asset,
                'amount': qty,
                'type': 'REPAY',
            })
            return {
                'success': True,
                'asset': asset,
                'amount': float(qty),
                'exchange_order_id': str(data.get('tranId') or ''),
                'raw': data,
            }
        except Exception as e:
            logger.warning(f"Binance margin 还币失败 | {asset} | amount={qty} | {e}")
            return {'success': False, 'asset': asset, 'amount': float(amount or 0), 'reason': str(e)[:200]}

    def _place_binance_margin_order(self, order: Dict) -> Dict:
        """Binance Cross Margin 市价/IOC 下单，用于反向现货腿。"""
        base_asset = order.get('base_asset', '')
        symbol = f"{base_asset}USDT"
        side = 'BUY' if order.get('trade_direction') == 'buy' else 'SELL'
        order_type = 'LIMIT' if order.get('protective_price') is not None else 'MARKET'
        params = {
            'symbol': symbol,
            'side': side,
            'type': order_type,
            'newClientOrderId': f"arb_{order.get('order_uuid', '')[:8]}_mspot",
            'newOrderRespType': 'FULL',
        }

        if order_type == 'LIMIT':
            protective_price = float(order.get('protective_price') or 0)
            if protective_price <= 0:
                return {'success': False, 'reason': 'margin spot保护IOC缺少有效保护价'}
            price_precision = self._get_spot_price_precision(base_asset)
            price = truncate_to_precision(protective_price, price_precision)
            if price is None or price <= 0:
                return {'success': False, 'reason': f'margin spot保护价无效({protective_price})'}
            params['price'] = f"{price:.{price_precision}f}"
            params['timeInForce'] = 'IOC'

        quantity = float(order.get('target_qty', 0))
        qty_precision = self._get_spot_qty_precision(base_asset)
        quantity = truncate_to_precision(quantity, qty_precision)
        if quantity is None or quantity <= 0:
            return {'success': False, 'reason': f'margin spot数量无效({order.get("target_qty")})'}
        params['quantity'] = str(quantity)

        try:
            data = self._binance_signed_post('/sapi/v1/margin/order', params)
            return self._parse_binance_response(data)
        except requests.exceptions.Timeout:
            return {'success': False, 'reason': f'Binance margin 请求超时({self.config.timeout_sec}s)'}
        except requests.exceptions.ConnectionError as e:
            return {'success': False, 'reason': f'Binance margin 连接失败: {str(e)[:100]}'}
        except Exception as e:
            return {'success': False, 'reason': f'Binance margin 异常: {str(e)[:200]}'}

    def _unwind_margin_spot_leg(self, spot_order: Dict, spot_result: Dict) -> Dict:
        exec_qty = float(spot_result.get('exec_qty') or 0)
        if exec_qty <= 0:
            return {'success': True, 'reason': 'margin spot无成交无需撤腿'}
        reverse_order = dict(spot_order)
        reverse_order.pop('protective_price', None)
        reverse_order.pop('order_type', None)
        reverse_order['trade_direction'] = 'buy' if spot_order.get('trade_direction') == 'sell' else 'sell'
        reverse_order['quantity_mode'] = 'base'
        reverse_order['target_qty'] = exec_qty
        reverse_order['target_amount'] = spot_result.get('exec_amount')
        return self._place_binance_margin_order(reverse_order)

    def _parse_binance_response(self, data: Dict) -> Dict:
        """
        解析 Binance 订单响应

        市价单响应字段：
        - status: FILLED
        - executedQty: 实际成交数量（总量，未扣手续费）
        - cummulativeQuoteQty: 成交金额
        - fills: [{price, qty, commission, commissionAsset}]

        注意：BUY 时手续费从买到的币中扣除（如买 25 XLM，扣 0.025 XLM 手续费，
        实际到账 24.975）。必须减去手续费，否则平仓卖出时会因余额不足被拒。
        """
        status = data.get('status', '')
        exec_qty = float(data.get('executedQty', 0))
        if status != 'FILLED' and exec_qty <= 0:
            if data.get('timeInForce') == 'IOC' or data.get('type') == 'LIMIT':
                return {
                    'success': False,
                    'reason': f"保护IOC未成交(fill=0,status={status}, orderId={data.get('orderId')})"
                }
            return {
                'success': False,
                'reason': f"订单状态异常: {status}, orderId={data.get('orderId')}"
            }

        exec_amount = float(data.get('cummulativeQuoteQty', 0))

        # 扣除以 base asset 计价的手续费（BUY 时手续费从买到的币中扣除）
        symbol = data.get('symbol', '')
        # base_asset = symbol 去掉末尾的 quote（如 XLMUSDT -> XLM）
        base_asset = symbol.replace('USDT', '') if symbol.endswith('USDT') else symbol
        fills = data.get('fills', [])
        commission_in_base = 0.0
        commission_by_asset = {}
        for fill in fills:
            fee_asset = fill.get('commissionAsset', '')
            commission = float(fill.get('commission', 0))
            if fee_asset == base_asset:
                commission_in_base += commission
            if fee_asset:
                commission_by_asset[fee_asset] = commission_by_asset.get(fee_asset, 0.0) + commission

        # 实际可用数量 = 成交量 - 手续费
        net_qty = exec_qty - commission_in_base
        exec_price = exec_amount / exec_qty if exec_qty > 0 else 0

        if commission_in_base > 0:
            logger.info(
                f"Binance 手续费扣减 | {symbol} | "
                f"gross_qty={exec_qty}, commission={commission_in_base} {base_asset}, "
                f"net_qty={net_qty}"
            )

        fee_asset = None
        fee_amount = None
        fee_amount_usdt = 0.0
        fee_amount_usdt_complete = True
        if len(commission_by_asset) == 1:
            fee_asset, fee_amount = next(iter(commission_by_asset.items()))
        elif len(commission_by_asset) > 1:
            fee_asset = 'MIXED'

        for asset, amount in commission_by_asset.items():
            converted = self._convert_fee_to_usdt(asset, amount, base_asset, exec_price)
            if converted is None:
                fee_amount_usdt_complete = False
                continue
            fee_amount_usdt += converted

        return {
            'success': True,
            'exec_price': exec_price,
            'exec_qty': net_qty,  # 返回扣除手续费后的净量
            'exec_amount': exec_amount,
            'coverage_ratio': 0,  # 实盘无覆盖率概念
            'exchange_order_id': str(data.get('orderId', '')),
            'fee_amount': fee_amount,
            'fee_amount_usdt': round(fee_amount_usdt, 8) if fee_amount_usdt_complete else None,
            'fee_asset': fee_asset,
        }

    @staticmethod
    def _format_decimal(value: float, precision: int = 12) -> str:
        text = f"{float(value):.{precision}f}".rstrip('0').rstrip('.')
        return text if text else '0'

    def _convert_fee_to_usdt(
        self,
        fee_asset: str,
        fee_amount: float,
        base_asset: str,
        exec_price: float,
    ) -> Optional[float]:
        fee_asset = str(fee_asset or '').upper()
        fee_amount = float(fee_amount or 0)
        if fee_amount == 0:
            return 0.0
        if fee_asset == 'USDT':
            return fee_amount
        if fee_asset == str(base_asset or '').upper() and exec_price:
            return fee_amount * float(exec_price)
        price = self._get_binance_usdt_price(fee_asset)
        if price is None:
            logger.warning(f"Binance 手续费USDT折算失败 | asset={fee_asset} | amount={fee_amount}")
            return None
        return fee_amount * price

    def _get_binance_usdt_price(self, asset: str) -> Optional[float]:
        asset = str(asset or '').upper()
        if not asset or asset == 'USDT':
            return 1.0
        symbol = f'{asset}USDT'
        now = time.time()
        cached = self._binance_price_cache.get(symbol)
        if cached and now - cached[1] <= 60:
            return cached[0]

        url = f"{self.config.binance_base_url}/api/v3/ticker/price"
        try:
            resp = self._session.get(
                url,
                params={'symbol': symbol},
                timeout=min(self.config.timeout_sec, 5),
            )
            if resp.status_code != 200:
                logger.warning(f"Binance 价格查询失败 | {symbol} | HTTP {resp.status_code}: {resp.text[:120]}")
                return None
            price = float(resp.json().get('price') or 0)
            if price <= 0:
                return None
            self._binance_price_cache[symbol] = (price, now)
            return price
        except Exception as e:
            logger.warning(f"Binance 价格查询异常 | {symbol} | {e}")
            return None

    def _binance_sign(self, query_string: str) -> str:
        """Binance HMAC SHA256 签名"""
        return hmac.new(
            self.config.binance_api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    def fetch_binance_account_balances(self) -> List[Dict]:
        """
        拉取 Binance 现货账户非零资产余额（只读，资金使用）。

        返回字段保持贴近交易所原始口径：
        [{asset, free, locked, total}]
        """
        timestamp = int(time.time() * 1000)
        params = {'timestamp': timestamp}
        query_string = urlencode(params)
        params['signature'] = self._binance_sign(query_string)

        url = f"{self.config.binance_base_url}/api/v3/account"
        headers = {'X-MBX-APIKEY': self.config.binance_api_key}
        resp = self._session.get(url, params=params, headers=headers, timeout=self.config.timeout_sec)
        if resp.status_code != 200:
            raise RuntimeError(f"Binance account HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        result: List[Dict] = []
        for item in data.get('balances', []):
            asset = str(item.get('asset') or '').upper()
            if not asset:
                continue
            free = float(item.get('free') or 0)
            locked = float(item.get('locked') or 0)
            total = free + locked
            if total == 0:
                continue
            result.append({
                'asset': asset,
                'free': free,
                'locked': locked,
                'total': total,
            })
        return result

    def fetch_binance_cross_margin_account(self) -> Dict:
        """拉取 Binance Cross Margin 账户摘要（只读，正向融资风控使用）。"""
        return self._binance_signed_get('/sapi/v1/margin/account')

    def fetch_binance_spot_balances(self) -> List[Dict]:
        """拉取 Binance 非 USDT 现货余额（对账使用）。"""
        return [b for b in self.fetch_binance_account_balances() if b.get('asset') != 'USDT']

    def fetch_binance_ticker_prices(self, assets: Iterable[str]) -> Dict[str, float]:
        """批量拉取 Binance 现货 USDT 价格（公开接口，资金估值使用）。"""
        symbols = [f"{str(asset).upper()}USDT" for asset in assets if str(asset).upper() != 'USDT']
        if not symbols:
            return {}
        params = {'symbols': json.dumps(symbols)}
        url = f"{self.config.binance_base_url}/api/v3/ticker/price"
        resp = self._session.get(url, params=params, timeout=self.config.timeout_sec)
        if resp.status_code != 200:
            result: Dict[str, float] = {}
            for symbol in symbols:
                single = self._session.get(
                    url,
                    params={'symbol': symbol},
                    timeout=self.config.timeout_sec,
                )
                if single.status_code != 200:
                    logger.debug(f"Binance ticker 跳过 | {symbol} | HTTP {single.status_code}")
                    continue
                item = single.json()
                if symbol.endswith('USDT'):
                    result[symbol[:-4]] = float(item.get('price') or 0)
            return result
        data = resp.json()
        result: Dict[str, float] = {}
        for item in data if isinstance(data, list) else []:
            symbol = str(item.get('symbol') or '').upper()
            if symbol.endswith('USDT'):
                result[symbol[:-4]] = float(item.get('price') or 0)
        return result

    def fetch_binance_my_trades(self, symbol: str, start_time_ms: Optional[int] = None, limit: int = 1000) -> List[Dict]:
        """拉取 Binance 现货账户成交记录（只读，资金快照手续费使用）。"""
        symbol = str(symbol or '').upper()
        if not symbol:
            return []

        all_rows: List[Dict] = []
        from_id: Optional[int] = None
        while True:
            params = {
                'symbol': symbol,
                'limit': min(max(int(limit or 1000), 1), 1000),
                'timestamp': int(time.time() * 1000),
            }
            if start_time_ms is not None and from_id is None:
                params['startTime'] = int(start_time_ms)
            if from_id is not None:
                params['fromId'] = from_id

            query_string = urlencode(params)
            params['signature'] = self._binance_sign(query_string)
            url = f"{self.config.binance_base_url}/api/v3/myTrades"
            headers = {'X-MBX-APIKEY': self.config.binance_api_key}
            resp = self._session.get(url, params=params, headers=headers, timeout=self.config.timeout_sec)
            if resp.status_code != 200:
                raise RuntimeError(f"Binance myTrades {symbol} HTTP {resp.status_code}: {resp.text[:200]}")

            data = resp.json()
            rows = data if isinstance(data, list) else []
            all_rows.extend(rows)
            if len(rows) < params['limit']:
                break
            last_id = rows[-1].get('id')
            if last_id is None:
                break
            from_id = int(last_id) + 1
            if len(all_rows) >= 10000:
                logger.warning(f"Binance myTrades {symbol} reached 10000 row cap for capital snapshot")
                break
        return all_rows

    # ──────────────────────────────────────────────────────────────────
    # Gate 期货
    # ──────────────────────────────────────────────────────────────────

    def _ensure_open_leverage(self, future_order: Dict) -> tuple[bool, str]:
        """开仓前确认 Gate 保证金模式；已有仓位模式不匹配时拒单，避免改动历史仓位。"""
        if future_order.get('order_side') != 'open':
            return True, ''
        base_asset = future_order.get('base_asset', '')
        contract = future_order.get('future_contract') or f"{base_asset}_USDT"
        return self._ensure_leverage(contract)

    def _ensure_leverage(self, contract: str) -> tuple[bool, str]:
        """
        确保合约已设置为目标保证金模式

        Gate API: POST /api/v4/futures/usdt/positions/{contract}/leverage
        - leverage > 0: 逐仓模式（isolated margin），值为杠杆倍数
        - leverage = 0: 全仓模式（cross margin）

        仅在每个合约首次下单前调用一次，后续通过缓存跳过。
        """
        if contract in self._leverage_set:
            return True, ''

        existing = self._gate_existing_position(contract)
        if existing is None:
            msg = f"Gate杠杆设置跳过({contract}:无法确认是否已有仓位)"
            logger.warning(msg)
            return False, msg
        if existing:
            current_leverage = self._float_or_none(existing.get('leverage'))
            if current_leverage is not None and abs(current_leverage - float(self.leverage)) < 1e-9:
                self._leverage_set.add(contract)
                logger.info(f"Gate 已有仓位保证金模式匹配 | {contract} | {self._gate_margin_mode_label()}")
                return True, ''
            msg = (
                f"Gate已有仓位，禁止修改保证金模式 | {contract} | "
                f"current={self._gate_margin_mode_label(current_leverage)},"
                f"target={self._gate_margin_mode_label()}"
            )
            logger.warning(msg)
            return False, msg

        api_path = f'/api/v4/futures/usdt/positions/{contract}/leverage'
        query_string = f'leverage={self.leverage}'
        headers = self._gate_sign('POST', api_path, query_string, '')
        url = f"{self.config.gate_base_url}{api_path}?{query_string}"

        try:
            resp = self._session.post(url, headers=headers, timeout=self.config.timeout_sec)
            if resp.status_code == 200:
                self._leverage_set.add(contract)
                logger.info(f"Gate 保证金模式设置成功 | {contract} | {self._gate_margin_mode_label()}")
                return True, ''
            else:
                msg = f"Gate 杠杆设置失败 | {contract} | HTTP {resp.status_code}: {resp.text[:150]}"
                logger.warning(msg)
                return False, msg
        except Exception as e:
            msg = f"Gate 杠杆设置异常 | {contract} | {e}"
            logger.warning(msg)
            return False, msg

    def _gate_existing_position(self, contract: str) -> Optional[Dict]:
        """返回指定合约的 Gate 实仓；拉取失败返回 None，无持仓返回空 dict。"""
        try:
            for pos in self.fetch_gate_futures_positions():
                if str(pos.get('contract') or '').upper() == str(contract or '').upper():
                    return pos
            return {}
        except Exception as e:
            logger.warning(f"Gate 持仓检查失败 | {contract} | {e}")
            return None

    @staticmethod
    def _float_or_none(value) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _gate_margin_mode_label(self, leverage: Optional[float] = None) -> str:
        value = self._float_or_none(self.leverage if leverage is None else leverage)
        if value is not None and abs(value) < 1e-9:
            return '全仓'
        if value is None:
            return 'unknown'
        return f'逐仓 {value:g}x'

    def _place_gate_futures_order(self, order: Dict) -> Dict:
        """
        向 Gate 发送期货市价单

        API: POST /api/v4/futures/usdt/orders
        Auth: HMAC SHA512
        """
        base_asset = order.get('base_asset', '')
        contract = order.get('future_contract') or f"{base_asset}_USDT"
        target_qty = float(order.get('target_qty', 0))

        # 开仓前确保 Gate 保证金模式；平仓/reduce-only 不修改已有仓位。
        leverage_ok, leverage_reason = self._ensure_open_leverage(order)
        if not leverage_ok:
            return {'success': False, 'reason': leverage_reason}

        # 计算期货张数（空头为负数）
        quanto_multiplier = self._get_quanto_multiplier(base_asset)
        contracts_size = int(target_qty / quanto_multiplier) if quanto_multiplier > 0 else 0

        if contracts_size == 0:
            return {'success': False, 'reason': f'合约张数为0(数量{target_qty}/乘数{quanto_multiplier}), 无法下单'}

        # 期货做空 → size 为负
        direction = order.get('trade_direction', 'sell')
        if direction == 'sell':
            contracts_size = -abs(contracts_size)
        else:
            contracts_size = abs(contracts_size)

        price = '0'
        tif = 'ioc'
        protective_price = order.get('protective_price')
        if protective_price is not None:
            price = self._format_gate_price(base_asset, float(protective_price))
        if self._is_future_maker_order(order):
            maker_price = float(order.get('maker_price') or 0)
            if maker_price <= 0:
                return {'success': False, 'reason': 'future maker缺少有效挂单价'}
            price = self._format_gate_price(base_asset, maker_price)
            tif = 'poc'

        # 构造请求体
        body = {
            'contract': contract,
            'size': contracts_size,
            'price': price,     # 0=市价IOC；非0=保护限价/POC
            'tif': tif,         # ioc=即时成交或取消；poc=post-only
            'text': f"t-arb{order.get('order_uuid', '')[:8]}",
        }

        # 平仓时设置 reduce_only，防止反向开新仓
        if order.get('order_side') == 'close':
            body['reduce_only'] = True
        body_str = json.dumps(body)

        # 签名
        method = 'POST'
        api_path = '/api/v4/futures/usdt/orders'
        headers = self._gate_sign(method, api_path, '', body_str)

        # 请求
        url = f"{self.config.gate_base_url}{api_path}"

        try:
            resp = self._session.post(
                url, data=body_str, headers=headers,
                timeout=self.config.timeout_sec
            )

            if resp.status_code not in (200, 201):
                error_msg = resp.text[:200]
                logger.warning(f"Gate 下单失败 | {contract} | HTTP {resp.status_code}: {error_msg}")
                return {'success': False, 'reason': f"HTTP {resp.status_code}: {error_msg}"}

            data = resp.json()
            if tif == 'poc':
                return self._wait_gate_maker_fill(
                    order=order,
                    initial_order=data,
                    quanto_multiplier=quanto_multiplier,
                    requested_contracts=abs(contracts_size),
                    maker_price=float(price),
                    taker_reference_price=order.get('maker_taker_reference_price'),
                    spot_reference_price=order.get('maker_spot_reference_price'),
                )
            return self._parse_gate_response(data, quanto_multiplier)

        except requests.exceptions.Timeout:
            return {'success': False, 'reason': f'Gate 请求超时({self.config.timeout_sec}s)'}
        except requests.exceptions.ConnectionError as e:
            return {'success': False, 'reason': f'Gate 连接失败: {str(e)[:100]}'}
        except Exception as e:
            return {'success': False, 'reason': f'Gate 异常: {str(e)[:100]}'}

    def _wait_gate_maker_fill(
        self,
        order: Dict,
        initial_order: Dict,
        quanto_multiplier: float,
        requested_contracts: int,
        maker_price: float,
        taker_reference_price: Optional[float] = None,
        spot_reference_price: Optional[float] = None,
    ) -> Dict:
        order_id = str(initial_order.get('id') or '')
        contract = order.get('future_contract') or f"{order.get('base_asset')}_USDT"
        ttl_ms = max(int(order.get('maker_ttl_ms') or 0), 0)
        deadline = time.monotonic() + ttl_ms / 1000.0
        latest = dict(initial_order)

        while order_id and time.monotonic() < deadline:
            filled_contracts = self._gate_filled_contracts(latest)
            if filled_contracts >= requested_contracts:
                break
            sleep_sec = min(0.1, max(deadline - time.monotonic(), 0))
            if sleep_sec > 0:
                time.sleep(sleep_sec)
            fresh = self._get_gate_futures_order(contract, order_id)
            if fresh:
                latest = fresh

        filled_contracts = self._gate_filled_contracts(latest)
        terminal_confirmed = (
            filled_contracts >= requested_contracts
            or str(latest.get('status') or '').lower() == 'finished'
        )
        if order_id and filled_contracts < requested_contracts:
            cancelled = self._cancel_gate_futures_order(contract, order_id)
            if cancelled:
                latest = cancelled
                filled_contracts = self._gate_filled_contracts(latest)
                terminal_confirmed = (
                    filled_contracts >= requested_contracts
                    or str(latest.get('status') or '').lower() == 'finished'
                )
            else:
                reconciled, terminal_confirmed = self._reconcile_gate_maker_terminal_state(
                    contract=contract,
                    order_id=order_id,
                    initial_order=latest,
                    requested_contracts=requested_contracts,
                )
                if reconciled:
                    latest = reconciled
                    filled_contracts = self._gate_filled_contracts(latest)

        elapsed_ms = (ttl_ms / 1000.0 - max(deadline - time.monotonic(), 0)) * 1000.0
        result = self._parse_gate_response(latest, quanto_multiplier, allow_partial=True)
        fill_ratio = (filled_contracts / requested_contracts) if requested_contracts > 0 else 0
        improvement_bps = None
        exec_price = result.get('exec_price')
        if exec_price and taker_reference_price and spot_reference_price:
            if order.get('trade_direction') == 'buy':
                improvement_bps = (float(taker_reference_price) - float(exec_price)) / float(spot_reference_price) * 10000.0
            else:
                improvement_bps = (float(exec_price) - float(taker_reference_price)) / float(spot_reference_price) * 10000.0

        stats = {
            'future_maker': {
                'attempted': True,
                'filled': bool(result.get('success')),
                'order_side': order.get('order_side'),
                'trade_direction': order.get('trade_direction'),
                'fill_ratio': round(fill_ratio, 4),
                'wait_ms': round(elapsed_ms, 0),
                'ttl_ms': ttl_ms,
                'maker_price': maker_price,
                'future_exec_price': exec_price,
                'requested_contracts': requested_contracts,
                'filled_contracts': filled_contracts,
                'remaining_contracts': max(requested_contracts - filled_contracts, 0),
                'terminal_confirmed': terminal_confirmed,
                'fill_state_uncertain': not terminal_confirmed,
                'exchange_order_id': order_id,
                'improvement_bps': round(improvement_bps, 2) if improvement_bps is not None else None,
            }
        }
        result['execution_stats'] = stats
        if not result.get('success'):
            state_suffix = ',终态未确认' if not terminal_confirmed else ''
            result['reason'] = (
                f"future maker未成交(fill={fill_ratio:.0%},wait={elapsed_ms:.0f}ms,"
                f"ttl={ttl_ms}ms,id={order_id}{state_suffix})"
            )
        return result

    def _reconcile_gate_maker_terminal_state(
        self,
        contract: str,
        order_id: str,
        initial_order: Dict,
        requested_contracts: int,
    ) -> Tuple[Optional[Dict], bool]:
        """Recheck a maker order after an ambiguous cancel response.

        A terminal order snapshot is authoritative. Trades can prove how much was
        filled, but a partial trade alone cannot prove that the resting remainder
        is gone, so residual fallback stays disabled in that case.
        """
        latest = self._get_gate_futures_order(contract, order_id)
        if latest:
            filled_contracts = self._gate_filled_contracts(latest)
            terminal = (
                filled_contracts >= requested_contracts
                or str(latest.get('status') or '').lower() == 'finished'
            )
            if terminal:
                return latest, True

        try:
            trades = self.fetch_gate_futures_my_trades(
                contract=contract,
                start_time=int(time.time()) - 60,
                end_time=int(time.time()) + 1,
                limit=1000,
            )
        except Exception as exc:
            logger.warning(
                f"Gate maker终态复核成交查询失败 | {contract} | {order_id} | {exc}"
            )
            return latest, False

        matched = [
            trade for trade in trades
            if str(trade.get('order_id') or trade.get('order') or '') == str(order_id)
        ]
        if not matched:
            return latest, False

        filled_contracts = sum(
            abs(float(trade.get('size') or 0)) for trade in matched
        )
        if filled_contracts <= 0:
            return latest, False
        weighted_amount = sum(
            abs(float(trade.get('size') or 0)) * float(trade.get('price') or 0)
            for trade in matched
        )
        fill_price = weighted_amount / filled_contracts if filled_contracts > 0 else 0
        signed_size = self._gate_int(initial_order.get('size'))
        if signed_size == 0:
            signed_size = requested_contracts
        sign = -1 if signed_size < 0 else 1
        bounded_filled = min(int(round(filled_contracts)), requested_contracts)
        terminal = bounded_filled >= requested_contracts
        synthetic = dict(initial_order)
        synthetic.update({
            'id': order_id,
            'size': sign * requested_contracts,
            'left': sign * max(requested_contracts - bounded_filled, 0),
            'fill_price': str(fill_price),
            'status': 'finished' if terminal else synthetic.get('status', 'open'),
            'finish_as': 'filled' if terminal else synthetic.get('finish_as', 'unknown'),
        })
        logger.warning(
            f"Gate maker撤单状态不明，已按成交明细复核 | {contract} | {order_id} | "
            f"filled={bounded_filled}/{requested_contracts} | terminal={terminal}"
        )
        return synthetic, terminal

    def _get_gate_futures_order(self, contract: str, order_id: str) -> Optional[Dict]:
        method = 'GET'
        api_path = f'/api/v4/futures/usdt/orders/{order_id}'
        headers = self._gate_sign(method, api_path, '', '')
        url = f"{self.config.gate_base_url}{api_path}"
        try:
            resp = self._session.get(url, headers=headers, timeout=self.config.timeout_sec)
            if resp.status_code != 200:
                logger.debug(f"Gate 查询订单失败 | {contract} | {order_id} | HTTP {resp.status_code}: {resp.text[:120]}")
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.debug(f"Gate 查询订单异常 | {contract} | {order_id} | {e}")
            return None

    def _cancel_gate_futures_order(self, contract: str, order_id: str) -> Optional[Dict]:
        method = 'DELETE'
        api_path = f'/api/v4/futures/usdt/orders/{order_id}'
        headers = self._gate_sign(method, api_path, '', '')
        url = f"{self.config.gate_base_url}{api_path}"
        try:
            resp = self._session.delete(url, headers=headers, timeout=self.config.timeout_sec)
            if resp.status_code not in (200, 201):
                logger.warning(f"Gate 撤单失败 | {contract} | {order_id} | HTTP {resp.status_code}: {resp.text[:150]}")
                return None
            data = resp.json() if resp.text else {}
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning(f"Gate 撤单异常 | {contract} | {order_id} | {e}")
            return None

    @staticmethod
    def _gate_int(value) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    def _gate_filled_contracts(self, data: Dict) -> int:
        size = abs(self._gate_int(data.get('size')))
        if data.get('left') is None:
            return size if data.get('status') == 'finished' else 0
        left = abs(self._gate_int(data.get('left')))
        return max(size - left, 0)

    def _parse_gate_response(self, data: Dict, quanto_multiplier: float, allow_partial: bool = False) -> Dict:
        """
        解析 Gate 期货订单响应

        响应字段：
        - status: finished
        - size: 成交张数（负数=空头）
        - fill_price: 成交均价
        """
        status = data.get('status', '')
        filled_size = self._gate_filled_contracts(data)
        if status != 'finished' and not (allow_partial and filled_size > 0):
            return {
                'success': False,
                'reason': f"订单状态异常: {status}, id={data.get('id')}, "
                         f"finish_as={data.get('finish_as', 'unknown')}"
            }

        fill_price_str = data.get('fill_price', '0')
        exec_price = float(fill_price_str) if fill_price_str else 0
        size = filled_size

        # 将张数转回标的资产数量
        exec_qty = size * quanto_multiplier
        exec_amount = round(exec_price * exec_qty, 2)

        if exec_price == 0 or size == 0:
            finish_as = str(data.get('finish_as', 'unknown') or 'unknown')
            if finish_as.lower() in {'ioc', 'cancelled', 'canceled'} or size == 0:
                return {
                    'success': False,
                    'reason': f"IOC未成交(fill=0, finish_as={finish_as})"
                }
            return {
                'success': False,
                'reason': f"成交数据异常: price={exec_price}, size={size}, "
                         f"finish_as={finish_as}"
            }

        fee_amount = data.get('fee')
        fee_amount = float(fee_amount) if fee_amount not in (None, '') else None
        return {
            'success': True,
            'exec_price': exec_price,
            'exec_qty': exec_qty,
            'exec_amount': exec_amount,
            'coverage_ratio': 0,
            'exchange_order_id': str(data.get('id', '')),
            'fee_amount': fee_amount,
            'fee_amount_usdt': fee_amount,
            'fee_asset': 'USDT' if fee_amount is not None else None,
        }

    def _gate_sign(self, method: str, api_path: str, query_string: str, body: str) -> Dict:
        """
        Gate.io API V4 签名

        签名格式: HMAC SHA512
        签名字符串: {method}
{api_path}
{query_string}
{hashed_body}
{timestamp}
        """
        timestamp = str(int(time.time()))
        hashed_payload = hashlib.sha512(body.encode('utf-8')).hexdigest()

        sign_string = f"{method}\n{api_path}\n{query_string}\n{hashed_payload}\n{timestamp}"
        signature = hmac.new(
            self.config.gate_api_secret.encode('utf-8'),
            sign_string.encode('utf-8'),
            hashlib.sha512
        ).hexdigest()

        return {
            'KEY': self.config.gate_api_key,
            'SIGN': signature,
            'Timestamp': timestamp,
            'Content-Type': 'application/json',
        }

    def fetch_gate_futures_positions(self) -> List[Dict]:
        """
        拉取 Gate USDT 永续持仓（只读，对账使用）。

        返回字段包含张数 size，空头为负；对账层取 abs(size) 与本地张数比较。
        """
        method = 'GET'
        api_path = '/api/v4/futures/usdt/positions'
        headers = self._gate_sign(method, api_path, '', '')
        url = f"{self.config.gate_base_url}{api_path}"
        resp = self._session.get(url, headers=headers, timeout=self.config.timeout_sec)
        if resp.status_code != 200:
            raise RuntimeError(f"Gate positions HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        result: List[Dict] = []
        for item in data if isinstance(data, list) else []:
            contract = str(item.get('contract') or '').upper()
            if not contract.endswith('_USDT'):
                continue
            size = float(item.get('size') or 0)
            if size == 0:
                continue
            result.append({
                'contract': contract,
                'base_asset': contract[:-5],
                'size': size,
                'entry_price': item.get('entry_price'),
                'mark_price': item.get('mark_price'),
                'liq_price': item.get('liq_price'),
                'unrealised_pnl': item.get('unrealised_pnl'),
                'leverage': item.get('leverage'),
                'margin': item.get('margin'),
                'initial_margin': item.get('initial_margin'),
                'maintenance_margin': item.get('maintenance_margin'),
                'maintenance_rate': item.get('maintenance_rate'),
                'value': item.get('value'),
                'update_time': item.get('update_time'),
                'mode': item.get('mode'),
            })
        return result

    def fetch_gate_futures_my_trades(
        self,
        contract: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 1000,
    ) -> List[Dict]:
        """拉取 Gate USDT 永续成交历史（只读，对账/ADL 识别使用）。"""
        method = 'GET'
        api_path = '/api/v4/futures/usdt/my_trades'
        params = {'limit': min(max(int(limit or 1000), 1), 1000)}
        if contract:
            params['contract'] = str(contract).upper()
        if start_time is not None:
            params['from'] = int(start_time)
        if end_time is not None:
            params['to'] = int(end_time)
        query_string = urlencode(params)
        headers = self._gate_sign(method, api_path, query_string, '')
        url = f"{self.config.gate_base_url}{api_path}?{query_string}"
        resp = self._session.get(url, headers=headers, timeout=self.config.timeout_sec)
        if resp.status_code != 200:
            raise RuntimeError(f"Gate my_trades HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        return data if isinstance(data, list) else []

    def fetch_gate_futures_auto_deleverages(
        self,
        contract: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """拉取 Gate USDT 永续 ADL 自动减仓记录，用于 WS 重连补偿。"""
        return self._fetch_gate_futures_risk_records(
            api_leaf='auto_deleverages',
            contract=contract,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

    def fetch_gate_futures_liquidates(
        self,
        contract: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """拉取 Gate USDT 永续强平记录，用于 WS 重连补偿。"""
        return self._fetch_gate_futures_risk_records(
            api_leaf='liquidates',
            contract=contract,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

    def _fetch_gate_futures_risk_records(
        self,
        api_leaf: str,
        contract: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100,
    ) -> List[Dict]:
        method = 'GET'
        api_path = f'/api/v4/futures/usdt/{api_leaf}'
        params = {'limit': min(max(int(limit or 100), 1), 1000)}
        if contract:
            params['contract'] = str(contract).upper()
        if start_time is not None:
            params['from'] = int(start_time)
        if end_time is not None:
            params['to'] = int(end_time)
        query_string = urlencode(params)
        headers = self._gate_sign(method, api_path, query_string, '')
        url = f"{self.config.gate_base_url}{api_path}?{query_string}"
        resp = self._session.get(url, headers=headers, timeout=self.config.timeout_sec)
        if resp.status_code != 200:
            raise RuntimeError(f"Gate {api_leaf} HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        return data if isinstance(data, list) else []

    def fetch_gate_futures_account(self) -> Dict:
        """拉取 Gate USDT 永续账户资金（只读，资金快照使用）。"""
        method = 'GET'
        api_path = '/api/v4/futures/usdt/accounts'
        headers = self._gate_sign(method, api_path, '', '')
        url = f"{self.config.gate_base_url}{api_path}"
        resp = self._session.get(url, headers=headers, timeout=self.config.timeout_sec)
        if resp.status_code != 200:
            raise RuntimeError(f"Gate futures account HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return data if isinstance(data, dict) else {}

    def fetch_gate_futures_account_book(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 1000,
    ) -> List[Dict]:
        """拉取 Gate USDT 永续账户账务流水（只读，资金快照收益使用）。"""
        if start_time is not None and end_time is not None and end_time - start_time > 30 * 86400:
            rows: List[Dict] = []
            cursor = int(start_time)
            end = int(end_time)
            max_span = 30 * 86400 - 1
            while cursor <= end:
                chunk_end = min(cursor + max_span, end)
                rows.extend(self.fetch_gate_futures_account_book(cursor, chunk_end, limit))
                cursor = chunk_end + 1
            return rows

        method = 'GET'
        api_path = '/api/v4/futures/usdt/account_book'
        all_rows: List[Dict] = []
        offset = 0
        page_limit = min(max(int(limit or 1000), 1), 1000)

        while True:
            params = {'limit': page_limit, 'offset': offset}
            if start_time is not None:
                params['from'] = int(start_time)
            if end_time is not None:
                params['to'] = int(end_time)
            query_string = urlencode(params)
            headers = self._gate_sign(method, api_path, query_string, '')
            url = f"{self.config.gate_base_url}{api_path}?{query_string}"
            resp = self._session.get(url, headers=headers, timeout=self.config.timeout_sec)
            if resp.status_code != 200:
                raise RuntimeError(f"Gate account_book HTTP {resp.status_code}: {resp.text[:200]}")

            data = resp.json()
            rows = data if isinstance(data, list) else []
            all_rows.extend(rows)
            if len(rows) < page_limit:
                break
            offset += page_limit
            if len(all_rows) >= 10000:
                logger.warning("Gate account_book reached 10000 row cap for capital snapshot")
                break
        return all_rows

    # ──────────────────────────────────────────────────────────────────
    # 辅助方法
    # ──────────────────────────────────────────────────────────────────

    def _get_quanto_multiplier(self, base_asset: str) -> float:
        """获取合约面值乘数"""
        if base_asset in self.contract_meta:
            return float(self.contract_meta[base_asset].get('quanto_multiplier', 1.0))
        return 1.0

    def _format_gate_price(self, base_asset: str, price: float) -> str:
        """按 Gate 合约价格最小变动单位格式化保护限价。"""
        if price <= 0:
            return '0'
        precision = 8
        if base_asset in self.contract_meta:
            order_price_round = self.contract_meta[base_asset].get('order_price_round')
            if order_price_round:
                try:
                    precision = self._precision_from_tick_str(str(order_price_round))
                except Exception:
                    precision = 8
        return f"{price:.{precision}f}"

    @staticmethod
    def _precision_from_tick_str(tick_str: str) -> int:
        from decimal import Decimal, InvalidOperation
        try:
            d = Decimal(tick_str)
            return max(0, -d.as_tuple().exponent)
        except (InvalidOperation, ValueError):
            return 8

    def _get_spot_qty_precision(self, base_asset: str) -> int:
        """从 spot_meta 的 step_size 推导数量小数位数"""
        if base_asset in self.spot_meta:
            step_size = self.spot_meta[base_asset].get('step_size', 0.00001)
            step_str = str(step_size)
            if '.' in step_str:
                return len(step_str.split('.')[-1].rstrip('0')) or 0
        return 5  # 安全默认值

    def _get_spot_price_precision(self, base_asset: str) -> int:
        """从 spot_meta 的 tick_size 推导 Binance 限价价格小数位数。"""
        if base_asset in self.spot_meta:
            tick_size = self.spot_meta[base_asset].get('tick_size')
            if tick_size:
                try:
                    return self._precision_from_tick_str(str(tick_size))
                except Exception:
                    return 8
        return 8

    def reload_meta(self, contract_meta: Dict, spot_meta: Dict = None):
        """热更新元数据（与 VirtualExecutor 保持接口一致）"""
        self.contract_meta = contract_meta
        if spot_meta is not None:
            self.spot_meta = spot_meta
        logger.info(f'RealExecutor 元数据已刷新: 合约 {len(contract_meta)} 条, 现货 {len(self.spot_meta)} 条')

    def test_connectivity(self) -> Dict:
        """
        测试交易所 API 连通性（不下单，仅验证签名和网络）

        Returns:
            {'binance': {'ok': bool, 'msg': str}, 'gate': {'ok': bool, 'msg': str}}
        """
        result = {'binance': {'ok': False, 'msg': ''}, 'gate': {'ok': False, 'msg': ''}}

        # Binance: GET /api/v3/account
        try:
            timestamp = int(time.time() * 1000)
            params = {'timestamp': timestamp}
            query_string = urlencode(params)
            signature = self._binance_sign(query_string)
            params['signature'] = signature

            url = f"{self.config.binance_base_url}/api/v3/account"
            headers = {'X-MBX-APIKEY': self.config.binance_api_key}
            resp = self._session.get(url, params=params, headers=headers, timeout=self.config.timeout_sec)

            if resp.status_code == 200:
                data = resp.json()
                balances = [b for b in data.get('balances', []) if float(b.get('free', 0)) > 0]
                result['binance'] = {'ok': True, 'msg': f'账户正常, {len(balances)} 个有余额的资产'}
            else:
                result['binance'] = {'ok': False, 'msg': f'HTTP {resp.status_code}: {resp.text[:100]}'}
        except Exception as e:
            result['binance'] = {'ok': False, 'msg': str(e)[:100]}

        # Gate: GET /api/v4/futures/usdt/accounts
        try:
            method = 'GET'
            api_path = '/api/v4/futures/usdt/accounts'
            headers = self._gate_sign(method, api_path, '', '')
            url = f"{self.config.gate_base_url}{api_path}"
            resp = self._session.get(url, headers=headers, timeout=self.config.timeout_sec)

            if resp.status_code == 200:
                data = resp.json()
                available = data.get('available', '0')
                result['gate'] = {'ok': True, 'msg': f'账户正常, 可用余额={available}'}
            else:
                result['gate'] = {'ok': False, 'msg': f'HTTP {resp.status_code}: {resp.text[:100]}'}
        except Exception as e:
            result['gate'] = {'ok': False, 'msg': str(e)[:100]}

        return result
