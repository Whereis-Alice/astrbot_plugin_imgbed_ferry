"""配置解析与 schema 一致性测试。"""

from __future__ import annotations

import dataclasses
import json
import unittest
from pathlib import Path

from imgbed_ferry.config import (
    AUTH_MODE_AUTH_CODE,
    AUTH_MODE_NONE,
    AUTH_MODE_TOKEN,
    EndpointConfig,
    FerryConfig,
    UploadConfig,
    as_bool,
    as_choice,
    as_float,
    as_int,
    as_str,
    as_str_list,
    normalize_extension,
    section,
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"


def schema_defaults(node: dict) -> dict:
    """把 _conf_schema.json 折叠成 AstrBot 注入插件的默认配置字典。"""

    out: dict = {}
    for key, spec in node.items():
        if isinstance(spec, dict) and "items" in spec:
            out[key] = schema_defaults(spec["items"])
        else:
            out[key] = spec.get("default")
    return out


class ScalarHelperTest(unittest.TestCase):
    def test_as_bool_words(self) -> None:
        for value in (True, 1, "1", "true", "YES", "on", "开", "是"):
            self.assertTrue(as_bool(value), msg=repr(value))
        for value in (False, 0, "0", "false", "NO", "off", "关", "否"):
            self.assertFalse(as_bool(value), msg=repr(value))

    def test_as_bool_fallback(self) -> None:
        self.assertTrue(as_bool(None, True))
        self.assertFalse(as_bool(None, False))
        self.assertTrue(as_bool("随便", True))
        self.assertTrue(as_bool("", True))

    def test_as_int_clamp(self) -> None:
        self.assertEqual(as_int("12"), 12)
        self.assertEqual(as_int("abc", 7), 7)
        self.assertEqual(as_int(None, 3), 3)
        self.assertEqual(as_int(999, 1, maximum=16), 16)
        self.assertEqual(as_int(-5, 1, minimum=1), 1)
        # 小数字符串不是合法 int，回落到默认值后再夹紧
        self.assertEqual(as_int(2.9, 4), 4)
        self.assertEqual(as_int("2.9", 1, minimum=2), 2)

    def test_as_float_clamp(self) -> None:
        self.assertAlmostEqual(as_float("1.5"), 1.5)
        self.assertAlmostEqual(as_float("x", 2.0), 2.0)
        self.assertAlmostEqual(as_float(0.0, 1.0, minimum=0.1), 0.1)

    def test_as_str(self) -> None:
        self.assertEqual(as_str("  hi  "), "hi")
        self.assertEqual(as_str(None, "d"), "d")
        self.assertEqual(as_str(12), "12")
        # 只有 None 才走默认值，空串保持空串（配置里"清空"是有意义的操作）
        self.assertEqual(as_str("", "d"), "")

    def test_as_str_list_splits(self) -> None:
        self.assertEqual(as_str_list("png, jpg\nwebp"), ["png", "jpg", "webp"])
        self.assertEqual(as_str_list("png，jpg"), ["png", "jpg"])
        self.assertEqual(as_str_list("a\r\nb"), ["a", "b"])
        self.assertEqual(as_str_list(["a", "", " b "]), ["a", "b"])
        self.assertEqual(as_str_list(None), [])
        self.assertEqual(as_str_list(""), [])

    def test_as_choice(self) -> None:
        self.assertEqual(as_choice("S3", ("s3", "cfr2"), "s3"), "s3")
        self.assertEqual(as_choice("nope", ("s3", "cfr2"), "cfr2"), "cfr2")
        self.assertEqual(as_choice(None, ("s3",), "s3"), "s3")

    def test_normalize_extension(self) -> None:
        self.assertEqual(normalize_extension(".PNG"), "png")
        self.assertEqual(normalize_extension("*.jpg"), "jpg")
        self.assertEqual(normalize_extension("  webp "), "webp")
        self.assertEqual(normalize_extension(""), "")

    def test_section(self) -> None:
        self.assertEqual(section({"a": {"b": 1}}, "a"), {"b": 1})
        self.assertEqual(section({"a": 1}, "a"), {})
        self.assertEqual(section({}, "missing"), {})


class EndpointConfigTest(unittest.TestCase):
    def test_normalized_base_url(self) -> None:
        self.assertEqual(
            EndpointConfig(base_url="img.example.com/").normalized_base_url,
            "https://img.example.com",
        )
        self.assertEqual(
            EndpointConfig(base_url="http://127.0.0.1:8787//").normalized_base_url,
            "http://127.0.0.1:8787",
        )
        self.assertEqual(EndpointConfig().normalized_base_url, "")
        self.assertFalse(EndpointConfig().configured)
        self.assertTrue(EndpointConfig(base_url="x.com").configured)

    def test_effective_auth_mode(self) -> None:
        self.assertEqual(
            EndpointConfig(auth_mode=AUTH_MODE_TOKEN, api_token="t").effective_auth_mode,
            AUTH_MODE_TOKEN,
        )
        self.assertEqual(
            EndpointConfig(auth_mode=AUTH_MODE_AUTH_CODE, auth_code="c").effective_auth_mode,
            AUTH_MODE_AUTH_CODE,
        )
        # 选了 token 但只填了 authCode，自动降级到 auth_code
        self.assertEqual(
            EndpointConfig(auth_mode=AUTH_MODE_TOKEN, auth_code="c").effective_auth_mode,
            AUTH_MODE_AUTH_CODE,
        )
        self.assertEqual(
            EndpointConfig(auth_mode=AUTH_MODE_AUTH_CODE, api_token="t").effective_auth_mode,
            AUTH_MODE_TOKEN,
        )
        self.assertEqual(EndpointConfig().effective_auth_mode, AUTH_MODE_NONE)

    def test_timeout_clamped_by_from_mapping(self) -> None:
        cfg = FerryConfig.from_mapping({"endpoint": {"timeout_seconds": 5}})
        self.assertAlmostEqual(cfg.endpoint.timeout_seconds, 10.0)
        cfg = FerryConfig.from_mapping({"endpoint": {"timeout_seconds": 5000}})
        self.assertAlmostEqual(cfg.endpoint.timeout_seconds, 900.0)


class UploadConfigTest(unittest.TestCase):
    def test_byte_helpers(self) -> None:
        cfg = UploadConfig(max_file_mb=2.0, max_total_mb=10.0)
        self.assertEqual(cfg.max_file_bytes, 2 * 1024 * 1024)
        self.assertEqual(cfg.max_total_bytes, 10 * 1024 * 1024)

    def test_allowed_extensions_merges_extra(self) -> None:
        cfg = UploadConfig(extra_allowed_extensions=["psd"])
        allowed = cfg.allowed_extensions()
        self.assertIn("png", allowed)
        self.assertIn("psd", allowed)

    def test_extension_rejection(self) -> None:
        cfg = UploadConfig(blocked_extensions=["exe"])
        self.assertIn("黑名单", cfg.extension_rejection("exe"))
        self.assertEqual(cfg.extension_rejection("png"), "")

        loose = UploadConfig(enforce_extension_whitelist=False)
        self.assertEqual(loose.extension_rejection("xyz"), "")

        strict = UploadConfig(enforce_extension_whitelist=True)
        self.assertIn("无法判定扩展名", strict.extension_rejection(""))
        self.assertIn("白名单", strict.extension_rejection("xyz"))
        self.assertEqual(strict.extension_rejection("PNG"), "")

    def test_url_ingest_rejection_disabled(self) -> None:
        cfg = UploadConfig()
        self.assertIn("allow_url_ingest", cfg.url_ingest_rejection("https://a.com/x.png"))

    def test_url_ingest_rejection_scheme_and_host(self) -> None:
        cfg = UploadConfig(allow_url_ingest=True)
        self.assertIn("http / https", cfg.url_ingest_rejection("ftp://a.com/x.png"))
        self.assertIn("主机名", cfg.url_ingest_rejection("https:///x.png"))

    def test_url_ingest_rejection_private_targets(self) -> None:
        cfg = UploadConfig(allow_url_ingest=True)
        self.assertIn("本机", cfg.url_ingest_rejection("http://localhost/x.png"))
        self.assertIn("本机", cfg.url_ingest_rejection("http://api.localhost/x.png"))
        for url in (
            "http://127.0.0.1/x.png",
            "http://10.0.0.5/x.png",
            "http://192.168.1.1/x.png",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/x.png",
        ):
            self.assertIn("内网或保留地址", cfg.url_ingest_rejection(url), msg=url)

    def test_url_ingest_allowlist(self) -> None:
        cfg = UploadConfig(allow_url_ingest=True, url_ingest_allowlist=["example.com"])
        self.assertEqual(cfg.url_ingest_rejection("https://example.com/a.png"), "")
        self.assertEqual(cfg.url_ingest_rejection("https://cdn.example.com/a.png"), "")
        self.assertIn("白名单", cfg.url_ingest_rejection("https://evil.com/a.png"))
        self.assertIn("白名单", cfg.url_ingest_rejection("https://notexample.com/a.png"))


class FerryConfigTest(unittest.TestCase):
    def test_from_mapping_nested(self) -> None:
        cfg = FerryConfig.from_mapping(
            {
                "endpoint": {"base_url": "img.example.com", "api_token": "tok"},
                "upload": {"max_files": "9", "blocked_extensions": ".EXE, bat"},
                "compress": {"enabled": "关"},
                "archive": {"max_entries": 99999},
                "behavior": {"output_format": "MD"},
                "permission": {"group_whitelist": "1，2"},
                "debug": "是",
            }
        )
        self.assertEqual(cfg.endpoint.normalized_base_url, "https://img.example.com")
        self.assertEqual(cfg.endpoint.effective_auth_mode, AUTH_MODE_TOKEN)
        self.assertEqual(cfg.upload.max_files, 9)
        self.assertEqual(cfg.upload.blocked_extensions, ["exe", "bat"])
        self.assertFalse(cfg.compress.enabled)
        self.assertEqual(cfg.archive.max_entries, 2000)
        self.assertEqual(cfg.behavior.output_format, "markdown")
        self.assertEqual(cfg.permission.group_whitelist, ["1", "2"])
        self.assertTrue(cfg.debug)

    def test_from_mapping_empty_equals_defaults(self) -> None:
        self.assertEqual(FerryConfig.from_mapping({}), FerryConfig())
        self.assertEqual(FerryConfig.from_mapping(None), FerryConfig())


class SchemaConsistencyTest(unittest.TestCase):
    """防止 _conf_schema.json 与 dataclass 默认值悄悄跑偏。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_schema_defaults_match_dataclass_defaults(self) -> None:
        parsed = FerryConfig.from_mapping(schema_defaults(self.schema))
        self.assertEqual(parsed, FerryConfig())

    def test_schema_covers_every_dataclass_field(self) -> None:
        expected = {
            "endpoint": FerryConfig().endpoint,
            "upload": FerryConfig().upload,
            "compress": FerryConfig().compress,
            "archive": FerryConfig().archive,
            "behavior": FerryConfig().behavior,
            "permission": FerryConfig().permission,
            "integration": FerryConfig().integration,
        }
        for name, block in expected.items():
            with self.subTest(section=name):
                self.assertIn(name, self.schema)
                items = self.schema[name].get("items", {})
                names = {f.name for f in dataclasses.fields(block)}
                self.assertEqual(set(items), names)

    def test_every_leaf_has_description(self) -> None:
        def walk(node: dict, prefix: str = "") -> None:
            for key, spec in node.items():
                path = f"{prefix}{key}"
                self.assertIsInstance(spec, dict, msg=path)
                self.assertIn("description", spec, msg=path)
                self.assertTrue(str(spec["description"]).strip(), msg=path)
                if "items" in spec:
                    self.assertEqual(spec.get("type"), "object", msg=path)
                    walk(spec["items"], f"{path}.")
                else:
                    self.assertIn("type", spec, msg=path)
                    self.assertIn("default", spec, msg=path)

        walk(self.schema)


if __name__ == "__main__":
    unittest.main()
