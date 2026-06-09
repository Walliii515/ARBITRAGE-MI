# coding: utf-8
"""
基础持仓对账。

只读交易所真实持仓，与本地 mi_trade_position 的 holding 聚合值做差，
写入 mi_recon_snapshot；当 Gate 侧实仓缺失/减少时，识别 ADL 并标记持仓风险。
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
    mark_exchange_risk: bool = True
    adl_lookback_sec: int = 24 * 3600


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
        mark_exchange_risk=config.get_bool('reconciliation.mark_exchange_risk', True),
        adl_lookback_sec=config.get_int('reconciliation.adl_lookback_sec', 24 * 3600),
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
            gate_rows = self._compare_gate(snapshot_at, local_gate, gate_positions)
            if self.cfg.mark_exchange_risk:
                self._mark_gate_desync_risks(snapshot_at, gate_rows)
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

    def _mark_gate_desync_risks(self, snapshot_at: datetime, rows: List[Dict]):
        """Gate 实仓小于本地 holding 时标记持仓；ADL 通过 Gate my_trades text 识别。"""
        for row in rows:
            if row.get('exchange') != 'gate' or row.get('dimension') != 'position':
                continue
            local_value = float(row.get('local_value') or 0)
            exchange_value = float(row.get('exchange_value') or 0)
            if local_value <= 0 or exchange_value + GATE_FUTURE_CONTRACT_TOLERANCE >= local_value:
                continue

            base_asset = str(row.get('base_asset') or '').upper()
            if not base_asset:
                continue

            risk = self._detect_gate_desync_risk(base_asset, snapshot_at, local_value, exchange_value)
            row.setdefault('detail', {})
            row['detail']['exchange_risk'] = risk
            updated = self._mark_positions_exchange_risk(base_asset, risk)
            if updated:
                logger.warning(
                    "Gate 持仓对账发现断腿风险 | asset=%s | type=%s | local=%s | exchange=%s | marked=%s",
                    base_asset, risk.get('type'), local_value, exchange_value, updated,
                )

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
        }

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
                'detail': str(risk.get('detail') or '')[:1000],
                'reason': reason[:500],
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
