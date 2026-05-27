# coding: utf-8
"""
ETL 数据管道 - 统一调度所有数据采集、计算与清理任务

通过 ETL_TASKS 注册表统一管理所有任务：
- 每个任务定义名称、描述、执行函数、调度频率、是否启用
- 支持两种调度频率：
  · interval: 每隔 N 分钟执行（常规 ETL，由 orderbook_server 的 _refresh_meta_loop 驱动）
  · daily:    每天指定时刻执行一次（由 DailyScheduler 守护线程驱动）

对外暴露：
- run_etl_pipeline()          : 执行所有 interval 类型的已启用任务
- start_daily_schedulers()    : 启动所有 daily 类型任务的定时调度器
- stop_daily_schedulers()     : 停止所有 daily 定时调度器
"""
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from common.config import config
from common.database import db_manager
from common.logger import get_logger

logger = get_logger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 任务定义
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class ETLTask:
    """
    ETL 任务描述对象

    Attributes:
        name:        任务唯一标识，用于日志和查找
        description: 任务说明（做什么、写哪张表、更新策略）
        runner:      实际执行函数（无参数，阻塞执行）
        schedule:    调度类型
                     - 'interval': 跟随常规 ETL 循环（每 N 分钟），
                                   频率由 config trade.meta_refresh_interval_min 控制
                     - 'daily':    每天固定时刻执行一次
        daily_hour:  daily 模式的执行小时（0-23），interval 模式忽略
        daily_minute:daily 模式的执行分钟（0-59），interval 模式忽略
        enabled:     是否启用，False 则跳过执行
        kwargs:      传递给 runner 的额外关键字参数
    """
    name: str
    description: str
    runner: Callable
    schedule: str = 'interval'           # 'interval' | 'daily'
    daily_hour: int = 0
    daily_minute: int = 0
    enabled: bool = True
    kwargs: dict = field(default_factory=dict)


def _run_update_gate_future_contracts():
    """
    Gate.io 永续合约信息更新

    数据源：Gate.io Futures API (contracts + tickers)
    目标表：mi_gate_future_contracts
    更新策略：全删全进
    写入内容：合约基本信息（quanto_multiplier, order_price_round 等）
              + 24h 成交额（volume_24h_settle）
              + 当期资金费率（funding_rate, funding_rate_24h）
              + 下次支付时间（funding_next_apply）
    """
    from calc.update_gate_future_contracts import update_gate_future_contracts
    update_gate_future_contracts()


def _run_update_binance_spot_info():
    """
    Binance 现货交易对信息更新

    数据源：Binance exchangeInfo API + 24h Ticker API
    目标表：mi_binance_spot_info
    更新策略：全删全进
    写入内容：交易对基本信息（step_size, tick_size, min_qty）
              + 24h 报价成交额（quote_volume）
    """
    from calc.update_binance_spot_info import update_binance_spot_info
    update_binance_spot_info()


def _run_update_gate_future_his_funding_rate():
    """
    Gate.io 历史资金费率更新

    数据源：Gate.io Futures Funding Rate History API（每批最多 10 个合约）
    目标表：mi_gate_future_his_funding_rates
    更新策略：增量更新（INSERT IGNORE，按 contract+timestamp 唯一索引去重）
    写入内容：每个合约的历史资金费率（funding_rate, funding_rate_24h, timestamp）
    用途：为后续阈值计算提供历史样本
    """
    from calc.update_gate_future_his_funding_rate import update_gate_future_his_funding_rates
    update_gate_future_his_funding_rates()


def _run_calculate_funding_rate_threshold():
    """
    资金费率分位阈值计算

    数据源：mi_gate_future_his_funding_rates（最近 max_days 天）
    目标表：mi_gate_future_funding_rate_threshold
    更新策略：UPSERT（按 contract 唯一键）
    计算内容：每个合约正的 24h 资金费率的 20%/30%/40% 分位数、最大值、最小值
    用途：前端资金费率过滤开关的阈值依据
    """
    from calc.calculate_funding_rate_threshold import count_positive_funding_rates
    count_positive_funding_rates(max_days=30)


