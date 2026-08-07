# coding: utf-8
"""
基础持仓对账。

只读交易所真实持仓，与本地 mi_trade_position 的 holding 聚合值做差，
写入 mi_recon_snapshot；当 Gate 侧实仓缺失/减少时，识别 ADL 并标记持仓风险。
"""
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from calc.exchange_desync_remediator import (
    ExchangeDesyncRemediationConfig,
    ExchangeDesyncRemediator,
)
from calc.real_executor import ExchangeConfig, GATE_CROSS_MARGIN_LEVERAGE, RealExecutor
from common.config import config
from common.database import db_manager
from common.logger import get_logger
from common.meta_loader import fetch_contract_meta, fetch_spot_meta
from common.strategy_accounts import get_binance_credentials, get_gate_futures_credentials

logger = get_logger(__name__)


BINANCE_SPOT_TOLERANCE = 1e-6
GATE_FUTURE_CONTRACT_TOLERANCE = 1.0
DIFF_RATIO_EPSILON = 1e-12


@dataclass
class ReconciliationConfig:
    enabled: bool = True
    retention_days: int = 30
    ignored_binance_spot_assets: Set[str] = field(default_factory=lambda: {'BNB'})
    mark_exchange_risk: bool = True
    adl_lookback_sec: int = 24 * 3600
    auto_remediate_enabled: bool = True
    auto_remediate_confirm_runs: int = 2
    auto_remediate_confirm_window_sec: int = 15
    auto_remediate_fast_confirm_delay_sec: float = 3.0
    auto_remediate_max_positions_per_run: int = 20
    auto_remediate_min_spot_qty: float = 0.0
    auto_remediate_close_extra_gate_position: bool = True
    auto_remediate_binance_spot_position: bool = True


def normalize_asset_set(values) -> Set[str]:
    """Normalize config values such as ['BNB'] or 'BNB,FDUSD' into upper-case asset symbols."""
    if values is None:
        return set()
    if isinstance(values, str):
        raw_values = values.split(',')
    else:
        raw_values = values
    result: Set[str] = set()
    for value in raw_values:
        asset = str(value or '').strip().upper()
        if asset:
            result.add(asset)
    return result


def get_ignored_binance_spot_assets() -> Set[str]:
    """Assets intentionally held in Binance spot wallet but excluded from position reconciliation."""
    return normalize_asset_set(config.get('reconciliation.ignored_binance_spot_assets', ['BNB']))


def build_exchange_config() -> ExchangeConfig:
    """按当前 trade.mode 构建真实交易所只读配置。"""
    env = config.get_real_executor_env()
    timeout_sec = config.get_int('real_executor.timeout_sec', 10)
    if env == 'mainnet':
        binance_creds = get_binance_credentials('forward', mainnet=True)
        gate_creds = get_gate_futures_credentials('forward', mainnet=True)
        return ExchangeConfig(
            binance_base_url='https://api1.binance.com',
            binance_api_key=binance_creds.api_key,
            binance_api_secret=binance_creds.api_secret,
            gate_base_url='https://api.gateio.ws',
            gate_api_key=gate_creds.api_key,
            gate_api_secret=gate_creds.api_secret,
            timeout_sec=timeout_sec,
            env='mainnet',
        )
    return ExchangeConfig(
        binance_base_url='https://testnet.binance.vision',
        binance_api_key=os.getenv('BINANCE_TESTNET_API_KEY', ''),
        binance_api_secret=os.getenv('BINANCE_TESTNET_API_SECRET', ''),
        gate_base_url='https://fx-api-testnet.gateio.ws',
        gate_api_key=os.getenv('GATE_FUTURES_TESTNET_API_KEY', ''),
        gate_api_secret=os.getenv('GATE_FUTURES_TESTNET_API_SECRET', ''),
        timeout_sec=timeout_sec,
        env='testnet',
    )


