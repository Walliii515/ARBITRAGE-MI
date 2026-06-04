# coding: utf-8
"""
通用配置加载工具

- 默认从 `src/config/config.yaml` 加载 YAML 配置
- 支持通过点路径访问嵌套字段（如 `orderbook.settle`）
- 优先级：环境变量 > YAML 配置 > 默认值
- 提供全局单例 `config`，业务模块直接 `from common.config import config`
"""
import os
import threading
from typing import Any, Optional

import yaml


_DEFAULT_CONFIG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', 'config', 'config.yaml')
)


class Config:
    """轻量级 YAML 配置加载器，支持点路径访问与环境变量覆盖"""

    def __init__(self, path: Optional[str] = None):
        self._path = path or os.getenv('APP_CONFIG_PATH') or _DEFAULT_CONFIG_PATH
        self._lock = threading.Lock()
        self._data: dict = {}
        self.reload()

    def reload(self) -> None:
        """重新读取配置文件（线程安全）"""
        with self._lock:
            if not os.path.isfile(self._path):
                self._data = {}
                return
            with open(self._path, 'r', encoding='utf-8') as f:
                loaded = yaml.safe_load(f) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f'配置文件根节点必须是 mapping: {self._path}')
            self._data = loaded

    @property
    def path(self) -> str:
        return self._path

    def get(self, key: str, default: Any = None, env: Optional[str] = None) -> Any:
        """
        获取配置项，支持点路径（如 'orderbook.settle'）。

        Args:
            key:    点分隔的配置键
            default:不存在时返回的默认值
            env:    可选环境变量名，若已设置则优先使用其值
        """
        if env:
            env_val = os.getenv(env)
            if env_val is not None and env_val != '':
                return env_val

        node: Any = self._data
        for part in key.split('.'):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def get_int(self, key: str, default: int = 0, env: Optional[str] = None) -> int:
        return int(self.get(key, default, env=env))

    def get_float(self, key: str, default: float = 0.0, env: Optional[str] = None) -> float:
        return float(self.get(key, default, env=env))

    def get_str(self, key: str, default: str = '', env: Optional[str] = None) -> str:
        value = self.get(key, default, env=env)
        return '' if value is None else str(value)

    def get_bool(self, key: str, default: bool = False, env: Optional[str] = None) -> bool:
        value = self.get(key, default, env=env)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 'yes', 'y', 'on')
        return bool(value)

    # ─── 运行模式统一入口（virtual / testnet / live） ───
    # 主参数：trade.mode，则其他参数（成交引擎 URL、交易所环境）由它派生。
    # 环境变量 TRADE_MODE 可临时覆盖；EXCHANGE_ENV 仅作为老变量向后兼容。
    _VALID_TRADE_MODES = ('virtual', 'testnet', 'live')

    def get_trade_mode(self) -> str:
        """获取交易运行模式：virtual（虚拟盘） / testnet（模拟盘） / live（实盘）。

        优先级：环境变量 TRADE_MODE > config.yaml 的 trade.mode > 默认 virtual。
        对未知枚举值返回 virtual（安全默认，避免误上实盘）。
        兼容老别名 'mainnet' → 视为 'live'。
        """
        mode = self.get_str('trade.mode', default='virtual', env='TRADE_MODE').lower()
        if mode == 'mainnet':
            mode = 'live'  # 向后兼容旧配置
        return mode if mode in self._VALID_TRADE_MODES else 'virtual'

    def get_executor_url(self) -> str:
        """成交引擎 HTTP 地址。

        - virtual 模式 → virtual_executor_service（默认 8081）
        - testnet/live 模式 → real_executor_service（默认 8082）
        - 如需自定义主机/端口，可在 config.yaml 显式设置 trade.executor.url 覆盖。
        """
        explicit = self.get_str('trade.executor.url', default='').strip()
        if explicit:
            return explicit
        return 'http://localhost:8081' if self.get_trade_mode() == 'virtual' else 'http://localhost:8082'

    def get_real_executor_env(self) -> str:
        """真实成交引擎使用的交易所环境：mainnet 或 testnet（交易所术语）。

        由 trade.mode 派生：live → mainnet，其余（virtual/testnet）→ testnet。
        """
        return 'mainnet' if self.get_trade_mode() == 'live' else 'testnet'


# 全局单例
config = Config()
