"""跨插件资源句柄协议与安全校验。

``upload_asset`` 是给其它 AstrBot 插件用的内部接口，不是一个让模型提交
``C:\\...`` 路径的通用上传器。这里把 meme_magpie 的两种公开形态都统一成一个
受控句柄：

* 真实的 ``MemeAssetHandle``（提供异步 ``read_bytes()``，不暴露路径）；
* 文档中约定的 mapping 描述符（带来源、短 TTL、大小、哈希和受控路径/内联数据）。

所有内容在交给上传流水线前都会再次读取、限长、校验签名和 SHA-256。提供方的源文件
只读不删，生命周期仍由提供方管理。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import math
import os
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .media import (
    guess_mime,
    normalize_extension,
    sanitize_filename,
    sha256_hex,
    sniff_extension,
    split_extension,
)

ASSET_PROTOCOL = "astrbot-meme-asset/v1"
ASSET_API_VERSION = 1
DEFAULT_ASSET_SOURCE = "astrbot_plugin_meme_magpie"
IMAGE_ASSET_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})


class AssetValidationError(ValueError):
    """资源句柄或其内容未通过安全校验。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code or "invalid_asset")
        self.message = str(message or self.code)
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        return {"success": False, "code": self.code, "error": self.message}


def _value(obj: Any, *keys: str, default: Any = None) -> Any:
    """从 mapping 或 duck-typed 对象安全取值。"""
    for key in keys:
        try:
            value = obj.get(key, None) if isinstance(obj, Mapping) else getattr(obj, key, None)
        except Exception:
            value = None
        if value not in (None, ""):
            return value
    return default


def _as_timestamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number) or number <= 0:
        return 0.0
    # 少数协议端会传毫秒时间戳；统一成 Unix seconds。
    while number > 100_000_000_000:
        number /= 1000.0
    return number


