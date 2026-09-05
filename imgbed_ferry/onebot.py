"""OneBot v11 系（LLOneBot / NapCat / Lagrange / llbot SnowLuma）的取件兜底。

AstrBot 的 aiocqhttp 适配器已经帮我们做了两件重要的事：`reply` 段默认会调
`get_msg` 把被引用消息递归解析进 `Reply.chain`；`file` 段会尝试换取下载直链。
但适配器的 `file` 分支有两个已知短板，插件必须自己补上：

1. 它用 `event.message[0]["data"]["file_id"]` 取 file_id —— 索引硬编码为 0，
   当消息里「文字 + 文件」或「多个文件」并存时会取错甚至取空。
2. `get_group_file_url` / `get_private_file_url` 失败时它只打日志，
   **不会把 File 段放进 chain**，插件层看上去就像没收到文件。

所以这里直接读 `event.message_obj.raw_message` 里的原始段，自己走一条更长的
action 兜底链。所有 action 名与参数都按 SnowLuma 的 `catalog.json` 核对过：

* `get_group_file_url(group_id, file_id, busid?)` -> `{url}`
* `get_private_file_url(file_id, user_id?, file_hash?)` -> `{url}`
* `get_file(file_id? | file?)` -> `{url|file|path|base64}`（只缓存图片/语音）
* `get_image(file? | file_id?)` -> `{url|file}`

这个模块刻意不 import astrbot，只吃 duck-typed 的 event，方便单测注入假对象。
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .logs import logger

HTTP_PREFIXES = ("http://", "https://")

# 依次尝试的取件键名：url 类在前，本地路径其次，base64 兜底。
URL_KEYS = ("url", "download_url", "file_url")
PATH_KEYS = ("path", "file", "local_path", "filename")
BASE64_KEYS = ("base64", "file_base64", "data")


def is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(HTTP_PREFIXES)


def safe_get(obj: Any, key: str, default: Any = None) -> Any:
    """兼容 dict 与普通对象的取值，任何异常都退回默认值。"""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    getter = getattr(obj, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:
            return default
    return getattr(obj, key, default)


def normalize_id(value: Any) -> Any:
    """OneBot 实现对 group_id/user_id 的类型要求不一，数字串统一转 int。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return int(text) if text.isdigit() else text


def unwrap_action_response(response: Any) -> dict[str, Any]:
    """把 `{status, retcode, data: {...}}` 解成里面的 data。"""
    if isinstance(response, Mapping):
        nested = response.get("data")
        if isinstance(nested, Mapping):
            return dict(nested)
        return dict(response)
    try:
        mapped = dict(response)
    except (TypeError, ValueError):
        return {}
    nested = mapped.get("data")
    return dict(nested) if isinstance(nested, Mapping) else mapped


def resolve_call_action(event: Any) -> Any | None:
    """aiocqhttp 的 `event.bot` 就是 CQHttp 实例，call_action 可能挂在 bot 或 bot.api。"""
    bot = getattr(event, "bot", None)
    for owner in (bot, getattr(bot, "api", None)):
        call_action = getattr(owner, "call_action", None)
        if callable(call_action):
            return call_action
    return None


def extract_onebot_segments(raw_message: Any) -> list[dict[str, Any]]:
    """从原始事件里取出 OneBot 段数组，顺序保持不变。"""
    if raw_message is None:
        return []
    candidates: Any = raw_message
    if not isinstance(candidates, (list, tuple)):
        candidates = safe_get(raw_message, "message", None)
    if isinstance(candidates, str) or not isinstance(candidates, Iterable):
        return []
    segments: list[dict[str, Any]] = []
    for item in candidates:
        if isinstance(item, Mapping):
            segments.append(dict(item))
    return segments


def segment_payload(segment: Mapping[str, Any]) -> dict[str, Any]:
    data = segment.get("data")
    return dict(data) if isinstance(data, Mapping) else {}


