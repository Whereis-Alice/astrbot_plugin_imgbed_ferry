"""上传编排：取件 → 归一化 → 解压 → 去重 → 压缩 → 上传。

`main.py` 与 `tools.py` 只负责把「谁要传什么」整理成 `Material` 列表，
剩下的取件兜底、压缩包展开、批内去重、图片预压缩、并发上传与记账都在这里完成。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .archive import ArchiveError, detect_archive_kind, extract_archive
from .client import ImgBedClient, ImgBedError, UploadOutcome, sanitize_upload_folder
from .collector import KIND_FILE, KIND_IMAGE, Material, name_from_url
from .config import CompressConfig, FerryConfig, normalize_extension
from .logs import logger
from .media import (
    ARCHIVE_HINT_EXTENSIONS,
    NormalizedFile,
    compress_image,
    human_size,
    is_image_extension,
    normalize_file,
    sanitize_filename,
    sha256_hex,
    split_extension,
)
from .onebot import (
    URL_KEYS,
    fetch_file_payload,
    fetch_image_payload,
    payload_base64,
    payload_local_path,
    payload_url,
)
from .results import FailedItem, UploadedItem, UploadReport
from .store import FerryStore

DEFAULT_USER_AGENT = "AstrBot-ImgBedFerry/1.0"
FINGERPRINT_VERSION = "imgbed-ferry/v2"

#: 只有这几类错误重试才有意义；鉴权、参数、超限类错误重试一百次也是一样的结果。
RETRYABLE_KINDS = frozenset({"network", "timeout", "server", "rate_limit"})

#: `upload.folder_template` 支持的占位符，配置说明与帮助文本都引用这一份。
FOLDER_VARIABLES = (
    "date",
    "datetime",
    "yyyy",
    "mm",
    "dd",
    "group",
    "sender",
    "platform",
    "type",
    "ext",
)

#: 解压失败时给用户的下一步建议，键对齐 `ArchiveError.kind`。
ARCHIVE_HINTS = {
    "unsupported": "目前支持 zip / tar(.gz/.bz2/.xz) / gz / bz2 / xz / rar / 7z。",
    "broken": "压缩包可能在传输中损坏了，重新打包再发一次。",
    "password": "带密码的包请先把口令填到 archive.password。",
    "bomb": "解压体积或压缩比超出上限，按需调大 archive.max_total_mb / max_ratio。",
    "empty": "包里没有可上传的普通文件，可能只有空目录。",
    "missing_backend": "rar 需要 rarfile + unrar，7z 需要 py7zr，装好可选依赖再试。",
}

#: 取件用的下载函数：`(url, 允许的最大字节数) -> (数据, 文件名, MIME)`。测试里会注入假实现。
Fetcher = Callable[[str, int], Awaitable[tuple[bytes, str, str]]]
# 可选的「真正发起新上传前」检查。缓存命中或同指纹并发任务不会调用它。
QuotaCheck = Callable[[], Awaitable[str]]


def endpoint_identity(endpoint: Any) -> dict[str, Any]:
    """生成不含明文凭据的图床配置身份，用于去重隔离。"""
    mode = str(getattr(endpoint, "effective_auth_mode", "") or "")
    credential = ""
    if mode == "token":
        credential = str(getattr(endpoint, "api_token", "") or "")
    elif mode == "auth_code":
        credential = str(getattr(endpoint, "auth_code", "") or "")
    # 凭据只以摘要参与隔离，不会出现在 KV、日志或幂等键的明文中。
    credential_digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16]
    return {
        "base_url": str(getattr(endpoint, "normalized_base_url", "") or ""),
        "auth_mode": mode,
        "credential": credential_digest,
        "upload_channel": str(getattr(endpoint, "upload_channel", "") or ""),
        "channel_name": str(getattr(endpoint, "channel_name", "") or ""),
        "server_compress": bool(getattr(endpoint, "server_compress", True)),
        "auto_retry": bool(getattr(endpoint, "auto_retry", True)),
        "public_url_prefix": str(getattr(endpoint, "public_url_prefix", "") or "").rstrip("/"),
        "prefer_public_url": bool(getattr(endpoint, "prefer_public_url", True)),
    }


def compression_identity(cfg: CompressConfig, *, mode: str, changed: bool) -> dict[str, Any]:
    """压缩策略的可序列化身份；同一 payload 但策略不同也不混用缓存。"""
    return {
        "mode": str(mode or "default"),
        "enabled": bool(cfg.enabled),
        "trigger_bytes": int(cfg.trigger_bytes),
        "max_edge": int(cfg.max_edge),
        "quality": int(cfg.quality),
        "target_format": str(cfg.target_format),
        "keep_transparency": bool(cfg.keep_transparency),
        "skip_gif": bool(cfg.skip_gif),
        "changed": bool(changed),
    }


def build_upload_fingerprint(
    *,
    endpoint: Any,
    digest: str,
    folder: str,
    name_type: str,
    compression: Mapping[str, Any],
) -> str:
    """为一次最终上传生成稳定、无敏感信息的去重指纹。"""
    payload = {
        "version": FINGERPRINT_VERSION,
        "endpoint": endpoint_identity(endpoint),
        "sha256": str(digest),
        "folder": str(folder or ""),
        "name_type": str(name_type or "default"),
        "compression": dict(compression),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{FINGERPRINT_VERSION}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def idempotency_key_for_fingerprint(fingerprint: str) -> str:
    """把指纹变成可安全放进 HTTP header 的稳定幂等键。"""
    digest = hashlib.sha256(str(fingerprint).encode("utf-8")).hexdigest()
    return f"astrbot-imgbed-ferry-{digest}"


class ResolveError(Exception):
    """取件失败。

    `fatal=True` 表示体积超限这类硬失败 —— 换别的通道也一样，直接放弃这个素材；
    `fatal=False` 只是这条通道走不通，外层会继续尝试后面的兜底。
    """

    def __init__(self, reason: str, *, hint: str = "", fatal: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.hint = hint
        self.fatal = fatal


def _filename_from_disposition(value: str) -> str:
    """从 `Content-Disposition` 里取文件名，优先 RFC 5987 的 `filename*`。"""
    text = (value or "").strip()
    if not text:
        return ""
    plain = ""
    for chunk in text.split(";"):
        piece = chunk.strip()
        lowered = piece.lower()
        if lowered.startswith("filename*="):
            raw = piece[len("filename*=") :].strip().strip('"')
            if "''" in raw:
                raw = raw.split("''", 1)[1]
            return sanitize_filename(unquote(raw), fallback="")
        if lowered.startswith("filename=") and not plain:
            plain = piece[len("filename=") :].strip().strip('"')
    return sanitize_filename(unquote(plain), fallback="") if plain else ""


def _too_large(size: int, limit: int) -> ResolveError:
    return ResolveError(
        f"文件体积 {human_size(size)} 超过上限 {human_size(limit)}",
        hint="调大 upload.max_file_mb，或先自行压缩再发。",
        fatal=True,
    )


async def aiohttp_fetch(
    url: str,
    limit: int,
    *,
    timeout_seconds: float = 180.0,
    verify_ssl: bool = True,
) -> tuple[bytes, str, str]:
    """流式下载一个直链。

    边下边算体积，一旦超过 `limit` 立刻放弃 —— 不然一个几百 MB 的直链会把内存吃干。
    """
    import aiohttp

    connector = aiohttp.TCPConnector(ssl=None if verify_ssl else False)
    client_timeout = aiohttp.ClientTimeout(total=max(10.0, float(timeout_seconds)))
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    try:
        async with (
            aiohttp.ClientSession(
                connector=connector, timeout=client_timeout, headers=headers
            ) as session,
            session.get(url, allow_redirects=True) as response,
        ):
            if response.status >= 400:
                raise ResolveError(f"直链返回 HTTP {response.status}")
            declared = response.headers.get("Content-Length", "")
            if limit > 0 and declared.isdigit() and int(declared) > limit:
                raise _too_large(int(declared), limit)
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if limit > 0 and total > limit:
                    raise _too_large(total, limit)
                chunks.append(chunk)
            name = _filename_from_disposition(
                response.headers.get("Content-Disposition", "")
            ) or name_from_url(str(response.url))
            mime = (response.headers.get("Content-Type", "") or "").split(";")[0].strip()
            return b"".join(chunks), name, mime
    except ResolveError:
        raise
    except asyncio.TimeoutError as exc:
        raise ResolveError("下载直链超时") from exc
    except Exception as exc:
        raise ResolveError(f"下载直链失败：{type(exc).__name__}") from exc


@dataclass(slots=True)
class UploadContext:
    """一次上传请求的会话信息，供目录模板、配额与记录使用。"""

    session_id: str = ""
    user_id: str = ""
    group_id: str = ""
    platform: str = ""
    sender_name: str = ""

    @classmethod
    def from_event(cls, event: Any) -> UploadContext:
        def call(name: str) -> str:
            method = getattr(event, name, None)
            if not callable(method):
                return ""
            try:
                return str(method() or "")
            except Exception:
                # 各平台事件实现差异很大，取不到就当空值，不能因此中断上传。
                return ""

        try:
            session_id = str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:
            session_id = ""
        return cls(
            session_id=session_id,
            user_id=call("get_sender_id"),
            group_id=call("get_group_id"),
            platform=call("get_platform_name"),
            sender_name=call("get_sender_name"),
        )


def render_folder(
    template: str,
    *,
    ctx: UploadContext,
    kind: str = KIND_FILE,
    extension: str = "",
    sub: str = "",
) -> str:
    """把 `astrbot/{date}/{group}` 这样的模板渲染成真实目录。

    刻意不用 `str.format`：用户手写模板里出现一个不认识的花括号就会抛 KeyError。
    逐个 replace 更宽容，认不出来的原样留着，最后统一交给 `sanitize_upload_folder` 清洗。
    """
    now = datetime.now()
    mapping = {
        "date": now.strftime("%Y-%m-%d"),
        "datetime": now.strftime("%Y-%m-%d_%H%M%S"),
        "yyyy": now.strftime("%Y"),
        "mm": now.strftime("%m"),
        "dd": now.strftime("%d"),
        "group": ctx.group_id or "private",
        "sender": ctx.user_id or "unknown",
        "platform": ctx.platform or "astrbot",
        "type": kind or KIND_FILE,
        "ext": normalize_extension(extension) or "bin",
    }
    rendered = str(template or "")
    for key, value in mapping.items():
        rendered = rendered.replace("{" + key + "}", value)
    if sub:
        rendered = f"{rendered.rstrip('/')}/{sub}" if rendered.strip() else sub
    return sanitize_upload_folder(rendered)


@dataclass(slots=True)
class _Job:
    """一个已经拿到 bytes、等待上传的文件。"""

    file: NormalizedFile
    kind: str = KIND_FILE
    source: str = ""
    archive: str = ""
    sub_folder: str = ""
    digest: str = ""
    note: str = ""
    #: 内容与队列里第几个 job 完全相同（批内去重）；-1 表示它自己就要真传。
    clone_of: int = -1


@dataclass(slots=True)
class _InflightResult:
    """同一指纹的并发上传共享的结果，错误也用值传递避免 Future 警告。"""

    url: str = ""
    file_id: str = ""
    error: Exception | None = None


class _BatchCoordinator:
    """让同一批次内的相同指纹复用结果，即使任务串行执行。"""

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[_InflightResult]] = {}
        self._lock = asyncio.Lock()

    async def claim(self, fingerprint: str) -> tuple[asyncio.Future[_InflightResult], bool]:
        async with self._lock:
            future = self._futures.get(fingerprint)
            if future is not None:
                return future, False
            future = asyncio.get_running_loop().create_future()
            self._futures[fingerprint] = future
            return future, True

    async def finish(
        self,
        future: asyncio.Future[_InflightResult],
        result: _InflightResult,
    ) -> None:
        if not future.done():
            future.set_result(result)


class UploadService:
    """把素材列表变成上传结果的流水线。

    对外只有 `upload_materials` 一个入口，并且承诺不抛异常 —— 所有失败都写进
    `UploadReport`，这样指令层和 LLM 工具层都能拿到结构化的结果。
    """

    def __init__(
        self,
        config: FerryConfig,
        client: ImgBedClient,
        store: FerryStore | None = None,
        *,
        fetcher: Fetcher | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store
        self._fetcher = fetcher
        # 不仅同一批次，跨请求同时上传同一资源时也只允许一个真实请求。
        self._inflight: dict[str, asyncio.Future[_InflightResult]] = {}
        self._inflight_lock = asyncio.Lock()

    def update(self, config: FerryConfig, client: ImgBedClient) -> None:
        """配置热重载：换掉配置与客户端，保留注入的 fetcher 与 store。"""
        self.config = config
        self.client = client

    # ---------- 取件 ----------

    async def _fetch(self, url: str, limit: int) -> tuple[bytes, str, str]:
        if self._fetcher is not None:
            return await self._fetcher(url, limit)
        endpoint = self.config.endpoint
        return await aiohttp_fetch(
            url,
            limit,
            timeout_seconds=endpoint.timeout_seconds,
            verify_ssl=endpoint.verify_ssl,
        )

    def _read_local(self, path: str, limit: int) -> bytes:
        """读本地文件。

        故意写成同步方法：调用点都在 async 里，放进 async 反而要处理阻塞 IO 告警，
        而协议端给的本地文件通常就在同一台机器上，读起来很快。
        """
        try:
            target = Path(path)
            if not target.is_file():
                raise ResolveError("本地文件不存在或不是普通文件")
            size = target.stat().st_size
            if limit > 0 and size > limit:
                raise _too_large(size, limit)
            return target.read_bytes()
        except ResolveError:
            raise
        except OSError as exc:
            raise ResolveError(f"读取本地文件失败：{exc.strerror or exc}") from exc

    def _download_limit(self, name: str) -> int:
        """压缩包本体允许比单文件上限更大，否则一个 50 MB 的包永远进不了门。"""
        upload = self.config.upload
        extension = split_extension(sanitize_filename(name))[1]
        if self.config.archive.enabled and extension in ARCHIVE_HINT_EXTENSIONS:
            return max(upload.max_file_bytes, self.config.archive.max_total_bytes)
        return upload.max_file_bytes

    async def resolve(self, event: Any, material: Material) -> tuple[str, bytes, str]:
        """把素材变成 `(文件名, bytes, MIME)`，按可靠性依次尝试各条取件通道。"""
        limit = self._download_limit(material.name)
        last: ResolveError | None = None

        if material.data:
            return material.display_name(), material.data, material.mime

        if material.path:
            try:
                return (
                    material.display_name(),
                    self._read_local(material.path, limit),
                    material.mime,
                )
            except ResolveError as exc:
                if exc.fatal:
                    raise
                last = exc

        if material.url:
            try:
                data, fetched, mime = await self._fetch(material.url, limit)
            except ResolveError as exc:
                if exc.fatal:
                    raise
                last = exc
            else:
                if data:
                    name = material.name or fetched or material.display_name()
                    return name, data, material.mime or mime
                last = ResolveError("直链返回了空内容")

        payload = await self._fetch_via_onebot(event, material)
        if payload:
            resolved = await self._from_payload(material, payload, limit)
            if resolved is not None:
                return resolved
            last = last or ResolveError("协议端返回的取件信息不可用")

        try:
            data = await self._resolve_component(material, limit)
        except ResolveError as exc:
            if exc.fatal:
                raise
            last = exc
        else:
            if data:
                return material.display_name(), data, material.mime

        if last is not None:
            raise last
        raise ResolveError(
            "取不到文件内容",
            hint="协议端可能没实现取件接口，或直链已过期；重新发一次文件再试。",
        )

    async def _from_payload(
        self, material: Material, payload: dict[str, Any], limit: int
    ) -> tuple[str, bytes, str] | None:
        """把协议端回给的描述符落成实际内容，依次试直链 / 本地路径 / 内联 base64。"""
        url = payload_url(payload)
        if url:
            data, fetched, mime = await self._fetch(url, limit)
            if data:
                name = material.name or fetched or material.display_name()
                return name, data, material.mime or mime

        local = payload_local_path(payload)
        if local:
            return material.display_name(), self._read_local(local, limit), material.mime

        inline = payload_base64(payload)
        if inline:
            if limit > 0 and len(inline) > limit:
                raise _too_large(len(inline), limit)
            return material.display_name(), inline, material.mime
        return None

    async def _fetch_via_onebot(self, event: Any, material: Material) -> dict[str, Any]:
        """走协议端接口补直链。

        这是最重要的一条兜底：aiocqhttp 适配器解析 `file` 段时用了
        `event.message[0]["data"]["file_id"]` 这样的硬编码索引，多附件消息里除第一个
        以外的文件段会直接丢失，引用消息同样踩坑。这里带着原始 payload 自己再问一次。
        """
        if event is None:
            return {}
        payload = {key: value for key, value in material.payload.items() if key not in URL_KEYS}
        if material.file_id and not payload.get("file_id"):
            payload["file_id"] = material.file_id
        if not payload:
            return {}
        try:
            if material.kind == KIND_IMAGE:
                return await fetch_image_payload(event, payload)
            return await fetch_file_payload(event, payload)
        except Exception as exc:
            logger.debug("[图床摆渡] 协议端取件失败 %s: %s", material.display_name(), exc)
            return {}

    async def _resolve_component(self, material: Material, limit: int) -> bytes:
        """最后一招：直接问组件自己。

        `File.file` 这个 property 在异步上下文里会返回空串，只能用 `await get_file()`。
        """
        component = material.component
        if component is None:
            return b""
        try:
            if material.kind == KIND_IMAGE:
                getter = getattr(component, "convert_to_file_path", None)
                if not callable(getter):
                    return b""
                path = str(await getter() or "")
                return self._read_local(path, limit) if path else b""
            getter = getattr(component, "get_file", None)
            if not callable(getter):
                return b""
            located = str(await getter(allow_return_url=True) or "")
            if not located:
                return b""
            if located.lower().startswith(("http://", "https://")):
                data, _, _ = await self._fetch(located, limit)
                return data
            return self._read_local(located, limit)
        except ResolveError:
            raise
        except Exception as exc:
            logger.debug("[图床摆渡] 组件取件失败 %s: %s", material.display_name(), exc)
            return b""

    # ---------- 编排 ----------

    async def upload_materials(
        self,
        event: Any,
        materials: Sequence[Material],
        *,
        folder: str = "",
        name_type: str = "",
        extract: bool | None = None,
        compress: bool | None = None,
        quota_check: QuotaCheck | None = None,
    ) -> UploadReport:
        """完整流水线入口，永远返回 report 而不向外抛异常。"""
        started = time.monotonic()
        report = UploadReport()
        if not self.config.endpoint.configured:
            report.failures.append(
                FailedItem(
                    "",
                    "图床地址未配置",
                    "先在插件配置里填好 endpoint.base_url 与 api_token / auth_code。",
                )
            )
            return report
        if not materials:
            report.notes.append("没有找到可上传的图片或文件。")
            return report

        ctx = UploadContext.from_event(event)
        jobs = await self._prepare(event, materials, report, extract=extract)
        if jobs:
            await self._run_jobs(
                jobs,
                report,
                ctx=ctx,
                folder=folder,
                name_type=name_type,
                compress=compress,
                quota_check=quota_check,
            )
        report.elapsed = time.monotonic() - started
        return report

    async def _prepare(
        self,
        event: Any,
        materials: Sequence[Material],
        report: UploadReport,
        *,
        extract: bool | None,
    ) -> list[_Job]:
        """取件 + 展开压缩包 + 三重上限把关，产出真正要上传的 job 列表。"""
        cfg = self.config
        jobs: list[_Job] = []
        total = 0

        def add_job(job: _Job) -> bool:
            """把 job 排进队列。返回 False 表示整批到顶了，外层应停止收集。"""
            nonlocal total
            if len(jobs) >= cfg.upload.max_files:
                report.truncated = True
                return False
            name = job.file.filename
            size = job.file.size
            if size <= 0:
                report.skipped.append((name, "文件内容为空"))
                return True
            if size > cfg.upload.max_file_bytes:
                report.skipped.append(
                    (name, f"超出单文件上限 {human_size(cfg.upload.max_file_bytes)}")
                )
                return True
            if jobs and total + size > cfg.upload.max_total_bytes:
                report.truncated = True
                return False
            rejection = cfg.upload.extension_rejection(job.file.extension)
            if rejection:
                report.skipped.append((name, rejection))
                return True
            # 去重必须推迟到压缩、目录模板和上传配置全部确定之后；这里的
            # digest 仍然是原始 bytes 的摘要，仅用于旧代码兼容，不参与新缓存键。
            total += size
            jobs.append(job)
            return True

        want_extract = cfg.archive.default_extract if extract is None else bool(extract)
        for material in materials:
            if len(jobs) >= cfg.upload.max_files:
                report.truncated = True
                break
            label = material.display_name()
            try:
                name, data, mime = await self.resolve(event, material)
            except ResolveError as exc:
                report.failures.append(FailedItem(label, exc.reason, exc.hint, "resolve_failed"))
                continue
            except Exception as exc:
                logger.warning("[图床摆渡] 取件异常 %s: %s", label, exc, exc_info=True)
                report.failures.append(
                    FailedItem(label, f"取件异常：{type(exc).__name__}", code="resolve_failed")
                )
                continue
            if not data:
                report.failures.append(FailedItem(label, "取到的内容为空"))
                continue

            normalized = normalize_file(name or label, data, declared_mime=mime)
            archive_kind = (
                detect_archive_kind(normalized.filename, normalized.data)
                if cfg.archive.enabled
                else ""
            )
            if archive_kind and want_extract:
                if not self._expand_archive(normalized, material, report, add_job):
                    break
                continue
            if archive_kind and not cfg.archive.upload_archive_when_disabled:
                report.skipped.append((normalized.filename, "压缩包未解压，也未允许整包上传"))
                continue
            kind = KIND_IMAGE if is_image_extension(normalized.extension) else material.kind
            if not add_job(_Job(file=normalized, kind=kind, source=material.source)):
                break

        return jobs

    def _expand_archive(
        self,
        normalized: NormalizedFile,
        material: Material,
        report: UploadReport,
        add_job: Callable[[_Job], bool],
    ) -> bool:
        """解压压缩包并把成员逐个排队。返回 False 表示整批到顶，外层应停止。"""
        cfg = self.config.archive
        name = normalized.filename
        try:
            extraction = extract_archive(name, normalized.data, cfg)
        except ArchiveError as exc:
            fallback = exc.kind in {"missing_backend", "unsupported"}
            if fallback and cfg.upload_archive_when_disabled:
                report.notes.append(f"{name} 没能解压（{exc.message}），已按整包上传。")
                return add_job(
                    _Job(
                        file=normalized,
                        kind=material.kind,
                        source=material.source,
                        note="压缩包未解压，整包上传",
                    )
                )
            report.failures.append(FailedItem(name, exc.message, ARCHIVE_HINTS.get(exc.kind, "")))
            return True
        except Exception as exc:
            logger.warning("[图床摆渡] 解压异常 %s: %s", name, exc, exc_info=True)
            report.failures.append(FailedItem(name, f"解压异常：{type(exc).__name__}"))
            return True

        report.skipped.extend(extraction.skipped)
        if extraction.truncated:
            report.truncated = True
        if extraction.entries:
            report.notes.append(f"{name} 解压出 {len(extraction.entries)} 个文件。")

        stem = split_extension(name)[0] or "archive"
        if name.lower().endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")):
            stem = split_extension(stem)[0] or stem
        for entry in extraction.entries:
            leaf = entry.name.rsplit("/", 1)[-1]
            inner = entry.name.rsplit("/", 1)[0] if "/" in entry.name else ""
            member = normalize_file(leaf, entry.data)
            job = _Job(
                file=member,
                kind=KIND_IMAGE if is_image_extension(member.extension) else KIND_FILE,
                source=material.source,
                archive=name,
                sub_folder="/".join(part for part in (stem, inner) if part),
            )
            if not add_job(job):
                return False
        return True

    async def _run_jobs(
        self,
        jobs: list[_Job],
        report: UploadReport,
        *,
        ctx: UploadContext,
        folder: str,
        name_type: str,
        compress: bool | None,
        quota_check: QuotaCheck | None,
    ) -> None:
        """并发上传，按原顺序汇总结果并写入最近记录。

        相同指纹的任务由 ``_process`` 里的 Future 协调器合并；因此不能在
        ``_prepare`` 阶段按原始 bytes 提前标记 clone，因为压缩后的内容、实际
        目录和图床配置都可能改变最终是否可以复用。
        """
        compress_cfg = self._compress_config(compress)
        results: list[UploadedItem | FailedItem | None] = [None] * len(jobs)
        semaphore = asyncio.Semaphore(max(1, self.config.upload.concurrency))
        batch = _BatchCoordinator()

        async def run(index: int, job: _Job) -> None:
            async with semaphore:
                try:
                    results[index] = await self._process(
                        job,
                        ctx=ctx,
                        folder=folder,
                        name_type=name_type,
                        compress_cfg=compress_cfg,
                        batch=batch,
                        quota_check=quota_check,
                    )
                except ImgBedError as exc:
                    results[index] = FailedItem(
                        job.file.filename, exc.message, exc.hint, exc.kind or "upload_failed"
                    )
                except Exception as exc:
                    logger.warning(
                        "[图床摆渡] 上传异常 %s: %s", job.file.filename, exc, exc_info=True
                    )
                    results[index] = FailedItem(
                        job.file.filename, f"上传异常：{type(exc).__name__}", code="upload_failed"
                    )

        pending = [asyncio.create_task(run(index, job)) for index, job in enumerate(jobs)]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        stamp = int(time.time())
        records: list[dict[str, Any]] = []
        for result in results:
            if isinstance(result, UploadedItem):
                report.items.append(result)
                records.append(
                    {
                        "ts": stamp,
                        "name": result.name,
                        "url": result.url,
                        "size": result.size,
                        "file_id": result.file_id,
                        "sha256": result.digest,
                        "fingerprint": result.fingerprint,
                        "original_size": result.original_size,
                        "user": ctx.user_id,
                        "group": ctx.group_id,
                        "session": ctx.session_id,
                        "reused": result.reused,
                    }
                )
            elif isinstance(result, FailedItem):
                report.failures.append(result)

        if records and self.store is not None:
            await self.store.add_records(records)

    async def _process(
        self,
        job: _Job,
        *,
        ctx: UploadContext,
        folder: str,
        name_type: str,
        compress_cfg: CompressConfig,
        batch: _BatchCoordinator,
        quota_check: QuotaCheck | None,
    ) -> UploadedItem | FailedItem:
        """单个文件：最终化 payload → 指纹去重 → 幂等上传 → 记住摘要。"""
        payload = job.file
        note = job.note
        original_size = job.file.size
        compression_mode = "default"
        if is_image_extension(job.file.extension):
            compressed = compress_image(job.file, compress_cfg)
            if compressed.changed:
                payload = compressed.file
                note = "；".join(part for part in (note, compressed.note) if part)
        if compress_cfg is not self.config.compress:
            compression_mode = "force" if compress_cfg.enabled else "off"

        target = self._folder_for(job, override=folder, ctx=ctx, extension=payload.extension)
        digest = sha256_hex(payload.data)
        compression = compression_identity(
            compress_cfg,
            mode=compression_mode,
            changed=payload is not job.file,
        )
        effective_name_type = name_type or self.config.endpoint.name_type
        fingerprint = build_upload_fingerprint(
            endpoint=self.config.endpoint,
            digest=digest,
            folder=target,
            name_type=effective_name_type,
            compression=compression,
        )

        batch_future, batch_owner = await batch.claim(fingerprint)
        if not batch_owner:
            completed = await asyncio.shield(batch_future)
            if completed.error is not None:
                raise completed.error
            return self._item_from_inflight(
                job,
                payload=payload,
                original_size=original_size,
                digest=digest,
                fingerprint=fingerprint,
                completed=completed,
                note="与本批次内另一个文件内容相同，复用链接",
            )

        future: asyncio.Future[_InflightResult] | None = None
        global_owner = False
        try:
            cached, future, global_owner = await self._claim_upload(fingerprint, digest)
            if cached is not None:
                completed = _InflightResult(
                    url=str(cached.get("url") or ""),
                    file_id=str(cached.get("file_id") or ""),
                )
                await batch.finish(batch_future, completed)
                return self._item_from_cache(
                    job,
                    payload=payload,
                    original_size=original_size,
                    digest=digest,
                    fingerprint=fingerprint,
                    cached=cached,
                    note="命中去重缓存，未重复上传",
                )

            if not global_owner and future is not None:
                completed = await asyncio.shield(future)
                await batch.finish(batch_future, completed)
                if completed.error is not None:
                    raise completed.error
                return self._item_from_inflight(
                    job,
                    payload=payload,
                    original_size=original_size,
                    digest=digest,
                    fingerprint=fingerprint,
                    completed=completed,
                    note="与并发任务内容和上传选项相同，复用链接",
                )

            # global_owner=True：只有这个协程可以触发真实 HTTP 请求，其余协程等待同一个 Future。
            assert future is not None
            if quota_check is not None:
                quota_error = await quota_check()
                if quota_error:
                    raise ImgBedError(quota_error, kind="quota_exceeded")
            idempotency_key = idempotency_key_for_fingerprint(fingerprint)
            outcome = await self._upload_with_retry(
                filename=payload.filename,
                data=payload.data,
                content_type=payload.mime,
                folder=target,
                name_type=effective_name_type,
                idempotency_key=idempotency_key,
            )
            endpoint = self.config.endpoint
            url = self._apply_public_prefix(
                outcome.best_url(
                    base_url=endpoint.normalized_base_url,
                    prefer_public=endpoint.prefer_public_url,
                ),
                outcome,
            )
            if not self._is_usable_result_url(url):
                raise ImgBedError("图床返回了无效链接", kind="parse")
            await self._remember_cache(
                digest,
                fingerprint=fingerprint,
                url=url,
                file_id=outcome.file_id,
                name=payload.filename,
                size=payload.size,
                original_size=original_size,
                folder=target,
                compression=compression,
            )
            completed = _InflightResult(url=url, file_id=outcome.file_id)
            await self._finish_inflight(fingerprint, future, completed)
            await batch.finish(batch_future, completed)
            return UploadedItem(
                name=payload.filename,
                url=url,
                size=payload.size,
                original_size=original_size,
                file_id=outcome.file_id,
                digest=digest,
                fingerprint=fingerprint,
                kind=job.kind,
                source=job.source,
                archive=job.archive,
                note=note,
            )
        except BaseException as exc:
            completed = _InflightResult(
                error=exc if isinstance(exc, Exception) else RuntimeError("上传任务被取消")
            )
            if global_owner and future is not None:
                await self._finish_inflight(fingerprint, future, completed)
            await batch.finish(batch_future, completed)
            raise

    @staticmethod
    def _is_usable_result_url(url: str) -> bool:
        text = str(url or "").strip()
        return bool(text) and text.startswith(("http://", "https://", "/"))

    async def _claim_upload(
        self, fingerprint: str, digest: str
    ) -> tuple[dict[str, Any] | None, asyncio.Future[_InflightResult] | None, bool]:
        """原子地查缓存或登记一个正在进行的上传。"""
        async with self._inflight_lock:
            cached = await self._lookup_cache(digest, fingerprint=fingerprint)
            if cached and self._is_usable_result_url(str(cached.get("url") or "")):
                return cached, None, False
            future = self._inflight.get(fingerprint)
            if future is not None:
                return None, future, False
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._inflight[fingerprint] = future
            return None, future, True

    async def _finish_inflight(
        self, fingerprint: str, future: asyncio.Future[_InflightResult], result: _InflightResult
    ) -> None:
        if not future.done():
            future.set_result(result)
        async with self._inflight_lock:
            if self._inflight.get(fingerprint) is future:
                self._inflight.pop(fingerprint, None)

    async def _lookup_cache(self, digest: str, *, fingerprint: str) -> dict[str, Any] | None:
        if self.store is None or not digest:
            return None
        lookup = getattr(self.store, "lookup", None)
        if not callable(lookup):
            return None
        try:
            signature = inspect.signature(lookup)
            accepts = "fingerprint" in signature.parameters or any(
                item.kind == inspect.Parameter.VAR_KEYWORD for item in signature.parameters.values()
            )
        except (TypeError, ValueError):
            accepts = True
        if accepts:
            return await lookup(digest, fingerprint=fingerprint)
        # 兼容第三方注入的旧式 store；旧记录没有上下文，不能在新指纹逻辑中复用。
        return None

    async def _remember_cache(self, digest: str, **kwargs: Any) -> None:
        if self.store is None or not digest:
            return
        remember = getattr(self.store, "remember", None)
        if not callable(remember):
            return
        try:
            signature = inspect.signature(remember)
            accepts = "fingerprint" in signature.parameters or any(
                item.kind == inspect.Parameter.VAR_KEYWORD for item in signature.parameters.values()
            )
        except (TypeError, ValueError):
            accepts = True
        if not accepts:
            # 老式 store 仍可保存 digest，但这里不写入，避免污染无上下文旧格式。
            return
        try:
            await remember(digest, **kwargs)
        except Exception as exc:
            logger.debug("[图床摆渡] 写入去重缓存失败：%s", exc)

    @staticmethod
    def _item_from_cache(
        job: _Job,
        *,
        payload: NormalizedFile,
        original_size: int,
        digest: str,
        fingerprint: str,
        cached: Mapping[str, Any],
        note: str,
    ) -> UploadedItem:
        try:
            size = int(cached.get("size") or payload.size)
        except (TypeError, ValueError):
            size = payload.size
        return UploadedItem(
            name=payload.filename,
            url=str(cached.get("url") or ""),
            size=size,
            original_size=original_size,
            file_id=str(cached.get("file_id") or ""),
            digest=digest,
            fingerprint=fingerprint,
            kind=job.kind,
            source=job.source,
            archive=job.archive,
            note=note,
            reused=True,
        )

    @staticmethod
    def _item_from_inflight(
        job: _Job,
        *,
        payload: NormalizedFile,
        original_size: int,
        digest: str,
        fingerprint: str,
        completed: _InflightResult,
        note: str,
    ) -> UploadedItem:
        return UploadedItem(
            name=payload.filename,
            url=completed.url,
            size=payload.size,
            original_size=original_size,
            file_id=completed.file_id,
            digest=digest,
            fingerprint=fingerprint,
            kind=job.kind,
            source=job.source,
            archive=job.archive,
            note=note,
            reused=True,
        )

    def _folder_for(self, job: _Job, *, override: str, ctx: UploadContext, extension: str) -> str:
        template = override or self.config.upload.folder_template
        return render_folder(
            template, ctx=ctx, kind=job.kind, extension=extension, sub=job.sub_folder
        )

    def _compress_config(self, compress: bool | None) -> CompressConfig:
        cfg = self.config.compress
        if compress is None:
            return cfg
        if compress:
            # 用户明确要求压缩，就不再受「超过多少 MB 才压」的门槛限制。
            return replace(cfg, enabled=True, trigger_mb=min(cfg.trigger_mb, 0.05))
        return replace(cfg, enabled=False)

    async def _upload_with_retry(
        self,
        *,
        filename: str,
        data: bytes,
        content_type: str,
        folder: str,
        name_type: str,
        idempotency_key: str = "",
    ) -> UploadOutcome:
        upload = self.config.upload
        attempts = max(0, upload.retry_times) + 1
        last: ImgBedError | None = None
        for attempt in range(attempts):
            try:
                return await self._client_upload(
                    filename=filename,
                    data=data,
                    content_type=content_type,
                    folder=folder,
                    name_type=name_type,
                    idempotency_key=idempotency_key,
                )
            except ImgBedError as exc:
                last = exc
                if exc.kind not in RETRYABLE_KINDS or attempt == attempts - 1:
                    raise
                delay = max(0.0, upload.retry_backoff_seconds) * (attempt + 1)
                logger.debug(
                    "[图床摆渡] 上传 %s 第 %d 次失败（%s），%.1fs 后重试",
                    filename,
                    attempt + 1,
                    exc.kind,
                    delay,
                )
                if delay:
                    await asyncio.sleep(delay)
        if last is not None:
            raise last
        raise ImgBedError("上传没有实际执行", kind="unknown")

    async def _client_upload(self, **kwargs: Any) -> UploadOutcome:
        """调用客户端并兼容旧版/第三方 fake client。

        正式 ``ImgBedClient`` 支持 ``idempotency_key``；一些外部集成可能仍注入
        旧签名的 fake client，检测签名后省略新参数，避免破坏原有测试与扩展。
        """
        method = getattr(self.client, "upload", None)
        if not callable(method):
            raise ImgBedError("图床客户端没有 upload 方法", kind="unknown")
        try:
            signature = inspect.signature(method)
            accepts = "idempotency_key" in signature.parameters or any(
                item.kind == inspect.Parameter.VAR_KEYWORD for item in signature.parameters.values()
            )
        except (TypeError, ValueError):
            accepts = True
        if not accepts:
            kwargs.pop("idempotency_key", None)
        return await method(**kwargs)

    def _apply_public_prefix(self, url: str, outcome: UploadOutcome) -> str:
        """用自定义域名改写链接：Cloudflare 自定义域、反代或 CDN 前置时会用到。"""
        prefix = self.config.endpoint.public_url_prefix.strip().rstrip("/")
        if not prefix:
            return url
        if "://" not in prefix:
            prefix = f"https://{prefix}"
        source = outcome.src or url
        if "://" in source:
            source = "/" + source.split("://", 1)[1].partition("/")[2]
        if not source.startswith("/"):
            source = f"/{source}"
        return f"{prefix}{source}"
