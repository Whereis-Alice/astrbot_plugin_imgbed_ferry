"""`imgbed_ferry.service` 上传流水线测试。

全程注入假 client / 假 fetcher，不碰网络也不碰图床。覆盖顺序大致等于流水线本身：
取件通道优先级 → 归一化 → 压缩包展开 → 三重上限 → 批内与跨批次去重 → 重试 → 链接改写。
"""

from __future__ import annotations

import base64
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from imgbed_ferry import service
from imgbed_ferry.client import ImgBedError, UploadOutcome
from imgbed_ferry.collector import KIND_FILE, KIND_IMAGE, Material
from imgbed_ferry.config import (
    ArchiveConfig,
    BehaviorConfig,
    CompressConfig,
    EndpointConfig,
    FerryConfig,
    UploadConfig,
)
from imgbed_ferry.media import ARCHIVE_HINT_EXTENSIONS
from imgbed_ferry.service import (
    FOLDER_VARIABLES,
    RETRYABLE_KINDS,
    ResolveError,
    UploadContext,
    UploadService,
    render_folder,
)
from imgbed_ferry.store import FerryStore


def make_config(
    *,
    endpoint: dict[str, Any] | None = None,
    upload: dict[str, Any] | None = None,
    compress: dict[str, Any] | None = None,
    archive: dict[str, Any] | None = None,
    behavior: dict[str, Any] | None = None,
) -> FerryConfig:
    """默认给一份「已配置好、串行上传、不压缩」的配置，测试再按需覆盖。"""
    endpoint_kwargs: dict[str, Any] = {
        "base_url": "https://img.example.com",
        "api_token": "tok-abc",
        "channel_name": "HF-Buckets",
    }
    endpoint_kwargs.update(endpoint or {})
    upload_kwargs: dict[str, Any] = {"concurrency": 1, "retry_times": 0}
    upload_kwargs.update(upload or {})
    compress_kwargs: dict[str, Any] = {"enabled": False}
    compress_kwargs.update(compress or {})
    return FerryConfig(
        endpoint=EndpointConfig(**endpoint_kwargs),
        upload=UploadConfig(**upload_kwargs),
        compress=CompressConfig(**compress_kwargs),
        archive=ArchiveConfig(**(archive or {})),
        behavior=BehaviorConfig(**(behavior or {})),
    )


class FakeClient:
    """只实现 `UploadService` 会用到的 `upload()`。"""

    def __init__(self, results: list[Any] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[dict[str, Any]] = []

    async def upload(
        self,
        *,
        filename: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        folder: str = "",
        name_type: str = "",
    ) -> UploadOutcome:
        self.calls.append(
            {
                "filename": filename,
                "size": len(data),
                "content_type": content_type,
                "folder": folder,
                "name_type": name_type,
            }
        )
        item = self.results.pop(0) if self.results else None
        if isinstance(item, Exception):
            raise item
        if item is not None:
            return item
        src = f"/file/{folder}/{filename}" if folder else f"/file/{filename}"
        return UploadOutcome(
            src=src,
            public_url=f"https://cdn.example.com{src}",
            file_id=f"fid-{filename}",
        )

    @property
    def folders(self) -> list[str]:
        return [call["folder"] for call in self.calls]

    @property
    def names(self) -> list[str]:
        return [call["filename"] for call in self.calls]


class FakeFetcher:
    """按 URL 查表返回 `(bytes, 文件名, MIME)`，未预置的 URL 直接失败。"""

    def __init__(self, table: dict[str, Any] | None = None) -> None:
        self.table = dict(table or {})
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, url: str, limit: int) -> tuple[bytes, str, str]:
        self.calls.append((url, limit))
        item = self.table.get(url)
        if item is None:
            raise ResolveError(f"未预置的直链：{url}")
        if isinstance(item, Exception):
            raise item
        return item