def event_group_id(event: Any, payload: Mapping[str, Any] | None = None) -> str:
    """群号优先取段内的，其次取事件的，最后翻原始事件。"""
    if payload:
        candidate = payload.get("group_id")
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            value = ""
        if value is not None and str(value).strip():
            return str(value).strip()
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    raw_group = safe_get(raw, "group_id", "")
    return str(raw_group).strip() if raw_group is not None else ""


def event_user_id(event: Any, payload: Mapping[str, Any] | None = None) -> str:
    if payload:
        candidate = payload.get("user_id")
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    getter = getattr(event, "get_sender_id", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            value = ""
        if value is not None and str(value).strip():
            return str(value).strip()
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    raw_user = safe_get(raw, "user_id", "")
    return str(raw_user).strip() if raw_user is not None else ""


def payload_url(payload: Mapping[str, Any]) -> str:
    for key in URL_KEYS:
        value = payload.get(key)
        if is_http_url(value):
            return str(value).strip()
    return ""


def strip_file_uri(value: str) -> str:
    """把 `file://` URI 还原成本地路径。

    只吃掉 `file://` 这七个字符，剩下的第一个斜杠交给平台判断：Linux 上
    `file:///data/a.png` 的开头斜杠必须留着，Windows 上 `file:///C:/a.png`
    的却要去掉，只有「/盘符:」这种形状才多吃一个字符。
    """
    text = value.strip()
    if not text.lower().startswith("file://"):
        return text
    text = text[7:]
    if len(text) >= 3 and text[0] == "/" and text[2] == ":":
        return text[1:]
    return text


def _path_candidates(text: str) -> list[str]:
    """路径的候选写法：原样优先，其次是百分号解码后的样子。

    规范的 `file://` URI 会把空格写成 `%20`，但不少客户端直接塞原始路径，
    所以两种都试一遍，反正调用方只接受真实存在的文件。
    """
    if "%" not in text:
        return [text]
    decoded = unquote(text)
    return [text] if decoded == text else [text, decoded]


def payload_local_path(payload: Mapping[str, Any]) -> str:
    """段里给的路径只有真实存在才算可用，否则容易把 file_id 当路径读。"""
    for key in PATH_KEYS:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        if text.lower().startswith(HTTP_PREFIXES) or text.startswith("base64://"):
            continue
        for guess in _path_candidates(strip_file_uri(text)):
            try:
                candidate = Path(guess)
                if candidate.is_file():
                    return str(candidate.resolve())
            except (OSError, ValueError):
                continue
    return ""


def payload_base64(payload: Mapping[str, Any]) -> bytes:
    for key in BASE64_KEYS:
        value = payload.get(key)
        if not isinstance(value, str) or len(value) < 16:
            continue
        text = value.strip()
        if text.startswith("base64://"):
            text = text[9:]
        elif text.startswith("data:") and "," in text:
            text = text.split(",", 1)[1]
        elif key == "data":
            # `data` 只有明确带前缀时才当 base64，否则它通常是结构体。
            continue
        try:
            return base64.b64decode(text, validate=False)
        except (binascii.Error, ValueError):
            continue
    return b""


def payload_is_usable(payload: Mapping[str, Any]) -> bool:
    return bool(payload_url(payload) or payload_local_path(payload) or payload_base64(payload))


def _debug(message: str, *args: Any) -> None:
    logger.debug("[图床摆渡] " + message, *args)


async def run_action_attempts(
    call_action: Any,
    attempts: Sequence[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """按顺序尝试多个 action，返回第一个能拿到可用素材的响应。"""
    for action, params in attempts:
        try:
            response = await call_action(action=action, **params)
        except Exception as exc:
            _debug("OneBot %s 调用失败 params=%s: %s", action, sorted(params), exc)
            continue
        unwrapped = unwrap_action_response(response)
        if payload_is_usable(unwrapped):
            _debug("OneBot %s 命中，可用键=%s", action, sorted(unwrapped))
            return unwrapped
        _debug("OneBot %s 未给出可用素材，返回键=%s", action, sorted(unwrapped))
    return {}


def build_file_attempts(
    event: Any,
    payload: Mapping[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """文件段的 action 兜底链，群聊优先群接口，私聊优先私聊接口。"""
    file_id = str(payload.get("file_id") or payload.get("id") or "").strip()
    raw_file = str(payload.get("file") or "").strip()
    group_id = event_group_id(event, payload)
    user_id = event_user_id(event, payload)
    busid = payload.get("busid")
    attempts: list[tuple[str, dict[str, Any]]] = []

    if file_id and group_id:
        base = {"group_id": normalize_id(group_id), "file_id": file_id}
        if busid is not None and str(busid).strip():
            attempts.append(("get_group_file_url", {**base, "busid": busid}))
        attempts.append(("get_group_file_url", dict(base)))
    if file_id:
        if user_id:
            attempts.append(
                ("get_private_file_url", {"file_id": file_id, "user_id": normalize_id(user_id)})
            )
        attempts.append(("get_private_file_url", {"file_id": file_id}))
        attempts.append(("get_file", {"file_id": file_id}))
    if raw_file and raw_file != file_id:
        attempts.append(("get_file", {"file": raw_file}))
    return attempts


def build_image_attempts(payload: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """图片段：优先 get_image（协议端一定实现），再退到 get_file。"""
    file_id = str(payload.get("file_id") or "").strip()
    raw_file = str(payload.get("file") or "").strip()
    attempts: list[tuple[str, dict[str, Any]]] = []
    if raw_file:
        attempts.append(("get_image", {"file": raw_file}))
    if file_id:
        attempts.append(("get_image", {"file_id": file_id}))
        attempts.append(("get_file", {"file_id": file_id}))
    if raw_file and raw_file != file_id:
        attempts.append(("get_file", {"file": raw_file}))
    return attempts


async def fetch_file_payload(event: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """把一个 OneBot `file` 段解析成可下载/可读的描述符。"""
    direct = payload_url(payload)
    if direct:
        return {"url": direct}
    local = payload_local_path(payload)
    if local:
        return {"path": local}

    call_action = resolve_call_action(event)
    if call_action is None:
        _debug("拿不到 OneBot call_action，文件段无法取件")
        return {}
    attempts = build_file_attempts(event, payload)
    if not attempts:
        _debug("文件段缺少 file_id 与 file，无法构造取件请求")
        return {}
    return await run_action_attempts(call_action, attempts)


async def fetch_image_payload(event: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """把一个 OneBot `image` 段解析成可下载/可读的描述符。"""
    direct = payload_url(payload)
    if direct:
        return {"url": direct}
    local = payload_local_path(payload)
    if local:
        return {"path": local}

    call_action = resolve_call_action(event)
    if call_action is None:
        return {}
    attempts = build_image_attempts(payload)
    if not attempts:
        return {}
    return await run_action_attempts(call_action, attempts)


async def fetch_reply_segments(event: Any, message_id: Any) -> list[dict[str, Any]]:
    """自己再调一次 `get_msg`，拿被引用消息的原始段。

    适配器解析 `reply` 时会递归走一遍同一套转换逻辑，于是被引用消息里的文件段
    同样会踩到 `event.message[0]` 硬编码索引的坑而整段丢失。这里直接取原始段，
    引用一个多附件消息时也能全部拿到。
    """
    call_action = resolve_call_action(event)
    text_id = str(message_id or "").strip()
    if call_action is None or not text_id:
        return []
    params: dict[str, Any] = {
        "message_id": int(text_id) if text_id.lstrip("-").isdigit() else text_id
    }
    try:
        response = await call_action(action="get_msg", **params)
    except Exception as exc:
        _debug("get_msg 取被引用消息失败 id=%s: %s", text_id, exc)
        return []
    return extract_onebot_segments(unwrap_action_response(response))
