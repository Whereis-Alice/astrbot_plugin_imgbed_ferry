"""从消息事件里收集可上传素材。

三个来源按优先级合并：

1. `event.get_messages()` 解析好的组件链（`Image` / `File`）；
2. `Reply.chain` —— 引用消息里的图片和文件，另外自己再调一次 `get_msg`
   补齐适配器可能丢掉的文件段；
3. `event.message_obj.raw_message` 里的原始 OneBot 段 —— 这是关键兜底：
   aiocqhttp 适配器换取文件直链失败时不会把 `File` 放进组件链，只有原始段还在。

素材统一抽象成 `Material`，只带「怎么取到它」的描述符，真正下载/读取推迟到
上传阶段，避免大文件长期驻留内存。
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .media import ARCHIVE_HINT_EXTENSIONS, sanitize_filename, short_hash, split_extension
from .onebot import (
    extract_onebot_segments,
    fetch_reply_segments,
    is_http_url,
    payload_local_path,
    payload_url,
    segment_payload,
    strip_file_uri,
)

IMAGE_TYPE = "Image"
FILE_TYPE = "File"
REPLY_TYPE = "Reply"

KIND_IMAGE = "image"
KIND_FILE = "file"

SOURCE_MESSAGE = "message"
SOURCE_REPLY = "reply"
SOURCE_RAW = "raw"
SOURCE_SESSION = "session"

# 保留原始段里真正有用的字段，别把整段塞进会话缓存。
PAYLOAD_KEEP_KEYS = ("file_id", "file", "file_name", "name", "busid", "group_id", "user_id", "size")

_NAME_KEYS = ("file_name", "name", "filename", "file")


def component_type(component: Any) -> str:
    """组件的类型名。`ComponentType` 是 str 枚举，取 value 更稳。"""
    raw = getattr(component, "type", "")
    return str(getattr(raw, "value", raw) or "")


def payload_name(payload: Mapping[str, Any]) -> str:
    for key in _NAME_KEYS:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip().replace("\\", "/")
        if text.lower().startswith(("http://", "https://", "base64://", "data:")):
            continue
        return text.rsplit("/", 1)[-1]
    return ""


def name_from_url(url: str) -> str:
    """从 URL 里猜文件名，只有带扩展名才认，避免拿到一串 hash。"""
    if not is_http_url(url):
        return ""
    tail = url.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    stem, extension = split_extension(tail)
    return tail if stem and extension else ""


def _decode_inline_base64(value: str) -> bytes:
    text = value.strip()
    if text.startswith("base64://"):
        text = text[9:]
    elif text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    else:
        return b""
    try:
        return base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError):
        return b""


def trim_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in PAYLOAD_KEEP_KEYS
        if key in payload and payload[key] not in (None, "")
    }


@dataclass(slots=True)
class Material:
    """一份待上传素材的「取件单」。"""

    kind: str = KIND_FILE
    name: str = ""
    source: str = SOURCE_MESSAGE
    url: str = ""
    path: str = ""
    data: bytes = b""
    size: int = 0
    mime: str = ""
    file_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    component: Any = field(default=None, repr=False, compare=False)
    material_id: str = ""

    def __post_init__(self) -> None:
        if not self.material_id:
            self.material_id = self.build_id()
        if self.data and not self.size:
            self.size = len(self.data)

    @property
    def signature(self) -> str:
        """跨来源去重用的稳定标识。"""
        for candidate in (self.url, self.path, self.file_id):
            if candidate:
                return candidate
        if self.data:
            return f"inline:{len(self.data)}:{short_hash(str(self.data[:64]))}"
        return f"{self.kind}:{self.name}:{self.size}"

    def build_id(self) -> str:
        extension = split_extension(self.name)[1]
        if self.kind == KIND_IMAGE:
            prefix = "img"
        elif extension in ARCHIVE_HINT_EXTENSIONS:
            prefix = "arc"
        else:
            prefix = "doc"
        return f"{prefix}_{short_hash(self.kind, self.signature)}"

    @property
    def resolvable(self) -> bool:
        return bool(self.url or self.path or self.data or self.file_id or self.component)

    @property
    def cacheable(self) -> bool:
        """能不能写进会话缓存后还取得回来。

        `descriptor()` 刻意不带 bytes，也带不走组件对象，所以只靠 `data` /
        `component` 才能取到的素材（例如 base64 内联图）进了缓存就是死条目，
        列出来只会误导用户，干脆不收。
        """
        return bool(self.url or self.path or self.file_id)

    def display_name(self) -> str:
        return self.name or ("image" if self.kind == KIND_IMAGE else "file")

    def descriptor(self) -> dict[str, Any]:
        """写进会话缓存的轻量描述符，刻意不含 bytes。"""
        return {
            "material_id": self.material_id,
            "kind": self.kind,
            "name": self.name,
            "source": self.source,
            "url": self.url,
            "path": self.path,
            "size": self.size,
            "mime": self.mime,
            "file_id": self.file_id,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_descriptor(cls, raw: Mapping[str, Any]) -> Material:
        payload = raw.get("payload")
        return cls(
            kind=str(raw.get("kind") or KIND_FILE),
            name=str(raw.get("name") or ""),
            source=str(raw.get("source") or SOURCE_SESSION),
            url=str(raw.get("url") or ""),
            path=str(raw.get("path") or ""),
            size=int(raw.get("size") or 0),
            mime=str(raw.get("mime") or ""),
            file_id=str(raw.get("file_id") or ""),
            payload=dict(payload) if isinstance(payload, Mapping) else {},
            material_id=str(raw.get("material_id") or ""),
        )


def material_from_component(component: Any, *, source: str) -> Material | None:
    """把 `Image` / `File` 组件转成素材。"""
    ctype = component_type(component)
    if ctype == IMAGE_TYPE:
        kind = KIND_IMAGE
    elif ctype == FILE_TYPE:
        kind = KIND_FILE
    else:
        return None

    url = ""
    path = ""
    data = b""
    name = ""

    raw_url = str(getattr(component, "url", "") or "")
    if is_http_url(raw_url):
        url = raw_url.strip()

    if kind == KIND_IMAGE:
        raw_file = str(getattr(component, "file", "") or "")
        if not url and is_http_url(raw_file):
            url = raw_file.strip()
        elif raw_file.startswith(("base64://", "data:")):
            data = _decode_inline_base64(raw_file)
        raw_path = str(getattr(component, "path", "") or "")
        if raw_path and not raw_path.lower().startswith(("http://", "https://")):
            path = raw_path
        elif raw_file.lower().startswith("file://"):
            path = strip_file_uri(raw_file)
    else:
        # File.file 属性在异步上下文里会返回空串并打 warning，只能读私有字段。
        raw_file = str(getattr(component, "file_", "") or "")
        if not url and is_http_url(raw_file):
            url = raw_file.strip()
        elif raw_file:
            path = raw_file
        name = str(getattr(component, "name", "") or "")

    name = name or name_from_url(url) or ""
    return Material(
        kind=kind,
        name=sanitize_filename(name, fallback="") if name else "",
        source=source,
        url=url,
        path=path,
        data=data,
        component=component,
    )


def material_from_segment(segment: Mapping[str, Any], *, source: str) -> Material | None:
    """把原始 OneBot 段转成素材（适配器丢段时的兜底路径）。"""
    seg_type = str(segment.get("type") or "").lower()
    if seg_type == "image":
        kind = KIND_IMAGE
    elif seg_type == "file":
        kind = KIND_FILE
    else:
        return None
    payload = segment_payload(segment)
    name = payload_name(payload)
    url = payload_url(payload)
    return Material(
        kind=kind,
        name=sanitize_filename(name, fallback="") if name else "",
        source=source,
        url=url,
        path=payload_local_path(payload),
        file_id=str(payload.get("file_id") or "").strip(),
        payload=trim_payload(payload),
    )


def _iter_media_segments(segments: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    pending: list[tuple[str, dict[str, Any]]] = []
    for segment in segments:
        seg_type = str(segment.get("type") or "").lower()
        if seg_type in ("image", "file"):
            pending.append((seg_type, segment))
    return pending


def _take_segment(
    pending: list[tuple[str, dict[str, Any]]],
    kind: str,
    name: str,
) -> dict[str, Any]:
    """给组件配上对应的原始段：文件按名字配，图片按出现顺序配。"""
    wanted = "image" if kind == KIND_IMAGE else "file"
    fallback_index = -1
    for index, (seg_type, segment) in enumerate(pending):
        if seg_type != wanted:
            continue
        if wanted == "file" and name:
            candidate = payload_name(segment_payload(segment))
            if candidate and candidate.lower() != name.lower():
                if fallback_index < 0:
                    fallback_index = index
                continue
        return pending.pop(index)[1]
    if fallback_index >= 0:
        return pending.pop(fallback_index)[1]
    return {}


def _merge_segment(material: Material, segment: Mapping[str, Any]) -> None:
    if not segment:
        return
    payload = segment_payload(segment)
    material.payload = trim_payload(payload)
    if not material.file_id:
        material.file_id = str(payload.get("file_id") or "").strip()
    if not material.name:
        candidate = payload_name(payload)
        if candidate:
            material.name = sanitize_filename(candidate, fallback="")
    if not material.url:
        material.url = payload_url(payload)
    if not material.path:
        material.path = payload_local_path(payload)
    if not material.size:
        try:
            material.size = int(payload.get("size") or 0)
        except (TypeError, ValueError):
            material.size = 0
    material.material_id = material.build_id()


async def collect_materials(event: Any, *, resolve_reply: bool = True) -> list[Material]:
    """按「当前消息 -> 引用消息 -> 原始段兜底」的顺序收集素材并去重。"""
    raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
    pending = _iter_media_segments(extract_onebot_segments(raw_message))

    getter = getattr(event, "get_messages", None)
    chain = []
    if callable(getter):
        try:
            chain = list(getter() or [])
        except Exception:
            chain = []

    materials: list[Material] = []
    seen: set[str] = set()

    def push(material: Material | None) -> None:
        if material is None or not material.resolvable:
            return
        signature = material.signature
        if signature in seen:
            return
        seen.add(signature)
        materials.append(material)

    for component in chain:
        if component_type(component) == REPLY_TYPE:
            reply_pending: list[tuple[str, dict[str, Any]]] = []
            if resolve_reply:
                reply_pending = _iter_media_segments(
                    await fetch_reply_segments(event, getattr(component, "id", ""))
                )
            for sub in list(getattr(component, "chain", None) or []):
                material = material_from_component(sub, source=SOURCE_REPLY)
                if material is None:
                    continue
                _merge_segment(material, _take_segment(reply_pending, material.kind, material.name))
                push(material)
            for _, segment in reply_pending:
                push(material_from_segment(segment, source=SOURCE_REPLY))
            continue

        material = material_from_component(component, source=SOURCE_MESSAGE)
        if material is None:
            continue
        _merge_segment(material, _take_segment(pending, material.kind, material.name))
        push(material)

    # 组件链没覆盖到的原始段：适配器换取直链失败时文件就只剩这里了。
    for _, segment in pending:
        push(material_from_segment(segment, source=SOURCE_RAW))

    return materials