def make_service(
    config: FerryConfig | None = None,
    *,
    client: FakeClient | None = None,
    fetcher: FakeFetcher | None = None,
    store: FerryStore | None = None,
) -> tuple[UploadService, FakeClient, FakeFetcher]:
    fake_client = client or FakeClient()
    fake_fetcher = fetcher or FakeFetcher()
    svc = UploadService(
        config or make_config(),
        fake_client,  # type: ignore[arg-type]
        store,
        fetcher=fake_fetcher,
    )
    return svc, fake_client, fake_fetcher


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def noise_png(width: int = 240, height: int = 240) -> bytes:
    """噪点图：平滑渐变的 PNG 压完反而更大，测压缩必须用不可压的内容。"""
    import random

    from PIL import Image

    rng = random.Random(20260905)
    image = Image.frombytes("RGB", (width, height), rng.randbytes(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def inline(name: str, data: bytes, *, kind: str = KIND_FILE, mime: str = "") -> Material:
    return Material(kind=kind, name=name, data=data, mime=mime)


class FilenameFromDispositionTest(unittest.TestCase):
    def test_plain_filename(self) -> None:
        self.assertEqual(
            service._filename_from_disposition('attachment; filename="报表.xlsx"'),
            "报表.xlsx",
        )

    def test_rfc5987_wins(self) -> None:
        header = "attachment; filename=fallback.bin; filename*=UTF-8''%E5%9C%BE%E5%9C%BE.png"
        self.assertEqual(service._filename_from_disposition(header), "圾圾.png")

    def test_percent_decoding_on_plain(self) -> None:
        self.assertEqual(
            service._filename_from_disposition("attachment; filename=a%20b.png"),
            "a b.png",
        )

    def test_directory_traversal_stripped(self) -> None:
        self.assertEqual(
            service._filename_from_disposition('attachment; filename="../../etc/passwd"'),
            "passwd",
        )

    def test_blank_and_missing(self) -> None:
        for header in ("", "   ", "inline", "attachment"):
            with self.subTest(header=header):
                self.assertEqual(service._filename_from_disposition(header), "")


class UploadContextTest(unittest.TestCase):
    def test_from_event(self) -> None:
        event = SimpleNamespace(
            unified_msg_origin="aiocqhttp:GroupMessage:12345",
            get_sender_id=lambda: 999,
            get_group_id=lambda: 12345,
            get_platform_name=lambda: "aiocqhttp",
            get_sender_name=lambda: "阿丽丝",
        )
        ctx = UploadContext.from_event(event)
        self.assertEqual(ctx.session_id, "aiocqhttp:GroupMessage:12345")
        self.assertEqual(ctx.user_id, "999")
        self.assertEqual(ctx.group_id, "12345")
        self.assertEqual(ctx.platform, "aiocqhttp")
        self.assertEqual(ctx.sender_name, "阿丽丝")

    def test_missing_methods(self) -> None:
        ctx = UploadContext.from_event(SimpleNamespace())
        self.assertEqual(ctx, UploadContext())

    def test_broken_methods_are_swallowed(self) -> None:
        def boom() -> str:
            raise RuntimeError("适配器没实现")

        event = SimpleNamespace(
            unified_msg_origin="s1",
            get_sender_id=boom,
            get_group_id=boom,
            get_platform_name="not-callable",
            get_sender_name=lambda: None,
        )
        ctx = UploadContext.from_event(event)
        self.assertEqual(ctx.session_id, "s1")
        self.assertEqual(
            (ctx.user_id, ctx.group_id, ctx.platform, ctx.sender_name), ("", "", "", "")
        )

    def test_broken_session_property(self) -> None:
        class Hostile:
            @property
            def unified_msg_origin(self) -> str:
                raise RuntimeError("没有会话")

        self.assertEqual(UploadContext.from_event(Hostile()).session_id, "")


class RenderFolderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = UploadContext(
            session_id="s1", user_id="999", group_id="12345", platform="aiocqhttp"
        )

    def test_date_variables(self) -> None:
        rendered = render_folder("astrbot/{yyyy}/{mm}/{dd}", ctx=UploadContext())
        head, year, month, day = rendered.split("/")
        self.assertEqual(head, "astrbot")
        self.assertEqual((len(year), len(month), len(day)), (4, 2, 2))
        self.assertTrue(rendered.replace("/", "").replace("astrbot", "").isdigit())

    def test_date_and_datetime_shapes(self) -> None:
        date_part = render_folder("{date}", ctx=UploadContext())
        stamp = render_folder("{datetime}", ctx=UploadContext())
        self.assertEqual(len(date_part), len("2026-09-05"))
        self.assertTrue(stamp.startswith(date_part))
        self.assertEqual(len(stamp), len("2026-09-05_134530"))

    def test_session_variables(self) -> None:
        self.assertEqual(
            render_folder("{group}/{sender}/{platform}", ctx=self.ctx), "12345/999/aiocqhttp"
        )

    def test_session_fallbacks(self) -> None:
        rendered = render_folder("{group}/{sender}/{platform}", ctx=UploadContext())
        self.assertEqual(rendered, "private/unknown/astrbot")

    def test_kind_and_extension(self) -> None:
        self.assertEqual(
            render_folder("{type}/{ext}", ctx=self.ctx, kind=KIND_IMAGE, extension=".PNG"),
            "image/png",
        )
        self.assertEqual(render_folder("{type}/{ext}", ctx=self.ctx), "file/bin")

    def test_sub_folder_appended(self) -> None:
        self.assertEqual(render_folder("wall/", ctx=self.ctx, sub="pack/inner"), "wall/pack/inner")
        self.assertEqual(render_folder("   ", ctx=self.ctx, sub="pack"), "pack")

    def test_unknown_placeholder_does_not_raise(self) -> None:
        self.assertEqual(render_folder("a/{nope}/b", ctx=self.ctx), "a/nope/b")

    def test_all_documented_variables_are_substituted(self) -> None:
        for name in FOLDER_VARIABLES:
            with self.subTest(name=name):
                rendered = render_folder("{" + name + "}", ctx=self.ctx, extension="png")
                self.assertNotIn("{", rendered)
                self.assertTrue(rendered)

    def test_traversal_is_sanitized(self) -> None:
        self.assertEqual(render_folder("../../etc//passwd", ctx=self.ctx), "_/_/etc/passwd")


class DownloadLimitTest(unittest.TestCase):
    """取件阶段的下载上限：压缩包本体要能比单文件上限更大。"""

    def test_plain_file_uses_single_file_limit(self) -> None:
        svc, _, _ = make_service(
            make_config(upload={"max_file_mb": 20.0}, archive={"max_total_mb": 500.0})
        )
        self.assertEqual(svc._download_limit("report.txt"), svc.config.upload.max_file_bytes)

    def test_archive_takes_the_larger_ceiling(self) -> None:
        svc, _, _ = make_service(
            make_config(
                upload={"max_file_mb": 20.0},
                archive={"enabled": True, "max_total_mb": 500.0},
            )
        )
        self.assertEqual(svc._download_limit("pack.zip"), svc.config.archive.max_total_bytes)
        self.assertGreater(svc.config.archive.max_total_bytes, svc.config.upload.max_file_bytes)

    def test_archive_never_shrinks_the_limit(self) -> None:
        # 压缩包上限比单文件上限还小时取较大值，避免「解压开关反而卡住大文件」。
        svc, _, _ = make_service(
            make_config(upload={"max_file_mb": 20.0}, archive={"max_total_mb": 1.0})
        )
        self.assertEqual(svc._download_limit("pack.zip"), svc.config.upload.max_file_bytes)

    def test_archive_disabled_falls_back_to_single_file(self) -> None:
        svc, _, _ = make_service(
            make_config(
                upload={"max_file_mb": 20.0},
                archive={"enabled": False, "max_total_mb": 500.0},
            )
        )
        self.assertEqual(svc._download_limit("pack.zip"), svc.config.upload.max_file_bytes)

    def test_all_archive_hints_are_recognised(self) -> None:
        svc, _, _ = make_service(
            make_config(upload={"max_file_mb": 1.0}, archive={"max_total_mb": 500.0})
        )
        for extension in sorted(ARCHIVE_HINT_EXTENSIONS):
            with self.subTest(extension=extension):
                self.assertEqual(
                    svc._download_limit(f"pack.{extension}"),
                    svc.config.archive.max_total_bytes,
                )

    def test_name_is_sanitised_before_matching(self) -> None:
        svc, _, _ = make_service(
            make_config(upload={"max_file_mb": 1.0}, archive={"max_total_mb": 500.0})
        )
        self.assertEqual(svc._download_limit("../../pack.zip"), svc.config.archive.max_total_bytes)

    def test_extensionless_name(self) -> None:
        svc, _, _ = make_service()
        self.assertEqual(svc._download_limit(""), svc.config.upload.max_file_bytes)


class CompressConfigTest(unittest.TestCase):
    """`--compress` / `--no-compress` 这类临时开关如何覆盖配置。"""

    def test_none_keeps_configured_object(self) -> None:
        svc, _, _ = make_service(make_config(compress={"enabled": True, "trigger_mb": 4.0}))
        self.assertIs(svc._compress_config(None), svc.config.compress)

    def test_true_forces_on_and_lowers_trigger(self) -> None:
        svc, _, _ = make_service(make_config(compress={"enabled": False, "trigger_mb": 4.0}))
        cfg = svc._compress_config(True)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.trigger_mb, 0.05)

    def test_true_does_not_raise_an_already_lower_trigger(self) -> None:
        svc, _, _ = make_service(make_config(compress={"trigger_mb": 0.01}))
        self.assertEqual(svc._compress_config(True).trigger_mb, 0.01)

    def test_false_disables_without_touching_trigger(self) -> None:
        svc, _, _ = make_service(make_config(compress={"enabled": True, "trigger_mb": 4.0}))
        cfg = svc._compress_config(False)
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.trigger_mb, 4.0)

    def test_other_fields_are_preserved(self) -> None:
        svc, _, _ = make_service(
            make_config(compress={"max_edge": 1280, "quality": 70, "target_format": "jpeg"})
        )
        cfg = svc._compress_config(True)
        self.assertEqual((cfg.max_edge, cfg.quality, cfg.target_format), (1280, 70, "jpeg"))


