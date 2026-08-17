# coding: utf-8
"""
统一日志工具
- 默认输出目录: <repo>/src/log/app.log
- 文件按天滚动 (TimedRotatingFileHandler, when='midnight')，保留 30 天
- 同时输出到 stdout（保留终端体验）和文件
- 仅依赖 Python 标准库 logging，无第三方依赖
- 默认级别 INFO，可通过环境变量 LOG_LEVEL/LOG_DIR/LOG_FILENAME/LOG_BACKUP_COUNT 覆盖

对外 API:
    setup_logging(...)        显式初始化（一般无需手动调用，首次 get_logger 会自动初始化）
    get_logger(name=None)     获取一个 logger，等价于 logging.getLogger
    log_print(*args, ...)     与内置 print 兼容的日志函数，按 INFO 级别记录
"""
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional, TextIO

_INITIALIZED = False
_DEFAULT_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s:%(funcName)s:%(lineno)d | %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"
_PRINT_LOGGER_NAME = "print"


class _SafeTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Keep writing to the current file when Windows file locks block rollover."""

    def rotate(self, source: str, dest: str) -> None:
        try:
            super().rotate(source, dest)
        except OSError:
            return


def _configure_stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding='utf-8', errors='replace')
        except (OSError, ValueError, AttributeError):
            continue


def _project_log_dir() -> Path:
    """默认日志目录: <repo>/src/log"""
    here = Path(__file__).resolve()
    # here = src/common/logger.py -> parents[1] = src
    return here.parents[1] / "log"


def setup_logging(level: Optional[str] = None,
                  log_dir: Optional[str] = None,
                  filename: Optional[str] = None,
                  backup_count: Optional[int] = None,
                  force: bool = False) -> logging.Logger:
    """
    初始化全局日志系统（幂等；多次调用默认不会重复添加 handler）

    Args:
        level: 日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL），默认读取 LOG_LEVEL，否则 INFO
        log_dir: 日志目录，默认读取 LOG_DIR，否则 <repo>/src/log
        filename: 日志文件名，默认读取 LOG_FILENAME，否则 app.log
        backup_count: 保留多少个历史归档，默认读取 LOG_BACKUP_COUNT，否则 30
        force: 是否强制重建 handler（用于在测试或 reload 时重置）
    """
    global _INITIALIZED
    if _INITIALIZED and not force:
        return logging.getLogger()

    level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    level_value = getattr(logging, level_name, logging.INFO)

    log_dir_path = Path(log_dir or os.getenv("LOG_DIR") or _project_log_dir())
    log_dir_path.mkdir(parents=True, exist_ok=True)

    file_name = filename or os.getenv("LOG_FILENAME", "app.log")
    backup = int(backup_count if backup_count is not None
                 else os.getenv("LOG_BACKUP_COUNT", "30"))

    formatter = logging.Formatter(_DEFAULT_FORMAT, _DEFAULT_DATEFMT)

    root = logging.getLogger()
    root.setLevel(level_value)

    if force:
        for h in list(root.handlers):
            root.removeHandler(h)
            try:
                h.close()
            except OSError:
                pass

    _configure_stdio_utf8()

    # 终端 handler（stdout）
    if not any(getattr(h, "_arb_console", False) for h in root.handlers):
        console = logging.StreamHandler(stream=sys.stdout)
        console.setLevel(level_value)
        console.setFormatter(formatter)
        console._arb_console = True  # 标记，便于幂等判断
        root.addHandler(console)

    # 文件 handler（按天滚动，保留 backup_count 天）
    if not any(getattr(h, "_arb_file", False) for h in root.handlers):
        file_path = log_dir_path / file_name
        file_handler = _SafeTimedRotatingFileHandler(
            filename=str(file_path),
            when="midnight",
            interval=1,
            backupCount=backup,
            encoding="utf-8",
            delay=False,
            utc=False,
        )
        file_handler.suffix = "%Y-%m-%d"
        file_handler.setLevel(level_value)
        file_handler.setFormatter(formatter)
        file_handler._arb_file = True
        root.addHandler(file_handler)

    # 抑制部分第三方过于啰嗦的 INFO 日志
    for noisy in ("urllib3", "websockets", "websocket", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _INITIALIZED = True
    logging.getLogger(__name__).debug(
        "logger initialized: dir=%s file=%s level=%s backup=%s",
        log_dir_path, file_name, level_name, backup,
    )
    return root


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """获取一个 logger（首次调用会自动初始化日志系统）"""
    if not _INITIALIZED:
        setup_logging()
    return logging.getLogger(name if name else "app")


def log_print(*args: object, sep: str = " ", end: str = "\n", file: Optional[TextIO] = None,
              flush: bool = False, level: int = logging.INFO) -> None:
    """
    与内置 print 兼容的日志函数：将参数按 print 语义拼接后写入日志，
    同时输出到终端与文件。用法等价于 print：

        log_print("foo", value, sep=" | ")

    备注:
        - end / file / flush 参数仅为兼容签名，实际由 logging 控制刷写
        - 使用固定 logger 名 "print"，便于在日志中过滤纯打印类输出
    """
    if not _INITIALIZED:
        setup_logging()
    msg = sep.join(str(a) for a in args)
    # 去掉自定义 end 的尾部，避免日志末尾多出非换行字符
    if end and end != "\n" and msg.endswith(end):
        msg = msg[: -len(end)]
    # stacklevel=2 让日志中的 funcName/lineno 指向真正的调用方，而不是本函数
    logging.getLogger(_PRINT_LOGGER_NAME).log(level, msg, stacklevel=2)
