"""会话素材缓存。

LLM 工具的调用时机常常晚于图片到达的时机：用户先甩三张图，隔一句话才说
「传到图床」，这时当前消息里已经没有图片了。所以每条消息都把看到的素材登记到
按会话隔离的缓存里，工具再按 `material_id` 或序号取件。

缓存只放 `Material.descriptor()`（不含 bytes），既省内存也避免大文件长期驻留；
因此只有 `Material.cacheable` 为真（有 url / 本地路径 / file_id）的素材才会登记，
纯内联 base64 图片不进缓存，避免列出取不回来的死条目。
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from typing import Any

from .collector import Material
from .config import BehaviorConfig


class SessionMaterials:
    """按 `unified_msg_origin` 隔离的素材 LRU + TTL 缓存。"""

    def __init__(self, behavior: BehaviorConfig | None = None) -> None:
        self._behavior = behavior or BehaviorConfig()
        # session_id -> {material_id: (最后出现时间, 描述符)}
        self._sessions: dict[str, dict[str, tuple[float, dict[str, Any]]]] = {}

    # ---------- 配置 ----------

    def update_config(self, behavior: BehaviorConfig) -> None:
        self._behavior = behavior
        for session_id in list(self._sessions):
            self._prune(session_id)

    @property
    def ttl_seconds(self) -> float:
        return max(60.0, float(self._behavior.session_ttl_minutes) * 60.0)

    @property
    def limit(self) -> int:
        return max(1, int(self._behavior.session_material_limit))

    # ---------- 写入 ----------

    def remember(self, session_id: str, materials: Iterable[Material]) -> int:
        """登记一批素材，返回本次新增（或刷新）的条数。"""
        key = str(session_id or "").strip()
        if not key:
            return 0
        bucket = self._sessions.setdefault(key, {})
        now = time.time()
        touched = 0
        for material in materials:
            if not material.cacheable:
                continue
            # 重新插入以更新 LRU 顺序。
            bucket.pop(material.material_id, None)
            bucket[material.material_id] = (now, material.descriptor())
            touched += 1
        self._prune(key)
        return touched

    def forget(self, session_id: str) -> int:
        """清掉一个会话的缓存，返回被清掉的条数。"""
        bucket = self._sessions.pop(str(session_id or "").strip(), {})
        return len(bucket)

    def clear(self) -> None:
        self._sessions.clear()

    # ---------- 读取 ----------

    def snapshot(self, session_id: str) -> list[Material]:
        """按「旧 → 新」顺序返回当前会话里还有效的素材。"""
        key = str(session_id or "").strip()
        if not key:
            return []
        self._prune(key)
        bucket = self._sessions.get(key, {})
        return [Material.from_descriptor(entry) for _, entry in bucket.values()]

    def latest(self, session_id: str) -> Material | None:
        items = self.snapshot(session_id)
        return items[-1] if items else None

    def pick(
        self,
        session_id: str,
        *,
        item_ids: Sequence[str] = (),
        indexes: Sequence[int] = (),
    ) -> tuple[list[Material], list[str]]:
        """按 id 或序号取件，返回 (命中素材, 未命中的标识)。

        序号规则与 `imgbed_list_materials` 的输出一致：1 是最早，-1 是最新。
        """
        items = self.snapshot(session_id)
        by_id = {item.material_id: item for item in items}
        picked: list[Material] = []
        missing: list[str] = []
        seen: set[str] = set()

        def push(material: Material | None, label: str) -> None:
            if material is None:
                missing.append(label)
                return
            if material.material_id in seen:
                return
            seen.add(material.material_id)
            picked.append(material)

        for raw in item_ids:
            text = str(raw or "").strip()
            if not text:
                continue
            push(by_id.get(text), text)

        for raw_index in indexes:
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                missing.append(str(raw_index))
                continue
            if index == 0:
                missing.append("0")
                continue
            position = index - 1 if index > 0 else len(items) + index
            if 0 <= position < len(items):
                push(items[position], str(index))
            else:
                missing.append(str(index))

        return picked, missing

    def stats(self) -> dict[str, int]:
        return {
            "sessions": len(self._sessions),
            "materials": sum(len(bucket) for bucket in self._sessions.values()),
        }

    # ---------- 内部 ----------

    def _prune(self, session_id: str) -> None:
        bucket = self._sessions.get(session_id)
        if bucket is None:
            return
        deadline = time.time() - self.ttl_seconds
        for material_id, (stamp, _) in list(bucket.items()):
            if stamp < deadline:
                bucket.pop(material_id, None)
        overflow = len(bucket) - self.limit
        if overflow > 0:
            for material_id in list(bucket)[:overflow]:
                bucket.pop(material_id, None)
        if not bucket:
            self._sessions.pop(session_id, None)
