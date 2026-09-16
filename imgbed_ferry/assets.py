"""跨插件资源上传协议的兼容导出。"""

from .integration import (
    ASSET_API_VERSION,
    ASSET_PROTOCOL,
    DEFAULT_ASSET_SOURCE,
    AssetHandle,
    AssetValidationError,
    parse_asset_handle,
    read_asset_bytes,
    validate_asset_handle,
)

__all__ = [
    "ASSET_API_VERSION",
    "ASSET_PROTOCOL",
    "DEFAULT_ASSET_SOURCE",
    "AssetHandle",
    "AssetValidationError",
    "parse_asset_handle",
    "read_asset_bytes",
    "validate_asset_handle",
]