class ApplyPublicPrefixTest(unittest.TestCase):
    """自定义域名 / 反代前置时的链接改写。"""

    @staticmethod
    def _service(prefix: str) -> UploadService:
        svc, _, _ = make_service(make_config(endpoint={"public_url_prefix": prefix}))
        return svc

    def test_empty_prefix_returns_url_untouched(self) -> None:
        svc = self._service("")
        outcome = UploadOutcome(src="/file/a.png", public_url="https://cdn.example.com/file/a.png")
        self.assertEqual(
            svc._apply_public_prefix("https://cdn.example.com/file/a.png", outcome),
            "https://cdn.example.com/file/a.png",
        )

    def test_blank_prefix_is_treated_as_empty(self) -> None:
        svc = self._service("   ")
        self.assertEqual(svc._apply_public_prefix("https://a/b", UploadOutcome()), "https://a/b")

    def test_bare_host_gets_https(self) -> None:
        svc = self._service("cdn.mine.com")
        outcome = UploadOutcome(src="/file/a.png")
        self.assertEqual(
            svc._apply_public_prefix("https://img.example.com/file/a.png", outcome),
            "https://cdn.mine.com/file/a.png",
        )

    def test_trailing_slash_is_trimmed(self) -> None:
        svc = self._service("https://cdn.mine.com/")
        self.assertEqual(
            svc._apply_public_prefix("", UploadOutcome(src="/file/a.png")),
            "https://cdn.mine.com/file/a.png",
        )

    def test_absolute_src_is_stripped_to_path(self) -> None:
        svc = self._service("https://cdn.mine.com")
        outcome = UploadOutcome(src="https://old.example.com/file/2026/a.png")
        self.assertEqual(
            svc._apply_public_prefix("https://old.example.com/file/2026/a.png", outcome),
            "https://cdn.mine.com/file/2026/a.png",
        )

    def test_relative_src_gets_a_leading_slash(self) -> None:
        svc = self._service("https://cdn.mine.com")
        self.assertEqual(
            svc._apply_public_prefix("", UploadOutcome(src="file/a.png")),
            "https://cdn.mine.com/file/a.png",
        )

    def test_falls_back_to_url_when_src_missing(self) -> None:
        svc = self._service("https://cdn.mine.com")
        self.assertEqual(
            svc._apply_public_prefix("https://old.example.com/file/a.png", UploadOutcome()),
            "https://cdn.mine.com/file/a.png",
        )

    def test_http_prefix_is_respected(self) -> None:
        svc = self._service("http://cdn.mine.com")
        self.assertEqual(
            svc._apply_public_prefix("", UploadOutcome(src="/x.png")),
            "http://cdn.mine.com/x.png",
        )

    def test_prefix_with_sub_path(self) -> None:
        svc = self._service("https://cdn.mine.com/img")
        self.assertEqual(
            svc._apply_public_prefix("", UploadOutcome(src="/file/a.png")),
            "https://cdn.mine.com/img/file/a.png",
        )


