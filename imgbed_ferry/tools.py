"""LLM 工具定义：让模型自己决定什么时候把素材送上图床。

工具全部收在 `imgbed_` 前缀下，`call()` 只做参数搬运，真正的业务在 `main.py` 的
`tool_*` 桥接方法里。所有工具统一返回 JSON 字符串，方便模型二次组织语言。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

NO_CONTEXT = '{"success": false, "error": "工具缺少事件上下文"}'

_NAME_TYPE_DESC = (
    "图床上的文件命名方式：default=前缀 + 原名，index=纯随机短名，"
    "origin=完全保留原名，short=极短随机名。不确定就别填。"
)
_OUTPUT_DESC = (
    "链接风格：plain 只给裸 URL，markdown 给 ![]() 或 []()，html 给标签，bbcode 给论坛码。"
)


def _event_from_context(context: ContextWrapper[AstrAgentContext]):
    return getattr(getattr(context, "context", None), "event", None)


def _str_list(value: Any) -> list[str]:
    """模型有时把数组写成 `"a, b"`，这里一并容错。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _int_list(value: Any) -> list[int]:
    result: list[int] = []
    for item in _str_list(value):
        try:
            result.append(int(float(item)))
        except (TypeError, ValueError):
            continue
    return result


def _optional_bool(value: Any) -> bool | None:
    """区分「模型没提」和「模型明确要求关闭」——两者行为完全不同。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on", "是", "要"}:
        return True
    if text in {"false", "0", "no", "n", "off", "否", "不要"}:
        return False
    return None


@dataclass
class ImgBedUploadTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = "imgbed_upload"
    description: str = (
        "把用户这条消息里带的、或者用户引用的图片、文件、压缩包上传到图床，返回可直接分享的直链。"
        "用户说「传图床」「上传一下」「转成链接」「帮我存起来」时调用。"
        "压缩包默认会解压后逐个上传，需要整包上传时把 extract_archive 设为 false。"
        "也能把用户给出的外部图片链接转存进图床（source_urls），需要管理员先开启该开关。"
        "不确定要传哪些素材时先调用 imgbed_list_materials 看一眼再决定。"
    )
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["auto", "latest", "reply", "all", "index"],
                    "description": (
                        "要传哪些素材。auto=本条消息带的和引用的全部（默认，绝大多数场景用这个）；"
                        "latest=最近出现的那一个；reply=只要被引用消息里的；"
                        "all=把会话里缓存的素材全部上传；index=按 indexes / item_ids 精确挑选。"
                    ),
                },
                "indexes": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "配合 target=index 使用的序号，1 是最早出现的，-1 是最新的。",
                },
                "item_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "配合 target=index 使用的素材 id，来自 imgbed_list_materials。",
                },
                "source_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "要转存进图床的外部 http/https 直链。只有用户在消息里明确给出链接、"
                        "并要求把它存进图床时才填；不要自己编造或猜测链接。"
                    ),
                },
                "folder": {
                    "type": "string",
                    "description": (
                        "图床上的目标目录，例如 `壁纸/2026`。用户明确说了放哪里才填，"
                        "留空使用插件配置的目录模板。"
                    ),
                },
                "extract_archive": {
                    "type": "boolean",
                    "description": "压缩包是否解压后逐个上传。默认按插件配置；用户要整包链接时填 false。",
                },
                "compress": {
                    "type": "boolean",
                    "description": "是否对图片做本地压缩再上传。用户嫌图太大时填 true，要求原图时填 false。",
                },
                "name_type": {
                    "type": "string",
                    "enum": ["default", "index", "origin", "short"],
                    "description": _NAME_TYPE_DESC,
                },
                "output_format": {
                    "type": "string",
                    "enum": ["plain", "markdown", "html", "bbcode"],
                    "description": _OUTPUT_DESC,
                },
            },
            "required": [],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        event = _event_from_context(context)
        if self.plugin is None or event is None:
            return NO_CONTEXT
        return await self.plugin.tool_upload(
            event=event,
            target=str(kwargs.get("target") or "auto"),
            indexes=_int_list(kwargs.get("indexes")),
            item_ids=_str_list(kwargs.get("item_ids")),
            source_urls=_str_list(kwargs.get("source_urls")),
            folder=str(kwargs.get("folder") or ""),
            extract=_optional_bool(kwargs.get("extract_archive")),
            compress=_optional_bool(kwargs.get("compress")),
            name_type=str(kwargs.get("name_type") or ""),
            output_format=str(kwargs.get("output_format") or ""),
        )


@dataclass
class ImgBedListMaterialsTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = "imgbed_list_materials"
    description: str = (
        "列出当前会话里还能上传的图片、文件和压缩包，含稳定的素材 id、名称与体积。"
        "用户说「刚才那张」「第二个文件」这类模糊指代时，先用它确认目标再调用 imgbed_upload。"
    )
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}, "required": []}
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        event = _event_from_context(context)
        if self.plugin is None or event is None:
            return NO_CONTEXT
        return await self.plugin.tool_list_materials(event=event)


@dataclass
class ImgBedRecentTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = "imgbed_recent_uploads"
    description: str = "查最近通过本插件上传成功的文件与链接。用户问「刚才传的链接是啥」「之前那张图的地址」时调用。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "description": "要看几条，默认 5 条。",
                }
            },
            "required": [],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        event = _event_from_context(context)
        if self.plugin is None or event is None:
            return NO_CONTEXT
        raw = kwargs.get("limit")
        try:
            limit = int(float(raw)) if raw not in (None, "") else 5
        except (TypeError, ValueError):
            limit = 5
        return await self.plugin.tool_recent(event=event, limit=limit)


@dataclass
class ImgBedQuotaTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = "imgbed_quota"
    description: str = (
        "查图床连通性、当前渠道配置，以及本群 / 本人今天还剩多少上传额度。"
        "用户问「图床还能用吗」「我今天还能传几个」时调用。"
    )
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}, "required": []}
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        event = _event_from_context(context)
        if self.plugin is None or event is None:
            return NO_CONTEXT
        return await self.plugin.tool_quota(event=event)


@dataclass
class ImgBedDeleteTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = "imgbed_delete"
    description: str = (
        "从图床删除已上传的文件，接受完整链接或 fileId。这是不可恢复操作，"
        "只有用户明确说要删除某个具体链接时才调用，绝不要为了「清理」「腾空间」主动调用。"
        "默认只允许管理员使用。"
    )
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要删除的图床链接或 fileId 列表，至少一个。",
                }
            },
            "required": ["targets"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        event = _event_from_context(context)
        if self.plugin is None or event is None:
            return NO_CONTEXT
        return await self.plugin.tool_delete(event=event, targets=_str_list(kwargs.get("targets")))


#: 注册顺序即模型看到的顺序，把最常用的放前面。
TOOL_CLASSES = (
    ImgBedUploadTool,
    ImgBedListMaterialsTool,
    ImgBedRecentTool,
    ImgBedQuotaTool,
    ImgBedDeleteTool,
)
