"""Conservative permission for closing a hedge with an explained spot remainder."""
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from common.config import config

VERIFIED_SPOT_RESIDUAL = 'verified_spot_residual'
RESIDUAL_DETAIL_PREFIX = '微量现货残差已核验|'
EPSILON = Decimal('0.00000001')


def decimal_value(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def residual_enabled():
    return config.get_bool('reconciliation.spot_residual_close.enabled', False)


def residual_quantity(detail, spot_meta, price=None):
    """Return a bounded positive remainder; None means isolation is required."""
    try:
        spot, future, actual_spot, actual_future = [decimal_value(detail[k]) for k in (
            'local_spot_qty', 'local_gate_qty', 'binance_qty', 'gate_qty',
        )]
        size = decimal_value(detail['gate_detail']['size'])
        locked = decimal_value(detail['binance_detail']['locked'])
        mark = decimal_value(price if price is not None else detail['gate_detail']['mark_price'])
        minimum = decimal_value(spot_meta['min_notional'])
        step = decimal_value(spot_meta['step_size'])
        cap = decimal_value(config.get_float('reconciliation.spot_residual_close.max_usdt', 1.0))
        bps = decimal_value(config.get_float('reconciliation.spot_residual_close.max_bps', 1.0))
        positives = (spot, future, actual_spot, actual_future, mark, minimum, step, cap, bps)
        if any(v is None or v <= 0 for v in positives):
            return None
        if size is None or size >= 0 or locked != 0:
            return None
        if abs(spot - actual_spot) > EPSILON or abs(future - actual_future) > EPSILON:
            return None
        residual = spot - future
        if residual <= EPSILON or residual * mark > cap or residual * 10000 > future * bps:
            return None
        if residual * mark >= minimum:
            return None
        return residual
    except (KeyError, TypeError):
        return None


def proportional_spot_slice(spot_qty, slice_contracts, total_contracts, step_size):
    """Round down in decimal arithmetic, never exceed the exact allocated slice."""
    values = [decimal_value(v) for v in (spot_qty, slice_contracts, total_contracts, step_size)]
    if any(v is None or v <= 0 for v in values):
        raise ValueError('invalid_spot_slice_metadata')
    spot, part, total, step = values
    if part > total:
        raise ValueError('spot_slice_exceeds_position')
    return float((spot * part / total / step).to_integral_value(rounding=ROUND_DOWN) * step)
