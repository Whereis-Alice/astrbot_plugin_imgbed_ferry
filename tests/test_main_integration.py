"""插件公开 upload_asset() 接口的集成测试。"""

from __future__ import annotations

import hashlib
import time
import unittest
from types import SimpleNamespace
from typing import Any, ClassVar

from astrbot_plugin_imgbed_ferry import main
from astrbot_plugin_imgbed_ferry.imgbed_ferry.client import UploadOutcome
from astrbot_plugin_imgbed_ferry.imgbed_ferry.config import (
    EndpointConfig,
    FerryConfig,
    IntegrationConfig,
    UploadConfig,
)
from astrbot_plugin_imgbed_ferry.imgbed_ferry.service import UploadService
from astrbot_plugin_imgbed_ferry.imgbed_ferry.store import FerryStore

PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\x0dIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\x0dIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff"
    b"\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)
PNG_ALT = PNG + b"\x00"


class FakeHandle:
    provider = "astrbot_plugin_meme_magpie"
    api_version = 1
    asset_id = "emoji_1"
    filename = "smile.png"
    mime_type = "image/png"
    size = len(PNG)
    sha256 = hashlib.sha256(PNG).hexdigest()
    created_at = time.time() - 1
    expires_at = time.time() + 120
    metadata: ClassVar[dict[str, Any]] = {}

    async def read_bytes(self) -> bytes:
        return PNG


class AlternateHandle(FakeHandle):
    asset_id = "emoji_2"
    filename = "different.png"
    size = len(PNG_ALT)
    sha256 = hashlib.sha256(PNG_ALT).hexdigest()

    async def read_bytes(self) -> bytes:
        return PNG_ALT


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def upload(self, **kwargs: Any) -> UploadOutcome:
        self.calls.append(kwargs)
        filename = kwargs["filename"]
        folder = kwargs["folder"]
        path = f"/file/{folder}/{filename}"
        return UploadOutcome(
            src=path,
            public_url=f"https://cdn.example.com{path}",
            file_id=f"fid-{filename}",
        )


def make_plugin(*, daily_quota: int = 0) -> tuple[Any, FakeClient, FerryStore]:
    config = FerryConfig(
        endpoint=EndpointConfig(base_url="https://img.example.com", api_token="token"),
        upload=UploadConfig(concurrency=1, retry_times=0),
        integration=IntegrationConfig(),
    )
    config.permission.daily_quota_per_user = daily_quota
    client = FakeClient()
    store = FerryStore(SimpleNamespace())
    plugin = object.__new__(main.ImgBedFerryPlugin)
    plugin.config = config
    plugin.store = store
    plugin.service = UploadService(config, client, store)
    return plugin, client, store


def event() -> SimpleNamespace:
    return SimpleNamespace(
        get_sender_id=lambda: "user-1",
        get_group_id=lambda: "",
        is_admin=lambda: False,
    )


class UploadAssetTest(unittest.IsolatedAsyncioTestCase):
    async def test_upload_and_reuse_returns_markdown(self) -> None:
        plugin, client, _ = make_plugin()
        first = await plugin.upload_asset(event(), FakeHandle(), folder="astrbook/memes")
        second = await plugin.upload_asset(
            event(), FakeHandle(), folder="astrbook/memes", output_format="plain"
        )

        self.assertTrue(first["success"])
        self.assertFalse(first["reused"])
        self.assertEqual(first["markdown"], f"![smile.png]({first['url']})")
        self.assertTrue(second["success"])
        self.assertTrue(second["reused"])
        self.assertTrue(second["formatted"].endswith(second["url"]))
        self.assertEqual(len(client.calls), 1)

    async def test_output_formats_are_supported(self) -> None:
        for style in ("plain", "markdown", "html", "bbcode"):
            with self.subTest(style=style):
                plugin, _, _ = make_plugin()
                result = await plugin.upload_asset(event(), FakeHandle(), output_format=style)
                self.assertTrue(result["success"])
                self.assertEqual(result["output_format"], style)
                self.assertTrue(result["formatted"])

    async def test_invalid_handle_is_structured_error(self) -> None:
        plugin, client, _ = make_plugin()
        result = await plugin.upload_asset(event(), "C:\\secret.png")
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "invalid_asset")
        self.assertEqual(client.calls, [])

    async def test_permission_and_quota_are_enforced(self) -> None:
        plugin, client, _ = make_plugin(daily_quota=1)
        plugin.config.permission.user_blacklist = ["user-1"]
        denied = await plugin.upload_asset(event(), FakeHandle())
        self.assertEqual(denied["code"], "forbidden")
        self.assertEqual(client.calls, [])

        plugin.config.permission.user_blacklist = []
        first = await plugin.upload_asset(event(), FakeHandle())
        reused = await plugin.upload_asset(event(), FakeHandle())
        second = await plugin.upload_asset(event(), AlternateHandle())
        self.assertTrue(first["success"])
        self.assertTrue(reused["success"])
        self.assertTrue(reused["reused"])
        self.assertEqual(second["code"], "quota_exceeded")
        self.assertEqual(len(client.calls), 1)

    async def test_disabled_integration_does_not_read_asset(self) -> None:
        plugin, client, _ = make_plugin()
        plugin.config.integration.enabled = False
        result = await plugin.upload_asset(event(), FakeHandle())
        self.assertEqual(result["code"], "integration_disabled")
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