def fake_event(reply: Any = None, **actions: Any) -> SimpleNamespace:
    """造一个「协议端可用」的假 event：call_action 按 action 名查表。"""

    async def call_action(action: str, **params: Any) -> Any:
        if action not in actions:
            raise RuntimeError(f"unsupported action: {action}")
        result = actions[action]
        if isinstance(result, Exception):
            raise result
        return result

    return SimpleNamespace(
        bot=SimpleNamespace(call_action=call_action),
        message_obj=SimpleNamespace(raw_message=reply),
    )


class ResolveTest(unittest.IsolatedAsyncioTestCase):
    """取件通道优先级：data → path → url → 协议端 → 组件。"""

    def setUp(self) -> None:
        self.tmpdir = self.enterContext(tempfile.TemporaryDirectory())

    def temp_file(self, name: str, data: bytes) -> str:
        target = Path(self.tmpdir) / name
        target.write_bytes(data)
        return str(target)

    async def test_inline_data_short_circuits(self) -> None:
        svc, _, fetcher = make_service()
        material = inline("a.txt", b"hello", mime="text/plain")
        material.url = "https://never.example.com/a.txt"
        self.assertEqual(await svc.resolve(None, material), ("a.txt", b"hello", "text/plain"))
        self.assertEqual(fetcher.calls, [])

    async def test_inline_data_uses_display_name(self) -> None:
        svc, _, _ = make_service()
        name, data, _ = await svc.resolve(None, Material(kind=KIND_IMAGE, data=b"\x89PNG"))
        self.assertEqual((name, data), ("image", b"\x89PNG"))

    async def test_local_path(self) -> None:
        svc, _, fetcher = make_service()
        path = self.temp_file("doc.txt", b"from disk")
        material = Material(name="doc.txt", path=path, mime="text/plain")
        self.assertEqual(await svc.resolve(None, material), ("doc.txt", b"from disk", "text/plain"))
        self.assertEqual(fetcher.calls, [])

    async def test_missing_path_falls_through_to_url(self) -> None:
        url = "https://cdn.example.com/doc.txt"
        svc, _, fetcher = make_service(
            fetcher=FakeFetcher({url: (b"from net", "net.txt", "text/plain")})
        )
        material = Material(path=str(Path(self.tmpdir) / "nope.txt"), url=url)
        self.assertEqual(await svc.resolve(None, material), ("net.txt", b"from net", "text/plain"))
        self.assertEqual(fetcher.calls, [(url, svc.config.upload.max_file_bytes)])

    async def test_oversized_local_file_is_fatal(self) -> None:
        url = "https://cdn.example.com/big.txt"
        svc, _, fetcher = make_service(
            make_config(upload={"max_file_mb": 0.00005}),
            fetcher=FakeFetcher({url: (b"tiny", "big.txt", "text/plain")}),
        )
        material = Material(name="big.txt", path=self.temp_file("big.txt", b"x" * 4096), url=url)
        with self.assertRaises(ResolveError) as caught:
            await svc.resolve(None, material)
        self.assertTrue(caught.exception.fatal)
        self.assertIn("超过上限", caught.exception.reason)
        self.assertEqual(fetcher.calls, [], "致命错误必须立刻中断，不再尝试后续通道")

    async def test_material_name_and_mime_win_over_fetched(self) -> None:
        url = "https://cdn.example.com/x"
        svc, _, _ = make_service(fetcher=FakeFetcher({url: (b"body", "server.bin", "text/plain")}))
        material = Material(name="mine.png", url=url, mime="image/png")
        self.assertEqual(await svc.resolve(None, material), ("mine.png", b"body", "image/png"))

    async def test_empty_url_body_is_reported(self) -> None:
        url = "https://cdn.example.com/empty"
        svc, _, _ = make_service(fetcher=FakeFetcher({url: (b"", "", "")}))
        with self.assertRaises(ResolveError) as caught:
            await svc.resolve(None, Material(name="empty.txt", url=url))
        self.assertEqual(caught.exception.reason, "直链返回了空内容")

    async def test_fatal_fetch_error_propagates(self) -> None:
        url = "https://cdn.example.com/gone"
        boom = ResolveError("直链已失效", fatal=True)
        svc, _, _ = make_service(fetcher=FakeFetcher({url: boom}))
        with self.assertRaises(ResolveError) as caught:
            await svc.resolve(None, Material(name="gone.png", url=url))
        self.assertIs(caught.exception, boom)

    async def test_onebot_channel_supplies_a_url(self) -> None:
        url = "https://qq.example.com/f1"
        svc, _, fetcher = make_service(fetcher=FakeFetcher({url: (b"group file", "real.zip", "")}))
        event = fake_event(get_private_file_url={"url": url})
        material = Material(name="real.zip", file_id="fid-1", payload={"file_id": "fid-1"})
        name, data, _ = await svc.resolve(event, material)
        self.assertEqual((name, data), ("real.zip", b"group file"))
        self.assertEqual([call[0] for call in fetcher.calls], [url])

    async def test_onebot_channel_supplies_a_local_path(self) -> None:
        path = self.temp_file("from-protocol.txt", b"local payload")
        svc, _, fetcher = make_service()
        event = fake_event(get_file={"file": path})
        material = Material(name="from-protocol.txt", payload={"file_id": "fid-2"})
        self.assertEqual(
            await svc.resolve(event, material), ("from-protocol.txt", b"local payload", "")
        )
        self.assertEqual(fetcher.calls, [])

    async def test_onebot_channel_supplies_inline_base64(self) -> None:
        svc, _, _ = make_service()
        encoded = base64.b64encode(b"\x89PNG inline").decode("ascii")
        event = fake_event(get_image={"base64": encoded})
        material = Material(kind=KIND_IMAGE, name="pic.png", payload={"file": "pic.png"})
        self.assertEqual(await svc.resolve(event, material), ("pic.png", b"\x89PNG inline", ""))

    async def test_stale_url_keys_are_not_replayed_to_the_protocol(self) -> None:
        # payload 里的 url 已经过期，剔除后没有任何可用键，协议端通道直接放弃。
        svc, _, _ = make_service()
        material = Material(name="stale.png", payload={"url": "https://expired.example.com/x"})
        with self.assertRaises(ResolveError) as caught:
            await svc.resolve(fake_event(), material)
        self.assertEqual(caught.exception.reason, "取不到文件内容")

    async def test_protocol_exceptions_are_swallowed(self) -> None:
        svc, _, _ = make_service()
        event = fake_event(get_file=RuntimeError("协议端炸了"))
        material = Material(name="x.zip", payload={"file_id": "fid-3"})
        with self.assertRaises(ResolveError) as caught:
            await svc.resolve(event, material)
        self.assertEqual(caught.exception.reason, "取不到文件内容")

    async def test_component_fallback_for_image(self) -> None:
        path = self.temp_file("comp.png", b"component bytes")

        async def convert_to_file_path() -> str:
            return path

        svc, _, _ = make_service()
        material = Material(
            kind=KIND_IMAGE,
            name="comp.png",
            component=SimpleNamespace(convert_to_file_path=convert_to_file_path),
        )
        self.assertEqual(await svc.resolve(None, material), ("comp.png", b"component bytes", ""))

    async def test_component_fallback_for_file_url(self) -> None:
        url = "https://cdn.example.com/comp.zip"

        async def get_file(*, allow_return_url: bool = False) -> str:
            assert allow_return_url is True
            return url

        svc, _, fetcher = make_service(fetcher=FakeFetcher({url: (b"zip bytes", "ignored", "")}))
        material = Material(name="comp.zip", component=SimpleNamespace(get_file=get_file))
        self.assertEqual(await svc.resolve(None, material), ("comp.zip", b"zip bytes", ""))
        self.assertEqual([call[0] for call in fetcher.calls], [url])

    async def test_component_fallback_for_file_path(self) -> None:
        path = self.temp_file("comp.zip", b"disk zip")

        async def get_file(*, allow_return_url: bool = False) -> str:
            return path

        svc, _, _ = make_service()
        material = Material(name="comp.zip", component=SimpleNamespace(get_file=get_file))
        self.assertEqual(await svc.resolve(None, material), ("comp.zip", b"disk zip", ""))

    async def test_component_without_usable_getter(self) -> None:
        svc, _, _ = make_service()
        for component in (SimpleNamespace(), SimpleNamespace(get_file="not-callable")):
            with self.subTest(component=component):
                material = Material(name="x.zip", component=component)
                with self.assertRaises(ResolveError) as caught:
                    await svc.resolve(None, material)
                self.assertEqual(caught.exception.reason, "取不到文件内容")

    async def test_nothing_works_at_all(self) -> None:
        svc, _, _ = make_service()
        with self.assertRaises(ResolveError) as caught:
            await svc.resolve(None, Material(name="ghost.png"))
        self.assertEqual(caught.exception.reason, "取不到文件内容")
        self.assertTrue(caught.exception.hint)