def build_default_reconciler() -> 'Reconciler':
    """构建一次性对账器，供定时任务和手动接口共用。"""
    contract_meta = fetch_contract_meta()
    spot_meta = fetch_spot_meta()
    executor = RealExecutor(
        build_exchange_config(),
        contract_meta=contract_meta,
        spot_meta=spot_meta,
        leverage=GATE_CROSS_MARGIN_LEVERAGE,
    )
    cfg = ReconciliationConfig(
        enabled=config.get_bool('reconciliation.enabled', True),
        retention_days=config.get_int('reconciliation.retention_days', 30),
        ignored_binance_spot_assets=get_ignored_binance_spot_assets(),
        mark_exchange_risk=config.get_bool('reconciliation.mark_exchange_risk', True),
        adl_lookback_sec=config.get_int('reconciliation.adl_lookback_sec', 24 * 3600),
        auto_remediate_enabled=config.get_bool('reconciliation.auto_remediate.enabled', True),
        auto_remediate_confirm_runs=config.get_int('reconciliation.auto_remediate.confirm_runs', 2),
        auto_remediate_confirm_window_sec=config.get_int('reconciliation.auto_remediate.confirm_window_sec', 15),
        auto_remediate_fast_confirm_delay_sec=config.get_float(
            'reconciliation.auto_remediate.fast_confirm_delay_sec', 3.0
        ),
        auto_remediate_max_positions_per_run=config.get_int('reconciliation.auto_remediate.max_positions_per_run', 20),
        auto_remediate_min_spot_qty=config.get_float('reconciliation.auto_remediate.min_spot_qty', 0.0),
        auto_remediate_close_extra_gate_position=config.get_bool(
            'reconciliation.auto_remediate.close_extra_gate_position', True
        ),
        auto_remediate_binance_spot_position=config.get_bool(
            'reconciliation.auto_remediate.binance_spot_position', True
        ),
    )
    return Reconciler(executor, cfg)


