<div align="center">

<img src="assets/logo.png" alt="图床摆渡" width="160" />

# 图床摆渡 · astrbot_plugin_imgbed_ferry

一句话把图片、文件和压缩包摆渡进 **CloudFlare-ImgBed**，自动去重、压缩、解压，回你一串直链。

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16-blue)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-AGPL--3.0-red)](LICENSE)

</div>

---

## 这是什么

给 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 装上这个插件后，你不需要记指令：

> 「把这张图传图床」
> 「引用那个压缩包，解压后全传上去」
> 「刚才那几个文件都传了，给我链接」

Bot 会自己调 LLM 工具，把消息里带的、被引用的、乃至会话里缓存过的素材送上你自建的
[CloudFlare-ImgBed](https://github.com/MarSeventh/CloudFlare-ImgBed)，然后把直链回给你。
不想说话就用 `/图床上传`，参数比自然语言更精确。

**核心能力**

- **自然语言驱动**：5 个 LLM 工具（上传 / 列举素材 / 查最近记录 / 查配额 / 删除），带防幻觉提示词，模型不会凭空编链接
- **批量上传**：一条消息里的多图多文件、引用消息里的附件、会话缓存的历史素材，一次全走
- **压缩包解压上传**：zip / 7z / rar / tar 系列解压后逐个上传，`不解压` 则整包直传
- **sha256 去重**：跨批次 KV 缓存 + 批内复用，重复文件直接返回旧链接，不吃图床空间
- **图片预压缩**：超阈值先转 WebP / 缩边再上传，单次可覆盖
- **OneBot 深度适配**：LLOneBot / NapCat / SnowLuma 的文件直链拿得到，并绕过了官方适配器丢文件段的老问题

---

## 安装

**方式一：Dashboard**（推荐）

AstrBot 面板 → 插件市场 → 搜索「图床摆渡」→ 安装。

**方式二：手动**

```bash
cd AstrBot/data/plugins
git clone https://github.com/Whereis-Alice/astrbot_plugin_imgbed_ferry.git
pip install -r astrbot_plugin_imgbed_ferry/requirements.txt
```

重启 AstrBot，然后在 Dashboard 的插件管理里打开「图床摆渡」填配置。

依赖只有两个：`aiohttp>=3.8.0`（AstrBot 自带）、`Pillow>=10.0.0`（图片压缩用，装不上就把
`compress.enabled` 关掉）。想解 rar / 7z 再按需装 `rarfile`（还需系统 `unrar`）和 `py7zr`。

---

## 第一步：图床侧要准备什么

以 [cloudflare-imgbed-hf-buckets](https://github.com/Whereis-Alice/cloudflare-imgbed-hf-buckets)
这套「CloudFlare-ImgBed + HuggingFace Storage Buckets」的部署为例：

### 1. 建一个 API Token

图床后台 → 系统设置 → API Token，新建一个，勾上 **upload**（要用删除功能再勾 **delete**、
要用 `/图床文件` 再勾 **list**）。复制出来填进插件的 `endpoint.api_token`。

> `endpoint.auth_mode` 保持 `token` 即可。如果你的图床只开了 authCode，就把 `auth_mode` 改成
> `auth_code` 并填上 `endpoint.auth_code`；两个都不需要（纯公开实例）就显式设成 `none`，插件会确保不发送任何残留凭据。

### 2. 对齐上传渠道

HuggingFace Storage Buckets 走的是 **S3 兼容协议**，所以：

| 配置项 | 该填什么 |
| --- | --- |
| `endpoint.upload_channel` | `s3` |
| `endpoint.channel_name` | 图床后台 S3 渠道列表里那个渠道的名字，本方案默认是 `HF-Buckets` |

渠道名填错图床会直接报错，别猜，去后台复制。

### 3. 填地址

`endpoint.base_url` 填图床根地址，例如 `https://img.example.com`，**不要**带 `/upload`。
用了自定义 CDN 域名的话，再填 `endpoint.public_url_prefix`，插件会把返回的链接重写成你的域名。

### 4. 自检

配完在群里发 `/图床自检`（管理员）。它会真的传一个 1×1 的小 PNG 到 `astrbot/probe`，
成功后自动删掉，把耗时和链接回给你。这条命令过了，其余功能就都能用。
---

## 怎么用

### 自然语言（推荐）

带着图片、文件说话就行，Bot 自己决定调哪个工具：

| 你说 | 会发生什么 |
| --- | --- |
| 「把这张图传图床」 | 上传本条消息里的图，回直链 |
| 「引用那个压缩包，解压后全传上去」 | 取引用消息里的压缩包，解压后逐个上传 |
| 「刚才那几个文件都传了」 | 从会话素材缓存里取，批量上传 |
| 「传第 2 个和最后一个」 | 按序号精确挑（1 最早、-1 最新） |
| 「这压缩包别解压，我要整包链接」 | `extract_archive=false`，整包直传 |
| 「图太大了压一下再传」 | 强制走本地压缩 |
| 「放到 壁纸/2026 目录」 | 覆盖目录模板 |
| 「刚才传的链接是啥」 | 查最近上传记录 |
| 「图床还能传多少」 | 查配额与今日用量 |

素材缓存有有效期（默认 180 分钟）和条数上限（默认 40 条 / 会话），过期就得重新发一遍。

### 指令

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `/图床` | `imgbed` | 帮助面板，顺带显示当前图床状态与已注册工具 |
| `/图床上传 [参数…]` | `ibup` `imgbedup` | 手动上传，参数见下表 |
| `/图床列表` | `ibls` `iblist` | 当前会话缓存了哪些素材（带序号和 id） |
| `/图床记录 [条数]` | `iblog` | 最近上传成功的文件与链接 |
| `/图床配额` | `ibquota` | 图床状态与今日用量 |
| `/图床自检` | `ibcheck` | **管理员**，跑一次完整上传链路并自动清理 |
| `/图床文件 [目录]` | `ibfiles` | **管理员**，查看图床上已有的文件 |
| `/图床删除 <链接或 fileId…>` | `ibdel` | **管理员**，不可恢复，单次最多 50 个 |
| `/图床清理 素材\|记录\|全部` | `ibclear` | **管理员**，只清本地缓存，不动图床 |

`/图床上传` 的参数是自由顺序的，中英文都认：

| 参数 | 写法 | 说明 |
| --- | --- | --- |
| 取件目标 | `auto` `latest` `reply` `all` / `自动` `最新` `引用` `全部` | 默认 `auto`：本条消息 + 引用里的全部 |
| 序号 | `1` `#3` `-1` | 1 最早、-1 最新，可多个 |
| 素材 id | 直接写 | 来自 `/图床列表` |
| 外链 | `https://…` | 需要先开 `upload.allow_url_ingest` |
| 目录 | `-f 壁纸/2026` `--folder=…` `--dir=…` `目录 …` | 支持模板变量；单独写一个含 `/` 的词也算目录 |
| 输出格式 | `-o plain` `--format=…` `--output=…` `格式 …` | `plain` `markdown` `html` `bbcode` |
| 命名方式 | `-n origin` `--name=…` `命名 …` | `default` `index` `origin` `short` |
| 解压开关 | `解压` / `不解压` `整包` `--extract` `--no-extract` | 覆盖 `archive.default_extract` |
| 压缩开关 | `压缩` / `不压缩` `原图` `原档` `--compress` `--no-compress` | 覆盖 `compress.enabled` |

例子：

```text
/图床上传                                  # 把这条消息和引用里的东西全传了
/图床上传 引用 -f 壁纸/{date} --no-extract   # 引用里的压缩包整包传进按日期分的目录
/图床上传 -1 -o plain                      # 只传最新那个，回纯链接
/图床上传 1 3 5 -n origin                  # 挑第 1/3/5 个，保留原文件名
```

### LLM 工具

| 工具名 | 干什么 | 默认 |
| --- | --- | --- |
| `imgbed_upload` | 上传素材 / 外链转存，回直链 | 开 |
| `imgbed_list_materials` | 列出会话里可上传的素材（供模型挑选） | 开 |
| `imgbed_recent_uploads` | 查最近上传成功的记录 | 开 |
| `imgbed_quota` | 查图床状态与今日配额用量 | 开 |
| `imgbed_delete` | 按链接 / fileId 删除 | **关**（危险操作，要用请自行打开） |

每个工具都能在 `permission` 分组里单独关掉，也可以用 `permission.llm_tools_enabled` 一键全关，
只留指令。工具的返回值里带了明确的转达要求（原样输出链接、不要编造），尽量压掉模型幻觉。

### 目录模板变量

`upload.folder_template` 和 `-f` 参数都支持：

| 变量 | 含义 |
| --- | --- |
| `{date}` | `2026-09-05` |
| `{datetime}` | `2026-09-05_231500` |
| `{yyyy}` `{mm}` `{dd}` | 年 / 月 / 日 |
| `{group}` | 群号，私聊回落成 `private` |
| `{sender}` | 发送者 id，取不到回落成 `unknown` |
| `{platform}` | 平台名，如 `aiocqhttp`，回落 `astrbot` |
| `{type}` | `image` 或 `file` |
| `{ext}` | 扩展名（不含点），未知回落 `bin` |

解压上传时，压缩包内的目录结构默认保留并接在模板后面（`flatten_names=true` 可压平）。
认不出来的变量名会原样留着（花括号被清掉），不会抛异常；模板里的 `..`、`//`、非法字符也都会被清洗，不用担心穿越。
---

## 配置项

全部配置在 Dashboard 的「图床摆渡」里可视化编辑，下面是完整清单。

### `endpoint` · 图床实例

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | `""` | 图床根地址，不要带 `/upload` |
| `auth_mode` | `token` | `token` / `auth_code` / `none` |
| `api_token` | `""` | API Token，走 `Authorization: Bearer` |
| `auth_code` | `""` | authCode 认证码 |
| `upload_channel` | `s3` | HuggingFace Buckets 方案填 `s3` |
| `channel_name` | `""` | 图床后台的渠道名，本方案是 `HF-Buckets` |
| `name_type` | `default` | 默认命名方式：`default` `index` `origin` `short` |
| `server_compress` | `true` | 允许图床服务端再压一次 |
| `auto_retry` | `true` | 允许图床自己切换渠道重试 |
| `public_url_prefix` | `""` | 自定义 CDN 域名，会重写返回链接 |
| `prefer_public_url` | `true` | 优先用响应里的 `publicUrl` |
| `timeout_seconds` | `180.0` | 单次请求超时 |
| `verify_ssl` | `true` | 校验 TLS 证书 |

### `upload` · 上传行为

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `folder_template` | `astrbot/{date}` | 目录模板 |
| `max_file_mb` | `20.0` | 单文件上限 |
| `max_total_mb` | `200.0` | 单次任务总量上限，超了截断并提示 |
| `max_files` | `30` | 单次任务文件数上限 |
| `concurrency` | `3` | 并发上传数 |
| `enforce_extension_whitelist` | `true` | 本地先按图床白名单挡一层，省一趟网络 |
| `extra_allowed_extensions` | `[]` | 额外放行的扩展名 |
| `blocked_extensions` | `[]` | 黑名单，优先级最高 |
| `retry_times` | `2` | 失败重试次数（只对网络 / 超时 / 5xx / 限流重试） |
| `retry_backoff_seconds` | `1.5` | 退避基数，第 n 次等 `n × 基数` 秒 |
| `allow_url_ingest` | `false` | 是否允许把外链下载后转存进图床 |
| `url_ingest_allowlist` | `[]` | 外链域名白名单，强烈建议配 |

### `compress` · 本地压缩

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 上传前本地压缩 |
| `trigger_mb` | `4.0` | 超过这个体积才压 |
| `max_edge` | `4096` | 最长边上限（像素） |
| `quality` | `86` | 重编码质量 |
| `target_format` | `webp` | 输出格式 |
| `keep_transparency` | `true` | 保留透明通道 |
| `skip_gif` | `true` | 跳过 GIF（避免动图变静图） |

### `archive` · 压缩包解压上传

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `default_extract` | `true` | 默认解压；`false` 则默认整包传 |
| `max_entries` | `200` | 单个压缩包最多取多少条目 |
| `max_total_mb` | `500.0` | 解压后总大小上限（同时也是压缩包本体的下载上限） |
| `max_ratio` | `120.0` | 最大膨胀比，防解压炸弹 |
| `password` | `""` | 统一解压密码 |
| `flatten_names` | `false` | 压平压缩包内目录结构 |
| `upload_archive_when_disabled` | `true` | 不解压时是否仍上传压缩包本体 |

### `behavior` · 回显与记忆

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `output_format` | `markdown` | 默认链接格式 |
| `forward_threshold` | `5` | 超过这么多条就折叠成合并转发（仅 aiocqhttp） |
| `show_size` | `true` | 结果里显示体积 |
| `dedupe_enabled` | `true` | sha256 去重 |
| `dedupe_ttl_days` | `30` | 去重缓存有效期 |
| `dedupe_max_entries` | `500` | 去重缓存条数上限 |
| `record_limit` | `50` | 保留最近多少条上传记录 |
| `session_material_limit` | `40` | 每会话缓存多少条素材 |
| `session_ttl_minutes` | `180` | 会话素材有效期 |
| `reply_with_image_preview` | `false` | 回执附带图片预览（最多 9 张） |

### `permission` · 权限与配额

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `llm_tools_enabled` | `true` | LLM 工具总开关 |
| `upload_tool_enabled` | `true` | `imgbed_upload` |
| `list_tool_enabled` | `true` | `imgbed_list_materials` |
| `records_tool_enabled` | `true` | `imgbed_recent_uploads` |
| `quota_tool_enabled` | `true` | `imgbed_quota` |
| `delete_tool_enabled` | `false` | `imgbed_delete`，危险 |
| `group_whitelist` | `[]` | 非空则只在这些群生效 |
| `group_blacklist` | `[]` | 黑名单群 |
| `user_blacklist` | `[]` | 黑名单用户 |
| `admin_only` | `false` | 只有管理员能用 |
| `delete_requires_admin` | `true` | 删除要求管理员 |
| `daily_quota_per_user` | `0` | 每人每日文件数上限，0 = 不限 |
| `daily_quota_per_group` | `0` | 每群每日文件数上限，0 = 不限 |

`debug`（顶层，默认 `false`）打开后会输出取件与上传的详细日志，排查「文件段拿不到」时很有用。

配额只统计**真正上传**的文件，命中去重复用旧链接的不计数；管理员不受每日配额限制。
---

## 顺手做的那些事

除了「上传 + 批量 + 解压」这三件正事，实际用起来会硌脚的地方也都处理了：

**省空间 / 省流量**

- **sha256 去重**：上传前算摘要，跨批次查 KV 缓存（默认 30 天 / 500 条），命中直接回旧链接；
  同一批次里内容相同的文件只传一次，其余复用结果并在回执里标注。
- **图片预压缩**：超过阈值的图片先缩边、转 WebP 再上传，回执里给出「压缩前 → 压缩后」体积。
  GIF 默认跳过，透明通道默认保留。

**别把图床和 Bot 打挂**

- 并发上传（默认 3），失败按错误类型分类重试：只对网络 / 超时 / 5xx / 429 重试，
  401 / 403 / 白名单不过这类「重试也没用」的错误立刻放弃。
- 三重上限：单文件、单次总量、文件数。总量超了就截断并明确告诉你截在哪。
- 扩展名白名单本地先挡一层（对齐图床服务端的允许列表），配合魔数嗅探补全无扩展名的文件。

**压缩包是不可信输入**

- zip slip（`../` 穿越）、绝对路径、盘符全部清洗成安全文件名
- 解压炸弹：条目数、解压后总量、膨胀比三道闸
- 乱码文件名修复：按 utf-8 → gbk → shift_jis 顺序试解，救回一堆日文 / 中文压缩包
- 加密包能识别出来并给出「需要密码」的明确提示，而不是丢一个 traceback
- 解不开就按配置回落成整包上传，并在回执里说明原因

**回执不刷屏**

- 四种链接格式：`plain` / `markdown` / `html` / `bbcode`
- 条数超过阈值自动折叠成合并转发（**仅 aiocqhttp**，其余平台老老实实发长文本）
- 失败项带分类 hint：401 会提示「检查 API Token 权限或 authCode」，5xx 会提示「检查上传渠道配置
  与后端可用性」，413 会提示「换小文件或先本地压缩」，而不是甩一串状态码
- 可选附带图片预览（最多 9 张）

**记得住**

- 会话素材缓存：图片和文件出现过就记下来，后面说「刚才那几个」还能捞到（带 TTL 和条数上限）
- 最近上传记录：`/图床记录` 随时回看链接
- 纯 base64 内联图**不进**缓存 —— 那玩意存下来只会白占内存

---

## OneBot 适配（LLOneBot / NapCat / SnowLuma）

QQ 侧拿文件直链是这个插件最麻烦的一块，做了这些事：

1. **绕过适配器丢文件段的问题**。AstrBot 的 `aiocqhttp` 适配器在处理 `file` 段时用了
   `event.message[0]["data"]["file_id"]` 这样的硬编码索引，一条消息里图文混排、或者文件不在
   第 0 段时就取不到；而且它只试 `get_group_file_url` / `get_private_file_url`，失败就干脆
   不把 File 组件放进消息链——上层看到的是「没有文件」。引用消息里同样踩这个坑。
   本插件直接读原始 OneBot 段做兜底：文件段按 `get_group_file_url`（带 / 不带 busid）→
   `get_private_file_url` → `get_file` 依次尝试，图片段按 `get_image` → `get_file` 尝试，
   群号 / 用户 id 从消息段和事件上双向补齐，任一路拿到 url / 本地路径 / base64 就算成功。
2. **修 `file://` URI 少一个斜杠的问题**。部分实现返回 `file:///data/xxx`，
   朴素地切掉 `file://` 会得到 `/data/xxx` 之外的畸形路径（Linux 下必踩）。
   插件只吃 `file://` 这七个字符，只有识别出「`/` + 盘符 `:`」这种 Windows 形状才多吃一个，
   同时对含 `%` 的路径追加一份 `unquote` 候选。
3. **本地路径直读**。LLOneBot / SnowLuma 与 Bot 同机时，能拿到本地路径就直接读文件，
   完全不走 HTTP，快且稳。
4. **SnowLuma 专项**：已核对其支持 `get_file`、`get_group_file_url`、`get_private_file_url`、
   `get_image`、`get_rkey`、`download_file`、`get_group_root_files` 等接口，取件链路按这些能力排的序。

拿不到文件时打开顶层 `debug` 再试一次，日志里会列出每一次 action 调用和它的返回。

---

## 限制与注意

- **图床配额查询**：`/api/manage/quota` 需要 manage 权限，只配 API Token 的话必然 401。
  这时 `/图床配额` 只显示插件侧的今日用量和本地记录，属于预期行为。
- **分块上传未接入**：超大文件请走图床网页端。插件这边靠 `upload.max_file_mb` 挡住。
- **`archive.max_total_mb` 是双重身份**：它既是解压后总量上限，也是压缩包本体的下载上限。
- **`endpoint.verify_ssl` 影响面比想象的大**：关掉它，图床请求和素材直链下载都会跳过证书校验。
  只在自签证书的内网实例上关。
- **外链转存有残余风险**：`upload.allow_url_ingest` 打开后，虽然已经拦掉回环 / 私有 / 链路本地 /
  保留 / 多播地址和 `localhost`，但域名解析发生在拦截之后，理论上仍存在 DNS rebinding 的余地。
  **强烈建议同时配 `url_ingest_allowlist`**，把可转存的域名钉死。
- **`imgbed_delete` 默认关闭**，且删除不可恢复。真要开就把 `delete_requires_admin` 留着。
- **rar 需要 `rarfile` + 系统 `unrar`，7z 需要 `py7zr`**，没装的话会给出「缺少解压后端」的提示
  而不是静默失败。
- **HuggingFace Storage Buckets 侧的限制**（图床配置时注意）：必须开 path-style；
  对象 key 不能以 `/` 开头或结尾、不能含 `//` `../` `\`；不支持 `x-amz-meta-*` / ACL / tagging /
  versioning / lifecycle；未完成的分块上传 7 天过期；免费私有 bucket 100 GB。
- **合并转发只在 aiocqhttp 上启用**，其它平台会退回长文本，这是刻意的。

---

## 开发

```bash
# 代码风格（配置见 ruff.toml）
ruff format .
ruff check .

# 测试
python -m pytest -q
```

测试全部是纯单元测试，不碰真实网络：图床用 fake transport，OneBot 用 fake `call_action`，
压缩包用内存里现造的 zip。

---

## 许可证

[AGPL-3.0](LICENSE) · 图床本体请见 [MarSeventh/CloudFlare-ImgBed](https://github.com/MarSeventh/CloudFlare-ImgBed)