class UploadMaterialsTest(unittest.IsolatedAsyncioTestCase):
    """`upload_materials()` 的编排：永不抛异常，所有结果都进 report。"""

    async def test_unconfigured_endpoint_fails_fast(self) -> None:
        svc, client, _ = make_service(make_config(endpoint={"base_url": ""}))
        report = await svc.upload_materials(None, [inline("a.txt", b"hello")])
        self.assertFalse(report.ok)
        self.assertEqual(len(report.failures), 1)
        self.assertEqual(report.failures[0].reason, "图床地址未配置")
        self.assertTrue(report.failures[0].hint)
        self.assertEqual(client.calls, [])

    async def test_empty_material_list(self) -> None:
        svc, client, _ = make_service()
        report = await svc.upload_materials(None, [])
        self.assertEqual(report.notes, ["没有找到可上传的图片或文件。"])
        self.assertEqual(client.calls, [])
        self.assertFalse(report.ok)

    async def test_single_file_round_trip(self) -> None:
        svc, client, _ = make_service()
        report = await svc.upload_materials(None, [inline("a.txt", b"hello")], folder="wall")
        self.assertTrue(report.ok)
        self.assertEqual(len(report.items), 1)
        item = report.items[0]
        self.assertEqual(item.name, "a.txt")
        self.assertEqual(item.url, "https://cdn.example.com/file/wall/a.txt")
        self.assertEqual(item.size, 5)
        self.assertEqual(item.original_size, 5)
        self.assertEqual(item.file_id, "fid-a.txt")
        self.assertFalse(item.reused)
        self.assertEqual(client.folders, ["wall"])
        self.assertEqual(report.uploaded_bytes, 5)
        self.assertGreaterEqual(report.elapsed, 0.0)

    async def test_default_folder_template_is_used(self) -> None:
        svc, client, _ = make_service()
        await svc.upload_materials(None, [inline("a.txt", b"hello")])
        self.assertRegex(client.folders[0], r"^astrbot/\d{4}-\d{2}-\d{2}$")

    async def test_name_type_is_forwarded(self) -> None:
        svc, client, _ = make_service()
        await svc.upload_materials(None, [inline("a.txt", b"x")], folder="wall", name_type="index")
        self.assertEqual(client.calls[0]["name_type"], "index")

    async def test_image_kind_comes_from_extension(self) -> None:
        svc, _, _ = make_service()
        report = await svc.upload_materials(None, [inline("pic.png", b"\x89PNG" * 8)], folder="w")
        self.assertEqual(report.items[0].kind, KIND_IMAGE)
        self.assertTrue(report.items[0].is_image)

    async def test_resolve_failure_is_collected(self) -> None:
        svc, client, _ = make_service()
        report = await svc.upload_materials(
            None, [Material(name="ghost.png"), inline("ok.txt", b"fine")], folder="wall"
        )
        self.assertEqual([item.name for item in report.items], ["ok.txt"])
        self.assertEqual([failure.name for failure in report.failures], ["ghost.png"])
        self.assertEqual(client.names, ["ok.txt"])

    async def test_order_is_preserved_under_concurrency(self) -> None:
        svc, _, _ = make_service(make_config(upload={"concurrency": 4}))
        materials = [inline(f"f{index}.txt", f"body-{index}".encode()) for index in range(6)]
        report = await svc.upload_materials(None, materials, folder="wall")
        self.assertEqual(
            [item.name for item in report.items], [f"f{index}.txt" for index in range(6)]
        )

    async def test_in_batch_dedupe_reuses_one_upload(self) -> None:
        svc, client, _ = make_service()
        report = await svc.upload_materials(
            None,
            [inline("first.txt", b"same body"), inline("second.txt", b"same body")],
            folder="wall",
        )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual([item.name for item in report.items], ["first.txt", "second.txt"])
        self.assertEqual(report.items[0].url, report.items[1].url)
        self.assertFalse(report.items[0].reused)
        self.assertTrue(report.items[1].reused)
        self.assertEqual(report.items[1].note, "与本批次内另一个文件内容相同，复用链接")
        self.assertEqual(report.reused_count, 1)

    async def test_cross_batch_dedupe_hits_the_store(self) -> None:
        store = FerryStore(SimpleNamespace())
        svc, client, _ = make_service(store=store)
        first = await svc.upload_materials(None, [inline("a.txt", b"same body")], folder="wall")
        second = await svc.upload_materials(None, [inline("b.txt", b"same body")], folder="wall")
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(second.items[0].reused)
        self.assertEqual(second.items[0].note, "命中去重缓存，未重复上传")
        self.assertEqual(second.items[0].url, first.items[0].url)
        self.assertEqual(len(await store.recent(10)), 2)

    async def test_dedupe_can_be_switched_off(self) -> None:
        store = FerryStore(SimpleNamespace(), BehaviorConfig(dedupe_enabled=False))
        svc, client, _ = make_service(store=store)
        await svc.upload_materials(None, [inline("a.txt", b"same body")], folder="wall")
        second = await svc.upload_materials(None, [inline("b.txt", b"same body")], folder="wall")
        self.assertEqual(len(client.calls), 2)
        self.assertFalse(second.items[0].reused)

    async def test_records_are_written_to_the_store(self) -> None:
        store = FerryStore(SimpleNamespace())
        svc, _, _ = make_service(store=store)
        event = SimpleNamespace(
            unified_msg_origin="aiocqhttp:GroupMessage:12345",
            get_sender_id=lambda: "999",
            get_group_id=lambda: "12345",
            get_platform_name=lambda: "aiocqhttp",
        )
        await svc.upload_materials(event, [inline("a.txt", b"hello")], folder="wall")
        records = await store.recent(10)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "a.txt")
        self.assertEqual(records[0]["user"], "999")
        self.assertEqual(records[0]["group"], "12345")
        self.assertEqual(records[0]["session"], "aiocqhttp:GroupMessage:12345")

    async def test_max_files_truncates(self) -> None:
        svc, client, _ = make_service(make_config(upload={"max_files": 2}))
        materials = [inline(f"f{index}.txt", f"body-{index}".encode()) for index in range(5)]
        report = await svc.upload_materials(None, materials, folder="wall")
        self.assertTrue(report.truncated)
        self.assertEqual(len(report.items), 2)
        self.assertEqual(len(client.calls), 2)

    async def test_total_size_truncates(self) -> None:
        svc, _, _ = make_service(make_config(upload={"max_total_mb": 0.0001}))
        materials = [inline(f"f{index}.txt", f"x{index}".encode() * 40) for index in range(3)]
        report = await svc.upload_materials(None, materials, folder="wall")
        self.assertTrue(report.truncated)
        self.assertEqual(len(report.items), 1, "第一个文件永远放行，之后才按总量截断")

    async def test_single_file_over_limit_is_skipped(self) -> None:
        svc, client, _ = make_service(make_config(upload={"max_file_mb": 0.0001}))
        report = await svc.upload_materials(
            None, [inline("big.txt", b"x" * 500), inline("ok.txt", b"tiny")], folder="wall"
        )
        self.assertEqual(client.names, ["ok.txt"])
        self.assertEqual(len(report.skipped), 1)
        self.assertEqual(report.skipped[0][0], "big.txt")
        self.assertIn("超出单文件上限", report.skipped[0][1])

    async def test_extension_whitelist_blocks_unknown_types(self) -> None:
        svc, client, _ = make_service()
        report = await svc.upload_materials(
            None, [inline("payload.bin", b"\x00\x01\x02")], folder="w"
        )
        self.assertEqual(client.calls, [])
        self.assertEqual(report.skipped[0][0], "payload.bin")
        self.assertIn("不在图床白名单", report.skipped[0][1])

    async def test_extension_blacklist(self) -> None:
        svc, client, _ = make_service(make_config(upload={"blocked_extensions": ["txt"]}))
        report = await svc.upload_materials(None, [inline("a.txt", b"hello")], folder="wall")
        self.assertEqual(client.calls, [])
        self.assertIn("黑名单", report.skipped[0][1])

    async def test_whitelist_can_be_extended(self) -> None:
        svc, client, _ = make_service(make_config(upload={"extra_allowed_extensions": ["bin"]}))
        report = await svc.upload_materials(None, [inline("payload.bin", b"\x00\x01")], folder="w")
        self.assertEqual(client.names, ["payload.bin"])
        self.assertEqual(report.skipped, [])


