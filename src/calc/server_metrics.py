# coding: utf-8
"""
服务器关键指标采集与查询。

生产环境为 Ubuntu ECS，优先读取 /proc；不额外引入 psutil 依赖。
"""
import json
import os
import platform
import shutil
import socket
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from common.database import db_manager
from common.logger import get_logger


logger = get_logger(__name__)


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mi_server_metric_snapshot (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
    snapshot_at DATETIME NOT NULL COMMENT '采样时间',
    hostname VARCHAR(128) NOT NULL COMMENT '主机名',
    cpu_usage_percent DECIMAL(8,4) NULL COMMENT 'CPU使用率百分比',
    load1 DECIMAL(10,4) NULL COMMENT '1分钟负载',
    load5 DECIMAL(10,4) NULL COMMENT '5分钟负载',
    load15 DECIMAL(10,4) NULL COMMENT '15分钟负载',
    cpu_count INT NULL COMMENT 'CPU核心数',
    memory_total_bytes BIGINT NULL COMMENT '内存总量',
    memory_used_bytes BIGINT NULL COMMENT '内存已用',
    memory_usage_percent DECIMAL(8,4) NULL COMMENT '内存使用率百分比',
    disk_path VARCHAR(255) NOT NULL DEFAULT '/' COMMENT '磁盘采样路径',
    disk_total_bytes BIGINT NULL COMMENT '硬盘总量',
    disk_used_bytes BIGINT NULL COMMENT '硬盘已用',
    disk_usage_percent DECIMAL(8,4) NULL COMMENT '硬盘使用率百分比',
    uptime_sec BIGINT NULL COMMENT '系统启动秒数',
    detail JSON NULL COMMENT '附加原始信息',
    INDEX idx_snapshot_at (snapshot_at),
    INDEX idx_hostname_snapshot (hostname, snapshot_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='服务器指标快照'
"""


def ensure_server_metric_table() -> None:
    with db_manager.get_cursor() as cursor:
        cursor.execute(CREATE_TABLE_SQL)


def _read_cpu_times() -> Optional[Tuple[int, int]]:
    try:
        with open('/proc/stat', 'r', encoding='utf-8') as f:
            parts = f.readline().split()
    except OSError:
        return None
    if not parts or parts[0] != 'cpu':
        return None
    values = [int(v) for v in parts[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return idle, total


def _cpu_usage_percent(sample_interval_sec: float = 0.2) -> Optional[float]:
    first = _read_cpu_times()
    if not first:
        return None
    time.sleep(sample_interval_sec)
    second = _read_cpu_times()
    if not second:
        return None
    idle_delta = second[0] - first[0]
    total_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0)), 4)


def _memory_info() -> Dict[str, Optional[float]]:
    try:
        with open('/proc/meminfo', 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return {'memory_total_bytes': None, 'memory_used_bytes': None, 'memory_usage_percent': None}

    values: Dict[str, int] = {}
    for line in lines:
        key, _, raw_value = line.partition(':')
        parts = raw_value.strip().split()
        if parts:
            values[key] = int(parts[0]) * 1024

    total = values.get('MemTotal')
    available = values.get('MemAvailable')
    if not total or available is None:
        return {'memory_total_bytes': total, 'memory_used_bytes': None, 'memory_usage_percent': None}

    used = max(0, total - available)
    return {
        'memory_total_bytes': total,
        'memory_used_bytes': used,
        'memory_usage_percent': round(used / total * 100.0, 4),
    }


def _uptime_sec() -> Optional[int]:
    try:
        with open('/proc/uptime', 'r', encoding='utf-8') as f:
            return int(float(f.readline().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def _decode_mount_path(value: str) -> str:
    return value.replace('\\040', ' ')


def _collect_local_disk_filesystems() -> List[Dict[str, Any]]:
    """Collect mounted local block filesystems and de-duplicate bind mounts."""
    filesystems: Dict[str, Dict[str, Any]] = {}
    try:
        with open('/proc/self/mountinfo', 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return []

    for line in lines:
        parts = line.strip().split()
        if '-' not in parts or len(parts) < 10:
            continue
        sep = parts.index('-')
        mount_point = _decode_mount_path(parts[4])
        fs_type = parts[sep + 1]
        source = parts[sep + 2]
        if not source.startswith('/dev/'):
            continue
        if fs_type in {'squashfs', 'tmpfs', 'devtmpfs'}:
            continue
        try:
            usage = shutil.disk_usage(mount_point)
        except OSError:
            continue

        key = os.path.realpath(source)
        existing = filesystems.get(key)
        if existing:
            existing['mount_points'].append(mount_point)
            if len(mount_point) < len(existing['mount_point']):
                existing['mount_point'] = mount_point
            continue

        filesystems[key] = {
            'source': source,
            'mount_point': mount_point,
            'mount_points': [mount_point],
            'fstype': fs_type,
            'total_bytes': usage.total,
            'used_bytes': usage.used,
            'free_bytes': usage.free,
            'usage_percent': round(usage.used / usage.total * 100.0, 4) if usage.total else None,
        }

    return sorted(filesystems.values(), key=lambda item: item['mount_point'])


def _disk_info(disk_path: str) -> Dict[str, Any]:
    filesystems = _collect_local_disk_filesystems()
    if filesystems:
        total = sum(int(item.get('total_bytes') or 0) for item in filesystems)
        used = sum(int(item.get('used_bytes') or 0) for item in filesystems)
        return {
            'disk_path': 'all',
            'disk_total_bytes': total,
            'disk_used_bytes': used,
            'disk_usage_percent': round(used / total * 100.0, 4) if total else None,
            'disk_filesystems': filesystems,
            'disk_sample_mode': 'all_local_filesystems',
        }

    disk = shutil.disk_usage(disk_path)
    return {
        'disk_path': disk_path,
        'disk_total_bytes': disk.total,
        'disk_used_bytes': disk.used,
        'disk_usage_percent': round(disk.used / disk.total * 100.0, 4) if disk.total else None,
        'disk_filesystems': [{
            'source': disk_path,
            'mount_point': disk_path,
            'mount_points': [disk_path],
            'fstype': '',
            'total_bytes': disk.total,
            'used_bytes': disk.used,
            'free_bytes': disk.free,
            'usage_percent': round(disk.used / disk.total * 100.0, 4) if disk.total else None,
        }],
        'disk_sample_mode': 'single_path',
    }


def collect_server_metrics(disk_path: str = '/') -> Dict[str, Any]:
    load1 = load5 = load15 = None
    if hasattr(os, 'getloadavg'):
        try:
            load1, load5, load15 = os.getloadavg()
        except OSError:
            pass

    memory = _memory_info()
    disk = _disk_info(disk_path)
    now = datetime.now().replace(microsecond=0)

    return {
        'snapshot_at': now.strftime('%Y-%m-%d %H:%M:%S'),
        'hostname': socket.gethostname(),
        'cpu_usage_percent': _cpu_usage_percent(),
        'load1': round(load1, 4) if load1 is not None else None,
        'load5': round(load5, 4) if load5 is not None else None,
        'load15': round(load15, 4) if load15 is not None else None,
        'cpu_count': os.cpu_count(),
        **memory,
        'disk_path': disk['disk_path'],
        'disk_total_bytes': disk['disk_total_bytes'],
        'disk_used_bytes': disk['disk_used_bytes'],
        'disk_usage_percent': disk['disk_usage_percent'],
        'uptime_sec': _uptime_sec(),
        'detail': {
            'platform': platform.platform(),
            'python': platform.python_version(),
            'disk_sample_mode': disk['disk_sample_mode'],
            'disk_filesystems': disk['disk_filesystems'],
        },
    }


def record_server_metrics(disk_path: str = '/', retention_days: int = 14) -> Dict[str, Any]:
    ensure_server_metric_table()
    snapshot = collect_server_metrics(disk_path=disk_path)
    cutoff = datetime.now() - timedelta(days=max(1, retention_days))
    sql = """
        INSERT INTO mi_server_metric_snapshot (
            snapshot_at, hostname, cpu_usage_percent, load1, load5, load15, cpu_count,
            memory_total_bytes, memory_used_bytes, memory_usage_percent,
            disk_path, disk_total_bytes, disk_used_bytes, disk_usage_percent,
            uptime_sec, detail
        ) VALUES (
            %(snapshot_at)s, %(hostname)s, %(cpu_usage_percent)s, %(load1)s, %(load5)s, %(load15)s, %(cpu_count)s,
            %(memory_total_bytes)s, %(memory_used_bytes)s, %(memory_usage_percent)s,
            %(disk_path)s, %(disk_total_bytes)s, %(disk_used_bytes)s, %(disk_usage_percent)s,
            %(uptime_sec)s, %(detail_json)s
        )
    """
    payload = dict(snapshot)
    payload['detail_json'] = json.dumps(snapshot.get('detail') or {}, ensure_ascii=False)
    with db_manager.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM mi_server_metric_snapshot WHERE snapshot_at < %s", (cutoff,))
            cursor.execute(sql, payload)
    return snapshot


def _normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(row)
    for key, value in list(normalized.items()):
        if isinstance(value, datetime):
            normalized[key] = value.strftime('%Y-%m-%d %H:%M:%S')
        elif isinstance(value, Decimal):
            normalized[key] = float(value)
    detail = normalized.get('detail')
    if isinstance(detail, str):
        try:
            normalized['detail'] = json.loads(detail)
        except json.JSONDecodeError:
            normalized['detail'] = {}
    return normalized


def list_server_metrics(days: int = 7) -> List[Dict[str, Any]]:
    ensure_server_metric_table()
    days = max(1, min(int(days), 30))
    with db_manager.get_cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM mi_server_metric_snapshot
            WHERE snapshot_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            ORDER BY snapshot_at ASC
            """,
            (days,),
        )
        return [_normalize_row(row) for row in cursor.fetchall()]


def get_latest_server_metrics() -> Optional[Dict[str, Any]]:
    ensure_server_metric_table()
    with db_manager.get_cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM mi_server_metric_snapshot
            ORDER BY snapshot_at DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
    return _normalize_row(row) if row else None
