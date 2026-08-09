# coding: utf-8
"""Process-local ownership for forward asset reduction execution."""
import threading
from contextlib import contextmanager
from typing import Dict, Iterator, Optional


class AssetReductionGuard:
    """Allow only one close/remediation owner per asset at a time."""

    def __init__(self):
        self._lock = threading.Lock()
        self._owners: Dict[str, Dict[str, object]] = {}

    @contextmanager
    def claim(self, base_asset: str, owner: str) -> Iterator[bool]:
        asset = str(base_asset or '').strip().upper()
        if not asset:
            yield False
            return

        acquired = False
        thread_id = threading.get_ident()
        with self._lock:
            active = self._owners.get(asset)
            if active is None:
                self._owners[asset] = {
                    'owner': owner,
                    'thread_id': thread_id,
                    'depth': 1,
                }
                acquired = True
            elif active.get('thread_id') == thread_id:
                active['depth'] = int(active.get('depth') or 0) + 1
                acquired = True
        try:
            yield acquired
        finally:
            if acquired:
                with self._lock:
                    active = self._owners.get(asset)
                    if active and active.get('thread_id') == thread_id:
                        depth = int(active.get('depth') or 1) - 1
                        if depth <= 0:
                            self._owners.pop(asset, None)
                        else:
                            active['depth'] = depth

    def owner(self, base_asset: str) -> Optional[str]:
        asset = str(base_asset or '').strip().upper()
        with self._lock:
            active = self._owners.get(asset)
            return str(active.get('owner')) if active else None


asset_reduction_guard = AssetReductionGuard()
