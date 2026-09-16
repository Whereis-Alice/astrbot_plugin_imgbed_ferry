# 跨插件资源上传

`astrbot_plugin_imgbed_ferry` 为其它 AstrBot 插件提供一个受控的异步入口：

```python
await ferry.upload_asset(event, asset, folder="", compress=None, output_format="")
```

它负责读取资源、校验、压缩、去重、上传并返回 URL；资源的选择和源文件生命周期由提供方负责。

## 调用流程

以 `astrbot_plugin_meme_magpie` 和 AstrBook 为例：

```python
# 在 AstrBook（或其它 Star 插件）的方法里使用当前插件的 self.context。
meme_meta = self.context.get_registered_star("astrbot_plugin_meme_magpie")
meme = (
    (getattr(meme_meta, "star_cls", None) or getattr(meme_meta, "star", None))
    if meme_meta
    else None
)

ferry_meta = self.context.get_registered_star("astrbot_plugin_imgbed_ferry")
ferry = (
    (getattr(ferry_meta, "star_cls", None) or getattr(ferry_meta, "star", None))
    if ferry_meta
    else None
)

if meme is None or ferry is None:
    raise RuntimeError("缺少 meme_magpie 或 imgbed_ferry")

asset = await meme.export_meme_asset("emoji_1", event)
try:
    result = await ferry.upload_asset(
        event,
        asset,
        folder="astrbook/memes",
        compress=True,
        output_format="markdown",
    )
finally:
    # upload_asset 已经读取完毕；释放 meme_magpie 的短期租约。
    release = getattr(asset, "release", None)
    if callable(release):
        release()

if not result["success"]:
    # 不要把失败结果当成 URL 发帖。
    raise RuntimeError(result["error"])
markdown = result["markdown"]
```

`event` 应是当前消息事件。meme_magpie 的候选选择、权限检查和 `emoji_id` 解析仍由
meme_magpie 完成，imgbed_ferry 不会根据标签自行挑选表情。

## 句柄契约

默认信任来源为 `astrbot_plugin_meme_magpie`。句柄可以是实际对象，也可以是兼容的 mapping，
但必须包含以下语义字段：

| 字段 | 说明 |
| --- | --- |
| `provider` 或 `source` | 来源插件标识 |
| `api_version` 或 `protocol` | 当前协议为 `1` / `astrbot-meme-asset/v1` |
| `filename` | 图片文件名，支持 PNG/JPEG/GIF/WebP/BMP |
| `size` | 原始字节数 |
| `sha256` | 原始内容 SHA-256，默认必填 |
| `created_at` / `expires_at` | 短期有效期 |
| `read_bytes()` 或受控内容 | 异步读取二进制内容 |

真实的 `MemeAssetHandle` 不暴露本地路径，应直接传对象。不要把 `C:\\...`、临时文件路径或
未经签名的 base64 字符串交给这个接口。

## 返回值

成功：

```json
{
  "success": true,
  "url": "https://img.example.com/2026/a.png",
  "file_id": "...",
  "sha256": "...",
  "reused": false,
  "markdown": "![a.png](https://img.example.com/2026/a.png)",
  "formatted": "![a.png](https://img.example.com/2026/a.png)",
  "name": "a.png",
  "size": 12345,
  "original_size": 23456,
  "asset_id": "emoji_1",
  "source": "astrbot_plugin_meme_magpie"
}
```

`reused=true` 表示相同最终内容、图床配置、目录、命名方式和压缩策略已经有可用链接，
本次没有再次发起上传。源文件不会因为命中缓存而被删除。

配额只针对真正发起的新上传计数；即使用户当天额度已用尽，只要命中同一去重记录，
`upload_asset()` 仍会返回可复用的 URL，不会重复占用额度。

失败：

```json
{"success": false, "code": "asset_expired", "error": "资源句柄已过期，请重新导出"}
```

常见 `code` 包括 `integration_disabled`、`invalid_asset`、`asset_expired`、`file_missing`、
`hash_mismatch`、`invalid_signature`、`too_large`、`forbidden`、`quota_exceeded` 和
`upload_failed`。消费者应先检查 `success`，再使用 `url` / `markdown`。

## 安全边界

- 句柄来源、协议、TTL、大小、扩展名、魔数、MIME 和 SHA-256 会在读取时再次校验。
- 默认拒绝裸路径、符号链接、非普通文件和过期句柄。
- `integration.allowed_asset_sources` 可扩展来源白名单；不要为了省事设成任意来源。
- `integration.max_handle_ttl_seconds` 默认 1800 秒，`require_declared_hash` 默认开启。
- 发帖失败时图床文件默认保留，后续相同资源可以复用 URL；如需清理应由明确的管理员流程完成。