class ArchiveExpansionTest(unittest.IsolatedAsyncioTestCase):
    """压缩包解压上传：目录结构、开关组合与失败兜底。"""

    async def test_zip_is_expanded_and_inner_dirs_are_kept(self) -> None:
        svc, client, _ = make_service()
        data = make_zip({"pack/a.txt": b"aaa", "pack/b.txt": b"bbb"})
        report = await svc.upload_materials(None, [inline("x.zip", data)], folder="wall")
        self.assertIn("x.zip 解压出 2 个文件。", report.notes)
        self.assertEqual(client.names, ["a.txt", "b.txt"])
        self.assertEqual(client.folders, ["wall/x/pack", "wall/x/pack"])
        self.assertEqual([item.archive for item in report.items], ["x.zip", "x.zip"])

    async def test_root_members_only_get_the_stem(self) -> None:
        svc, client, _ = make_service()
        await svc.upload_materials(None, [inline("x.zip", make_zip({"a.txt": b"aaa"}))], folder="w")
        self.assertEqual(client.folders, ["w/x"])

    async def test_tar_gz_double_suffix_is_stripped(self) -> None:
        import tarfile

        buffer = io.BytesIO()
        payload = b"aaa"
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            info = tarfile.TarInfo("inner/a.txt")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        svc, client, _ = make_service()
        await svc.upload_materials(None, [inline("x.tar.gz", buffer.getvalue())], folder="w")
        self.assertEqual(client.folders, ["w/x/inner"], "tar.gz 要去掉两层后缀")

    async def test_extract_false_uploads_the_whole_archive(self) -> None:
        svc, client, _ = make_service()
        data = make_zip({"pack/a.txt": b"aaa"})
        report = await svc.upload_materials(
            None, [inline("x.zip", data)], folder="wall", extract=False
        )
        self.assertEqual(client.names, ["x.zip"])
        self.assertEqual(client.folders, ["wall"])
        self.assertEqual(report.notes, [])

    async def test_default_extract_config_is_respected(self) -> None:
        svc, client, _ = make_service(make_config(archive={"default_extract": False}))
        data = make_zip({"pack/a.txt": b"aaa"})
        await svc.upload_materials(None, [inline("x.zip", data)], folder="wall")
        self.assertEqual(client.names, ["x.zip"])

    async def test_whole_archive_upload_can_be_refused(self) -> None:
        svc, client, _ = make_service(make_config(archive={"upload_archive_when_disabled": False}))
        data = make_zip({"pack/a.txt": b"aaa"})
        report = await svc.upload_materials(
            None, [inline("x.zip", data)], folder="wall", extract=False
        )
        self.assertEqual(client.calls, [])
        self.assertEqual(report.skipped, [("x.zip", "压缩包未解压，也未允许整包上传")])

    async def test_archive_support_disabled_entirely(self) -> None:
        svc, client, _ = make_service(make_config(archive={"enabled": False}))
        data = make_zip({"pack/a.txt": b"aaa"})
        await svc.upload_materials(None, [inline("x.zip", data)], folder="wall")
        self.assertEqual(client.names, ["x.zip"])

    async def test_empty_member_is_skipped(self) -> None:
        svc, client, _ = make_service()
        data = make_zip({"pack/a.txt": b"aaa", "pack/empty.txt": b""})
        report = await svc.upload_materials(None, [inline("x.zip", data)], folder="wall")
        self.assertEqual(client.names, ["a.txt"])
        self.assertTrue(any(name == "empty.txt" for name, _ in report.skipped))

    async def test_broken_archive_is_reported(self) -> None:
        svc, client, _ = make_service()
        report = await svc.upload_materials(
            None, [inline("x.zip", b"PK\x03\x04 truncated garbage")], folder="wall"
        )
        self.assertEqual(client.calls, [])
        self.assertEqual(report.failures[0].name, "x.zip")

    async def test_max_files_stops_mid_archive(self) -> None:
        svc, _, _ = make_service(make_config(upload={"max_files": 1}))
        data = make_zip({"a.txt": b"aaa", "b.txt": b"bbb"})
        report = await svc.upload_materials(None, [inline("x.zip", data)], folder="wall")
        self.assertTrue(report.truncated)
        self.assertEqual(len(report.items), 1)

    async def test_duplicate_members_are_deduped(self) -> None:
        svc, client, _ = make_service()
        data = make_zip({"a.txt": b"same", "sub/b.txt": b"same"})
        report = await svc.upload_materials(None, [inline("x.zip", data)], folder="wall")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(report.items), 2)
        self.assertTrue(report.items[1].reused)
        self.assertEqual(report.items[1].archive, "x.zip")