def _run_cleanup_vwap_snapshots():
    """
    VWAP 基差快照数据清理

    目标表：mi_vwap_basis_snapshot
    操作：DELETE 超过保留天数的历史快照记录
    保留天数：config trade.vwap_snapshot_retention_days（默认 14 天）
    """
    retention_days = config.get_int('trade.vwap_snapshot_retention_days', 14)
    try:
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM mi_vwap_basis_snapshot WHERE snapshot_time < DATE_SUB(NOW(), INTERVAL %s DAY)",
                (retention_days,)
            )
            deleted = cursor.rowcount
            conn.commit()
            if deleted > 0:
                logger.info(f'已清理 {deleted} 条过期 VWAP 快照数据（保留{retention_days}天）')
    except Exception as e:
        logger.error(f'清理VWAP快照失败: {e}')


def _run_daily_vwap_analysis():
    """
    VWAP 基差分位阈值 - 每日批量统计

    数据源：mi_vwap_basis_snapshot（最近 lookback_days 天的实时快照）
    目标表：mi_vwap_basis_threshold
    更新策略：UPSERT（按 base_asset + calc_date 唯一键）
    计算内容：
      - 开仓: 按 base_asset 分组，计算 open_vwap_basis_bps 的 max/min/mean/std/p10~p40
        open_basis_pX = 从大到小前 X% 的分界点，基差越大越有利
      - 平仓: 按 base_asset 分组，计算 close_vwap_basis_bps 的 max/min/mean/std/p10~p40
        close_basis_pX = 升序第 X 分位，基差越小越有利（价差已收敛）
    用途：按标的设置差异化开仓/平仓 VWAP 基差阈值
    """
    from calc.calculate_vwap_basis_threshold import run_analysis

    lookback_days = config.get_int('trade.vwap_threshold_lookback_days', 7)

    logger.info(f'开始每日VWAP基差分位阈值计算 (lookback={lookback_days})')
    try:
        run_analysis(lookback_days)
        logger.info('每日VWAP基差分位阈值计算完成')
    except Exception as e:
        logger.error(f'每日VWAP基差分位阈值计算失败: {e}', exc_info=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 任务注册表
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ETL_TASKS: List[ETLTask] = [
    # ── interval 类型：跟随常规 ETL 循环（默认每 15 分钟） ──
    ETLTask(
        name='update_gate_contracts',
        description='Gate.io 永续合约信息 → mi_gate_future_contracts（全删全进）',
        runner=_run_update_gate_future_contracts,
        schedule='interval',
    ),
    ETLTask(
        name='update_binance_spot',
        description='Binance 现货交易对信息 → mi_binance_spot_info（全删全进）',
        runner=_run_update_binance_spot_info,
        schedule='interval',
    ),
    ETLTask(
        name='update_funding_rate_history',
        description='Gate.io 历史资金费率 → mi_gate_future_his_funding_rates（增量）',
        runner=_run_update_gate_future_his_funding_rate,
        schedule='interval',
    ),
    ETLTask(
        name='calc_funding_rate_threshold',
        description='资金费率分位阈值 → mi_gate_future_funding_rate_threshold（UPSERT）',
        runner=_run_calculate_funding_rate_threshold,
        schedule='interval',
    ),
    ETLTask(
        name='cleanup_vwap_snapshots',
        description='清理过期 VWAP 基差快照 ← mi_vwap_basis_snapshot（DELETE）',
        runner=_run_cleanup_vwap_snapshots,
        schedule='daily',
        daily_hour=0,
        daily_minute=0,
    ),

    # ── daily 类型：每天固定时刻执行 ──
    ETLTask(
        name='daily_vwap_threshold',
        description='VWAP 基差分位阈值每日统计 → mi_vwap_basis_threshold（UPSERT）',
        runner=_run_daily_vwap_analysis,
        schedule='daily',
        daily_hour=0,
        daily_minute=0,
    ),
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 常规 ETL 执行入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_etl_pipeline():
    """
    执行所有 schedule='interval' 且 enabled=True 的任务

    由 orderbook_server 的 _refresh_meta_loop 在线程池中定时调用，
    执行频率由 config trade.meta_refresh_interval_min 控制（默认 15 分钟）。
    """
    interval_tasks = [t for t in ETL_TASKS if t.schedule == 'interval' and t.enabled]
    logger.info(f'开始常规 ETL 管道，共 {len(interval_tasks)} 个任务')

    for task in interval_tasks:
        try:
            logger.info(f'[ETL] 执行: {task.name} - {task.description}')
            task.runner(**task.kwargs)
        except Exception as e:
            # 单个任务失败不影响后续任务执行
            logger.error(f'[ETL] 任务 {task.name} 失败: {e}', exc_info=True)

    logger.info('常规 ETL 管道执行完毕')


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 每日定时调度器
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DailyScheduler:
    """
    简单的每日定时任务调度器

    在指定时刻执行一次任务，通过后台守护线程驱动。
    每 60 秒检查一次 stop 信号，确保能及时响应停止请求。
    """

    def __init__(self, target: Callable, hour: int = 0, minute: int = 0, name: str = 'daily'):
        self._target = target
        self._hour = hour
        self._minute = minute
        self._name = name
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _next_run_time(self) -> datetime:
        """计算下一次执行时间"""
        now = datetime.now()
        target_today = now.replace(hour=self._hour, minute=self._minute, second=0, microsecond=0)
        if now >= target_today:
            return target_today + timedelta(days=1)
        return target_today

    def _run_loop(self):
        """守护线程主循环"""
        while not self._stop_event.is_set():
            next_run = self._next_run_time()
            wait_seconds = (next_run - datetime.now()).total_seconds()
            logger.info(f'[{self._name}] 下次执行时间: {next_run.strftime("%Y-%m-%d %H:%M:%S")} (等待 {wait_seconds:.0f}s)')

            while wait_seconds > 0 and not self._stop_event.is_set():
                sleep_time = min(wait_seconds, 60)
                self._stop_event.wait(sleep_time)
                wait_seconds = (next_run - datetime.now()).total_seconds()

            if self._stop_event.is_set():
                break

            logger.info(f'[{self._name}] 开始执行每日任务...')
            try:
                self._target()
            except Exception as e:
                logger.error(f'[{self._name}] 每日任务执行失败: {e}', exc_info=True)

    def start(self):
        """启动调度器守护线程"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name=f'scheduler-{self._name}', daemon=True)
        self._thread.start()
        logger.info(f'[{self._name}] 每日调度器已启动 (执行时间: {self._hour:02d}:{self._minute:02d})')

    def stop(self):
        """停止调度器"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info(f'[{self._name}] 每日调度器已停止')


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 每日调度器统一管理
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_daily_schedulers: Dict[str, DailyScheduler] = {}


def start_daily_schedulers():
    """
    根据 ETL_TASKS 注册表，启动所有 schedule='daily' 且 enabled=True 的任务调度器

    在 orderbook_server lifespan 启动时调用。
    """
    stop_daily_schedulers()  # 先清理旧的

    daily_tasks = [t for t in ETL_TASKS if t.schedule == 'daily' and t.enabled]
    for task in daily_tasks:
        scheduler = DailyScheduler(
            target=task.runner,
            hour=task.daily_hour,
            minute=task.daily_minute,
            name=task.name,
        )
        scheduler.start()
        _daily_schedulers[task.name] = scheduler

    logger.info(f'已启动 {len(_daily_schedulers)} 个每日调度器: {list(_daily_schedulers.keys())}')


def stop_daily_schedulers():
    """
    停止所有已启动的每日调度器

    在 orderbook_server lifespan 关闭时调用。
    """
    for name, scheduler in _daily_schedulers.items():
        scheduler.stop()
    _daily_schedulers.clear()