class Reconciler:
    """交易所持仓对账器。"""

    def __init__(self, executor: RealExecutor, cfg: Optional[ReconciliationConfig] = None):
        self.executor = executor
        self.cfg = cfg or ReconciliationConfig()
        self.remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(
                enabled=self.cfg.auto_remediate_enabled,
                max_positions_per_run=self.cfg.auto_remediate_max_positions_per_run,
                min_spot_qty=self.cfg.auto_remediate_min_spot_qty,
                close_extra_gate_position=self.cfg.auto_remediate_close_extra_gate_position,
                remediate_binance_spot_position=self.cfg.auto_remediate_binance_spot_position,
                spot_open_fee=config.get_float('trade.fee.spot_open', 0.00075),
                spot_close_fee=config.get_float('trade.fee.spot_close', 0.00075),
                future_open_fee=config.get_float('trade.fee.future_open', 0.0002),
                future_close_fee=config.get_float('trade.fee.future_close', 0.0002),
                future_taker_open_fee=config.get_float('trade.fee.future_taker_open', 0.0005),
                future_taker_close_fee=config.get_float('trade.fee.future_taker_close', 0.0005),
            ),
        )

    def run_once(self) -> Dict:
        """执行一轮对账并落库，返回本轮摘要。"""
        snapshot_at = datetime.now()
        rows: List[Dict] = []
        remediation_results: List[Dict] = []

        local_spot = self._load_local_spot_positions()
        local_gate = self._load_local_gate_positions()

        try:
            binance_balances = self.executor.fetch_binance_spot_balances()
            binance_rows = self._compare_binance(snapshot_at, local_spot, binance_balances)
            rows.extend(binance_rows)
        except Exception as e:
            logger.warning(f'Binance 现货对账拉取失败: {e}', exc_info=True)
            binance_rows = []
            rows.append(self._error_row(snapshot_at, 'binance', e))

        try:
            gate_positions = self.executor.fetch_gate_futures_positions()
            gate_rows = self._compare_gate(snapshot_at, local_gate, gate_positions)
            gate_risks: List[Dict] = []
            if self.cfg.mark_exchange_risk:
                gate_risks = self._mark_gate_desync_risks(snapshot_at, gate_rows)
            if self.cfg.auto_remediate_enabled:
                remediation_results.extend(self._auto_remediate_gate_risks(snapshot_at, gate_risks, binance_rows))
                remediation_results.extend(
                    self._auto_remediate_post_close_spot_dust(binance_rows, gate_rows)
                )
            rows.extend(gate_rows)
        except Exception as e:
            logger.warning(f'Gate 期货对账拉取失败: {e}', exc_info=True)
            rows.append(self._error_row(snapshot_at, 'gate', e))

        if rows:
            self._insert_rows(rows)
        self.cleanup_old_snapshots()

        mismatch_count = sum(1 for r in rows if not r.get('is_match'))
        error_count = sum(1 for r in rows if r.get('dimension') == 'error')
        logger.info(
            f"对账完成 | snapshot_at={snapshot_at:%Y-%m-%d %H:%M:%S} | "
            f"rows={len(rows)} mismatch={mismatch_count} error={error_count}"
        )
        return {
            'success': True,
            'snapshot_at': snapshot_at.strftime('%Y-%m-%d %H:%M:%S'),
            'rows': len(rows),
            'mismatch_count': mismatch_count,
            'error_count': error_count,
            'remediation_count': sum(1 for r in remediation_results if r.get('attempted')),
            'remediation_success_count': sum(1 for r in remediation_results if r.get('success')),
            'confirmation_pending_count': sum(
                1
                for result in remediation_results
                if result.get('reason') == 'waiting_for_reconciliation_confirmation'
            ),
        }

    def run_with_fast_confirmation(self) -> Dict:
        """Run a second fresh reconciliation shortly after an unconfirmed mismatch."""
        first = self.run_once()
        pending = int(first.get('confirmation_pending_count') or 0)
        delay_sec = max(float(self.cfg.auto_remediate_fast_confirm_delay_sec or 0.0), 0.0)
        if pending <= 0 or delay_sec <= 0:
            return first

        logger.warning(
            "对账发现待确认差异，%.1f秒后快速复核 | pending=%s | snapshot_at=%s",
            delay_sec,
            pending,
            first.get('snapshot_at'),
        )
        time.sleep(delay_sec)
        second = self.run_once()
        second['fast_confirmation'] = True
        second['fast_confirmation_delay_sec'] = delay_sec
        second['initial_snapshot_at'] = first.get('snapshot_at')
        return second

    def cleanup_old_snapshots(self):
        """按配置清理历史快照；历史不一致记录保留用于风险追溯。"""
        if self.cfg.retention_days <= 0:
            return
        cutoff = datetime.now() - timedelta(days=self.cfg.retention_days)
        with db_manager.get_cursor() as cursor:
            cursor.execute(
                "DELETE FROM mi_recon_snapshot WHERE snapshot_at < %s AND is_match = 1",
                (cutoff,),
            )

    def _load_local_spot_positions(self) -> Dict[str, float]:
        sql = """
            SELECT UPPER(base_asset) AS base_asset, COALESCE(SUM(spot_open_qty), 0) AS local_qty
            FROM mi_trade_position
            WHERE status = 'holding'
            GROUP BY UPPER(base_asset)
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
        return {r['base_asset']: float(r.get('local_qty') or 0) for r in rows}

    def _load_local_gate_positions(self) -> Dict[str, float]:
        sql = """
            SELECT UPPER(base_asset) AS base_asset, COALESCE(SUM(future_open_contracts), 0) AS local_contracts
            FROM mi_trade_position
            WHERE status = 'holding'
            GROUP BY UPPER(base_asset)
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
        return {r['base_asset']: abs(float(r.get('local_contracts') or 0)) for r in rows}

    def _compare_binance(self, snapshot_at: datetime, local: Dict[str, float], balances: List[Dict]) -> List[Dict]:
        ignored = {asset.upper() for asset in self.cfg.ignored_binance_spot_assets}
        exchange = {str(b.get('asset') or '').upper(): float(b.get('total') or 0) for b in balances}
        detail = {str(b.get('asset') or '').upper(): b for b in balances}
        assets = sorted((set(local) | set(exchange)) - ignored)
        return [
            self._position_row(
                snapshot_at=snapshot_at,
                exchange='binance',
                base_asset=asset,
                local_value=local.get(asset, 0.0),
                exchange_value=exchange.get(asset, 0.0),
                tolerance=BINANCE_SPOT_TOLERANCE,
                detail=detail.get(asset, {}),
            )
            for asset in assets
        ]

    def _compare_gate(self, snapshot_at: datetime, local: Dict[str, float], positions: List[Dict]) -> List[Dict]:
        exchange = {str(p.get('base_asset') or '').upper(): abs(float(p.get('size') or 0)) for p in positions}
        detail = {str(p.get('base_asset') or '').upper(): p for p in positions}
        assets = sorted(set(local) | set(exchange))
        return [
            self._position_row(
                snapshot_at=snapshot_at,
                exchange='gate',
                base_asset=asset,
                local_value=local.get(asset, 0.0),
                exchange_value=exchange.get(asset, 0.0),
                tolerance=GATE_FUTURE_CONTRACT_TOLERANCE,
                detail=detail.get(asset, {}),
            )
            for asset in assets
        ]

    def _mark_gate_desync_risks(self, snapshot_at: datetime, rows: List[Dict]) -> List[Dict]:
        """Gate 实仓不匹配时生成风险项；确认后才同步标记本地持仓。"""
        risks: List[Dict] = []
        for row in rows:
            if row.get('exchange') != 'gate' or row.get('dimension') != 'position':
                continue
            local_value = float(row.get('local_value') or 0)
            exchange_value = float(row.get('exchange_value') or 0)
            if abs(exchange_value - local_value) <= GATE_FUTURE_CONTRACT_TOLERANCE:
                continue

            base_asset = str(row.get('base_asset') or '').upper()
            if not base_asset:
                continue

            if exchange_value + GATE_FUTURE_CONTRACT_TOLERANCE < local_value:
                risk = self._detect_gate_desync_risk(base_asset, snapshot_at, local_value, exchange_value)
            elif exchange_value > local_value + GATE_FUTURE_CONTRACT_TOLERANCE:
                risk = self._detect_gate_extra_risk(base_asset, snapshot_at, local_value, exchange_value, row)
            else:
                continue

            risk_type = self._gate_risk_type_from_values(local_value, exchange_value) or str(risk.get('type') or '')
            confirmed = self._is_gate_risk_confirmed(
                base_asset=base_asset,
                risk_type=risk_type,
                snapshot_at=snapshot_at,
            )
            risk['confirmed'] = confirmed
            updated = 0
            if risk.get('type') in {'adl', 'liquidation', 'missing_gate_position', 'qty_mismatch'}:
                if confirmed:
                    updated = self._mark_positions_exchange_risk(base_asset, risk)
                else:
                    logger.warning(
                        "Gate 持仓对账发现疑似断腿，等待连续确认 | asset=%s | type=%s | local=%s | exchange=%s",
                        base_asset, risk.get('type'), local_value, exchange_value,
                    )
            row.setdefault('detail', {})
            row['detail']['exchange_risk'] = risk
            if updated:
                logger.warning(
                    "Gate 持仓对账发现断腿风险 | asset=%s | type=%s | local=%s | exchange=%s | marked=%s",
                    base_asset, risk.get('type'), local_value, exchange_value, updated,
                )
            elif risk.get('type') == 'extra_gate_position':
                logger.warning(
                    "Gate 持仓对账发现多余合约风险 | asset=%s | local=%s | exchange=%s | confirmed=%s",
                    base_asset, local_value, exchange_value, confirmed,
                )
            risks.append({
                'base_asset': base_asset,
                'risk': risk,
                'local_contracts': local_value,
                'exchange_contracts': exchange_value,
                'missing_contracts': max(0.0, local_value - exchange_value),
                'extra_contracts': max(0.0, exchange_value - local_value),
                'confirmed': confirmed,
            })
        return risks

    def _detect_gate_extra_risk(
        self,
        base_asset: str,
        snapshot_at: datetime,
        local_contracts: float,
        exchange_contracts: float,
        row: Dict,
    ) -> Dict:
        contract = f"{base_asset}_USDT"
        detail = row.get('detail') or {}
        exchange_size = None
        try:
            exchange_size = float(detail.get('size')) if detail.get('size') is not None else None
        except (TypeError, ValueError):
            exchange_size = None
        mark_price = None
        for key in ('mark_price', 'mark_price_usdt'):
            try:
                if detail.get(key) is not None:
                    mark_price = float(detail.get(key))
                    break
            except (TypeError, ValueError):
                continue
        extra_contracts = max(0.0, exchange_contracts - local_contracts)
        return {
            'status': 'desynced',
            'type': 'extra_gate_position',
            'event_at': snapshot_at,
            'contract': contract,
            'detail': (
                f"Gate多余实仓|contract={contract}|local={local_contracts:g}|"
                f"exchange={exchange_contracts:g}|extra={extra_contracts:g}|size={exchange_size}"
            ),
            'exchange_size': exchange_size,
            'mark_price': mark_price,
            'future_close_size': extra_contracts,
            'future_close_price': mark_price,
            'future_liquidity_role': 'taker',
        }

    def _detect_gate_desync_risk(
        self,
        base_asset: str,
        snapshot_at: datetime,
        local_contracts: float,
        exchange_contracts: float,
    ) -> Dict:
        contract = f"{base_asset}_USDT"
        missing_contracts = max(0.0, local_contracts - exchange_contracts)
        started_at = snapshot_at - timedelta(seconds=max(int(self.cfg.adl_lookback_sec or 0), 60))
        adl_trades: List[Dict] = []
        try:
            trades = self.executor.fetch_gate_futures_my_trades(
                contract=contract,
                start_time=int(started_at.timestamp()),
                end_time=int(snapshot_at.timestamp()),
                limit=1000,
            )
            for trade in trades:
                text = str(trade.get('text') or '').lower()
                close_size = float(trade.get('close_size') or 0)
                if 'auto_deleveraging' in text and close_size > 0:
                    adl_trades.append(trade)
        except Exception as e:
            logger.warning(f"Gate ADL 成交查询失败 | {contract} | {e}", exc_info=True)

        if adl_trades:
            latest = max(adl_trades, key=lambda t: float(t.get('create_time') or 0))
            event_at = datetime.fromtimestamp(float(latest.get('create_time') or snapshot_at.timestamp()))
            total_close_size = sum(float(t.get('close_size') or 0) for t in adl_trades)
            total_pnl = self._load_gate_pnl_near_event(contract, event_at)
            if total_pnl is None:
                total_pnl = sum(float(t.get('pnl') or 0) for t in adl_trades)
            detail = (
                f"ADL自动减仓|contract={contract}|close_size={total_close_size:g}|"
                f"missing={missing_contracts:g}|price={latest.get('price')}|pnl={total_pnl:.6f}|"
                f"trade_id={latest.get('id')}|order_id={latest.get('order_id')}"
            )
            return {
                'status': 'desynced',
                'type': 'adl',
                'event_at': event_at,
                'detail': detail,
                'future_close_price': float(latest.get('price') or 0),
                'future_exchange_order_id': str(latest.get('order_id') or ''),
                'future_trade_id': str(latest.get('id') or ''),
                'future_liquidity_role': str(latest.get('role') or 'taker').lower(),
                'future_close_size': total_close_size,
                'future_pnl': total_pnl,
            }

        risk_type = 'missing_gate_position' if exchange_contracts <= GATE_FUTURE_CONTRACT_TOLERANCE else 'qty_mismatch'
        return {
            'status': 'desynced',
            'type': risk_type,
            'event_at': snapshot_at,
            'detail': (
                f"Gate实仓不匹配|contract={contract}|local={local_contracts:g}|"
                f"exchange={exchange_contracts:g}|missing={missing_contracts:g}"
            ),
            'future_close_price': None,
            'future_exchange_order_id': None,
            'future_trade_id': None,
            'future_liquidity_role': 'unknown',
            'future_close_size': missing_contracts,
            'future_pnl': None,
        }

    def _is_gate_risk_confirmed(self, base_asset: str, risk_type: str, snapshot_at: datetime) -> bool:
        """要求当前异常在历史快照中至少出现过，避免单次 API 抖动触发实盘动作。"""
        confirm_runs = max(int(self.cfg.auto_remediate_confirm_runs or 1), 1)
        if confirm_runs <= 1:
            return True
        if self._has_self_reported_gate_desync(base_asset, risk_type, snapshot_at):
            return True

        cutoff = snapshot_at - timedelta(
            seconds=max(int(self.cfg.auto_remediate_confirm_window_sec or 1), 1)
        )
        with db_manager.get_cursor() as cursor:
            cursor.execute(
                """
                SELECT local_value, exchange_value
                FROM mi_recon_snapshot
                WHERE exchange = 'gate'
                  AND dimension = 'position'
                  AND UPPER(base_asset) = %s
                  AND snapshot_at >= %s
                  AND is_match = 0
                ORDER BY snapshot_at DESC
                LIMIT %s
                """,
                (base_asset.upper(), cutoff, confirm_runs - 1),
            )
            rows = cursor.fetchall()

        if len(rows) < confirm_runs - 1:
            return False
        for row in rows:
            previous_type = self._gate_risk_type_from_values(
                float(row.get('local_value') or 0),
                float(row.get('exchange_value') or 0),
            )
            if previous_type != risk_type:
                return False
        return True

    def _has_self_reported_gate_desync(self, base_asset: str, risk_type: str, snapshot_at: datetime) -> bool:
        """系统平仓已确认 Gate 腿成交时，允许即时对账直接进入兜底处置。"""
        if risk_type not in {'missing_gate_position', 'qty_mismatch'}:
            return False
        cutoff = snapshot_at - timedelta(
            seconds=max(int(self.cfg.auto_remediate_confirm_window_sec or 1), 1)
        )
        with db_manager.get_cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM mi_trade_position
                WHERE status = 'holding'
                  AND UPPER(base_asset) = %s
                  AND exchange_risk_status = 'desynced'
                  AND exchange_risk_type = %s
                  AND exchange_risk_at >= %s
                  AND exchange_risk_detail LIKE '%%系统风险平仓Gate期货已成交但Binance现货失败%%'
                LIMIT 1
                """,
                (base_asset.upper(), risk_type, cutoff),
            )
            return cursor.fetchone() is not None

    @staticmethod
    def _gate_risk_type_from_values(local_value: float, exchange_value: float) -> Optional[str]:
        if exchange_value + GATE_FUTURE_CONTRACT_TOLERANCE < local_value:
            return 'missing_gate_position' if exchange_value <= GATE_FUTURE_CONTRACT_TOLERANCE else 'qty_mismatch'
        if exchange_value > local_value + GATE_FUTURE_CONTRACT_TOLERANCE:
            return 'extra_gate_position'
        return None

    def _auto_remediate_gate_risks(
        self,
        snapshot_at: datetime,
        risks: List[Dict],
        binance_rows: Optional[List[Dict]] = None,
    ) -> List[Dict]:
        results: List[Dict] = []
        if not risks:
            return results
        binance_by_asset = {
            str(row.get('base_asset') or '').upper(): row
            for row in (binance_rows or [])
            if row.get('exchange') == 'binance' and row.get('dimension') == 'position'
        }

        for item in risks:
            risk = item.get('risk') or {}
            if not item.get('confirmed'):
                result = {'attempted': False, 'reason': 'waiting_for_reconciliation_confirmation'}
                self._record_reconciliation_risk_event(snapshot_at, item, result)
                results.append(result)
                continue

            risk_type = str(risk.get('type') or '')
            if risk_type in {'adl', 'liquidation', 'missing_gate_position', 'qty_mismatch'}:
                remediation_risk = {
                    **risk,
                    'local_contracts': float(item.get('local_contracts') or 0),
                    'exchange_contracts': float(item.get('exchange_contracts') or 0),
                }
                result = self.remediator.remediate_gate_short_desync(
                    base_asset=item.get('base_asset'),
                    missing_contracts=float(item.get('missing_contracts') or 0),
                    risk=remediation_risk,
                    require_desynced=True,
                )
            elif risk_type == 'extra_gate_position':
                result = self.remediator.remediate_gate_extra_position(
                    base_asset=item.get('base_asset'),
                    extra_contracts=float(item.get('extra_contracts') or 0),
                    risk=risk,
                )
                spot_result = self._auto_remediate_binance_spot_for_gate_extra(item, risk, result, binance_by_asset)
                if spot_result.get('attempted'):
                    result = {
                        **result,
                        'paired_binance_spot_result': spot_result,
                        'success': bool(result.get('success')) and bool(spot_result.get('success')),
                    }
            else:
                result = {'attempted': False, 'reason': f'unsupported_gate_risk:{risk_type}'}

            self._record_reconciliation_risk_event(snapshot_at, item, result)
            results.append(result)
        return results

    def _auto_remediate_post_close_spot_dust(
        self,
        binance_rows: List[Dict],
        gate_rows: List[Dict],
    ) -> List[Dict]:
        """Retire matched spot dust only after both local and exchange Gate positions are zero."""
        gate_by_asset = {
            str(row.get('base_asset') or '').upper(): row
            for row in gate_rows
            if row.get('exchange') == 'gate' and row.get('dimension') == 'position'
        }
        results: List[Dict] = []
        for spot_row in binance_rows:
            if spot_row.get('exchange') != 'binance' or spot_row.get('dimension') != 'position':
                continue
            if not spot_row.get('is_match'):
                continue
            local_spot_qty = float(spot_row.get('local_value') or 0)
            exchange_spot_qty = float(spot_row.get('exchange_value') or 0)
            if local_spot_qty <= BINANCE_SPOT_TOLERANCE or exchange_spot_qty <= BINANCE_SPOT_TOLERANCE:
                continue

            base_asset = str(spot_row.get('base_asset') or '').upper()
            gate_row = gate_by_asset.get(base_asset)
            if not gate_row or not gate_row.get('is_match'):
                continue
            if abs(float(gate_row.get('local_value') or 0)) > 1e-9:
                continue
            if abs(float(gate_row.get('exchange_value') or 0)) > 1e-9:
                continue

            result = self.remediator.remediate_post_close_spot_dust(
                base_asset=base_asset,
                local_spot_qty=local_spot_qty,
                exchange_spot_qty=exchange_spot_qty,
            )
            if result.get('attempted'):
                results.append(result)
        return results

    def _auto_remediate_binance_spot_for_gate_extra(
        self,
        item: Dict,
        risk: Dict,
        gate_result: Dict,
        binance_by_asset: Dict[str, Dict],
    ) -> Dict:
        if not gate_result.get('success'):
            return {'attempted': False, 'reason': 'gate_extra_remediation_not_successful'}
        base_asset = str(item.get('base_asset') or '').upper()
        row = binance_by_asset.get(base_asset)
        if not row:
            return {'attempted': False, 'reason': 'no_binance_position_row'}
        local_qty = float(row.get('local_value') or 0)
        exchange_qty = float(row.get('exchange_value') or 0)
        if abs(exchange_qty - local_qty) <= BINANCE_SPOT_TOLERANCE:
            return {'attempted': False, 'reason': 'binance_spot_already_match'}
        return self.remediator.remediate_binance_spot_desync(
            base_asset=base_asset,
            local_qty=local_qty,
            exchange_qty=exchange_qty,
            risk=risk,
        )

    def _record_reconciliation_risk_event(self, snapshot_at: datetime, item: Dict, result: Dict):
        risk = item.get('risk') or {}
        base_asset = str(item.get('base_asset') or '').upper()
        risk_type = str(risk.get('type') or 'unknown')[:40]
        status = 'ignored'
        if result.get('attempted'):
            status = 'remediated' if result.get('success') else 'failed'
        event_key = (
            f"recon:gate:{risk_type}:{base_asset}:"
            f"{int(snapshot_at.timestamp())}:"
            f"{float(item.get('local_contracts') or 0):g}:"
            f"{float(item.get('exchange_contracts') or 0):g}"
        )[:160]
        raw = {
            'source': 'reconciliation',
            'risk': risk,
            'local_contracts': item.get('local_contracts'),
            'exchange_contracts': item.get('exchange_contracts'),
            'missing_contracts': item.get('missing_contracts'),
            'extra_contracts': item.get('extra_contracts'),
            'confirmed': item.get('confirmed'),
        }
        remediation_action = result.get('action')
        sql = """
            INSERT INTO mi_exchange_risk_event (
                event_key, exchange, market_type, risk_type, base_asset, contract, event_at,
                exchange_order_id, exchange_trade_id, side, size, fill_price, entry_price,
                mark_price, liq_price, pnl, raw_json, status, remediation_action, remediation_result
            ) VALUES (
                %(event_key)s, 'gate', 'future', %(risk_type)s, %(base_asset)s, %(contract)s, %(event_at)s,
                %(exchange_order_id)s, %(exchange_trade_id)s, %(side)s, %(size)s, %(fill_price)s, NULL,
                %(mark_price)s, NULL, %(pnl)s, %(raw_json)s, %(status)s, %(remediation_action)s,
                %(remediation_result)s
            )
            ON DUPLICATE KEY UPDATE
                status = VALUES(status),
                remediation_action = VALUES(remediation_action),
                remediation_result = VALUES(remediation_result),
                raw_json = VALUES(raw_json),
                updated_at = CURRENT_TIMESTAMP
        """
        future_result = result.get('future_result') or {}
        payload = {
            'event_key': event_key,
            'risk_type': risk_type,
            'base_asset': base_asset,
            'contract': risk.get('contract') or f'{base_asset}_USDT',
            'event_at': risk.get('event_at') or snapshot_at,
            'exchange_order_id': future_result.get('exchange_order_id') or risk.get('future_exchange_order_id'),
            'exchange_trade_id': risk.get('future_trade_id'),
            'side': 'reconciliation',
            'size': risk.get('future_close_size') or item.get('missing_contracts') or item.get('extra_contracts'),
            'fill_price': future_result.get('exec_price') or risk.get('future_close_price'),
            'mark_price': risk.get('mark_price'),
            'pnl': risk.get('future_pnl'),
            'raw_json': json.dumps(raw, ensure_ascii=False, default=str),
            'status': status,
            'remediation_action': remediation_action,
            'remediation_result': json.dumps(result, ensure_ascii=False, default=str),
        }
        try:
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, payload)
        except Exception as e:
            logger.warning("对账风险事件落库失败 | %s | %s", event_key, e, exc_info=True)

    def _load_gate_pnl_near_event(self, contract: str, event_at: datetime) -> Optional[float]:
        """Gate ADL 的 PnL 通常在 account_book 中，而不是 my_trades 中。"""
        try:
            rows = self.executor.fetch_gate_futures_account_book(
                int((event_at - timedelta(minutes=5)).timestamp()),
                int((event_at + timedelta(minutes=5)).timestamp()),
                limit=1000,
            )
        except Exception as e:
            logger.warning(f"Gate ADL PnL 流水查询失败 | {contract} | {e}", exc_info=True)
            return None

        total = 0.0
        matched = False
        for row in rows:
            row_contract = str(row.get('contract') or row.get('text') or '').upper()
            row_type = str(row.get('type') or row.get('ctype') or '').lower()
            if contract.upper() not in row_contract or row_type != 'pnl':
                continue
            try:
                total += float(row.get('change') or row.get('amount') or 0)
                matched = True
            except (TypeError, ValueError):
                continue
        return total if matched else None

    def _mark_positions_exchange_risk(self, base_asset: str, risk: Dict) -> int:
        sql = """
            UPDATE mi_trade_position
            SET exchange_risk_status = %(status)s,
                exchange_risk_type = %(type)s,
                exchange_risk_at = %(event_at)s,
                exchange_risk_detail = %(detail)s,
                close_reason = CASE
                    WHEN close_reason IS NULL OR close_reason = '' THEN %(reason)s
                    WHEN close_reason NOT LIKE %(reason_like)s THEN CONCAT(close_reason, '|', %(reason)s)
                    ELSE close_reason
                END
            WHERE status = 'holding'
              AND UPPER(base_asset) = %(base_asset)s
              AND (
                    exchange_risk_status <> %(status)s
                 OR exchange_risk_type <> %(type)s
                 OR exchange_risk_type IS NULL
                 OR exchange_risk_detail <> %(detail)s
                 OR exchange_risk_detail IS NULL
              )
        """
        reason = f"交易所仓位风险:{risk.get('type')}|{risk.get('detail')}"
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, {
                'status': risk.get('status') or 'desynced',
                'type': risk.get('type') or 'unknown',
                'event_at': risk.get('event_at') or datetime.now(),
                'detail': str(risk.get('detail') or ''),
                'reason': reason,
                'reason_like': f"%交易所仓位风险:{risk.get('type')}%",
                'base_asset': base_asset,
            })
            return int(cursor.rowcount or 0)

    def _position_row(
        self,
        snapshot_at: datetime,
        exchange: str,
        base_asset: str,
        local_value: float,
        exchange_value: float,
        tolerance: float,
        detail: Dict,
    ) -> Dict:
        diff = exchange_value - local_value
        denom = max(abs(local_value), abs(exchange_value), DIFF_RATIO_EPSILON)
        return {
            'snapshot_at': snapshot_at,
            'exchange': exchange,
            'base_asset': base_asset,
            'dimension': 'position',
            'local_value': local_value,
            'exchange_value': exchange_value,
            'diff_value': diff,
            'diff_ratio': diff / denom,
            'is_match': abs(diff) <= tolerance,
            'detail': detail,
        }

    def _error_row(self, snapshot_at: datetime, exchange: str, error: Exception) -> Dict:
        return {
            'snapshot_at': snapshot_at,
            'exchange': exchange,
            'base_asset': '__ERROR__',
            'dimension': 'error',
            'local_value': None,
            'exchange_value': None,
            'diff_value': None,
            'diff_ratio': None,
            'is_match': False,
            'detail': {'exchange': exchange, 'error_msg': str(error)[:500]},
        }

    def _insert_rows(self, rows: List[Dict]):
        sql = """
            INSERT INTO mi_recon_snapshot (
                snapshot_at, exchange, base_asset, dimension,
                local_value, exchange_value, diff_value, diff_ratio,
                is_match, detail
            ) VALUES (
                %(snapshot_at)s, %(exchange)s, %(base_asset)s, %(dimension)s,
                %(local_value)s, %(exchange_value)s, %(diff_value)s, %(diff_ratio)s,
                %(is_match)s, %(detail)s
            )
        """
        payload = []
        for row in rows:
            item = dict(row)
            item['is_match'] = 1 if row.get('is_match') else 0
            item['detail'] = json.dumps(row.get('detail') or {}, ensure_ascii=False, default=str)
            payload.append(item)
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.executemany(sql, payload)
            finally:
                cursor.close()