class FailureClassificationTest(unittest.IsolatedAsyncioTestCase):
    async def test_imgbed_error_becomes_failed_item(self) -> None:
        client = FakeClient([ImgBedError("图床拒绝", kind="bad_request", status=400)])
        svc, _, _ = make_service(client=client)
        report = await svc.upload_materials(None, [inline("a.txt", b"hello")], folder="wall")
        self.assertEqual(report.items, [])
        self.assertEqual(report.failures[0].name, "a.txt")
        self.assertEqual(report.failures[0].reason, "图床拒绝")

    async def test_clone_inherits_the_origin_failure(self) -> None:
        client = FakeClient([ImgBedError("图床拒绝", kind="bad_request")])
        svc, _, _ = make_service(client=client)
        report = await svc.upload_materials(
            None, [inline("a.txt", b"same"), inline("b.txt", b"same")], folder="wall"
        )
        self.assertEqual([failure.name for failure in report.failures], ["a.txt", "b.txt"])
        self.assertEqual(len(client.calls), 1)

    async def test_unexpected_exception_is_wrapped(self) -> None:
        client = FakeClient([RuntimeError("boom")])
        svc, _, _ = make_service(client=client)
        report = await svc.upload_materials(None, [inline("a.txt", b"hello")], folder="wall")
        self.assertEqual(report.failures[0].reason, "上传异常：RuntimeError")

    async def test_partial_success_still_counts_as_ok(self) -> None:
        client = FakeClient([ImgBedError("第一个失败", kind="bad_request"), None])
        svc, _, _ = make_service(client=client)
        report = await svc.upload_materials(
            None, [inline("a.txt", b"one"), inline("b.txt", b"two")], folder="wall"
        )
        self.assertEqual([item.name for item in report.items], ["b.txt"])
        self.assertEqual([failure.name for failure in report.failures], ["a.txt"])
        self.assertTrue(report.ok)