def _decode_inline(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    if not isinstance(value, str):
        return b""
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


def _object_mapping(asset: Any) -> dict[str, Any]:
    """提取句柄的公开字段，但不读取私有路径或任意对象属性。"""
    if isinstance(asset, Mapping):
        return dict(asset)

    for method_name in ("to_mapping", "to_dict", "model_dump"):
        try:
            method = getattr(asset, method_name, None)
            if callable(method):
                value = method()
                if isinstance(value, Mapping):
                    return dict(value)
        except Exception:
            continue

    # meme_magpie 的 MemeAssetHandle 是故意不暴露路径的 dataclass；只取协议字段，
    # 并保留对象本身供 read_bytes() 使用。
    fields = (
        "protocol",
        "provider",
        "source",
        "api_version",
        "asset_id",
        "token",
        "emoji_id",
        "name",
        "filename",
        "mime",
        "mime_type",
        "content_type",
        "size",
        "sha256",
        "issued_at",
        "created_at",
        "expires_at",
        "ttl_seconds",
        "metadata",
    )
    result: dict[str, Any] = {}
    for key in fields:
        try:
            value = getattr(asset, key, None)
        except Exception:
            value = None
        if value not in (None, ""):
            result[key] = value
    return result


def _reader_for(asset: Any, raw: Mapping[str, Any]) -> Callable[[], Any] | None:
    for key in ("read_bytes", "read"):
        try:
            method = raw.get(key) if isinstance(raw, Mapping) else None
            if not callable(method):
                method = getattr(asset, key, None)
            if callable(method):
                return method
        except Exception:
            continue
    return None


def _path_has_link(path: Path) -> bool:
    """检查路径本身及父级，拒绝 symlink / junction 绕过普通文件检查。"""
    try:
        current = path
        while True:
            try:
                # Python 3.12+ 在 Windows 上能识别 NTFS junction；它不是
                # `stat.S_ISLNK`，但同样可能把受控目录带出安全边界。
                is_junction = getattr(os.path, "isjunction", None)
                if callable(is_junction) and is_junction(current):
                    return True
                mode = os.lstat(current).st_mode
            except FileNotFoundError:
                current = current.parent
                if current == current.parent:
                    break
                continue
            except OSError:
                return True
            if stat.S_ISLNK(mode):
                return True
            if current == current.parent:
                break
            current = current.parent
    except (OSError, RuntimeError, ValueError):
        return True
    return False


def _validate_path(raw_path: Any) -> Path:
    if not isinstance(raw_path, (str, os.PathLike)) or not str(raw_path).strip():
        raise AssetValidationError("file_missing", "资源句柄没有可读取的文件")
    try:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            raise AssetValidationError("invalid_asset", "受控资源路径必须是绝对路径")
        # `absolute()` 保留 symlink/junction 形态，随后由 `_path_has_link` 拒绝；
        # 不使用 `resolve()`，否则会先跟随链接而失去这层安全检查。
        path = path.absolute()
        if _path_has_link(path):
            raise AssetValidationError("invalid_asset", "拒绝通过符号链接读取资源")
        mode = os.lstat(path).st_mode
        if not stat.S_ISREG(mode):
            raise AssetValidationError("file_missing", "资源路径不是普通文件")
        return path
    except AssetValidationError:
        raise
    except FileNotFoundError as exc:
        raise AssetValidationError("file_missing", "资源文件不存在或已被删除") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise AssetValidationError("invalid_asset", "资源路径无效") from exc


def _protocol_from(raw: Mapping[str, Any]) -> str:
    explicit = str(raw.get("protocol") or "").strip()
    if explicit:
        return explicit
    version = raw.get("api_version")
    if version not in (None, ""):
        text = str(version).strip().lower()
        if text in {"1", "v1", ASSET_PROTOCOL.casefold()}:
            return ASSET_PROTOCOL
        return text
    # 早期联动草案没有 protocol 字段；只要 source 仍然受白名单约束，兼容它。
    return ASSET_PROTOCOL


def _canonical_extension(extension: str) -> str:
    ext = normalize_extension(extension)
    return "jpg" if ext == "jpeg" else ext


@dataclass(slots=True)
class AssetHandle:
    """imgbed_ferry 内部统一的、已通过元数据校验的句柄。"""

    protocol: str
    source: str
    asset_id: str
    name: str
    mime: str
    size: int
    sha256: str
    issued_at: float
    expires_at: float
    metadata: dict[str, Any] = field(default_factory=dict)
    _reader: Callable[[], Any] | None = field(default=None, repr=False, compare=False)
    _path: Path | None = field(default=None, repr=False, compare=False)
    _data: bytes | None = field(default=None, repr=False, compare=False)
    _max_bytes: int = field(default=0, repr=False, compare=False)
    _require_hash: bool = field(default=True, repr=False, compare=False)

    async def read_bytes(self) -> bytes:
        """读取并再次校验内容；不会删除或移动提供方文件。"""
        now = time.time()
        if self.expires_at <= now:
            raise AssetValidationError("asset_expired", "资源句柄已过期，请重新导出")

        try:
            if self._data is not None:
                value: Any = self._data
            elif self._reader is not None:
                value = self._reader()
                if inspect.isawaitable(value):
                    value = await value
            elif self._path is not None:
                value = await asyncio.to_thread(self._path.read_bytes)
            else:
                raise AssetValidationError("invalid_asset", "资源句柄没有可读取内容")
        except AssetValidationError:
            raise
        except Exception as exc:
            provider_code = str(getattr(exc, "code", "") or "").strip()
            provider_message = str(getattr(exc, "message", "") or "").strip()
            if provider_code == "asset_expired":
                raise AssetValidationError(
                    "asset_expired", provider_message or "资源句柄已过期"
                ) from exc
            if provider_code in {"file_missing", "asset_released", "asset_invalid"}:
                raise AssetValidationError(
                    provider_code, provider_message or "资源句柄不可用"
                ) from exc
            raise AssetValidationError("asset_read_failed", "读取受控资源失败") from exc

        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise AssetValidationError("invalid_asset", "资源读取器没有返回二进制内容")
        data = bytes(value)
        _validate_content(
            self,
            data,
            max_bytes=self._max_bytes,
            require_declared_hash=self._require_hash,
        )
        if time.time() >= self.expires_at:
            raise AssetValidationError("asset_expired", "资源句柄在读取过程中已过期")
        return data

    def to_mapping(self) -> dict[str, Any]:
        """返回安全描述，不回传本地路径、内联数据或读取器。"""
        return {
            "protocol": self.protocol,
            "source": self.source,
            "asset_id": self.asset_id,
            "name": self.name,
            "mime": self.mime,
            "size": self.size,
            "sha256": self.sha256,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "metadata": dict(self.metadata),
        }


def _validate_content(
    handle: AssetHandle,
    data: bytes,
    *,
    max_bytes: int,
    require_declared_hash: bool,
) -> None:
    if not data:
        raise AssetValidationError("invalid_asset", "资源内容为空")
    if max_bytes > 0 and len(data) > max_bytes:
        raise AssetValidationError("too_large", "资源超过跨插件接口的大小上限")
    if handle.size > 0 and len(data) != handle.size:
        raise AssetValidationError("invalid_asset", "资源实际大小与句柄声明不一致")
    if handle.size <= 0:
        handle.size = len(data)

    digest = sha256_hex(data)
    declared_hash = str(handle.sha256 or "").strip().lower()
    if require_declared_hash and not declared_hash:
        raise AssetValidationError("invalid_asset", "资源句柄缺少 SHA-256")
    if declared_hash and digest != declared_hash:
        raise AssetValidationError("hash_mismatch", "资源内容与句柄声明的 SHA-256 不一致")
    handle.sha256 = digest

    extension = normalize_extension(split_extension(handle.name)[1])
    sniffed_ext, sniffed_mime = sniff_extension(data)
    if not extension:
        extension = sniffed_ext
        stem = split_extension(handle.name)[0] or "asset"
        if extension:
            handle.name = sanitize_filename(f"{stem}.{extension}")
    if _canonical_extension(extension) not in {
        _canonical_extension(item) for item in IMAGE_ASSET_EXTENSIONS
    }:
        raise AssetValidationError("invalid_asset", "只允许上传 PNG/JPEG/GIF/WebP/BMP 图片")
    if not sniffed_ext or _canonical_extension(sniffed_ext) != _canonical_extension(extension):
        raise AssetValidationError("invalid_signature", "文件签名与扩展名不一致")
    if handle.mime and handle.mime.lower().startswith("image/") and sniffed_mime:
        declared = handle.mime.split(";", 1)[0].strip().lower()
        actual = sniffed_mime.split(";", 1)[0].strip().lower()
        if declared not in {actual, "image/jpg"} and not (
            declared == "image/jpeg" and actual == "image/jpeg"
        ):
            raise AssetValidationError("invalid_signature", "文件签名与 MIME 类型不一致")
    handle.mime = sniffed_mime or handle.mime or guess_mime(extension)


def parse_asset_handle(
    asset: Any,
    *,
    allowed_sources: list[str] | tuple[str, ...] | set[str] | frozenset[str] = (
        DEFAULT_ASSET_SOURCE,
    ),
    max_ttl_seconds: int = 1800,
    max_bytes: int = 0,
    require_declared_hash: bool = True,
    now: float | None = None,
) -> AssetHandle:
    """解析并校验一个受控资源句柄的元数据。

    这个函数是同步的，实际调用读取器/文件内容在 ``AssetHandle.read_bytes`` 中进行，
    所以调用方可以在异步环境里安全地使用真实 meme_magpie 句柄。
    """
    if isinstance(asset, (str, bytes, bytearray, memoryview, os.PathLike)):
        raise AssetValidationError("invalid_asset", "不接受裸路径或未签名的内联数据")
    if asset is None:
        raise AssetValidationError("invalid_asset", "资源句柄为空")

    raw = _object_mapping(asset)
    nested = raw.get("asset") if isinstance(raw, Mapping) else None
    if nested is not None and nested is not asset:
        return parse_asset_handle(
            nested,
            allowed_sources=allowed_sources,
            max_ttl_seconds=max_ttl_seconds,
            max_bytes=max_bytes,
            require_declared_hash=require_declared_hash,
            now=now,
        )

    source = str(_value(raw, "source", "provider", default="") or "").strip()
    if not source:
        raise AssetValidationError("invalid_asset", "资源句柄缺少来源标识")
    allowed = {str(item).strip().casefold() for item in allowed_sources if str(item).strip()}
    if not allowed or source.casefold() not in allowed:
        raise AssetValidationError("invalid_asset", "资源句柄来源未被插件信任")

    protocol = _protocol_from(raw)
    if protocol.casefold() != ASSET_PROTOCOL.casefold():
        raise AssetValidationError("invalid_asset", "资源句柄协议版本不受支持")

    current = time.time() if now is None else float(now)
    issued = _as_timestamp(_value(raw, "issued_at", "created_at", default=0))
    expires = _as_timestamp(_value(raw, "expires_at", default=0))
    ttl = _as_timestamp(_value(raw, "ttl_seconds", default=0))
    if expires <= 0 and ttl > 0:
        expires = (issued or current) + ttl
    if expires <= 0:
        raise AssetValidationError("invalid_asset", "资源句柄缺少有效期")
    if issued <= 0:
        issued = expires - max(1, int(max_ttl_seconds))
    if expires <= current:
        raise AssetValidationError("asset_expired", "资源句柄已过期，请重新导出")
    if issued > current + 60:
        raise AssetValidationError("invalid_asset", "资源句柄签发时间异常")
    if expires - issued > max(1, int(max_ttl_seconds)) + 5:
        raise AssetValidationError("invalid_asset", "资源句柄有效期超过安全上限")

    name = str(_value(raw, "name", "filename", default="") or "").strip()
    name = sanitize_filename(name, fallback="asset")
    mime = str(_value(raw, "mime", "mime_type", "content_type", default="") or "").strip()
    asset_id = str(_value(raw, "asset_id", "token", "emoji_id", default="") or "").strip()
    try:
        declared_size = int(_value(raw, "size", default=0) or 0)
    except (TypeError, ValueError):
        raise AssetValidationError("invalid_asset", "资源句柄的大小字段无效") from None
    if declared_size < 0:
        raise AssetValidationError("invalid_asset", "资源句柄的大小字段无效")

    declared_hash = str(_value(raw, "sha256", "hash", default="") or "").strip().lower()
    if declared_hash and (
        len(declared_hash) != 64 or any(c not in "0123456789abcdef" for c in declared_hash)
    ):
        raise AssetValidationError("invalid_asset", "资源句柄的 SHA-256 格式无效")
    if require_declared_hash and not declared_hash:
        raise AssetValidationError("invalid_asset", "资源句柄缺少 SHA-256")

    reader = _reader_for(asset, raw)
    path_value = _value(raw, "path", "file_path", default=None)
    path: Path | None = None
    if reader is None and path_value is None:
        inline = b""
        for key in ("data", "bytes", "content"):
            if key in raw:
                inline = _decode_inline(raw.get(key))
                if inline:
                    break
        if inline:
            data = inline
        else:
            raise AssetValidationError("invalid_asset", "资源句柄没有受控读取器或文件内容")
    else:
        data = None
        if reader is None and path_value is not None:
            path = _validate_path(path_value)

    metadata = _value(raw, "metadata", default={})
    if not isinstance(metadata, Mapping):
        metadata = {}
    handle = AssetHandle(
        protocol=ASSET_PROTOCOL,
        source=source,
        asset_id=asset_id,
        name=name,
        mime=mime,
        size=declared_size,
        sha256=declared_hash,
        issued_at=issued,
        expires_at=expires,
        metadata=dict(metadata),
        _reader=reader,
        _path=path,
        _data=data,
        _max_bytes=max(0, int(max_bytes)),
        _require_hash=require_declared_hash,
    )
    return handle


async def read_asset_bytes(
    asset: Any,
    *,
    allowed_sources: list[str] | tuple[str, ...] | set[str] | frozenset[str] = (
        DEFAULT_ASSET_SOURCE,
    ),
    max_ttl_seconds: int = 1800,
    max_bytes: int = 0,
    require_declared_hash: bool = True,
) -> tuple[AssetHandle, bytes]:
    """解析并读取句柄，供插件入口和单元测试复用。"""
    handle = parse_asset_handle(
        asset,
        allowed_sources=allowed_sources,
        max_ttl_seconds=max_ttl_seconds,
        max_bytes=max_bytes,
        require_declared_hash=require_declared_hash,
    )
    return handle, await handle.read_bytes()


# 兼容方案早期文档中的函数名。
validate_asset_handle = parse_asset_handle
