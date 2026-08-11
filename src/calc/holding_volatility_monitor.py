# coding: utf-8
"""Persisted bell alerts for held contracts with extreme 24h upside volatility."""
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Callable, Dict, Iterable, List, Optional, Set

from calc.popup_notification_store import upsert_popup_notification
from common.config import config
from common.database import db_manager
from common.logger import get_logger


logger = get_logger(__name__)

CREATE_STATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mi_holding_volatility_alert_state (
    base_asset VARCHAR(64) NOT NULL PRIMARY KEY,
    active TINYINT(1) NOT NULL DEFAULT 0,
    episode_id BIGINT NOT NULL DEFAULT 0,
    notification_sent_at DATETIME NULL,
    triggered_at DATETIME NULL,
    recovered_at DATETIME NULL,
    last_amplitude_pct DECIMAL(18,8) NULL,
    last_range_position DECIMAL(18,8) NULL,
    last_price DECIMAL(28,12) NULL,
    high_24h DECIMAL(28,12) NULL,
    low_24h DECIMAL(28,12) NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_holding_volatility_active (active, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


@dataclass(frozen=True)
class VolatilityAlertConfig:
    enabled: bool = True
    trigger_amplitude_pct: float = 50.0
    trigger_range_position: float = 0.8
    recover_amplitude_pct: float = 40.0
    recover_range_position: float = 0.6

    @classmethod
    def load(cls) -> 'VolatilityAlertConfig':
        prefix = 'holding_volatility_alert'
        return cls(
            enabled=config.get_bool(f'{prefix}.enabled', True),
            trigger_amplitude_pct=config.get_float(f'{prefix}.trigger_amplitude_pct', 50.0),
            trigger_range_position=config.get_float(f'{prefix}.trigger_range_position', 0.8),
            recover_amplitude_pct=config.get_float(f'{prefix}.recover_amplitude_pct', 40.0),
            recover_range_position=config.get_float(f'{prefix}.recover_range_position', 0.6),
        )

    def validate(self) -> None:
        values = (
            self.trigger_amplitude_pct,
            self.trigger_range_position,
            self.recover_amplitude_pct,
            self.recover_range_position,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError('持仓振幅提醒阈值必须是有限数值')
        if self.trigger_amplitude_pct <= 0 or self.recover_amplitude_pct < 0:
            raise ValueError('持仓振幅提醒阈值必须为非负数')
        if not 0 <= self.recover_range_position < self.trigger_range_position <= 1:
            raise ValueError('持仓振幅提醒区间位置必须满足 0<=恢复阈值<触发阈值<=1')
        if self.recover_amplitude_pct >= self.trigger_amplitude_pct:
            raise ValueError('持仓振幅提醒恢复阈值必须低于触发阈值')


@dataclass(frozen=True)
class VolatilitySnapshot:
    base_asset: str
    amplitude_pct: float
    range_position: float
    last_price: float
    high_24h: float
    low_24h: float


@dataclass(frozen=True)
class PendingVolatilityAlert:
    snapshot: VolatilitySnapshot
    episode_id: int


def evaluate_transition(
    *,
    active: bool,
    snapshot: Optional[VolatilitySnapshot],
    is_holding: bool,
    cfg: VolatilityAlertConfig,
) -> str:
    """Return trigger/recover/hold without treating missing market data as recovery."""
    if not is_holding:
        return 'recover' if active else 'hold'
    if snapshot is None:
        return 'hold'
    if active:
        if (
            snapshot.amplitude_pct < cfg.recover_amplitude_pct
            or snapshot.range_position < cfg.recover_range_position
        ):
            return 'recover'
        return 'hold'
    if (
        snapshot.amplitude_pct >= cfg.trigger_amplitude_pct
        and snapshot.range_position >= cfg.trigger_range_position
    ):
        return 'trigger'
    return 'hold'


def _finite_float(value) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def build_volatility_snapshots(contracts: Iterable[Dict]) -> Dict[str, VolatilitySnapshot]:
    snapshots: Dict[str, VolatilitySnapshot] = {}
    for contract in contracts:
        base_asset = str(contract.get('base_asset') or '').strip().upper()
        amplitude = _finite_float(contract.get('range_24h_pct'))
        position = _finite_float(contract.get('range_position_24h'))
        last_price = _finite_float(contract.get('last_price'))
        high_24h = _finite_float(contract.get('high_24h'))
        low_24h = _finite_float(contract.get('low_24h'))
        if (
            not base_asset
            or amplitude is None
            or position is None
            or last_price is None
            or high_24h is None
            or low_24h is None
        ):
            continue
        snapshots[base_asset] = VolatilitySnapshot(
            base_asset=base_asset,
            amplitude_pct=amplitude,
            range_position=position,
            last_price=last_price,
            high_24h=high_24h,
            low_24h=low_24h,
        )
    return snapshots


class HoldingVolatilityStateStore:
    def ensure_table(self) -> None:
        with db_manager.get_cursor() as cursor:
            cursor.execute(CREATE_STATE_TABLE_SQL)

    def fetch_holding_assets(self) -> Set[str]:
        with db_manager.get_cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT UPPER(TRIM(base_asset)) AS base_asset
                FROM mi_trade_position
                WHERE status = 'holding'
                  AND base_asset IS NOT NULL
                  AND TRIM(base_asset) <> ''
                """
            )
            return {
                str(row.get('base_asset') or '').strip().upper()
                for row in (cursor.fetchall() or [])
                if row.get('base_asset')
            }

    def apply_transitions(
        self,
        holding_assets: Set[str],
        snapshots: Dict[str, VolatilitySnapshot],
        cfg: VolatilityAlertConfig,
    ) -> List[PendingVolatilityAlert]:
        now = datetime.now()
        pending: List[PendingVolatilityAlert] = []
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            if holding_assets:
                cursor.executemany(
                    "INSERT IGNORE INTO mi_holding_volatility_alert_state (base_asset) VALUES (%s)",
                    [(asset,) for asset in sorted(holding_assets)],
                )
            cursor.execute("SELECT * FROM mi_holding_volatility_alert_state FOR UPDATE")
            states = {
                str(row.get('base_asset') or '').strip().upper(): row
                for row in (cursor.fetchall() or [])
            }

            for asset, state in states.items():
                active = bool(state.get('active'))
                snapshot = snapshots.get(asset)
                transition = evaluate_transition(
                    active=active,
                    snapshot=snapshot,
                    is_holding=asset in holding_assets,
                    cfg=cfg,
                )
                if transition == 'recover':
                    cursor.execute(
                        """
                        UPDATE mi_holding_volatility_alert_state
                        SET active = 0, recovered_at = %s
                        WHERE base_asset = %s
                        """,
                        (now, asset),
                    )
                    continue

                if snapshot is not None:
                    cursor.execute(
                        """
                        UPDATE mi_holding_volatility_alert_state
                        SET last_amplitude_pct = %s, last_range_position = %s,
                            last_price = %s, high_24h = %s, low_24h = %s
                        WHERE base_asset = %s
                        """,
                        (
                            snapshot.amplitude_pct,
                            snapshot.range_position,
                            snapshot.last_price,
                            snapshot.high_24h,
                            snapshot.low_24h,
                            asset,
                        ),
                    )

                if transition == 'trigger' and snapshot is not None:
                    episode_id = int(state.get('episode_id') or 0) + 1
                    cursor.execute(
                        """
                        UPDATE mi_holding_volatility_alert_state
                        SET active = 1, episode_id = %s, notification_sent_at = NULL,
                            triggered_at = %s, recovered_at = NULL
                        WHERE base_asset = %s
                        """,
                        (episode_id, now, asset),
                    )
                    pending.append(PendingVolatilityAlert(snapshot, episode_id))
                elif active and state.get('notification_sent_at') is None:
                    retry_snapshot = snapshot or _snapshot_from_state(asset, state)
                    if retry_snapshot is not None:
                        pending.append(PendingVolatilityAlert(
                            retry_snapshot,
                            int(state.get('episode_id') or 0),
                        ))
            cursor.close()
        return pending

    def mark_notification_sent(self, base_asset: str, episode_id: int) -> None:
        with db_manager.get_cursor() as cursor:
            cursor.execute(
                """
                UPDATE mi_holding_volatility_alert_state
                SET notification_sent_at = NOW()
                WHERE base_asset = %s AND active = 1 AND episode_id = %s
                """,
                (base_asset, episode_id),
            )


def _snapshot_from_state(base_asset: str, state: Dict) -> Optional[VolatilitySnapshot]:
    values = [
        _finite_float(state.get(key))
        for key in ('last_amplitude_pct', 'last_range_position', 'last_price', 'high_24h', 'low_24h')
    ]
    if any(value is None for value in values):
        return None
    return VolatilitySnapshot(base_asset, *values)


class HoldingVolatilityMonitor:
    def __init__(
        self,
        *,
        cfg: Optional[VolatilityAlertConfig] = None,
        store: Optional[HoldingVolatilityStateStore] = None,
        notifier: Callable[..., Dict] = upsert_popup_notification,
    ) -> None:
        self.cfg = cfg or VolatilityAlertConfig.load()
        self.store = store or HoldingVolatilityStateStore()
        self.notifier = notifier

    def refresh(self, contracts: Iterable[Dict]) -> int:
        if not self.cfg.enabled:
            return 0
        self.cfg.validate()
        self.store.ensure_table()
        holdings = self.store.fetch_holding_assets()
        snapshots = build_volatility_snapshots(contracts)
        pending = self.store.apply_transitions(holdings, snapshots, self.cfg)
        sent = 0
        for event in pending:
            snapshot = event.snapshot
            try:
                self.notifier(
                    title=f'持仓合约24h振幅过高: {snapshot.base_asset}',
                    message=(
                        f'{snapshot.base_asset} 合约24h振幅 {snapshot.amplitude_pct:.2f}%，'
                        f'最新价位于24h高低区间的 {snapshot.range_position * 100:.1f}%；'
                        f'最新={snapshot.last_price:g}，最高={snapshot.high_24h:g}，最低={snapshot.low_24h:g}。'
                    ),
                    type='warning',
                    source='holding_volatility',
                    dedup_key=f'holding-volatility:{snapshot.base_asset}:{event.episode_id}',
                    event_at=datetime.now(),
                    payload={
                        'base_asset': snapshot.base_asset,
                        'episode_id': event.episode_id,
                        'amplitude_pct': snapshot.amplitude_pct,
                        'range_position': snapshot.range_position,
                        'last_price': snapshot.last_price,
                        'high_24h': snapshot.high_24h,
                        'low_24h': snapshot.low_24h,
                    },
                )
                self.store.mark_notification_sent(snapshot.base_asset, event.episode_id)
                sent += 1
            except Exception:
                logger.exception('持仓振幅铃铛写入失败，将在下一轮重试: %s', snapshot.base_asset)
        return sent


def refresh_holding_volatility_alerts(contracts: Iterable[Dict]) -> int:
    """Run after a successful Gate ticker refresh without letting alert failures break ETL."""
    try:
        return HoldingVolatilityMonitor().refresh(contracts)
    except Exception:
        logger.exception('持仓合约24h振幅监控失败')
        return 0
