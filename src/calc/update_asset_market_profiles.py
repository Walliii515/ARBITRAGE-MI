# coding: utf-8
"""
Update asset market profiles.

strategy_tier answers "can this asset belong to the trading pool"; market_profile
answers "what market-data/execution behavior should the open state machine expect".
The first version deliberately uses stable 24h liquidity inputs only. Runtime spread,
depth and freshness are still emitted for observation, but are not persisted here.
"""
from dataclasses import dataclass

from common.config import config
from common.database import db_manager
from common.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class MarketProfileThresholds:
    thin_spot_volume_usdt: float
    thin_future_volume_usdt: float


def _thresholds() -> MarketProfileThresholds:
    return MarketProfileThresholds(
        thin_spot_volume_usdt=config.get_float(
            'asset_market_profile.thin_spot_volume_24h_usdt',
            1_000_000.0,
        ),
        thin_future_volume_usdt=config.get_float(
            'asset_market_profile.thin_future_volume_24h_usdt',
            500_000.0,
        ),
    )


def update_asset_market_profiles() -> int:
    """Classify assets into normal/thin_bursty/illiquid_blocked profiles."""
    th = _thresholds()
    sql = """
        UPDATE mi_base_asset ba
        LEFT JOIN mi_binance_spot_info spot
            ON spot.base_asset = UPPER(TRIM(ba.base_asset))
        LEFT JOIN mi_gate_future_contracts fut
            ON fut.base_asset = UPPER(TRIM(ba.base_asset))
        SET
            ba.market_profile = CASE
                WHEN COALESCE(ba.strategy_tier, 'C') = 'C' THEN 'illiquid_blocked'
                WHEN COALESCE(spot.quote_volume, 0) < %(thin_spot)s
                  OR COALESCE(fut.volume_24h_settle, 0) < %(thin_future)s THEN 'thin_bursty'
                ELSE 'normal'
            END,
            ba.market_profile_reason = CONCAT(
                'liquidity: tier=', COALESCE(ba.strategy_tier, 'C'),
                ',spot24h=', ROUND(COALESCE(spot.quote_volume, 0), 0),
                ',future24h=', ROUND(COALESCE(fut.volume_24h_settle, 0), 0),
                ',thin_spot<', ROUND(%(thin_spot)s, 0),
                ',thin_future<', ROUND(%(thin_future)s, 0)
            ),
            ba.market_profile_updated_at = NOW()
        WHERE ba.is_valid = 'Y'
    """
    try:
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, {
                'thin_spot': th.thin_spot_volume_usdt,
                'thin_future': th.thin_future_volume_usdt,
            })
            affected = cursor.rowcount
            conn.commit()
        logger.info(
            '标的行情画像刷新完成 | affected=%s | thin_spot<%.0f thin_future<%.0f',
            affected,
            th.thin_spot_volume_usdt,
            th.thin_future_volume_usdt,
        )
        return int(affected or 0)
    except Exception as exc:
        logger.error('标的行情画像刷新失败: %s', exc, exc_info=True)
        return 0
