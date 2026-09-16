"""图床摆渡：把图片、文件和压缩包一键送进 CloudFlare-ImgBed。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Sequence
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star

from .imgbed_ferry.client import ImgBedClient, ImgBedError, extract_file_id
from .imgbed_ferry.collector import (
    KIND_FILE,
    KIND_IMAGE,
    SOURCE_REPLY,
    Material,
    collect_materials,
    name_from_url,
)
from .imgbed_ferry.config import FerryConfig
from .imgbed_ferry.formatting import (
    format_item,
    format_link,
    format_results,
    format_urls,
    normalize_style,
    summary_line,
)
from .imgbed_ferry.integration import AssetValidationError, parse_asset_handle
from .imgbed_ferry.media import human_size, is_image_extension, split_extension
from .imgbed_ferry.results import FailedItem, UploadReport
from .imgbed_ferry.service import FOLDER_VARIABLES, QuotaCheck, UploadService
from .imgbed_ferry.session import SessionMaterials
from .imgbed_ferry.store import SCOPE_GROUP, SCOPE_USER, FerryStore
from .imgbed_ferry.tools import TOOL_CLASSES

PLUGIN_ID = "astrbot_plugin_imgbed_ferry"
PLUGIN_NAME = "图床摆渡"
PLUGIN_VERSION = "1.1.0"
PLUGIN_REPO = "https://github.com/Whereis-Alice/astrbot_plugin_imgbed_ferry"

#: 取件目标，和 `imgbed_upload` 工具的 `target` 枚举保持一致。
TARGET_MODES = ("auto", "latest", "reply", "all", "index")
#: 一条指令最多删几个文件，避免手滑清空图床。
MAX_DELETE_TARGETS = 50
#: `_recent_batch` 最多缓存多少个会话，防止长期运行后无限增长。
MAX_BATCH_SESSIONS = 200
#: 预览最多贴几张图，超过就只给链接。
MAX_PREVIEW_IMAGES = 9
#: 外链转存的素材来源标记。
SOURCE_LINK = "link"

NO_MATERIAL_HINT = (
    "当前会话里没有可上传的图片或文件。请让用户把图片、文件或压缩包发出来，"
    "或者引用（回复）那条带附件的消息后再说一次要上传。绝对不要凭空编造图床链接。"
)
RELAY_HINT = (
    "把 urls 里的链接原样转达给用户，不要改写、不要缩短、不要补全，也不要编造没出现过的链接。"
)

_INDEX_TOKEN = re.compile(r"^#?[+-]?\d{1,4}$")
_URL_PREFIXES = ("http://", "https://")

#: 只有这些平台确认支持合并转发（Node/Nodes），其余平台一律退回长文本。
FORWARD_PLATFORMS = ("aiocqhttp",)

#: 工具名 -> 读取哪个权限开关。
_TOOL_SWITCHES = {
    "imgbed_upload": "upload_tool_enabled",
    "imgbed_list_materials": "list_tool_enabled",
    "imgbed_recent_uploads": "records_tool_enabled",
    "imgbed_quota": "quota_tool_enabled",
    "imgbed_delete": "delete_tool_enabled",
}


def _tokens(event: AstrMessageEvent) -> list[str]:
    """取指令名之后的参数。

    指令处理器一律只声明 `(self, event)`：AstrBot 会把 `event` 之后的形参当成具名
    参数注入，纯数字还会被悄悄转成 int，所以参数统一在这里自己解析。
    """
    text = re.sub(r"\s+", " ", str(getattr(event, "message_str", "") or "").strip())
    if not text:
        return []
    _, _, tail = text.partition(" ")
    return [token for token in tail.strip().split(" ") if token]


def _stamp(value: Any, pattern: str = "%m-%d %H:%M") -> str:
    """把 KV 里的时间戳格式化成人能读的样子，坏数据不抛异常。"""
    try:
        return time.strftime(pattern, time.localtime(float(value)))
    except (TypeError, ValueError, OSError, OverflowError):
        return "未知时间"


def _material_from_url(url: str) -> Material:
    """把一条外链包装成待上传素材，真正的下载交给 UploadService。"""
    name = name_from_url(url) or "download"
    kind = KIND_IMAGE if is_image_extension(split_extension(name)[1]) else KIND_FILE
    return Material(kind=kind, name=name, source=SOURCE_LINK, url=url)


class ImgBedFerryPlugin(Star):
    """把聊天里的图片、文件和压缩包摆渡到 CloudFlare-ImgBed 图床。

    元数据统一由 metadata.yaml 提供，插件类由 AstrBot 的 Star.__init_subclass__
    自动注册，因此不使用已废弃的 @register 装饰器。

    常用入口：直接对 Bot 说「把这张图传到图床」（LLM 工具），或 /图床上传、/图床列表。
    完整指令见 /图床，可视化配置见 Dashboard 中的「图床摆渡」页面。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.raw_config = config
        self.config = FerryConfig.from_mapping(config)
        self.client = ImgBedClient(self.config.endpoint)
        self.store = FerryStore(self, self.config.behavior)
        self.session = SessionMaterials(self.config.behavior)
        self.service = UploadService(self.config, self.client, self.store)
        #: session_id -> 上一批一起出现的素材 id，用于「隔一句话再说上传」时精确复原。
        self._recent_batch: dict[str, list[str]] = {}
        #: 跨插件上传在真正发起 HTTP 请求前预留的配额，避免并发请求绕过上限。
        self._asset_quota_lock = asyncio.Lock()
        self._asset_quota_reserved: dict[tuple[str, str], int] = {}
        self._tool_names: list[str] = []
        self._register_tools()
        if not self.config.endpoint.configured:
            logger.warning(
                "[%s] 还没有填写图床地址，请在插件配置里设置 endpoint.base_url 与鉴权信息",
                PLUGIN_NAME,
            )
        logger.info("[%s] v%s 已加载", PLUGIN_NAME, PLUGIN_VERSION)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _register_tools(self) -> None:
        perm = self.config.permission
        if not perm.llm_tools_enabled:
            logger.info("[%s] LLM 工具已在配置中关闭", PLUGIN_NAME)
            return
        tools = []
        for tool_class in TOOL_CLASSES:
            tool = tool_class(plugin=self)
            switch = _TOOL_SWITCHES.get(tool.name, "")
            if switch and not getattr(perm, switch, True):
                continue
            tools.append(tool)
        if not tools:
            logger.info("[%s] 所有 LLM 工具都被配置关闭了", PLUGIN_NAME)
            return
        self.context.add_llm_tools(*tools)
        self._tool_names = [tool.name for tool in tools]
        logger.info("[%s] 已注册工具: %s", PLUGIN_NAME, ", ".join(self._tool_names))

    async def terminate(self) -> None:
        unregister = getattr(self.context, "unregister_llm_tool", None)
        if callable(unregister):
            for name in self._tool_names:
                try:
                    unregister(name)
                except Exception as exc:
                    logger.debug("[%s] 注销工具 %s 失败：%s", PLUGIN_NAME, name, exc)
        self._tool_names = []
        self.session.clear()
        self._recent_batch.clear()
        logger.info("[%s] 已卸载并清理会话缓存", PLUGIN_NAME)

    # ------------------------------------------------------------------
    # 素材收集
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        """旁听每条消息，把看到的素材登记进会话缓存。

        这里刻意用 `resolve_reply=False`：解析引用要额外打一次 OneBot API，放在
        全量消息钩子里代价太大。引用消息在真正要上传时再解析。
        本钩子永远不产生结果、不调用 stop_event，不会影响其它插件。
        """
        try:
            materials = await collect_materials(event, resolve_reply=False)
        except Exception as exc:
            logger.debug("[%s] 旁听收集素材失败：%s", PLUGIN_NAME, exc)
            return
        if materials:
            self._remember(event, materials)

    def _remember(self, event: AstrMessageEvent, materials: Sequence[Material]) -> None:
        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        if not session_id or not materials:
            return
        if not self.session.remember(session_id, materials):
            return
        if len(self._recent_batch) >= MAX_BATCH_SESSIONS:
            for stale in list(self._recent_batch)[: MAX_BATCH_SESSIONS // 2]:
                self._recent_batch.pop(stale, None)
        self._recent_batch[session_id] = [item.material_id for item in materials]

    async def _collect(self, event: AstrMessageEvent) -> list[Material]:
        """上传时的完整收集：连引用消息一起解析。"""
        try:
            return await collect_materials(event, resolve_reply=True)
        except Exception as exc:
            logger.warning("[%s] 收集素材失败：%s", PLUGIN_NAME, exc, exc_info=True)
            return []

    async def _select(
        self,
        event: AstrMessageEvent,
        *,
        target: str = "auto",
        indexes: Sequence[int] = (),
        item_ids: Sequence[str] = (),
        source_urls: Sequence[str] = (),
    ) -> tuple[list[Material], list[str], list[tuple[str, str]]]:
        """把「传哪些」翻译成素材列表，返回 (素材, 没匹配上的标识, 被拒的外链)。"""
        rejected: list[tuple[str, str]] = []
        links: list[Material] = []
        for raw in source_urls:
            url = str(raw or "").strip()
            if not url:
                continue
            reason = self.config.upload.url_ingest_rejection(url)
            if reason:
                rejected.append((url, reason))
                continue
            links.append(_material_from_url(url))

        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        mode = str(target or "auto").strip().lower()
        if indexes or item_ids:
            mode = "index"
        if mode not in TARGET_MODES:
            mode = "auto"

        missing: list[str] = []
        base: list[Material] = []
        if mode == "index":
            base, missing = self.session.pick(session_id, item_ids=item_ids, indexes=indexes)
        else:
            live = await self._collect(event)
            if live:
                self._remember(event, live)
            base = self._resolve_mode(mode, session_id, live)

        merged: list[Material] = []
        seen: set[str] = set()
        for material in [*base, *links]:
            if material.signature in seen:
                continue
            seen.add(material.signature)
            merged.append(material)
        return merged, missing, rejected

    def _resolve_mode(self, mode: str, session_id: str, live: Sequence[Material]) -> list[Material]:
        if mode == "all":
            return self.session.snapshot(session_id)
        if mode == "reply":
            replied = [item for item in live if item.source == SOURCE_REPLY]
            if replied:
                return replied
            mode = "latest"
        if mode == "latest":
            if live:
                return [live[-1]]
            latest = self.session.latest(session_id)
            return [latest] if latest else []
        # auto：优先本条消息（含引用），否则回到上一批一起出现的素材。
        if live:
            return list(live)
        batch = self._recent_batch.get(session_id) or []
        if batch:
            picked, _ = self.session.pick(session_id, item_ids=batch)
            if picked:
                return picked
        latest = self.session.latest(session_id)
        return [latest] if latest else []

    # ------------------------------------------------------------------
    # 权限与配额
    # ------------------------------------------------------------------

    @staticmethod
    def _event_value(event: Any, method_name: str) -> str:
        method = getattr(event, method_name, None)
        if not callable(method):
            return ""
        try:
            return str(method() or "")
        except Exception:
            return ""

    def _permission_denied(self, event: AstrMessageEvent, *, need_admin: bool = False) -> str:
        """返回空串表示放行，否则返回可以直接展示给用户的拒绝原因。"""
        perm = self.config.permission
        user_id = self._event_value(event, "get_sender_id")
        group_id = self._event_value(event, "get_group_id")
        if user_id and user_id in perm.user_blacklist:
            return "你不在图床摆渡的可用名单里。"
        if group_id:
            if group_id in perm.group_blacklist:
                return "本群已被管理员禁用图床摆渡。"
            if perm.group_whitelist and group_id not in perm.group_whitelist:
                return "本群不在图床摆渡的白名单里。"
        if (perm.admin_only or need_admin) and not self._is_admin(event):
            return "这个操作只有 AstrBot 管理员能用。"
        return ""

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    async def _quota_denied(self, event: AstrMessageEvent, count: int) -> str:
        """每日配额预检。管理员不受限，配额为 0 表示不限量。"""
        perm = self.config.permission
        if count <= 0:
            return ""
        if perm.daily_quota_per_user <= 0 and perm.daily_quota_per_group <= 0:
            return ""
        if self._is_admin(event):
            return ""
        async with self._asset_quota_state()[0]:
            for scope, key, limit in self._quota_scopes(event):
                used = await self.store.quota_used(scope, key)
                pending = self._asset_quota_state()[1].get((scope, key), 0)
                if used + pending + count > limit:
                    return self._quota_message(scope, used + pending, limit, count)
        return ""

    def _asset_quota_state(self) -> tuple[asyncio.Lock, dict[tuple[str, str], int]]:
        """取得跨插件配额预留状态；兼容单测里绕过 ``__init__`` 构造的实例。"""
        lock = getattr(self, "_asset_quota_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._asset_quota_lock = lock
        reserved = getattr(self, "_asset_quota_reserved", None)
        if reserved is None:
            reserved = {}
            self._asset_quota_reserved = reserved
        return lock, reserved

    def _quota_scopes(self, event: Any) -> list[tuple[str, str, int]]:
        perm = self.config.permission
        scopes: list[tuple[str, str, int]] = []
        user_id = self._event_value(event, "get_sender_id")
        group_id = self._event_value(event, "get_group_id")
        if perm.daily_quota_per_user > 0 and user_id:
            scopes.append((SCOPE_USER, user_id, perm.daily_quota_per_user))
        if perm.daily_quota_per_group > 0 and group_id:
            scopes.append((SCOPE_GROUP, group_id, perm.daily_quota_per_group))
        return scopes

    @staticmethod
    def _quota_message(scope: str, used: int, limit: int, count: int) -> str:
        if scope == SCOPE_GROUP:
            return f"本群今天的图床额度不够了：已用 {used}/{limit}，这次还要 {count} 个。额度每天 0 点重置。"
        return (
            f"你今天的图床额度不够了：已用 {used}/{limit}，这次还要 {count} 个。额度每天 0 点重置。"
        )

    async def _reserve_asset_quota(self, event: Any, count: int = 1) -> str:
        """只在服务确认「要新传」时预留额度；命中去重不会调用此方法。"""
        if count <= 0 or self._is_admin(event):
            return ""
        perm = self.config.permission
        if perm.daily_quota_per_user <= 0 and perm.daily_quota_per_group <= 0:
            return ""
        lock, reserved = self._asset_quota_state()
        scopes = self._quota_scopes(event)
        async with lock:
            for scope, key, limit in scopes:
                used = await self.store.quota_used(scope, key)
                pending = reserved.get((scope, key), 0)
                if used + pending + count > limit:
                    return self._quota_message(scope, used + pending, limit, count)
            for scope, key, _ in scopes:
                reservation_key = (scope, key)
                reserved[reservation_key] = reserved.get(reservation_key, 0) + count
        return ""

    async def _release_asset_quota(self, event: Any, count: int = 1) -> None:
        if count <= 0:
            return
        _, reserved = self._asset_quota_state()
        scopes = self._quota_scopes(event)
        async with self._asset_quota_state()[0]:
            for scope, key, _ in scopes:
                reservation_key = (scope, key)
                remaining = reserved.get(reservation_key, 0) - count
                if remaining > 0:
                    reserved[reservation_key] = remaining
                else:
                    reserved.pop(reservation_key, None)

    async def _charge(self, event: AstrMessageEvent, report: UploadReport) -> None:
        """只对真正占用了图床空间的文件计数，命中去重复用的不算。

        无论配额是否设置都记账，这样 `/图床配额` 能给出真实用量。
        """
        billed = sum(1 for item in report.items if not item.reused)
        if billed <= 0:
            return
        try:
            async with self._asset_quota_state()[0]:
                user_id = self._event_value(event, "get_sender_id")
                if user_id:
                    await self.store.quota_add(SCOPE_USER, user_id, billed)
                group_id = self._event_value(event, "get_group_id")
                if group_id:
                    await self.store.quota_add(SCOPE_GROUP, group_id, billed)
        except Exception as exc:
            logger.debug("[%s] 写入配额计数失败：%s", PLUGIN_NAME, exc)

    # ------------------------------------------------------------------
    # 上传编排
    # ------------------------------------------------------------------

    async def _run_upload(
        self,
        event: AstrMessageEvent,
        *,
        materials: Sequence[Material],
        rejected: Sequence[tuple[str, str]] = (),
        folder: str = "",
        name_type: str = "",
        extract: bool | None = None,
        compress: bool | None = None,
        quota_check: QuotaCheck | None = None,
    ) -> UploadReport:
        """指令与 LLM 工具共用的上传出口，永远返回 report。"""
        if materials:
            report = await self.service.upload_materials(
                event,
                materials,
                folder=folder,
                name_type=name_type,
                extract=extract,
                compress=compress,
                quota_check=quota_check,
            )
        else:
            report = UploadReport()
        for url, reason in rejected:
            report.failures.append(
                FailedItem(
                    url,
                    reason,
                    "外链转存默认关闭；需要管理员开启 upload.allow_url_ingest 并配好域名白名单。",
                )
            )
        await self._charge(event, report)
        return report

    async def upload_asset(
        self,
        event: Any,
        asset: Any,
        folder: str = "",
        compress: bool | None = None,
        output_format: str = "",
    ) -> dict[str, Any]:
        """供其它 AstrBot 插件调用的受控资源上传接口。

        ``asset`` 必须是带来源、短 TTL、大小和 SHA-256 的资源句柄（例如
        meme_magpie 的 ``MemeAssetHandle``），不能传裸本地路径。接口不经过
        LLM 工具，也不修改/删除提供方源文件；返回值始终是可 JSON 序列化的
        字典，方便 AstrBook 等消费者直接拼接 Markdown。
        """
        integration = self.config.integration
        if not integration.enabled:
            return self._json_asset_error("跨插件资源上传已被管理员关闭。", "integration_disabled")
        if not self.config.endpoint.configured:
            return self._json_asset_error("图床地址未配置。", "not_configured")

        denied = self._permission_denied(event)
        if denied:
            return self._json_asset_error(denied, "forbidden")

        try:
            handle = parse_asset_handle(
                asset,
                allowed_sources=integration.allowed_asset_sources,
                max_ttl_seconds=integration.max_handle_ttl_seconds,
                max_bytes=self.config.upload.max_file_bytes,
                require_declared_hash=integration.require_declared_hash,
            )
            data = await handle.read_bytes()
        except AssetValidationError as exc:
            return self._json_asset_error(exc.message, exc.code)
        except Exception as exc:
            logger.debug("[%s] 读取跨插件资源失败：%s", PLUGIN_NAME, exc)
            return self._json_asset_error("读取受控资源失败。", "invalid_asset")

        material = Material(
            kind=KIND_IMAGE,
            name=handle.name,
            source=handle.source,
            data=data,
            size=len(data),
            mime=handle.mime,
            material_id=handle.asset_id,
        )
        try:
            report = await self._run_upload(
                event,
                materials=[material],
                folder=str(folder or ""),
                compress=compress if isinstance(compress, bool) else None,
                quota_check=lambda: self._reserve_asset_quota(event),
            )
        finally:
            # 真实上传完成后 ``_charge`` 已经记账；失败、取消和缓存命中则只会
            # 释放本次临时预留，不会凭空增加配额用量。
            await self._release_asset_quota(event)
        if not report.items:
            failure = report.failures[0] if report.failures else None
            return self._json_asset_error(
                failure.describe() if failure else "上传失败。",
                (failure.code if failure and failure.code else "upload_failed"),
                details=failure.to_dict() if failure else None,
            )

        item = report.items[0]
        style = normalize_style(output_format, self.config.behavior.output_format)
        formatted = format_item(item, style, show_size=False)
        result: dict[str, Any] = {
            "success": True,
            "url": item.url,
            "file_id": item.file_id,
            "sha256": item.digest,
            "reused": bool(item.reused),
            "name": item.name,
            "size": item.size,
            "original_size": item.original_size or item.size,
            "output_format": style,
            "formatted": formatted,
            "text": formatted,
            "markdown": format_link(item.url, item.name, "markdown", image=True),
            "asset_id": handle.asset_id,
            "source": handle.source,
        }
        if report.notes:
            result["notes"] = list(report.notes)
        return result

    @classmethod
    def _json_asset_error(
        cls, message: str, code: str, *, details: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"success": False, "code": code, "error": message}
        if details:
            result["details"] = details
        return result

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------

    def _self_uin(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_self_id() or "") or "0"
        except Exception:
            return "0"

    def _node(self, event: AstrMessageEvent, text: str) -> Node:
        return Node(name=PLUGIN_NAME, uin=self._self_uin(event), content=[Plain(text)])

    def _supports_forward(self, event: AstrMessageEvent) -> bool:
        """合并转发只在确认支持的平台上用，避免在其它协议端发出空消息。"""
        try:
            return str(event.get_platform_name() or "").lower() in FORWARD_PLATFORMS
        except Exception:
            return False

    def _should_forward(self, event: AstrMessageEvent, count: int) -> bool:
        threshold = self.config.behavior.forward_threshold
        if threshold <= 0:
            return False
        return count >= max(2, threshold) and self._supports_forward(event)

    def _result(self, event: AstrMessageEvent, header: str, chunks: Sequence[str]):
        """行数多就折叠成合并转发，否则拼成一条长文本。"""
        if not chunks:
            return event.plain_result(header)
        if self._should_forward(event, len(chunks)):
            nodes = [self._node(event, header)]
            nodes.extend(self._node(event, chunk) for chunk in chunks)
            return event.chain_result([Nodes(nodes=nodes)])
        return event.plain_result("\n".join([header, *chunks]))

    def _report_result(self, event: AstrMessageEvent, report: UploadReport, style: str):
        behavior = self.config.behavior
        if self._should_forward(event, len(report.items)):
            nodes = [self._node(event, summary_line(report, show_size=behavior.show_size))]
            nodes.extend(
                self._node(event, format_item(item, style, show_size=behavior.show_size))
                for item in report.items
            )
            urls = format_urls(report)
            if urls:
                nodes.append(self._node(event, f"只要链接：\n{urls}"))
            tail: list[str] = []
            tail.extend(f"⚠️ {item.name or '素材'}：{item.describe()}" for item in report.failures)
            tail.extend(f"↷ {name}：{reason}" for name, reason in report.skipped)
            tail.extend(report.notes)
            if tail:
                nodes.append(self._node(event, "\n".join(tail)))
            return event.chain_result([Nodes(nodes=nodes)])
        return event.plain_result(format_results(report, style, show_size=behavior.show_size))

    async def _send_preview(self, event: AstrMessageEvent, report: UploadReport) -> None:
        """可选的图片回显。默认关闭：群里刷图容易被风控。"""
        if not self.config.behavior.reply_with_image_preview:
            return
        urls = [item.url for item in report.items if item.is_image and item.url]
        if not urls:
            return
        try:
            chain = [Image.fromURL(url) for url in urls[:MAX_PREVIEW_IMAGES]]
            await event.send(MessageChain(chain=chain))
        except Exception as exc:
            logger.debug("[%s] 回显图片失败：%s", PLUGIN_NAME, exc)

    async def _notify(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(MessageChain(chain=[Plain(text)]))
        except Exception as exc:
            logger.debug("[%s] 发送进度提示失败：%s", PLUGIN_NAME, exc)

    @staticmethod
    def _json(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def _json_error(cls, message: str, **extra: Any) -> str:
        payload: dict[str, Any] = {"success": False, "error": message}
        payload.update(extra)
        return cls._json(payload)

    # ------------------------------------------------------------------
    # 共用查询
    # ------------------------------------------------------------------

    async def _overview(self, event: AstrMessageEvent) -> dict[str, Any]:
        """图床状态 + 今日用量。刻意不回显 api_token / auth_code。"""
        endpoint = self.config.endpoint
        upload = self.config.upload
        perm = self.config.permission
        data: dict[str, Any] = {
            "configured": endpoint.configured,
            "base_url": endpoint.normalized_base_url or "(未配置)",
            "auth_mode": endpoint.effective_auth_mode,
            "upload_channel": endpoint.upload_channel or "(图床默认)",
            "channel_name": endpoint.channel_name or "(图床默认)",
            "limits": {
                "max_file_mb": upload.max_file_mb,
                "max_total_mb": upload.max_total_mb,
                "max_files": upload.max_files,
                "concurrency": upload.concurrency,
            },
            "compress_enabled": self.config.compress.enabled,
            "archive_extract_default": self.config.archive.default_extract,
            "url_ingest_enabled": upload.allow_url_ingest,
        }

        today: dict[str, Any] = {}
        user_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        try:
            if user_id:
                used = await self.store.quota_used(SCOPE_USER, user_id)
                today["user"] = {"used": used, "limit": perm.daily_quota_per_user}
                if perm.daily_quota_per_user > 0:
                    today["user"]["left"] = max(0, perm.daily_quota_per_user - used)
            if group_id:
                used = await self.store.quota_used(SCOPE_GROUP, group_id)
                today["group"] = {"used": used, "limit": perm.daily_quota_per_group}
                if perm.daily_quota_per_group > 0:
                    today["group"]["left"] = max(0, perm.daily_quota_per_group - used)
        except Exception as exc:
            logger.debug("[%s] 读取配额用量失败：%s", PLUGIN_NAME, exc)
        data["today"] = today

        if endpoint.configured:
            try:
                data["remote_quota"] = await self.client.quota()
            except ImgBedError as exc:
                data["remote_quota_error"] = exc.describe()
                data["remote_quota_hint"] = (
                    "/api/manage/quota 需要 manage 权限，只填 API Token 时必然失败；"
                    "想看图床整体用量请改用 authCode 或直接看图床后台。"
                )
            except Exception as exc:
                data["remote_quota_error"] = str(exc)
        return data

    async def _delete_targets(self, targets: Sequence[str]) -> dict[str, Any]:
        """把链接或 fileId 列表变成一次删除操作。这是不可恢复的。"""
        if not self.config.endpoint.configured:
            return {"success": False, "error": "图床地址未配置。"}
        file_ids: list[str] = []
        invalid: list[str] = []
        for raw in targets:
            text = str(raw or "").strip()
            if not text:
                continue
            file_id = extract_file_id(text)
            if not file_id:
                invalid.append(text)
                continue
            if file_id not in file_ids:
                file_ids.append(file_id)
        if not file_ids:
            return {"success": False, "error": "没解析出可删除的 fileId。", "invalid": invalid}
        if len(file_ids) > MAX_DELETE_TARGETS:
            return {
                "success": False,
                "error": f"一次最多删除 {MAX_DELETE_TARGETS} 个文件，请分批来。",
                "received": len(file_ids),
            }
        try:
            if len(file_ids) == 1:
                await self.client.delete(file_ids[0])
            else:
                await self.client.delete_batch(file_ids)
        except ImgBedError as exc:
            return {"success": False, "error": exc.describe(), "file_ids": file_ids}
        except Exception as exc:
            return {"success": False, "error": f"删除失败：{exc}", "file_ids": file_ids}

        cleared = 0
        for file_id in file_ids:
            try:
                cleared += await self.store.drop_by_file_id(file_id)
            except Exception as exc:
                logger.debug("[%s] 清理去重缓存失败：%s", PLUGIN_NAME, exc)
        result: dict[str, Any] = {
            "success": True,
            "deleted": len(file_ids),
            "file_ids": file_ids,
            "dedupe_cleared": cleared,
        }
        if invalid:
            result["invalid"] = invalid
        return result

    # ------------------------------------------------------------------
    # LLM 工具桥接：统一返回 JSON 字符串
    # ------------------------------------------------------------------

    async def tool_upload(
        self,
        *,
        event: AstrMessageEvent,
        target: str = "auto",
        indexes: Sequence[int] = (),
        item_ids: Sequence[str] = (),
        source_urls: Sequence[str] = (),
        folder: str = "",
        extract: bool | None = None,
        compress: bool | None = None,
        name_type: str = "",
        output_format: str = "",
    ) -> str:
        if not self.config.permission.upload_tool_enabled:
            return self._json_error("图床上传工具已被管理员关闭。")
        denied = self._permission_denied(event)
        if denied:
            return self._json_error(denied)

        materials, missing, rejected = await self._select(
            event,
            target=target,
            indexes=indexes,
            item_ids=item_ids,
            source_urls=source_urls,
        )
        if not materials:
            payload: dict[str, Any] = {
                "success": False,
                "error": "没有找到可上传的素材。",
                "instruction": NO_MATERIAL_HINT,
            }
            if missing:
                payload["not_found"] = list(missing)
            if rejected:
                payload["rejected"] = [{"url": url, "reason": reason} for url, reason in rejected]
            return self._json(payload)

        quota = await self._quota_denied(event, len(materials))
        if quota:
            return self._json_error(quota)

        report = await self._run_upload(
            event,
            materials=materials,
            rejected=rejected,
            folder=folder,
            name_type=name_type,
            extract=extract,
            compress=compress,
        )
        behavior = self.config.behavior
        style = normalize_style(output_format, behavior.output_format)
        data = report.to_dict()
        data["text"] = format_results(report, style, show_size=behavior.show_size)
        if report.items:
            data["urls"] = [item.url for item in report.items]
        if missing:
            data.setdefault("notes", []).append("这些素材没匹配上：" + "、".join(missing))
        data["instruction"] = RELAY_HINT
        await self._send_preview(event, report)
        return self._json(data)

    async def tool_list_materials(self, *, event: AstrMessageEvent) -> str:
        if not self.config.permission.list_tool_enabled:
            return self._json_error("素材列表工具已被管理员关闭。")
        denied = self._permission_denied(event)
        if denied:
            return self._json_error(denied)

        live = await self._collect(event)
        if live:
            self._remember(event, live)
        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        items = self.session.snapshot(session_id)
        if not items:
            return self._json(
                {"success": True, "count": 0, "materials": [], "instruction": NO_MATERIAL_HINT}
            )
        total = len(items)
        materials = [
            {
                "index": index,
                "reverse_index": index - total - 1,
                "id": material.material_id,
                "kind": material.kind,
                "name": material.display_name(),
                "size": material.size,
                "size_text": human_size(material.size) if material.size else "未知",
                "source": material.source,
            }
            for index, material in enumerate(items, start=1)
        ]
        return self._json(
            {
                "success": True,
                "count": total,
                "materials": materials,
                "instruction": (
                    "把 id 填进 imgbed_upload 的 item_ids，或把 index / reverse_index 填进 indexes，"
                    "即可精确上传其中某几个。"
                ),
            }
        )

    async def tool_recent(self, *, event: AstrMessageEvent, limit: int = 5) -> str:
        if not self.config.permission.records_tool_enabled:
            return self._json_error("上传记录工具已被管理员关闭。")
        denied = self._permission_denied(event)
        if denied:
            return self._json_error(denied)
        try:
            capped = max(1, min(int(limit), 30))
        except (TypeError, ValueError):
            capped = 5
        records = await self.store.recent(capped)
        return self._json(
            {
                "success": True,
                "count": len(records),
                "records": [
                    {
                        "time": _stamp(record.get("ts")),
                        "name": record.get("name", ""),
                        "url": record.get("url", ""),
                        "size": record.get("size", 0),
                        "reused": bool(record.get("reused")),
                    }
                    for record in records
                ],
                "instruction": RELAY_HINT,
            }
        )

    async def tool_quota(self, *, event: AstrMessageEvent) -> str:
        if not self.config.permission.quota_tool_enabled:
            return self._json_error("图床状态工具已被管理员关闭。")
        denied = self._permission_denied(event)
        if denied:
            return self._json_error(denied)
        overview = await self._overview(event)
        return self._json({"success": True, **overview})

    async def tool_delete(self, *, event: AstrMessageEvent, targets: Sequence[str] = ()) -> str:
        perm = self.config.permission
        if not perm.delete_tool_enabled:
            return self._json_error(
                "删除工具默认关闭。需要的话请管理员在插件配置里打开 permission.delete_tool_enabled。"
            )
        denied = self._permission_denied(event, need_admin=perm.delete_requires_admin)
        if denied:
            return self._json_error(denied)
        if not targets:
            return self._json_error("没有给出要删除的链接或 fileId。")
        return self._json(await self._delete_targets(targets))

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command("图床", alias={"imgbed"})
    async def cmd_help(self, event: AstrMessageEvent):
        """帮助面板。"""
        endpoint = self.config.endpoint
        upload = self.config.upload
        state = "已配置" if endpoint.configured else "未配置"
        tools = ", ".join(self._tool_names) if self._tool_names else "（已全部关闭）"
        variables = " ".join("{" + name + "}" for name in FOLDER_VARIABLES)
        lines = [
            f"🛟 {PLUGIN_NAME} v{PLUGIN_VERSION}",
            f"图床：{state} · {endpoint.normalized_base_url or '请先填 base_url'}（鉴权 {endpoint.effective_auth_mode}）",
            f"渠道：{endpoint.upload_channel or '图床默认'} / {endpoint.channel_name or '图床默认'}",
            f"LLM 工具：{tools}",
            "",
            "最省事的用法是直接说话，例如：",
            "  「把这张图传图床」 / 「引用那个压缩包，解压后全传上去」 / 「刚才那几个文件都传了给我链接」",
            "",
            "指令：",
            "  /图床上传 [目标] [-f 目录] [-o 格式] [-n 命名] [解压|不解压] [压缩|原图]",
            "      目标：auto latest reply all，或序号（1 最早、-1 最新），或素材 id，或 http(s) 外链",
            "      格式：plain markdown html bbcode",
            "      命名：default index origin short",
            "  /图床列表  当前会话缓存了哪些素材",
            "  /图床记录 [条数]  最近上传成功的文件与链接",
            "  /图床配额  图床状态与今日用量",
            "",
            "管理员：",
            "  /图床自检  跑一次完整上传链路并自动清理",
            "  /图床文件 [目录]  查看图床上的文件",
            "  /图床删除 <链接或 fileId…>  不可恢复",
            "  /图床清理 素材|记录|全部  清本地缓存，不动图床",
            "",
            f"目录模板变量：{variables}",
            f"当前目录模板：{upload.folder_template or '(空)'}",
            "详细配置请在 Dashboard 的插件管理里打开「图床摆渡」。",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("图床上传", alias={"ibup", "imgbedup"})
    async def cmd_upload(self, event: AstrMessageEvent):
        """把消息里 / 引用里的素材送上图床。"""
        denied = self._permission_denied(event)
        if denied:
            yield event.plain_result(f"⛔ {denied}")
            return

        args = _parse_upload_args(_tokens(event))
        materials, missing, rejected = await self._select(
            event,
            target=args["target"],
            indexes=args["indexes"],
            item_ids=args["item_ids"],
            source_urls=args["urls"],
        )
        if not materials:
            lines = ["没找到可上传的图片或文件。"]
            if missing:
                lines.append("没匹配上：" + "、".join(missing))
            lines.extend(f"外链被拒：{url} —— {reason}" for url, reason in rejected)
            lines.append("把图片 / 文件直接发出来，或者引用那条带附件的消息，再发一次 /图床上传。")
            lines.append("想看会话里还剩什么素材：/图床列表")
            yield event.plain_result("\n".join(lines))
            return

        quota = await self._quota_denied(event, len(materials))
        if quota:
            yield event.plain_result(f"⛔ {quota}")
            return

        if len(materials) >= 3:
            await self._notify(event, f"⏳ 正在把 {len(materials)} 个素材送上图床…")

        report = await self._run_upload(
            event,
            materials=materials,
            rejected=rejected,
            folder=args["folder"],
            name_type=args["name_type"],
            extract=args["extract"],
            compress=args["compress"],
        )
        if missing:
            report.notes.append("这些素材没匹配上：" + "、".join(missing))
        style = normalize_style(args["style"], self.config.behavior.output_format)
        yield self._report_result(event, report, style)
        await self._send_preview(event, report)

    @filter.command("图床列表", alias={"ibls", "iblist"})
    async def cmd_list(self, event: AstrMessageEvent):
        """看会话里还能上传哪些素材。"""
        denied = self._permission_denied(event)
        if denied:
            yield event.plain_result(f"⛔ {denied}")
            return

        live = await self._collect(event)
        if live:
            self._remember(event, live)
        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        items = self.session.snapshot(session_id)
        if not items:
            yield event.plain_result(
                "当前会话还没缓存到图片或文件。\n把素材发出来，或者引用带附件的消息后再试。"
            )
            return

        chunks: list[str] = []
        for index, material in enumerate(items, start=1):
            size = human_size(material.size) if material.size else "大小未知"
            chunks.append(
                f"{index}. [{material.kind}] {material.display_name()}\n"
                f"   {size} · 来源 {material.source} · id {material.material_id}"
            )
        header = (
            f"🗂 会话素材 {len(items)} 个（序号 1 最早、-1 最新）\n"
            "上传其中某个：/图床上传 3   或   /图床上传 <id>"
        )
        yield self._result(event, header, chunks)

    @filter.command("图床记录", alias={"iblog"})
    async def cmd_records(self, event: AstrMessageEvent):
        """最近上传成功的文件与链接。"""
        denied = self._permission_denied(event)
        if denied:
            yield event.plain_result(f"⛔ {denied}")
            return

        limit = 10
        for token in _tokens(event):
            if token.isdigit():
                limit = max(1, min(int(token), 50))
                break
        records = await self.store.recent(limit)
        if not records:
            yield event.plain_result("还没有上传记录。")
            return

        chunks: list[str] = []
        for index, record in enumerate(records, start=1):
            size = int(record.get("size") or 0)
            tail = f" · {human_size(size)}" if size else ""
            mark = " · 复用" if record.get("reused") else ""
            chunks.append(
                f"{index}. {_stamp(record.get('ts'))} {record.get('name') or '未命名'}{tail}{mark}\n"
                f"   {record.get('url') or '(无链接)'}"
            )
        yield self._result(event, f"🧾 最近 {len(records)} 条上传记录", chunks)

    @filter.command("图床配额", alias={"ibquota"})
    async def cmd_quota(self, event: AstrMessageEvent):
        """图床状态与今日用量。"""
        denied = self._permission_denied(event)
        if denied:
            yield event.plain_result(f"⛔ {denied}")
            return

        data = await self._overview(event)
        limits = data["limits"]
        lines = [
            f"🛟 图床：{'已配置' if data['configured'] else '未配置'} · {data['base_url']}",
            f"鉴权：{data['auth_mode']} · 渠道：{data['upload_channel']} / {data['channel_name']}",
            f"上限：单文件 {limits['max_file_mb']}MB · 单次总量 {limits['max_total_mb']}MB · "
            f"最多 {limits['max_files']} 个 · 并发 {limits['concurrency']}",
            f"图片预压缩：{'开' if data['compress_enabled'] else '关'} · "
            f"压缩包默认解压：{'是' if data['archive_extract_default'] else '否'} · "
            f"外链转存：{'开' if data['url_ingest_enabled'] else '关'}",
        ]
        today = data.get("today") or {}
        for scope, label in (("user", "你今天"), ("group", "本群今天")):
            item = today.get(scope)
            if not item:
                continue
            if item.get("limit"):
                lines.append(f"{label}：{item['used']}/{item['limit']}，还剩 {item.get('left', 0)}")
            else:
                lines.append(f"{label}：已传 {item['used']} 个（未限量）")
        remote = data.get("remote_quota")
        if isinstance(remote, dict) and remote:
            lines.append("图床用量：" + self._json(remote))
        elif data.get("remote_quota_error"):
            lines.append(f"图床用量：查不到（{data['remote_quota_error']}）")
            if data.get("remote_quota_hint"):
                lines.append(f"　{data['remote_quota_hint']}")
        yield event.plain_result("\n".join(lines))

    @filter.command("图床自检", alias={"ibcheck"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_check(self, event: AstrMessageEvent):
        """上传一张 1x1 PNG 跑完整链路，再删掉。"""
        if not self.config.endpoint.configured:
            yield event.plain_result("⛔ 还没配置图床地址，先填 endpoint.base_url 与鉴权信息。")
            return

        await self._notify(event, "⏳ 正在用一张 1x1 PNG 跑完整上传链路…")
        try:
            report = await self.client.probe()
        except ImgBedError as exc:
            yield event.plain_result(f"❌ 自检失败：{exc.describe()}")
            return
        except Exception as exc:
            yield event.plain_result(f"❌ 自检异常：{exc}")
            return

        lines = [
            "✅ 上传链路正常",
            f"链接：{report.get('url') or '(图床没返回链接)'}",
            f"fileId：{report.get('file_id') or '(无)'}",
        ]
        if report.get("cleaned"):
            lines.append("测试文件已自动删除。")
        else:
            reason = report.get("clean_error") or "图床没返回 fileId"
            lines.append(f"⚠️ 测试文件没删掉（{reason}）")
            lines.append("请到图床后台手动清理 astrbot/probe 目录。")
        yield event.plain_result("\n".join(lines))

    @filter.command("图床文件", alias={"ibfiles"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_files(self, event: AstrMessageEvent):
        """看图床上已有的文件。"""
        if not self.config.endpoint.configured:
            yield event.plain_result("⛔ 还没配置图床地址。")
            return

        tokens = _tokens(event)
        directory = tokens[0] if tokens else ""
        try:
            payload = await self.client.list_files(count=20, directory=directory)
        except ImgBedError as exc:
            yield event.plain_result(f"❌ 拉取失败：{exc.describe()}")
            return
        except Exception as exc:
            yield event.plain_result(f"❌ 拉取异常：{exc}")
            return

        rows = payload.get("files")
        if not isinstance(rows, list):
            rows = []
        if not rows:
            yield event.plain_result(f"📂 {directory or '/'} 下没有文件。")
            return

        chunks: list[str] = []
        for index, row in enumerate(rows[:20], start=1):
            if not isinstance(row, dict):
                chunks.append(f"{index}. {row}")
                continue
            name = row.get("name") or row.get("id") or "未命名"
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            size = int(meta.get("FileSize") or meta.get("fileSize") or 0)
            tail = f" · {human_size(size)}" if size else ""
            chunks.append(f"{index}. {name}{tail}")
        header = f"📂 {directory or '/'} 共列出 {len(chunks)} 条（最多 20）"
        yield self._result(event, header, chunks)

    @filter.command("图床删除", alias={"ibdel"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_delete(self, event: AstrMessageEvent):
        """从图床删除文件，不可恢复。"""
        targets = [token for token in _tokens(event) if token]
        if not targets:
            yield event.plain_result(
                "用法：/图床删除 <链接或 fileId> [更多…]\n这是不可恢复操作，请确认后再执行。"
            )
            return

        result = await self._delete_targets(targets)
        if not result.get("success"):
            lines = [f"❌ {result.get('error') or '删除失败'}"]
            if result.get("invalid"):
                lines.append("没认出来：" + "、".join(result["invalid"]))
            yield event.plain_result("\n".join(lines))
            return

        lines = [f"🗑 已删除 {result['deleted']} 个文件。"]
        if result.get("dedupe_cleared"):
            lines.append(f"同时清掉了 {result['dedupe_cleared']} 条去重缓存。")
        if result.get("invalid"):
            lines.append("没认出来：" + "、".join(result["invalid"]))
        yield event.plain_result("\n".join(lines))

    @filter.command("图床清理", alias={"ibclear"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_clear(self, event: AstrMessageEvent):
        """清本地缓存，不动图床上的文件。"""
        tokens = _tokens(event)
        scope = tokens[0] if tokens else ""
        if scope in {"素材", "materials", "session"}:
            self.session.clear()
            self._recent_batch.clear()
            yield event.plain_result("🧹 已清空会话素材缓存。图床上的文件没动。")
            return
        if scope in {"记录", "records", "log"}:
            removed = await self.store.clear_records()
            yield event.plain_result(f"🧹 已清空 {removed} 条上传记录。图床上的文件没动。")
            return
        if scope in {"全部", "all"}:
            self.session.clear()
            self._recent_batch.clear()
            removed = await self.store.clear_records()
            yield event.plain_result(
                f"🧹 已清空会话素材缓存和 {removed} 条上传记录。图床上的文件没动。"
            )
            return
        yield event.plain_result(
            "用法：/图床清理 素材 | 记录 | 全部\n只清本地缓存，不会删图床上的文件。"
        )


# ----------------------------------------------------------------------
# 指令参数解析
#
# 放在类后面是刻意的：指令处理器只能声明 (self, event)，参数全靠这里手工拆，
# 单独成一块方便对照 `imgbed_upload` 工具的参数表。
# ----------------------------------------------------------------------

#: 需要吃掉下一个 token 的选项 -> 落到 `_parse_upload_args` 的哪个键。
_VALUE_FLAGS = {
    "-f": "folder",
    "--folder": "folder",
    "--dir": "folder",
    "目录": "folder",
    "文件夹": "folder",
    "-o": "style",
    "--format": "style",
    "--output": "style",
    "格式": "style",
    "-n": "name_type",
    "--name": "name_type",
    "命名": "name_type",
}

#: 取件模式的中文别名。
_TARGET_ALIASES = {
    "自动": "auto",
    "最新": "latest",
    "最后": "latest",
    "最后一个": "latest",
    "引用": "reply",
    "回复": "reply",
    "全部": "all",
    "所有": "all",
}

#: 开关型选项 -> (键, 值)。None 表示跟随配置，这里只处理显式覆盖。
_SWITCH_FLAGS = {
    "--extract": ("extract", True),
    "解压": ("extract", True),
    "--no-extract": ("extract", False),
    "不解压": ("extract", False),
    "整包": ("extract", False),
    "--compress": ("compress", True),
    "压缩": ("compress", True),
    "--no-compress": ("compress", False),
    "不压缩": ("compress", False),
    "原图": ("compress", False),
    "原档": ("compress", False),
}


def _parse_upload_args(tokens: Sequence[str]) -> dict[str, Any]:
    """把 /图床上传 后面的自由文本拆成结构化参数。"""
    args: dict[str, Any] = {
        "target": "auto",
        "indexes": [],
        "item_ids": [],
        "urls": [],
        "folder": "",
        "style": "",
        "name_type": "",
        "extract": None,
        "compress": None,
    }
    pending = ""
    for token in tokens:
        if pending:
            args[pending] = token
            pending = ""
            continue

        lowered = token.lower()
        if lowered in _VALUE_FLAGS:
            pending = _VALUE_FLAGS[lowered]
            continue
        if lowered in _SWITCH_FLAGS:
            key, value = _SWITCH_FLAGS[lowered]
            args[key] = value
            continue
        if "=" in token:
            head, _, tail = token.partition("=")
            key = _VALUE_FLAGS.get(head.lower(), "")
            if key and tail:
                args[key] = tail
                continue
        if lowered in TARGET_MODES:
            args["target"] = lowered
            continue
        if token in _TARGET_ALIASES:
            args["target"] = _TARGET_ALIASES[token]
            continue
        if _INDEX_TOKEN.match(token):
            args["indexes"].append(int(token.lstrip("#")))
            continue
        if lowered.startswith(_URL_PREFIXES):
            args["urls"].append(token)
            continue
        if "/" in token and not args["folder"]:
            args["folder"] = token
            continue
        args["item_ids"].append(token)

    if args["indexes"] or args["item_ids"]:
        args["target"] = "index"
    return args
