# coding: utf-8
"""
基础持仓对账。

本期只读交易所真实持仓，与本地 mi_trade_position 的 holding 聚合值做差，
并写入 mi_recon_snapshot；不告警、不修复、不触发任何交易动作。
"""
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from calc.real_executor import ExchangeConfig, RealExecutor
from common.config import config
from common.database import db_manager
from common.logger import get_logger
from common.meta_loader import fetch_contract_meta, fetch_spot_meta

logger = get_logger(__name__)


BINANCE_SPOT_TOLERANCE = 1e-6
GATE_FUTURE_CONTRACT_TOLERANCE = 1.0
DIFF_RATIO_EPSILON = 1e-12


@dataclass
class ReconciliationConfig:
    enabled: bool = True
    retention_days: int = 30
    leverage: int = 2
    ignored_binance_spot_assets: Set[str] = field(default_factory=lambda: {'BNB'})


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
        return ExchangeConfig(
            binance_base_url='https://api1.binance.com',
            binance_api_key=os.getenv('BINANCE_API_KEY', ''),
            binance_api_secret=os.getenv('BINANCE_API_SECRET', ''),
            gate_base_url='https://api.gateio.ws',
            gate_api_key=os.getenv('GATE_FUTURES_API_KEY', ''),
            gate_api_secret=os.getenv('GATE_FUTURES_API_SECRET', ''),
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
        leverage=config.get_int('margin.leverage', 2),
    )
    cfg = ReconciliationConfig(
        enabled=config.get_bool('reconciliation.enabled', True),
        retention_days=config.get_int('reconciliation.retention_days', 30),
        leverage=config.get_int('margin.leverage', 2),
        ignored_binance_spot_assets=get_ignored_binance_spot_assets(),
    )
    return Reconciler(executor, cfg)


class Reconciler:
    """交易所持仓对账器。"""

    def __init__(self, executor: RealExecutor, cfg: Optional[ReconciliationConfig] = None):
        self.executor = executor
        self.cfg = cfg or ReconciliationConfig()

    def run_once(self) -> Dict:
        """执行一轮对账并落库，返回本轮摘要。"""
        snapshot_at = datetime.now()
        rows: List[Dict] = []

        local_spot = self._load_local_spot_positions()
        local_gate = self._load_local_gate_positions()

        try:
            binance_balances = self.executor.fetch_binance_spot_balances()
            rows.extend(self._compare_binance(snapshot_at, local_spot, binance_balances))
        except Exception as e:
            logger.warning(f'Binance 现货对账拉取失败: {e}', exc_info=True)
            rows.append(self._error_row(snapshot_at, 'binance', e))

        try:
            gate_positions = self.executor.fetch_gate_futures_positions()
            rows.extend(self._compare_gate(snapshot_at, local_gate, gate_positions))
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
        }

    def cleanup_old_snapshots(self):
        """按配置清理历史快照。"""
        if self.cfg.retention_days <= 0:
            return
        cutoff = datetime.now() - timedelta(days=self.cfg.retention_days)
        with db_manager.get_cursor() as cursor:
            cursor.execute("DELETE FROM mi_recon_snapshot WHERE snapshot_at < %s", (cutoff,))

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