class RetryTest(unittest.IsolatedAsyncioTestCase):
    """只有网络类错误才值得重试，鉴权 / 参数错误重试只是浪费配额。"""

    @staticmethod
    def _service(
        client: FakeClient, *, retry_times: int = 2, backoff: float = 0.0
    ) -> UploadService:
        svc, _, _ = make_service(
            make_config(upload={"retry_times": retry_times, "retry_backoff_seconds": backoff}),
            client=client,
        )
        return svc

    async def test_retryable_kinds_are_retried(self) -> None:
        for kind in sorted(RETRYABLE_KINDS):
            with self.subTest(kind=kind):
                client = FakeClient([ImgBedError("暂时失败", kind=kind), None])
                svc = self._service(client)
                report = await svc.upload_materials(
                    None, [inline("a.txt", b"hello")], folder="wall"
                )
                self.assertEqual(len(client.calls), 2)
                self.assertEqual(len(report.items), 1)

    async def test_non_retryable_kinds_fail_immediately(self) -> None:
        for kind in ("auth", "forbidden", "bad_request", "too_large", "unknown"):
            with self.subTest(kind=kind):
                client = FakeClient([ImgBedError("不可重试", kind=kind), None])
                svc = self._service(client)
                report = await svc.upload_materials(
                    None, [inline("a.txt", b"hello")], folder="wall"
                )
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(len(report.failures), 1)

    async def test_attempt_count_is_capped(self) -> None:
        client = FakeClient([ImgBedError("网络炸了", kind="network")] * 5)
        svc = self._service(client, retry_times=2)
        report = await svc.upload_materials(None, [inline("a.txt", b"hello")], folder="wall")
        self.assertEqual(len(client.calls), 3, "retry_times=2 意味着总共 3 次尝试")
        self.assertEqual(report.failures[0].reason, "网络炸了")

    async def test_zero_retries(self) -> None:
        client = FakeClient([ImgBedError("网络炸了", kind="network")] * 3)
        svc = self._service(client, retry_times=0)
        await svc.upload_materials(None, [inline("a.txt", b"hello")], folder="wall")
        self.assertEqual(len(client.calls), 1)

    async def test_negative_retry_times_is_clamped(self) -> None:
        client = FakeClient([ImgBedError("网络炸了", kind="network")] * 3)
        svc = self._service(client, retry_times=-5)
        await svc.upload_materials(None, [inline("a.txt", b"hello")], folder="wall")
        self.assertEqual(len(client.calls), 1)

    async def test_backoff_grows_linearly(self) -> None:
        client = FakeClient([ImgBedError("网络炸了", kind="network")] * 3)
        svc = self._service(client, retry_times=2, backoff=1.5)
        with mock.patch.object(service.asyncio, "sleep") as sleep:
            report = await svc.upload_materials(None, [inline("a.txt", b"hello")], folder="wall")
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1.5, 3.0])
        self.assertEqual(len(report.failures), 1)


class CompressionTest(unittest.IsolatedAsyncioTestCase):
    """图片预压缩：省图床空间，也省下游流量。"""

    async def test_forced_compression_shrinks_the_image(self) -> None:
        svc, client, _ = make_service(
            make_config(compress={"enabled": False, "max_edge": 100, "quality": 60})
        )
        original = noise_png()
        report = await svc.upload_materials(
            None,
            [inline("noise.png", original, kind=KIND_IMAGE)],
            folder="wall",
            compress=True,
        )
        item = report.items[0]
        self.assertEqual(item.original_size, len(original))
        self.assertLess(item.size, item.original_size)
        self.assertTrue(item.note)
        self.assertTrue(item.name.endswith(".webp"))
        self.assertEqual(client.calls[0]["size"], item.size)

    async def test_compression_off_keeps_the_original_bytes(self) -> None:
        svc, client, _ = make_service()
        original = noise_png()
        report = await svc.upload_materials(
            None,
            [inline("noise.png", original, kind=KIND_IMAGE)],
            folder="wall",
            compress=False,
        )
        item = report.items[0]
        self.assertEqual((item.name, item.size, item.note), ("noise.png", len(original), ""))
        self.assertEqual(client.calls[0]["size"], len(original))

    async def test_non_image_is_never_compressed(self) -> None:
        svc, _, _ = make_service()
        report = await svc.upload_materials(
            None, [inline("a.txt", b"hello" * 4096)], folder="wall", compress=True
        )
        item = report.items[0]
        self.assertEqual(item.size, item.original_size)
        self.assertEqual(item.note, "")


if __name__ == "__main__":
    unittest.main()
