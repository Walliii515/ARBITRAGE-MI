# coding: utf-8
"""执行结果复盘文本格式化。"""
from typing import Dict, Optional


def _fmt_num(value, digits: int = 1, suffix: str = '') -> str:
    if value is None:
        return 'NA'
    return f"{float(value):.{digits}f}{suffix}"


def format_execution_audit(exec_result: Dict) -> Optional[str]:
    """把成交引擎返回的 execution_stats 压成订单原因里的短文本。"""
    stats = exec_result.get('execution_stats') or {}
    maker = stats.get('future_maker') or {}
    if not maker:
        return None

    fill_ratio = maker.get('fill_ratio')
    fill_pct = float(fill_ratio) * 100 if fill_ratio is not None else None
    return (
        "执行("
        f"future_maker={'Y' if maker.get('attempted') else 'N'},"
        f"filled={'Y' if maker.get('filled') else 'N'},"
        f"fill={_fmt_num(fill_pct, 0, '%')},"
        f"wait={_fmt_num(maker.get('wait_ms'), 0, 'ms')}/{_fmt_num(maker.get('ttl_ms'), 0, 'ms')},"
        f"maker_px={_fmt_num(maker.get('maker_price'), 8)},"
        f"future_px={_fmt_num(maker.get('future_exec_price'), 8)},"
        f"spot_px={_fmt_num(maker.get('spot_exec_price'), 8)},"
        f"spot_ioc={'Y' if maker.get('spot_protective_ioc') else 'N'},"
        f"spot_ioc_px={_fmt_num(maker.get('spot_protective_price'), 8)},"
        f"spot_ioc_floor={_fmt_num(maker.get('spot_protective_min_basis_bps'), 1, 'bps')},"
        f"spot_retry={'Y' if maker.get('spot_retry_market_attempted') else 'N'},"
        f"spot_retry_filled={'Y' if maker.get('spot_retry_market_filled') else 'N'},"
        f"spot_retry_px={_fmt_num(maker.get('spot_retry_market_price'), 8)},"
        f"spot_unwind={'Y' if maker.get('spot_unwind_attempted') else 'N'},"
        f"spot_unwind_filled={'Y' if maker.get('spot_unwind_filled') else 'N'},"
        f"spot_unwind_px={_fmt_num(maker.get('spot_unwind_price'), 8)},"
        f"future_unwind={'Y' if maker.get('future_unwind_attempted') else 'N'},"
        f"future_unwind_filled={'Y' if maker.get('future_unwind_filled') else 'N'},"
        f"future_unwind_px={_fmt_num(maker.get('future_unwind_price'), 8)},"
        f"improve={_fmt_num(maker.get('improvement_bps'), 1, 'bps')},"
        f"fallback={'Y' if maker.get('fallback_attempted') else 'N'},"
        f"fallback_filled={'Y' if maker.get('fallback_filled') else 'N'},"
        f"fallback_px={_fmt_num(maker.get('fallback_protective_price'), 8)},"
        f"fallback_future_px={_fmt_num(maker.get('fallback_future_exec_price'), 8)}"
        ")"
    )
