# coding: utf-8
"""
反向套利开仓信号监控。

职责边界：
- 只处理 reverse_status == candidate 的反向机会入表与监控；
- 不调用正向 TradingExecutor，不共享正向峰值/冷却/订单状态；
- 当前不真实下单，只在触底反弹和旁路风控通过后标记为可开仓观察态。
"""
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from common.database import db_manager
from common.logger import get_logger
from calc.calculate_hedge_metrics import calculate_hedge_metrics
from calc.merge_cross_exchange_orderbook import merge_orderbook_records
from calc.reverse_arbitrage import ReverseArbitrageConfig, enrich_reverse_opportunities

logger = get_logger(__name__)


@dataclass
class ReverseSignalMonitorConfig:
    open_amount_usdt: float
    monitor_timeout_sec: float = 60.0
    valley_rebound_pct: float = 0.05
    min_rebound_bps: float = 2.0
    min_monitor_sec: float = 1.5
    rebound_sustain_sec: float = 0.4
    max_orderbook_lag_ms: float = 500.0
    execution_enabled: bool = False


class ReverseSignalMonitor:
    """反向策略独立信号状态机。"""

    ACTIVE_STATUSES = {'monitoring'}
    TERMINAL_STATUSES = {'opened', 'conditions_lost', 'rejected', 'gate_rejected', 'monitor_timeout'}

    def __init__(
        self,
        cfg: ReverseSignalMonitorConfig,
        reverse_cfg: ReverseArbitrageConfig,
        contract_meta: Dict[str, Dict],
        spot_meta: Dict[str, Dict],
        reverse_threshold_meta: Dict[str, Dict],
    ):
        self.cfg = cfg
        self.reverse_cfg = reverse_cfg
        self.contract_meta = contract_meta
        self.spot_meta = spot_meta
        self.reverse_threshold_meta = reverse_threshold_meta
        self._gate_manager = None
        self._spot_manager = None
        self._states: Dict[str, Dict] = {}
        self._table_ready = False

    def update_meta(
        self,
        contract_meta: Dict[str, Dict],
        spot_meta: Dict[str, Dict],
        reverse_threshold_meta: Dict[str, Dict],
    ) -> None:
        self.contract_meta = contract_meta
        self.spot_meta = spot_meta
        self.reverse_threshold_meta = reverse_threshold_meta

    def set_orderbook_managers(self, gate_manager, spot_manager) -> None:
        self._gate_manager = gate_manager
        self._spot_manager = spot_manager

    def process_rows(self, rows: List[Dict], borrow_meta: Optional[Dict[str, Dict]] = None) -> List[Dict]:
        """处理一轮反向信号监控，返回本轮状态变化。"""
        self._ensure_table()
        self._load_active_states()

        now = datetime.now()
        results: List[Dict] = []
        candidate_by_asset: Dict[str, Dict] = {}

        for row in rows:
            base_asset = self._base_asset(row)
            if not base_asset:
                continue
            if row.get('reverse_status') == 'candidate':
                candidate_by_asset[base_asset] = row

        for base_asset in list(self._states):
            if base_asset not in candidate_by_asset:
                self._resolve_signal(base_asset, 'conditions_lost', '反向开仓前置条件丢失')
                results.append({'base_asset': base_asset, 'status': 'conditions_lost'})

        for base_asset, row in candidate_by_asset.items():
            result = self._process_candidate(base_asset, row, borrow_meta or {}, now)
            if result:
                results.append(result)

        return results

    def _process_candidate(
        self,
        base_asset: str,
        row: Dict,
        borrow_meta: Dict[str, Dict],
        now: datetime,
    ) -> Optional[Dict]:
        current_basis = self._as_float(row.get('reverse_basis_bps'))
        if current_basis is None:
            return None

        state = self._states.get(base_asset)
        if not state:
            signal_id = self._create_signal(row, current_basis, now)
            if not signal_id:
                return None
            if base_asset in self._states:
                state = self._states[base_asset]
            else:
                state = {
                    'id': signal_id,
                    'base_asset': base_asset,
                    'start_time': now,
                    'signal_basis_bps': current_basis,
                    'valley_basis_bps': current_basis,
                    'rebound_since': None,
                    'ready_notified': False,
                }
                self._states[base_asset] = state
                logger.info(f'反向信号进入监控 | {base_asset} | basis={current_basis:.2f}bps')
                return {'base_asset': base_asset, 'status': 'monitoring', 'created': True}

        duration_sec = int((now - state['start_time']).total_seconds())
        valley = float(state.get('valley_basis_bps', current_basis))
        if current_basis < valley:
            valley = current_basis
            state['valley_basis_bps'] = valley
            state['rebound_since'] = None

        self._update_monitoring_snapshot(base_asset, row, current_basis, valley, duration_sec)

        if duration_sec >= self.cfg.monitor_timeout_sec:
            self._resolve_signal(
                base_asset,
                'monitor_timeout',
                f'反向开仓监控超时(>{self.cfg.monitor_timeout_sec:.0f}s)',
                exit_basis_bps=current_basis,
            )
            return {'base_asset': base_asset, 'status': 'monitor_timeout'}

        if duration_sec < self.cfg.min_monitor_sec:
            return None

        required_rebound = self._required_rebound_bps(state, valley)
        rebound_bps = current_basis - valley
        if rebound_bps < required_rebound:
            state['rebound_since'] = None
            return None

        if state.get('rebound_since') is None:
            state['rebound_since'] = now
            return None

        sustain = (now - state['rebound_since']).total_seconds()
        if sustain < self.cfg.rebound_sustain_sec:
            return None

        if state.get('ready_notified'):
            return None

        passed, pre_gate_basis, reason = self._pre_execution_gate(base_asset, row, borrow_meta)
        if not passed:
            self._resolve_signal(
                base_asset,
                'gate_rejected',
                reason,
                exit_basis_bps=current_basis,
                trigger_type='valley_rebound',
                pre_gate_basis_bps=pre_gate_basis,
                rebound_basis_bps=current_basis,
            )
            return {'base_asset': base_asset, 'status': 'gate_rejected', 'reason': reason}

        state['ready_notified'] = True
        self._mark_ready_to_open(base_asset, current_basis, pre_gate_basis, duration_sec)
        return {'base_asset': base_asset, 'status': 'monitoring', 'trigger_type': 'valley_rebound'}

    def _required_rebound_bps(self, state: Dict, valley_basis: float) -> float:
        signal_basis = float(state.get('signal_basis_bps', valley_basis))
        drop_bps = abs(signal_basis - valley_basis)
        pct_rebound = max(drop_bps * self.cfg.valley_rebound_pct, 0.0)
        return max(pct_rebound, self.cfg.min_rebound_bps)

    def _pre_execution_gate(
        self,
        base_asset: str,
        row: Dict,
        borrow_meta: Dict[str, Dict],
    ) -> Tuple[bool, Optional[float], str]:
        if not self._gate_manager or not self._spot_manager:
            return True, self._as_float(row.get('reverse_basis_bps')), ''

        contract = row.get('contract') or f'{base_asset}_USDT'
        symbol = row.get('symbol') or f'{base_asset}USDT'
        try:
            gate_ob = self._gate_manager.get_orderbook(contract)
            spot_ob = self._spot_manager.get_orderbook(symbol)
            if not gate_ob or not spot_ob:
                return False, None, f'盘口不可用(gate={gate_ob is not None}, spot={spot_ob is not None})'

            now_ts = time.time()
            gate_local_ts = float(getattr(gate_ob, 'last_update_time', 0) or 0)
            spot_local_ts = float(getattr(spot_ob, 'last_update_time', 0) or 0)
            gate_lag_ms = (now_ts - gate_local_ts) * 1000.0 if gate_local_ts > 0 else float('inf')
            spot_lag_ms = (now_ts - spot_local_ts) * 1000.0 if spot_local_ts > 0 else float('inf')
            if gate_lag_ms > self.cfg.max_orderbook_lag_ms or spot_lag_ms > self.cfg.max_orderbook_lag_ms:
                return False, None, (
                    f'行情滞后(gate_lag={gate_lag_ms:.0f}ms, spot_lag={spot_lag_ms:.0f}ms, '
                    f'max={self.cfg.max_orderbook_lag_ms:.0f}ms)'
                )

            gate_ready = getattr(gate_ob, 'is_ready', lambda: True)()
            if not gate_ready:
                return False, None, 'Gate本地簿未接上连续WS增量'

            skew_ms = abs(gate_local_ts - spot_local_ts) * 1000.0
            if skew_ms > self.cfg.max_orderbook_lag_ms:
                return False, None, (
                    f'跨所盘口不同步(skew={skew_ms:.0f}ms, max={self.cfg.max_orderbook_lag_ms:.0f}ms)'
                )

            merged = merge_orderbook_records([gate_ob.to_dict_row()], [spot_ob.to_dict_row()])
            if not merged:
                return False, None, '盘口合并失败'
            merged = calculate_hedge_metrics(merged, self.contract_meta, self.spot_meta, self.cfg.open_amount_usdt)
            check_rows = [dict(merged[0])]
            check_rows[0]['base_asset'] = base_asset
            check_rows[0]['contract'] = contract
            check_rows[0]['symbol'] = symbol
            c_meta = self.contract_meta.get(base_asset, {})
            check_rows[0]['funding_rate_24h'] = c_meta.get('funding_rate_24h')
            enrich_reverse_opportunities(
                check_rows,
                self.contract_meta,
                self.reverse_cfg,
                borrow_meta=borrow_meta,
                reverse_threshold_meta=self.reverse_threshold_meta,
            )
            check = check_rows[0]
            pre_gate_basis = self._as_float(check.get('reverse_basis_bps'))
            if check.get('reverse_status') != 'candidate':
                return False, pre_gate_basis, f"旁路复核失败({check.get('reverse_status') or 'unknown'})"

            if not self.cfg.execution_enabled:
                return True, pre_gate_basis, '反向执行器未启用，仅记录可开仓观察态'

            return True, pre_gate_basis, ''
        except Exception as exc:
            logger.warning(f'反向开仓旁路异常 | {base_asset}: {exc}', exc_info=True)
            return False, None, f'旁路异常({str(exc)[:120]})'

    def _create_signal(self, row: Dict, basis_bps: float, now: datetime) -> Optional[int]:
        base_asset = self._base_asset(row)
        active_id = self._find_active_signal_id(base_asset)
        if active_id:
            return active_id

        sql = """
            INSERT INTO mi_reverse_trade_signal (
                base_asset, contract, symbol, status, signal_time, duration_sec,
                funding_rate_24h, reverse_open_basis_bps, signal_basis_bps, valley_basis_bps,
                reverse_open_basis_p20, reverse_close_basis_p20, margin_edge_bps,
                borrow_hourly_rate, borrow_24h_bps, borrow_limit, borrow_capacity_usdt,
                open_coverage, capacity_usdt, open_amount_usdt
            ) VALUES (
                %(base_asset)s, %(contract)s, %(symbol)s, 'monitoring', %(signal_time)s, 0,
                %(funding_rate_24h)s, %(basis)s, %(basis)s, %(basis)s,
                %(reverse_open_basis_p20)s, %(reverse_close_basis_p20)s, %(margin_edge_bps)s,
                %(borrow_hourly_rate)s, %(borrow_24h_bps)s, %(borrow_limit)s, %(borrow_capacity_usdt)s,
                %(open_coverage)s, %(capacity_usdt)s, %(open_amount_usdt)s
            )
        """
        params = self._signal_params(row, basis_bps, now)
        try:
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.lastrowid
        except Exception as exc:
            logger.error(f'反向信号创建失败 | {base_asset}: {exc}', exc_info=True)
            return None

    def _update_monitoring_snapshot(
        self,
        base_asset: str,
        row: Dict,
        current_basis: float,
        valley_basis: float,
        duration_sec: int,
    ) -> None:
        state = self._states.get(base_asset)
        if not state:
            return
        sql = """
            UPDATE mi_reverse_trade_signal
            SET duration_sec = %(duration_sec)s,
                reverse_open_basis_bps = %(basis)s,
                valley_basis_bps = %(valley)s,
                margin_edge_bps = %(margin_edge_bps)s,
                borrow_hourly_rate = %(borrow_hourly_rate)s,
                borrow_24h_bps = %(borrow_24h_bps)s,
                borrow_limit = %(borrow_limit)s,
                borrow_capacity_usdt = %(borrow_capacity_usdt)s,
                open_coverage = %(open_coverage)s,
                capacity_usdt = %(capacity_usdt)s
            WHERE id = %(id)s AND status = 'monitoring'
        """
        params = self._signal_params(row, current_basis, datetime.now())
        params.update({
            'id': state['id'],
            'duration_sec': duration_sec,
            'valley': round(valley_basis, 2),
        })
        try:
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, params)
        except Exception as exc:
            logger.warning(f'反向信号监控快照更新失败 | {base_asset}: {exc}')

    def _mark_ready_to_open(
        self,
        base_asset: str,
        rebound_basis: float,
        pre_gate_basis: Optional[float],
        duration_sec: int,
    ) -> None:
        state = self._states.get(base_asset)
        if not state:
            return
        sql = """
            UPDATE mi_reverse_trade_signal
            SET duration_sec = %(duration_sec)s,
                trigger_type = 'valley_rebound',
                rebound_basis_bps = %(rebound_basis)s,
                pre_gate_basis_bps = %(pre_gate_basis)s,
                reject_reason = %(reason)s
            WHERE id = %(id)s AND status = 'monitoring'
        """
        reason = '触底反弹与旁路风控已通过；反向执行器未启用，继续保持监控'
        try:
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, {
                    'duration_sec': duration_sec,
                    'rebound_basis': round(rebound_basis, 2),
                    'pre_gate_basis': round(pre_gate_basis, 2) if pre_gate_basis is not None else None,
                    'reason': reason,
                    'id': state['id'],
                })
        except Exception as exc:
            logger.warning(f'反向信号可开仓观察态更新失败 | {base_asset}: {exc}')

    def _resolve_signal(
        self,
        base_asset: str,
        status: str,
        reason: Optional[str],
        exit_basis_bps: Optional[float] = None,
        trigger_type: Optional[str] = None,
        pre_gate_basis_bps: Optional[float] = None,
        rebound_basis_bps: Optional[float] = None,
    ) -> None:
        state = self._states.get(base_asset)
        if not state:
            return
        now = datetime.now()
        duration_sec = int((now - state['start_time']).total_seconds())
        sql = """
            UPDATE mi_reverse_trade_signal
            SET status = %(status)s,
                resolved_time = %(resolved_time)s,
                duration_sec = %(duration_sec)s,
                trigger_type = %(trigger_type)s,
                reject_reason = %(reason)s,
                reverse_open_basis_bps = %(exit_basis)s,
                rebound_basis_bps = %(rebound_basis)s,
                pre_gate_basis_bps = %(pre_gate_basis)s
            WHERE id = %(id)s
        """
        try:
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, {
                    'status': status,
                    'resolved_time': now,
                    'duration_sec': duration_sec,
                    'trigger_type': trigger_type,
                    'reason': reason,
                    'exit_basis': round(exit_basis_bps, 2) if exit_basis_bps is not None else None,
                    'rebound_basis': round(rebound_basis_bps, 2) if rebound_basis_bps is not None else None,
                    'pre_gate_basis': round(pre_gate_basis_bps, 2) if pre_gate_basis_bps is not None else None,
                    'id': state['id'],
                })
        except Exception as exc:
            logger.error(f'反向信号结束失败 | {base_asset}: {exc}', exc_info=True)
        self._states.pop(base_asset, None)

    def _load_active_states(self) -> None:
        if self._states:
            return
        cutoff = datetime.now() - timedelta(seconds=max(self.cfg.monitor_timeout_sec * 2, 120))
        sql = """
            SELECT id, base_asset, signal_time, signal_basis_bps, valley_basis_bps, trigger_type
            FROM mi_reverse_trade_signal
            WHERE status = 'monitoring'
              AND signal_time >= %s
            ORDER BY signal_time ASC
        """
        try:
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, (cutoff,))
                rows = cursor.fetchall()
            for row in rows:
                base_asset = self._base_asset(row)
                if not base_asset:
                    continue
                signal_time = row.get('signal_time') or datetime.now()
                self._states[base_asset] = {
                    'id': row.get('id'),
                    'base_asset': base_asset,
                    'start_time': signal_time,
                    'signal_basis_bps': self._as_float(row.get('signal_basis_bps')),
                    'valley_basis_bps': self._as_float(row.get('valley_basis_bps')),
                    'rebound_since': None,
                    'ready_notified': row.get('trigger_type') == 'valley_rebound',
                }
        except Exception as exc:
            logger.warning(f'反向活跃信号加载失败: {exc}')

    def _find_active_signal_id(self, base_asset: str) -> Optional[int]:
        sql = """
            SELECT id, signal_time, signal_basis_bps, valley_basis_bps, trigger_type
            FROM mi_reverse_trade_signal
            WHERE base_asset = %s AND status = 'monitoring'
            ORDER BY signal_time DESC
            LIMIT 1
        """
        try:
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, (base_asset,))
                row = cursor.fetchone()
            if not row:
                return None
            signal_time = row.get('signal_time') or datetime.now()
            self._states[base_asset] = {
                'id': row.get('id'),
                'base_asset': base_asset,
                'start_time': signal_time,
                'signal_basis_bps': self._as_float(row.get('signal_basis_bps')),
                'valley_basis_bps': self._as_float(row.get('valley_basis_bps')),
                'rebound_since': None,
                'ready_notified': row.get('trigger_type') == 'valley_rebound',
            }
            return row.get('id')
        except Exception as exc:
            logger.warning(f'反向活跃信号查询失败 | {base_asset}: {exc}')
            return None

    def _signal_params(self, row: Dict, basis_bps: float, now: datetime) -> Dict:
        return {
            'base_asset': self._base_asset(row),
            'contract': row.get('contract'),
            'symbol': row.get('symbol'),
            'signal_time': now,
            'funding_rate_24h': self._as_float(row.get('funding_rate_24h')),
            'basis': round(basis_bps, 2),
            'reverse_open_basis_p20': self._round_or_none(row.get('reverse_open_basis_p20')),
            'reverse_close_basis_p20': self._round_or_none(row.get('reverse_close_basis_p20')),
            'margin_edge_bps': self._round_or_none(row.get('reverse_margin_edge_bps')),
            'borrow_hourly_rate': self._as_float(row.get('reverse_borrow_hourly_rate')),
            'borrow_24h_bps': self._round_or_none(row.get('reverse_borrow_24h_bps')),
            'borrow_limit': self._as_float(row.get('reverse_borrow_limit')),
            'borrow_capacity_usdt': self._round_or_none(row.get('reverse_borrow_capacity_usdt')),
            'open_coverage': self._as_float(row.get('reverse_open_coverage')),
            'capacity_usdt': self._round_or_none(row.get('reverse_capacity_usdt')),
            'open_amount_usdt': self.cfg.open_amount_usdt,
        }

    def _ensure_table(self) -> None:
        if self._table_ready:
            return
        sql = """
            CREATE TABLE IF NOT EXISTS mi_reverse_trade_signal (
                id INT AUTO_INCREMENT PRIMARY KEY,
                base_asset VARCHAR(20) NOT NULL,
                contract VARCHAR(50) DEFAULT NULL,
                symbol VARCHAR(50) DEFAULT NULL,
                status ENUM('monitoring','opened','conditions_lost','rejected','gate_rejected','monitor_timeout') NOT NULL DEFAULT 'monitoring',
                signal_time DATETIME NOT NULL,
                resolved_time DATETIME DEFAULT NULL,
                duration_sec INT DEFAULT NULL,
                trigger_type VARCHAR(32) DEFAULT NULL,
                reject_reason TEXT DEFAULT NULL,
                order_uuid VARCHAR(64) DEFAULT NULL,
                funding_rate_24h DECIMAL(18,10) DEFAULT NULL,
                reverse_open_basis_bps DECIMAL(10,2) DEFAULT NULL,
                signal_basis_bps DECIMAL(10,2) DEFAULT NULL,
                valley_basis_bps DECIMAL(10,2) DEFAULT NULL,
                rebound_basis_bps DECIMAL(10,2) DEFAULT NULL,
                pre_gate_basis_bps DECIMAL(10,2) DEFAULT NULL,
                actual_basis_bps DECIMAL(10,2) DEFAULT NULL,
                reverse_open_basis_p20 DECIMAL(10,2) DEFAULT NULL,
                reverse_close_basis_p20 DECIMAL(10,2) DEFAULT NULL,
                margin_edge_bps DECIMAL(10,2) DEFAULT NULL,
                borrow_hourly_rate DECIMAL(18,10) DEFAULT NULL,
                borrow_24h_bps DECIMAL(10,2) DEFAULT NULL,
                borrow_limit DECIMAL(24,8) DEFAULT NULL,
                borrow_capacity_usdt DECIMAL(18,2) DEFAULT NULL,
                open_coverage DECIMAL(10,4) DEFAULT NULL,
                capacity_usdt DECIMAL(18,2) DEFAULT NULL,
                open_amount_usdt DECIMAL(18,2) DEFAULT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_reverse_signal_time (signal_time),
                INDEX idx_reverse_status_time (status, signal_time),
                INDEX idx_reverse_asset_status (base_asset, status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='反向套利交易信号'
        """
        try:
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql)
                self._upgrade_existing_table(cursor)
            self._table_ready = True
        except Exception as exc:
            logger.error(f'反向信号表初始化失败: {exc}', exc_info=True)
            raise

    def _upgrade_existing_table(self, cursor) -> None:
        """兼容 main 上已存在的旧版反向信号表。"""
        cursor.execute("SHOW COLUMNS FROM mi_reverse_trade_signal")
        columns = {row['Field']: row for row in cursor.fetchall()}

        add_columns = {
            'symbol': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN symbol VARCHAR(50) DEFAULT NULL AFTER contract",
            'duration_sec': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN duration_sec INT DEFAULT NULL AFTER resolved_time",
            'trigger_type': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN trigger_type VARCHAR(32) DEFAULT NULL AFTER duration_sec",
            'order_uuid': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN order_uuid VARCHAR(64) DEFAULT NULL AFTER reject_reason",
            'reverse_open_basis_bps': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN reverse_open_basis_bps DECIMAL(10,2) DEFAULT NULL AFTER funding_rate_24h",
            'signal_basis_bps': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN signal_basis_bps DECIMAL(10,2) DEFAULT NULL AFTER reverse_open_basis_bps",
            'valley_basis_bps': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN valley_basis_bps DECIMAL(10,2) DEFAULT NULL AFTER signal_basis_bps",
            'rebound_basis_bps': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN rebound_basis_bps DECIMAL(10,2) DEFAULT NULL AFTER valley_basis_bps",
            'pre_gate_basis_bps': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN pre_gate_basis_bps DECIMAL(10,2) DEFAULT NULL AFTER rebound_basis_bps",
            'actual_basis_bps': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN actual_basis_bps DECIMAL(10,2) DEFAULT NULL AFTER pre_gate_basis_bps",
            'margin_edge_bps': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN margin_edge_bps DECIMAL(10,2) DEFAULT NULL AFTER reverse_close_basis_p20",
            'borrow_hourly_rate': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN borrow_hourly_rate DECIMAL(18,10) DEFAULT NULL AFTER margin_edge_bps",
            'borrow_24h_bps': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN borrow_24h_bps DECIMAL(10,2) DEFAULT NULL AFTER borrow_hourly_rate",
            'borrow_limit': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN borrow_limit DECIMAL(24,8) DEFAULT NULL AFTER borrow_24h_bps",
            'borrow_capacity_usdt': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN borrow_capacity_usdt DECIMAL(18,2) DEFAULT NULL AFTER borrow_limit",
            'open_coverage': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN open_coverage DECIMAL(10,4) DEFAULT NULL AFTER borrow_capacity_usdt",
            'capacity_usdt': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN capacity_usdt DECIMAL(18,2) DEFAULT NULL AFTER open_coverage",
            'open_amount_usdt': "ALTER TABLE mi_reverse_trade_signal ADD COLUMN open_amount_usdt DECIMAL(18,2) DEFAULT NULL AFTER capacity_usdt",
        }
        for column, ddl in add_columns.items():
            if column not in columns:
                cursor.execute(ddl)

        if 'reverse_status' in columns:
            cursor.execute("ALTER TABLE mi_reverse_trade_signal MODIFY COLUMN reverse_status VARCHAR(64) DEFAULT NULL")

        cursor.execute(
            """
            ALTER TABLE mi_reverse_trade_signal
            MODIFY COLUMN status ENUM(
                'candidate',
                'monitoring',
                'opened',
                'conditions_lost',
                'rejected',
                'gate_rejected',
                'monitor_timeout'
            ) NOT NULL DEFAULT 'monitoring'
            """
        )
        cursor.execute("UPDATE mi_reverse_trade_signal SET status = 'monitoring' WHERE status = 'candidate'")
        cursor.execute(
            """
            ALTER TABLE mi_reverse_trade_signal
            MODIFY COLUMN status ENUM(
                'monitoring',
                'opened',
                'conditions_lost',
                'rejected',
                'gate_rejected',
                'monitor_timeout'
            ) NOT NULL DEFAULT 'monitoring'
            """
        )

    @staticmethod
    def _base_asset(row: Dict) -> str:
        return str(row.get('base_asset') or '').strip().upper()

    @staticmethod
    def _as_float(value) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _round_or_none(cls, value) -> Optional[float]:
        number = cls._as_float(value)
        return round(number, 2) if number is not None else None
