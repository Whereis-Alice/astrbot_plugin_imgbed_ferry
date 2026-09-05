"""KV 持久化：去重表、上传记录、每日配额。

AstrBot 的 `Star.put_kv_data` / `get_kv_data` 依赖插件注册信息，在单测或异常环境下
可能不可用，所以这里全部走 try/except 兜底：KV 挂了只会退化成「仅内存」，不影响上传。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from datetime import date
from typing import Any

from .config import BehaviorConfig
from .logs import logger

KEY_DEDUPE = "dedupe_index"
KEY_RECORDS = "upload_records"
KEY_QUOTA = "daily_quota"

SCOPE_USER = "user"
SCOPE_GROUP = "group"


def _today() -> str:
    return date.today().isoformat()


class FerryStore:
    """一层薄薄的持久化封装，所有失败都降级而不抛出。"""

    def __init__(self, owner: Any, behavior: BehaviorConfig | None = None) -> None:
        self._owner = owner
        self._behavior = behavior or BehaviorConfig()
        self._lock = asyncio.Lock()
        self._loaded = False
        self._dedupe: dict[str, dict[str, Any]] = {}
        self._records: list[dict[str, Any]] = []
        self._quota: dict[str, Any] = {"date": _today(), SCOPE_USER: {}, SCOPE_GROUP: {}}

    def update_config(self, behavior: BehaviorConfig) -> None:
        self._behavior = behavior

    # ---------- KV 原语 ----------

    async def _kv_get(self, key: str, default: Any) -> Any:
        getter = getattr(self._owner, "get_kv_data", None)
        if not callable(getter):
            return default
        try:
            value = await getter(key, default)
        except Exception as exc:
            logger.debug("[图床摆渡] 读取 KV %s 失败：%s", key, exc)
            return default
        return default if value is None else value

    async def _kv_put(self, key: str, value: Any) -> None:
        setter = getattr(self._owner, "put_kv_data", None)
        if not callable(setter):
            return
        try:
            await setter(key, value)
        except Exception as exc:
            logger.debug("[图床摆渡] 写入 KV %s 失败：%s", key, exc)

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        dedupe = await self._kv_get(KEY_DEDUPE, {})
        if isinstance(dedupe, Mapping):
            self._dedupe = {
                str(digest): dict(entry)
                for digest, entry in dedupe.items()
                if isinstance(entry, Mapping)
            }
        records = await self._kv_get(KEY_RECORDS, [])
        if isinstance(records, list):
            self._records = [dict(item) for item in records if isinstance(item, Mapping)]
        quota = await self._kv_get(KEY_QUOTA, None)
        if isinstance(quota, Mapping) and str(quota.get("date") or "") == _today():
            self._quota = {
                "date": _today(),
                SCOPE_USER: dict(quota.get(SCOPE_USER) or {}),
                SCOPE_GROUP: dict(quota.get(SCOPE_GROUP) or {}),
            }
        self._prune_dedupe()

    # ---------- 去重表 ----------

    def _prune_dedupe(self) -> None:
        ttl_days = max(0, int(self._behavior.dedupe_ttl_days))
        if ttl_days:
            deadline = time.time() - ttl_days * 86400
            for digest, entry in list(self._dedupe.items()):
                try:
                    stamp = float(entry.get("ts") or 0.0)
                except (TypeError, ValueError):
                    stamp = 0.0
                if stamp < deadline:
                    self._dedupe.pop(digest, None)
        overflow = len(self._dedupe) - max(1, int(self._behavior.dedupe_max_entries))
        if overflow > 0:
            ordered = sorted(
                self._dedupe.items(),
                key=lambda item: float(item[1].get("ts") or 0.0),
            )
            for digest, _ in ordered[:overflow]:
                self._dedupe.pop(digest, None)

    async def lookup(self, digest: str) -> dict[str, Any] | None:
        """按内容 sha256 查已上传过的 URL。"""
        if not self._behavior.dedupe_enabled or not digest:
            return None
        async with self._lock:
            await self._ensure_loaded()
            entry = self._dedupe.get(digest)
            if not entry or not entry.get("url"):
                return None
            ttl_days = max(0, int(self._behavior.dedupe_ttl_days))
            if ttl_days:
                try:
                    stamp = float(entry.get("ts") or 0.0)
                except (TypeError, ValueError):
                    stamp = 0.0
                if stamp < time.time() - ttl_days * 86400:
                    self._dedupe.pop(digest, None)
                    return None
            return dict(entry)

    async def remember(
        self,
        digest: str,
        *,
        url: str,
        file_id: str = "",
        name: str = "",
        size: int = 0,
    ) -> None:
        if not self._behavior.dedupe_enabled or not digest or not url:
            return
        async with self._lock:
            await self._ensure_loaded()
            self._dedupe[digest] = {
                "url": url,
                "file_id": file_id,
                "name": name,
                "size": int(size),
                "ts": time.time(),
            }
            self._prune_dedupe()
            await self._kv_put(KEY_DEDUPE, dict(self._dedupe))

    async def drop(self, digest: str) -> None:
        """删除图床上的文件后要把去重项一起撤掉，否则会返回死链。"""
        if not digest:
            return
        async with self._lock:
            await self._ensure_loaded()
            if self._dedupe.pop(digest, None) is not None:
                await self._kv_put(KEY_DEDUPE, dict(self._dedupe))

    async def drop_by_file_id(self, file_id: str) -> int:
        text = str(file_id or "").strip()
        if not text:
            return 0
        async with self._lock:
            await self._ensure_loaded()
            removed = [
                digest
                for digest, entry in self._dedupe.items()
                if str(entry.get("file_id") or "") == text
            ]
            for digest in removed:
                self._dedupe.pop(digest, None)
            if removed:
                await self._kv_put(KEY_DEDUPE, dict(self._dedupe))
            return len(removed)

    # ---------- 上传记录 ----------

    async def add_records(self, entries: list[dict[str, Any]]) -> None:
        limit = max(0, int(self._behavior.record_limit))
        if not entries or not limit:
            return
        async with self._lock:
            await self._ensure_loaded()
            self._records.extend(entries)
            if len(self._records) > limit:
                self._records = self._records[-limit:]
            await self._kv_put(KEY_RECORDS, list(self._records))

    async def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        """最近的上传记录，新的在前。"""
        async with self._lock:
            await self._ensure_loaded()
            count = max(1, int(limit))
            return [dict(item) for item in reversed(self._records[-count:])]

    async def clear_records(self) -> int:
        async with self._lock:
            await self._ensure_loaded()
            removed = len(self._records)
            self._records = []
            await self._kv_put(KEY_RECORDS, [])
            return removed

    # ---------- 每日配额 ----------

    def _roll_day(self) -> None:
        if str(self._quota.get("date") or "") != _today():
            self._quota = {"date": _today(), SCOPE_USER: {}, SCOPE_GROUP: {}}

    async def quota_used(self, scope: str, key: str) -> int:
        if not key:
            return 0
        async with self._lock:
            await self._ensure_loaded()
            self._roll_day()
            bucket = self._quota.get(scope) or {}
            try:
                return int(bucket.get(key) or 0)
            except (TypeError, ValueError):
                return 0

    async def quota_add(self, scope: str, key: str, amount: int) -> int:
        if not key or amount <= 0:
            return 0
        async with self._lock:
            await self._ensure_loaded()
            self._roll_day()
            bucket = self._quota.setdefault(scope, {})
            try:
                current = int(bucket.get(key) or 0)
            except (TypeError, ValueError):
                current = 0
            bucket[key] = current + int(amount)
            await self._kv_put(KEY_QUOTA, dict(self._quota))
            return bucket[key]

    async def quota_snapshot(self) -> dict[str, Any]:
        async with self._lock:
            await self._ensure_loaded()
            self._roll_day()
            return {
                "date": self._quota.get("date"),
                SCOPE_USER: dict(self._quota.get(SCOPE_USER) or {}),
                SCOPE_GROUP: dict(self._quota.get(SCOPE_GROUP) or {}),
            }

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            await self._ensure_loaded()
            return {"dedupe": len(self._dedupe), "records": len(self._records)}
