"""配置解析：把 `_conf_schema.json` 的嵌套配置转换成带默认值的数据类。

AstrBot 传进来的 `AstrBotConfig` 是个 dict 子类，缺键、类型错乱、老版本残留都可能出现，
所以这里所有取值都走宽容的转换函数，保证插件不会因为一条配置写错而整体加载失败。
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

AUTH_MODE_TOKEN = "token"
AUTH_MODE_AUTH_CODE = "auth_code"
AUTH_MODE_NONE = "none"

UPLOAD_CHANNELS = (
    "s3",
    "cfr2",
    "telegram",
    "discord",
    "huggingface",
    "webdav",
    "external",
)

NAME_TYPES = ("default", "index", "origin", "short")

OUTPUT_FORMATS = ("plain", "markdown", "html", "bbcode")

# 与服务端 functions/upload/uploadTools.js 的 isExtValid() 保持一致。
# 本地先行校验可以避免把注定被拒的文件推上去，白白消耗图床配额与流量。
SERVER_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {
        # 图片
        "jpeg",
        "jpg",
        "png",
        "gif",
        "webp",
        "bmp",
        "ico",
        "svg",
        "eps",
        "psd",
        "ai",
        # 音视频
        "mp4",
        "webm",
        "mkv",
        "mov",
        "avi",
        "flv",
        "mp3",
        "wav",
        "flac",
        "aac",
        "ogg",
        "opus",
        "m4a",
        # 文档
        "doc",
        "docx",
        "ppt",
        "pptx",
        "xls",
        "xlsx",
        "pdf",
        "txt",
        "md",
        "csv",
        "rtf",
        "epub",
        # 数据与代码
        "json",
        "xml",
        "yaml",
        "yml",
        "toml",
        "ini",
        "html",
        "htm",
        "css",
        "js",
        "ts",
        "tsx",
        "jsx",
        "go",
        "java",
        "kt",
        "php",
        "py",
        "rb",
        "rs",
        "c",
        "h",
        "cpp",
        "hpp",
        "cs",
        "sh",
        "bat",
        "cmd",
        "ps1",
        "sql",
        "lua",
        # 压缩包与安装包
        "zip",
        "rar",
        "7z",
        "tar",
        "gz",
        "tgz",
        "bz2",
        "xz",
        "zst",
        "apk",
        "exe",
        "msi",
        "dmg",
        "iso",
        "torrent",
        # 字体
        "ttf",
        "otf",
        "woff",
        "woff2",
    }
)

_TRUE_WORDS = {"1", "true", "yes", "y", "on", "开", "是"}
_FALSE_WORDS = {"0", "false", "no", "n", "off", "关", "否"}


def as_dict(value: Any) -> dict[str, Any]:
    """尽最大努力把任意配置节点转成普通 dict。"""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, Mapping):
        return dict(value.items())
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def section(config: Mapping[str, Any], key: str) -> dict[str, Any]:
    """读取一个配置分组，缺失时返回空 dict。"""
    return as_dict(config.get(key, {}) if hasattr(config, "get") else {})


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
    return default


def as_int(
    value: Any, default: int = 0, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def as_float(
    value: Any,
    default: float = 0.0,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        result = default
    if result != result:  # NaN
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def as_str_list(value: Any) -> list[str]:
    """支持 list、逗号/换行分隔字符串两种写法。"""
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace("\r", "\n").replace(",", "\n").replace("，", "\n")
        return [item.strip() for item in raw.split("\n") if item.strip()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [as_str(item) for item in value if as_str(item)]
    return []


def as_choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = as_str(value).lower()
    return text if text in allowed else default


def normalize_extension(value: str) -> str:
    """把 `.PNG`、`PNG`、`*.png` 统一成 `png`。"""
    text = as_str(value).lower().lstrip("*").lstrip(".")
    return text


@dataclass(slots=True)
class EndpointConfig:
    """图床实例与鉴权。"""

    base_url: str = ""
    auth_mode: str = AUTH_MODE_TOKEN
    api_token: str = ""
    auth_code: str = ""
    upload_channel: str = "s3"
    channel_name: str = ""
    name_type: str = "default"
    server_compress: bool = True
    auto_retry: bool = True
    public_url_prefix: str = ""
    prefer_public_url: bool = True
    timeout_seconds: float = 180.0
    verify_ssl: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> EndpointConfig:
        return cls(
            base_url=as_str(raw.get("base_url")),
            auth_mode=as_choice(
                raw.get("auth_mode"),
                (AUTH_MODE_TOKEN, AUTH_MODE_AUTH_CODE, AUTH_MODE_NONE),
                AUTH_MODE_TOKEN,
            ),
            api_token=as_str(raw.get("api_token")),
            auth_code=as_str(raw.get("auth_code")),
            upload_channel=as_choice(raw.get("upload_channel"), UPLOAD_CHANNELS, "s3"),
            channel_name=as_str(raw.get("channel_name")),
            name_type=as_choice(raw.get("name_type"), NAME_TYPES, "default"),
            server_compress=as_bool(raw.get("server_compress"), True),
            auto_retry=as_bool(raw.get("auto_retry"), True),
            public_url_prefix=as_str(raw.get("public_url_prefix")),
            prefer_public_url=as_bool(raw.get("prefer_public_url"), True),
            timeout_seconds=as_float(
                raw.get("timeout_seconds"), 180.0, minimum=10.0, maximum=900.0
            ),
            verify_ssl=as_bool(raw.get("verify_ssl"), True),
        )

    @property
    def normalized_base_url(self) -> str:
        base = as_str(self.base_url).rstrip("/")
        if base and "://" not in base:
            base = f"https://{base}"
        return base

    @property
    def configured(self) -> bool:
        return bool(self.normalized_base_url)

    @property
    def effective_auth_mode(self) -> str:
        """把「配了模式但没填凭据」矫正成实际可用的模式。

        显式选了 `none` 就一定不带凭据，避免配置里残留的旧 token 被悄悄发出去。
        """
        if self.auth_mode == AUTH_MODE_NONE:
            return AUTH_MODE_NONE
        if self.auth_mode == AUTH_MODE_TOKEN and self.api_token:
            return AUTH_MODE_TOKEN
        if self.auth_mode == AUTH_MODE_AUTH_CODE and self.auth_code:
            return AUTH_MODE_AUTH_CODE
        if self.api_token:
            return AUTH_MODE_TOKEN
        if self.auth_code:
            return AUTH_MODE_AUTH_CODE
        return AUTH_MODE_NONE


@dataclass(slots=True)
class UploadConfig:
    """上传行为与安全上限。"""

    folder_template: str = "astrbot/{date}"
    max_file_mb: float = 20.0
    max_total_mb: float = 200.0
    max_files: int = 30
    concurrency: int = 3
    enforce_extension_whitelist: bool = True
    extra_allowed_extensions: list[str] = field(default_factory=list)
    blocked_extensions: list[str] = field(default_factory=list)
    retry_times: int = 2
    retry_backoff_seconds: float = 1.5
    allow_url_ingest: bool = False
    url_ingest_allowlist: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> UploadConfig:
        return cls(
            folder_template=as_str(raw.get("folder_template"), "astrbot/{date}"),
            max_file_mb=as_float(raw.get("max_file_mb"), 20.0, minimum=0.1, maximum=2048.0),
            max_total_mb=as_float(raw.get("max_total_mb"), 200.0, minimum=0.1, maximum=8192.0),
            max_files=as_int(raw.get("max_files"), 30, minimum=1, maximum=500),
            concurrency=as_int(raw.get("concurrency"), 3, minimum=1, maximum=16),
            enforce_extension_whitelist=as_bool(raw.get("enforce_extension_whitelist"), True),
            extra_allowed_extensions=[
                normalize_extension(item)
                for item in as_str_list(raw.get("extra_allowed_extensions"))
                if normalize_extension(item)
            ],
            blocked_extensions=[
                normalize_extension(item)
                for item in as_str_list(raw.get("blocked_extensions"))
                if normalize_extension(item)
            ],
            retry_times=as_int(raw.get("retry_times"), 2, minimum=0, maximum=6),
            retry_backoff_seconds=as_float(
                raw.get("retry_backoff_seconds"), 1.5, minimum=0.0, maximum=30.0
            ),
            allow_url_ingest=as_bool(raw.get("allow_url_ingest"), False),
            url_ingest_allowlist=[
                item.strip().lower().lstrip(".")
                for item in as_str_list(raw.get("url_ingest_allowlist"))
                if item.strip()
            ],
        )

    @property
    def max_file_bytes(self) -> int:
        return int(self.max_file_mb * 1024 * 1024)

    @property
    def max_total_bytes(self) -> int:
        return int(self.max_total_mb * 1024 * 1024)

    def allowed_extensions(self) -> frozenset[str]:
        return SERVER_ALLOWED_EXTENSIONS | frozenset(self.extra_allowed_extensions)

    def extension_rejection(self, extension: str) -> str:
        """返回拒绝原因；空串表示放行。"""
        ext = normalize_extension(extension)
        if ext and ext in self.blocked_extensions:
            return f"扩展名 .{ext} 在黑名单中"
        if not self.enforce_extension_whitelist:
            return ""
        if not ext:
            return "无法判定扩展名"
        if ext not in self.allowed_extensions():
            return f"扩展名 .{ext} 不在图床白名单中"
        return ""

    def url_ingest_rejection(self, url: str) -> str:
        """判断一条外链能不能转存。返回拒绝原因，空串表示放行。

        这条路径是「用户给 URL，Bot 去下载」，天然带 SSRF 风险：内网地址、云元数据
        服务都可能被当成目标。所以默认关闭，开启后仍然强制拦掉回环、私有网段、
        链路本地地址，并且支持用域名后缀白名单进一步收紧。
        """
        if not self.allow_url_ingest:
            return "外链转存已关闭（upload.allow_url_ingest）"
        parts = urlsplit(str(url or "").strip())
        if parts.scheme not in ("http", "https"):
            return "只支持 http / https 外链"
        host = (parts.hostname or "").strip().lower()
        if not host:
            return "链接里没有主机名"
        if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
            return "拒绝访问本机地址"
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return "拒绝访问内网或保留地址"
        allowlist = self.url_ingest_allowlist
        if allowlist and not any(host == item or host.endswith(f".{item}") for item in allowlist):
            return f"主机 {host} 不在外链白名单内"
        return ""


@dataclass(slots=True)
class CompressConfig:
    """上传前的本地图片压缩。"""

    enabled: bool = True
    trigger_mb: float = 4.0
    max_edge: int = 4096
    quality: int = 86
    target_format: str = "webp"
    keep_transparency: bool = True
    skip_gif: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> CompressConfig:
        return cls(
            enabled=as_bool(raw.get("enabled"), True),
            trigger_mb=as_float(raw.get("trigger_mb"), 4.0, minimum=0.05, maximum=512.0),
            max_edge=as_int(raw.get("max_edge"), 4096, minimum=256, maximum=16384),
            quality=as_int(raw.get("quality"), 86, minimum=40, maximum=100),
            target_format=as_choice(raw.get("target_format"), ("webp", "jpeg", "keep"), "webp"),
            keep_transparency=as_bool(raw.get("keep_transparency"), True),
            skip_gif=as_bool(raw.get("skip_gif"), True),
        )

    @property
    def trigger_bytes(self) -> int:
        return int(self.trigger_mb * 1024 * 1024)


@dataclass(slots=True)
class ArchiveConfig:
    """压缩包解压上传，含解压炸弹与目录穿越防护。"""

    enabled: bool = True
    default_extract: bool = True
    max_entries: int = 200
    max_total_mb: float = 500.0
    max_ratio: float = 120.0
    password: str = ""
    flatten_names: bool = False
    upload_archive_when_disabled: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ArchiveConfig:
        return cls(
            enabled=as_bool(raw.get("enabled"), True),
            default_extract=as_bool(raw.get("default_extract"), True),
            max_entries=as_int(raw.get("max_entries"), 200, minimum=1, maximum=2000),
            max_total_mb=as_float(raw.get("max_total_mb"), 500.0, minimum=1.0, maximum=8192.0),
            max_ratio=as_float(raw.get("max_ratio"), 120.0, minimum=2.0, maximum=10000.0),
            password=as_str(raw.get("password")),
            flatten_names=as_bool(raw.get("flatten_names"), False),
            upload_archive_when_disabled=as_bool(raw.get("upload_archive_when_disabled"), True),
        )

    @property
    def max_total_bytes(self) -> int:
        return int(self.max_total_mb * 1024 * 1024)


@dataclass(slots=True)
class BehaviorConfig:
    """回显与会话记忆。"""

    output_format: str = "markdown"
    forward_threshold: int = 5
    show_size: bool = True
    dedupe_enabled: bool = True
    dedupe_ttl_days: int = 30
    dedupe_max_entries: int = 500
    record_limit: int = 50
    session_material_limit: int = 40
    session_ttl_minutes: int = 180
    reply_with_image_preview: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> BehaviorConfig:
        return cls(
            output_format=as_choice(raw.get("output_format"), OUTPUT_FORMATS, "markdown"),
            forward_threshold=as_int(raw.get("forward_threshold"), 5, minimum=0, maximum=200),
            show_size=as_bool(raw.get("show_size"), True),
            dedupe_enabled=as_bool(raw.get("dedupe_enabled"), True),
            dedupe_ttl_days=as_int(raw.get("dedupe_ttl_days"), 30, minimum=0, maximum=3650),
            dedupe_max_entries=as_int(
                raw.get("dedupe_max_entries"), 500, minimum=16, maximum=20000
            ),
            record_limit=as_int(raw.get("record_limit"), 50, minimum=0, maximum=1000),
            session_material_limit=as_int(
                raw.get("session_material_limit"), 40, minimum=1, maximum=400
            ),
            session_ttl_minutes=as_int(
                raw.get("session_ttl_minutes"), 180, minimum=1, maximum=10080
            ),
            reply_with_image_preview=as_bool(raw.get("reply_with_image_preview"), False),
        )


@dataclass(slots=True)
class PermissionConfig:
    """谁能用、能用多少。"""

    llm_tools_enabled: bool = True
    upload_tool_enabled: bool = True
    list_tool_enabled: bool = True
    delete_tool_enabled: bool = False
    quota_tool_enabled: bool = True
    records_tool_enabled: bool = True
    group_whitelist: list[str] = field(default_factory=list)
    group_blacklist: list[str] = field(default_factory=list)
    user_blacklist: list[str] = field(default_factory=list)
    admin_only: bool = False
    delete_requires_admin: bool = True
    daily_quota_per_user: int = 0
    daily_quota_per_group: int = 0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> PermissionConfig:
        return cls(
            llm_tools_enabled=as_bool(raw.get("llm_tools_enabled"), True),
            upload_tool_enabled=as_bool(raw.get("upload_tool_enabled"), True),
            list_tool_enabled=as_bool(raw.get("list_tool_enabled"), True),
            delete_tool_enabled=as_bool(raw.get("delete_tool_enabled"), False),
            quota_tool_enabled=as_bool(raw.get("quota_tool_enabled"), True),
            records_tool_enabled=as_bool(raw.get("records_tool_enabled"), True),
            group_whitelist=as_str_list(raw.get("group_whitelist")),
            group_blacklist=as_str_list(raw.get("group_blacklist")),
            user_blacklist=as_str_list(raw.get("user_blacklist")),
            admin_only=as_bool(raw.get("admin_only"), False),
            delete_requires_admin=as_bool(raw.get("delete_requires_admin"), True),
            daily_quota_per_user=as_int(
                raw.get("daily_quota_per_user"), 0, minimum=0, maximum=100000
            ),
            daily_quota_per_group=as_int(
                raw.get("daily_quota_per_group"), 0, minimum=0, maximum=100000
            ),
        )


@dataclass(slots=True)
class IntegrationConfig:
    """跨插件资源句柄接口的安全边界。

    这个分组只影响公开的 ``upload_asset`` 接口，不会改变消息附件和
    ``imgbed_upload`` 工具的行为。默认只信任 meme_magpie 签发的短期图片句柄，
    避免把它误用成一个「给任意本地路径上传」的通用入口。
    """

    enabled: bool = True
    allowed_asset_sources: list[str] = field(default_factory=lambda: ["astrbot_plugin_meme_magpie"])
    max_handle_ttl_seconds: int = 1800
    require_declared_hash: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> IntegrationConfig:
        sources = [
            item.strip() for item in as_str_list(raw.get("allowed_asset_sources")) if item.strip()
        ]
        # 空列表通常是 Dashboard 用户清空了输入；这里回落到安全默认值，
        # 不让「误清空」变成接受任意来源的隐式开关。
        if not sources:
            sources = ["astrbot_plugin_meme_magpie"]
        return cls(
            enabled=as_bool(raw.get("enabled"), True),
            allowed_asset_sources=sources,
            max_handle_ttl_seconds=as_int(
                raw.get("max_handle_ttl_seconds"), 1800, minimum=30, maximum=86400
            ),
            require_declared_hash=as_bool(raw.get("require_declared_hash"), True),
        )


@dataclass(slots=True)
class FerryConfig:
    """插件的全部配置。"""

    endpoint: EndpointConfig = field(default_factory=EndpointConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)
    compress: CompressConfig = field(default_factory=CompressConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    permission: PermissionConfig = field(default_factory=PermissionConfig)
    integration: IntegrationConfig = field(default_factory=IntegrationConfig)
    debug: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> FerryConfig:
        data = as_dict(raw or {})
        return cls(
            endpoint=EndpointConfig.from_mapping(section(data, "endpoint")),
            upload=UploadConfig.from_mapping(section(data, "upload")),
            compress=CompressConfig.from_mapping(section(data, "compress")),
            archive=ArchiveConfig.from_mapping(section(data, "archive")),
            behavior=BehaviorConfig.from_mapping(section(data, "behavior")),
            permission=PermissionConfig.from_mapping(section(data, "permission")),
            integration=IntegrationConfig.from_mapping(section(data, "integration")),
            debug=as_bool(data.get("debug"), False),
        )
