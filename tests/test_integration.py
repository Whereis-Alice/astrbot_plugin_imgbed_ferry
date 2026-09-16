"""跨插件资源句柄协议与安全边界测试。"""

from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, ClassVar

from imgbed_ferry.integration import (
    ASSET_PROTOCOL,
    AssetValidationError,
    parse_asset_handle,
    read_asset_bytes,
)

PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\x0dIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\x0dIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff"
    b"\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


def descriptor(data: bytes = PNG, **overrides: Any) -> dict[str, Any]:
    now = time.time()
    value: dict[str, Any] = {
        "provider": "astrbot_plugin_meme_magpie",
        "api_version": 1,
        "asset_id": "emoji_1",
        "filename": "smile.png",
        "mime_type": "image/png",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "created_at": now - 1,
        "expires_at": now + 120,
        "data": data,
    }
    value.update(overrides)
    return value


class FakeMemeHandle:
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


class ExpiredProviderError(RuntimeError):
    code = "asset_expired"
    message = "provider says expired"


class IntegrationHandleTest(unittest.IsolatedAsyncioTestCase):
    async def test_mapping_round_trip(self) -> None:
        handle, data = await read_asset_bytes(descriptor())
        self.assertEqual(data, PNG)
        self.assertEqual(handle.protocol, ASSET_PROTOCOL)
        self.assertEqual(handle.source, "astrbot_plugin_meme_magpie")
        self.assertEqual(handle.sha256, hashlib.sha256(PNG).hexdigest())

    async def test_real_duck_typed_handle_is_supported(self) -> None:
        handle, data = await read_asset_bytes(FakeMemeHandle())
        self.assertEqual(data, PNG)
        self.assertEqual(handle.asset_id, "emoji_1")
        self.assertEqual(handle.mime, "image/png")

    async def test_wrapped_handle_is_supported(self) -> None:
        handle, data = await read_asset_bytes({"asset": FakeMemeHandle()})
        self.assertEqual(data, PNG)
        self.assertEqual(handle.name, "smile.png")

    def test_bare_values_are_rejected(self) -> None:
        for value in (b"bytes", "C:\\secret.png", Path("C:/secret.png")):
            with self.subTest(value=repr(value)):
                with self.assertRaises(AssetValidationError) as caught:
                    parse_asset_handle(value)
                self.assertEqual(caught.exception.code, "invalid_asset")

    def test_source_and_protocol_are_checked(self) -> None:
        cases = (
            (descriptor(provider="other"), "invalid_asset"),
            (descriptor(api_version=2), "invalid_asset"),
            (descriptor(protocol="other/v1"), "invalid_asset"),
        )
        for value, code in cases:
            with self.subTest(value=value):
                with self.assertRaises(AssetValidationError) as caught:
                    parse_asset_handle(value)
                self.assertEqual(caught.exception.code, code)

    def test_expiry_and_ttl_are_checked(self) -> None:
        with self.assertRaises(AssetValidationError) as expired:
            parse_asset_handle(descriptor(expires_at=time.time() - 1))
        self.assertEqual(expired.exception.code, "asset_expired")

        with self.assertRaises(AssetValidationError) as too_long:
            parse_asset_handle(
                descriptor(created_at=time.time() - 1, expires_at=time.time() + 3600),
                max_ttl_seconds=60,
            )
        self.assertEqual(too_long.exception.code, "invalid_asset")

    async def test_hash_size_limit_and_signature_are_checked(self) -> None:
        with self.assertRaises(AssetValidationError) as missing_hash:
            parse_asset_handle(descriptor(sha256=""))
        self.assertEqual(missing_hash.exception.code, "invalid_asset")

        with self.assertRaises(AssetValidationError) as mismatch:
            await read_asset_bytes(descriptor(sha256="0" * 64))
        self.assertEqual(mismatch.exception.code, "hash_mismatch")

        with self.assertRaises(AssetValidationError) as bad_size:
            await read_asset_bytes(descriptor(size=len(PNG) + 1))
        self.assertEqual(bad_size.exception.code, "invalid_asset")

        with self.assertRaises(AssetValidationError) as too_large:
            await read_asset_bytes(descriptor(), max_bytes=len(PNG) - 1)
        self.assertEqual(too_large.exception.code, "too_large")

        with self.assertRaises(AssetValidationError) as bad_extension:
            await read_asset_bytes(descriptor(filename="smile.txt"))
        self.assertEqual(bad_extension.exception.code, "invalid_asset")

        with self.assertRaises(AssetValidationError) as bad_mime:
            await read_asset_bytes(descriptor(mime_type="image/jpeg"))
        self.assertEqual(bad_mime.exception.code, "invalid_signature")

    async def test_missing_extension_is_inferred(self) -> None:
        handle, data = await read_asset_bytes(descriptor(filename="asset"))
        self.assertEqual(data, PNG)
        self.assertEqual(handle.name, "asset.png")

    async def test_provider_errors_are_normalized(self) -> None:
        class ProviderHandle(FakeMemeHandle):
            async def read_bytes(self) -> bytes:
                raise ExpiredProviderError()

        with self.assertRaises(AssetValidationError) as caught:
            await read_asset_bytes(ProviderHandle())
        self.assertEqual(caught.exception.code, "asset_expired")

    def test_existing_handle_is_revalidated_against_current_policy(self) -> None:
        handle = parse_asset_handle(descriptor())
        with self.assertRaises(AssetValidationError) as caught:
            parse_asset_handle(handle, allowed_sources=["another_provider"])
        self.assertEqual(caught.exception.code, "invalid_asset")

    async def test_controlled_path_is_read_without_deleting_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "asset.png"
            path.write_bytes(PNG)
            value = descriptor()
            value.pop("data")
            value["path"] = str(path)
            handle, data = await read_asset_bytes(value)
            self.assertEqual(data, PNG)
            self.assertEqual(handle.name, "smile.png")
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), PNG)

    def test_symlink_path_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            link = root / "link.png"
            source.write_bytes(PNG)
            try:
                link.symlink_to(source)
            except (OSError, NotImplementedError):
                self.skipTest("当前 Windows 环境不允许创建符号链接")
            value = descriptor(path=str(link))
            value.pop("data")
            with self.assertRaises(AssetValidationError) as caught:
                parse_asset_handle(value)
            self.assertEqual(caught.exception.code, "invalid_asset")


if __name__ == "__main__":
    unittest.main()
