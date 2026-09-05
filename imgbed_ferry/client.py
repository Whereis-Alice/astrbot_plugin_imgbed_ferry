"""CloudFlare-ImgBed HTTP 客户端。

契约按上游 `functions/upload/index.js` 与 `functions/api/manage/*` 核对：

* 上传：`POST {base}/upload`，`multipart/form-data`，字段名必须是 `file`；
  成功返回 **数组** `[{"src": "...", "publicUrl": "..."}]`，失败为非 200 且 body 是
  `Error: xxx` 文本。
* 删除：`DELETE {base}/api/manage/delete/<fileId>`，路径里的 `/` 必须写成 `,`。
* 批量删除：`POST {base}/api/manage/delete/batch`，body `{"fileIds": [...]}`。
* 列表：`GET {base}/api/manage/list`。
* 配额：`GET {base}/api/manage/quota` —— 需要 `manage` 权限，API Token 拿不到这个
  权限，因此 token 鉴权下必然 401，这里按「权限不足」而不是「配置错误」上报。

网络层通过 `transport` 注入，默认实现才会 import aiohttp，方便单测。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .config import AUTH_MODE_AUTH_CODE, AUTH_MODE_TOKEN, EndpointConfig

UPLOAD_FIELD_NAME = "file"
MAX_BATCH_DELETE = 500

ERROR_HINTS: dict[str, str] = {
    "not_configured": "还没填图床地址，先在插件配置里设置 endpoint.base_url。",
    "auth": "鉴权失败：检查 API Token 是否包含所需权限，或 authCode 是否正确。",
    "forbidden": "被图床拒绝：可能触发了 IP 封禁或安全策略。",
    "too_large": "文件超出图床或 Cloudflare 的单请求体积上限，换小文件或先本地压缩。",
    "rate_limit": "触发了图床限流，稍后重试。",
    "server": "图床服务端报错，检查所选上传渠道的配置与后端可用性。",
    "network": "网络不可达：确认 Bot 能访问图床域名。",
    "timeout": "请求超时：可适当调大 endpoint.timeout_seconds。",
    "bad_request": "请求被图床判为非法，检查扩展名与渠道设置。",
    "parse": "图床返回了无法解析的内容，可能被反代或 WAF 拦截。",
    "permission": "当前凭据权限不足（该接口需要管理员会话）。",
}


class ImgBedError(Exception):
    """带分类的图床错误，便于把可读的处理建议回给用户。"""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status = status

    @property
    def hint(self) -> str:
        return ERROR_HINTS.get(self.kind, "")

    def describe(self) -> str:
        parts = [self.message]
        if self.status:
            parts.append(f"HTTP {self.status}")
        if self.hint:
            parts.append(self.hint)
        return " | ".join(parts)

    def __str__(self) -> str:  # pragma: no cover - 交给 describe
        return self.describe()


@dataclass(slots=True)
class RawResponse:
    """与具体 HTTP 库解耦的响应体。"""

    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.text)


@dataclass(slots=True)
class FilePart:
    """multipart 的单个文件部件。"""

    field_name: str
    filename: str
    data: bytes
    content_type: str = "application/octet-stream"


Transport = Callable[..., Awaitable[RawResponse]]


def classify_status(status: int) -> str:
    if status == 401:
        return "auth"
    if status == 403:
        return "forbidden"
    if status in (413, 507):
        return "too_large"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server"
    if status >= 400:
        return "bad_request"
    return "unknown"


def clean_error_body(text: str, limit: int = 240) -> str:
    """把 `Error: xxx` 或 JSON 错误体压成一行短消息。"""
    body = (text or "").strip()
    if not body:
        return "图床未返回错误详情"
    if body.startswith(("{", "[")):
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            for key in ("error", "message", "msg", "detail"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    body = value.strip()
                    break
            else:
                body = json.dumps(parsed, ensure_ascii=False)
    if body.lower().startswith("error:"):
        body = body[6:].strip()
    body = " ".join(body.split())
    return body[:limit]


def encode_file_id_for_path(file_id: str) -> str:
    """管理接口把路径里的 `/` 当作 `,`（见 delete/[[path]].js）。"""
    return (file_id or "").strip().strip("/").replace("/", ",")


def extract_file_id(src: str) -> str:
    """从 `/file/xxx` 或 `https://host/file/xxx` 里取出 fileId。"""
    text = (src or "").strip()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
        text = text.partition("/")[2]
    text = text.split("?", 1)[0].split("#", 1)[0]
    text = text.lstrip("/")
    if text.startswith("file/"):
        text = text[5:]
    return text


@dataclass(slots=True)
class UploadOutcome:
    """一次成功上传的结果。"""

    src: str = ""
    public_url: str = ""
    file_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def best_url(self, *, base_url: str = "", prefer_public: bool = True) -> str:
        if prefer_public and self.public_url:
            return self.public_url
        if self.src.startswith("http://") or self.src.startswith("https://"):
            return self.src
        if base_url and self.src:
            return f"{base_url.rstrip('/')}/{self.src.lstrip('/')}"
        return self.src or self.public_url


def parse_upload_response(payload: Any) -> UploadOutcome:
    """上游固定返回单元素数组，但也兼容裸对象与 `{data: [...]}`。"""
    node: Any = payload
    if isinstance(node, Mapping) and "data" in node and not node.get("src"):
        node = node.get("data")
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        node = node[0] if node else None
    if not isinstance(node, Mapping):
        raise ImgBedError("图床返回体不含上传结果", kind="parse")
    src = str(node.get("src") or "").strip()
    public_url = str(node.get("publicUrl") or "").strip()
    if not src and not public_url:
        raise ImgBedError("图床返回体缺少 src 字段", kind="parse")
    return UploadOutcome(
        src=src,
        public_url=public_url,
        file_id=extract_file_id(src or public_url),
        raw=dict(node),
    )


async def _aiohttp_transport(
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    file_part: FilePart | None = None,
    timeout: float = 180.0,
    verify_ssl: bool = True,
) -> RawResponse:
    import aiohttp

    data: Any = None
    if file_part is not None:
        data = aiohttp.FormData(quote_fields=False)
        data.add_field(
            file_part.field_name,
            file_part.data,
            filename=file_part.filename,
            content_type=file_part.content_type,
        )

    connector = aiohttp.TCPConnector(ssl=None if verify_ssl else False)
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with (
            aiohttp.ClientSession(connector=connector, timeout=client_timeout) as session,
            session.request(
                method,
                url,
                params={k: str(v) for k, v in (params or {}).items()},
                headers=dict(headers or {}),
                json=json_body,
                data=data,
            ) as response,
        ):
            text = await response.text(errors="replace")
            return RawResponse(
                status=response.status,
                text=text,
                headers={k.lower(): v for k, v in response.headers.items()},
            )
    except asyncio.TimeoutError as exc:
        raise ImgBedError("请求图床超时", kind="timeout") from exc
    except ImgBedError:
        raise
    except Exception as exc:  # aiohttp 的异常层次太杂，统一归到网络类
        raise ImgBedError(f"访问图床失败：{exc}", kind="network") from exc


class ImgBedClient:
    """薄封装：只负责拼参数、发请求、把错误翻译成 `ImgBedError`。"""

    def __init__(
        self,
        endpoint: EndpointConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._transport: Transport = transport or _aiohttp_transport

    # ---------- 基础设施 ----------

    def _require_base(self) -> str:
        base = self.endpoint.normalized_base_url
        if not base:
            raise ImgBedError("图床地址未配置", kind="not_configured")
        return base

    def build_url(self, path: str) -> str:
        return f"{self._require_base()}/{path.lstrip('/')}"

    def auth_headers(self) -> dict[str, str]:
        mode = self.endpoint.effective_auth_mode
        if mode == AUTH_MODE_TOKEN:
            return {"Authorization": f"Bearer {self.endpoint.api_token}"}
        if mode == AUTH_MODE_AUTH_CODE:
            # 服务端也接受请求头形式的 authCode，比放在 URL 上更不容易进日志。
            return {"authCode": self.endpoint.auth_code}
        return {}

    def auth_query(self) -> dict[str, str]:
        if self.endpoint.effective_auth_mode == AUTH_MODE_AUTH_CODE:
            return {"authCode": self.endpoint.auth_code}
        return {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        file_part: FilePart | None = None,
        with_auth_query: bool = False,
    ) -> RawResponse:
        query: dict[str, Any] = dict(params or {})
        if with_auth_query:
            query.update(self.auth_query())
        return await self._transport(
            method,
            self.build_url(path),
            params=query,
            headers=self.auth_headers(),
            json_body=json_body,
            file_part=file_part,
            timeout=self.endpoint.timeout_seconds,
            verify_ssl=self.endpoint.verify_ssl,
        )

    @staticmethod
    def _ensure_ok(response: RawResponse, *, action: str) -> None:
        if 200 <= response.status < 300:
            return
        kind = classify_status(response.status)
        raise ImgBedError(
            f"{action}失败：{clean_error_body(response.text)}",
            kind=kind,
            status=response.status,
        )

    @staticmethod
    def _json_or_raise(response: RawResponse, *, action: str) -> Any:
        try:
            return response.json()
        except (TypeError, ValueError) as exc:
            raise ImgBedError(
                f"{action}返回非 JSON：{clean_error_body(response.text, 120)}",
                kind="parse",
                status=response.status,
            ) from exc

    # ---------- 业务接口 ----------

    def upload_params(self, *, folder: str = "", name_type: str = "") -> dict[str, str]:
        endpoint = self.endpoint
        params: dict[str, str] = {
            "uploadChannel": endpoint.upload_channel,
            "uploadNameType": name_type or endpoint.name_type,
            "returnFormat": "full",
            "autoRetry": "true" if endpoint.auto_retry else "false",
            "serverCompress": "true" if endpoint.server_compress else "false",
        }
        if endpoint.channel_name:
            params["channelName"] = endpoint.channel_name
        folder = sanitize_upload_folder(folder)
        if folder:
            params["uploadFolder"] = folder
        return params

    async def upload(
        self,
        *,
        filename: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        folder: str = "",
        name_type: str = "",
    ) -> UploadOutcome:
        response = await self._request(
            "POST",
            "/upload",
            params=self.upload_params(folder=folder, name_type=name_type),
            file_part=FilePart(UPLOAD_FIELD_NAME, filename, data, content_type),
            with_auth_query=False,
        )
        self._ensure_ok(response, action="上传")
        return parse_upload_response(self._json_or_raise(response, action="上传"))

    async def delete(self, file_id: str) -> dict[str, Any]:
        encoded = encode_file_id_for_path(file_id)
        if not encoded:
            raise ImgBedError("缺少要删除的 fileId", kind="bad_request")
        response = await self._request(
            "DELETE",
            f"/api/manage/delete/{encoded}",
            with_auth_query=True,
        )
        self._ensure_ok(response, action="删除")
        payload = self._json_or_raise(response, action="删除")
        return payload if isinstance(payload, dict) else {"success": True, "raw": payload}

    async def delete_batch(self, file_ids: Sequence[str]) -> dict[str, Any]:
        ids = [str(item).strip() for item in file_ids if str(item).strip()]
        if not ids:
            raise ImgBedError("缺少要删除的 fileId 列表", kind="bad_request")
        if len(ids) > MAX_BATCH_DELETE:
            raise ImgBedError(f"批量删除单次最多 {MAX_BATCH_DELETE} 个文件", kind="bad_request")
        response = await self._request(
            "POST",
            "/api/manage/delete/batch",
            json_body={"fileIds": ids},
            with_auth_query=True,
        )
        self._ensure_ok(response, action="批量删除")
        payload = self._json_or_raise(response, action="批量删除")
        return payload if isinstance(payload, dict) else {"success": True, "raw": payload}

    async def list_files(
        self,
        *,
        count: int = 20,
        directory: str = "",
        recursive: bool = False,
        search: str = "",
        channel: str = "",
        file_type: str = "",
        start: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"count": count, "start": start}
        if directory:
            params["dir"] = directory
        if recursive:
            params["recursive"] = "true"
        if search:
            params["search"] = search
        if channel:
            params["channel"] = channel
        if file_type:
            params["fileType"] = file_type
        response = await self._request(
            "GET", "/api/manage/list", params=params, with_auth_query=True
        )
        self._ensure_ok(response, action="拉取文件列表")
        payload = self._json_or_raise(response, action="拉取文件列表")
        return payload if isinstance(payload, dict) else {"files": payload}

    async def quota(self) -> dict[str, Any]:
        response = await self._request("GET", "/api/manage/quota", with_auth_query=True)
        if response.status == 401:
            raise ImgBedError(
                "查询配额需要管理员会话，API Token 无法获得 manage 权限",
                kind="permission",
                status=401,
            )
        self._ensure_ok(response, action="查询配额")
        payload = self._json_or_raise(response, action="查询配额")
        return payload if isinstance(payload, dict) else {"raw": payload}

    async def probe(self) -> dict[str, Any]:
        """自检：用一张 1x1 PNG 走完整上传链路，再把它删掉。"""
        probe_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
            b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        outcome = await self.upload(
            filename="astrbot-imgbed-ferry-probe.png",
            data=probe_png,
            content_type="image/png",
            folder="astrbot/probe",
        )
        report: dict[str, Any] = {
            "uploaded": True,
            "url": outcome.best_url(
                base_url=self.endpoint.normalized_base_url,
                prefer_public=self.endpoint.prefer_public_url,
            ),
            "file_id": outcome.file_id,
            "cleaned": False,
            "clean_error": "",
        }
        if outcome.file_id:
            try:
                await self.delete(outcome.file_id)
                report["cleaned"] = True
            except ImgBedError as exc:
                report["clean_error"] = exc.describe()
        return report


def sanitize_upload_folder(folder: str) -> str:
    """对齐服务端 `sanitizeUploadFolder`：去掉穿越与非法字符。"""
    text = (folder or "").strip().replace("\\", "/")
    if not text:
        return ""
    parts: list[str] = []
    for chunk in text.split("/"):
        piece = chunk.strip()
        if not piece or piece == ".":
            continue
        if piece == "..":
            piece = "_"
        cleaned = "".join(
            ch if (ch.isalnum() or ch in "-_.@" or ord(ch) > 127) else "_" for ch in piece
        )
        cleaned = cleaned.strip("._") or "_"
        parts.append(cleaned)
    return "/".join(parts)
