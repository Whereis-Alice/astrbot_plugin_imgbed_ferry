"""main.py 里模块级工具函数的测试：指令拆词、时间戳、外链素材、参数解析。

main.py 用的是包内相对导入，所以这里必须把插件目录的父目录塞进 sys.path，
再按 `astrbot_plugin_imgbed_ferry.main` 导入，不能像其它测试那样直接
`from imgbed_ferry import ...`。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT.parent))

from astrbot_plugin_imgbed_ferry import main  # noqa: E402
from astrbot_plugin_imgbed_ferry.imgbed_ferry.collector import (  # noqa: E402
    KIND_FILE,
    KIND_IMAGE,
)
from astrbot_plugin_imgbed_ferry.imgbed_ferry.tools import TOOL_CLASSES  # noqa: E402


def _event(message_str: Any) -> SimpleNamespace:
    """`_tokens` 只读 `message_str`，最小替身就够。"""
    return SimpleNamespace(message_str=message_str)


class TokensTest(unittest.TestCase):
    """`_tokens` 折叠空白、丢掉指令名，剩下的原样交给 `_parse_upload_args`。"""

    def test_basic_split(self) -> None:
        self.assertEqual(
            main._tokens(_event("/图床上传 最新 -f wall")),
            ["最新", "-f", "wall"],
        )

    def test_whitespace_is_collapsed(self) -> None:
        messy = "  /图床上传   最新\t\t-f   wall  "
        self.assertEqual(main._tokens(_event(messy)), ["最新", "-f", "wall"])

    def test_full_width_space_counts_as_whitespace(self) -> None:
        # 中文输入法下很容易打出全角空格，\s+ 在 str 模式下认它。
        self.assertEqual(main._tokens(_event("/图床上传\u3000最新")), ["最新"])

    def test_command_only(self) -> None:
        self.assertEqual(main._tokens(_event("/图床上传")), [])

    def test_blank_or_missing_payload(self) -> None:
        for value in ("", "   ", None, 0):
            with self.subTest(value=repr(value)):
                self.assertEqual(main._tokens(_event(value)), [])

    def test_event_without_attribute(self) -> None:
        self.assertEqual(main._tokens(SimpleNamespace()), [])

    def test_non_string_payload(self) -> None:
        # str(123) 拆不出第二段，等价于「只有指令名」。
        self.assertEqual(main._tokens(_event(123)), [])

    def test_token_case_is_preserved(self) -> None:
        # 大小写归一化是 `_parse_upload_args` 的事，拆词阶段不动原文。
        self.assertEqual(main._tokens(_event("/ibup --Folder=Wall")), ["--Folder=Wall"])


class StampTest(unittest.TestCase):
    """`_stamp` 只服务于展示，坏数据必须回落文案而不是抛异常。"""

    TS = 1767225600

    def test_default_pattern(self) -> None:
        expected = time.strftime("%m-%d %H:%M", time.localtime(self.TS))
        self.assertEqual(main._stamp(self.TS), expected)

    def test_custom_pattern(self) -> None:
        expected = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.TS))
        self.assertEqual(main._stamp(self.TS, "%Y-%m-%d %H:%M:%S"), expected)

    def test_numeric_string_matches_number(self) -> None:
        self.assertEqual(main._stamp(str(self.TS)), main._stamp(self.TS))

    def test_fraction_is_truncated(self) -> None:
        self.assertEqual(main._stamp(self.TS + 0.7), main._stamp(self.TS))

    def test_bad_values_fall_back(self) -> None:
        for value in (None, "", "  ", "abc", [], {}, object(), float("nan"), 1e30):
            with self.subTest(value=repr(value)):
                self.assertEqual(main._stamp(value), "未知时间")


class MaterialFromUrlTest(unittest.TestCase):
    """外链只做包装，下载和体积校验都留给 UploadService。"""

    def test_image_extension_is_case_insensitive(self) -> None:
        url = "https://cdn.example.com/a/b/pic.PNG"
        material = main._material_from_url(url)
        self.assertEqual(material.kind, KIND_IMAGE)
        self.assertEqual(material.name, "pic.PNG")
        self.assertEqual(material.source, main.SOURCE_LINK)
        self.assertEqual(material.url, url)
        self.assertEqual(material.data, b"")
        self.assertEqual(material.path, "")
        self.assertTrue(material.resolvable)

    def test_archive_is_plain_file(self) -> None:
        material = main._material_from_url("https://cdn.example.com/a/pack.zip")
        self.assertEqual(material.kind, KIND_FILE)
        self.assertEqual(material.name, "pack.zip")

    def test_query_string_is_stripped_from_name(self) -> None:
        material = main._material_from_url("https://cdn.example.com/a/pic.jpg?token=1&x=2")
        self.assertEqual(material.name, "pic.jpg")
        self.assertEqual(material.kind, KIND_IMAGE)

    def test_nameless_url_falls_back(self) -> None:
        material = main._material_from_url("https://cdn.example.com/files/abcdef123456")
        self.assertEqual(material.name, "download")
        self.assertEqual(material.kind, KIND_FILE)


class ParseUploadArgsTest(unittest.TestCase):
    """`/图床上传` 的自由文本解析。顺序敏感，逐条钉住。"""

    def test_defaults(self) -> None:
        self.assertEqual(
            main._parse_upload_args([]),
            {
                "target": "auto",
                "indexes": [],
                "item_ids": [],
                "urls": [],
                "folder": "",
                "style": "",
                "name_type": "",
                "extract": None,
                "compress": None,
            },
        )

    def test_short_value_flags(self) -> None:
        args = main._parse_upload_args(["-f", "wall/2026", "-o", "plain", "-n", "index"])
        self.assertEqual(args["folder"], "wall/2026")
        self.assertEqual(args["style"], "plain")
        # `-n index` 里的 index 是命名规则，不能被 TARGET_MODES 抢走。
        self.assertEqual(args["name_type"], "index")
        self.assertEqual(args["target"], "auto")

    def test_long_flags_with_equals(self) -> None:
        args = main._parse_upload_args(["--folder=wall", "--format=json", "--name=origin"])
        self.assertEqual(args["folder"], "wall")
        self.assertEqual(args["style"], "json")
        self.assertEqual(args["name_type"], "origin")

    def test_alias_flags_with_equals(self) -> None:
        args = main._parse_upload_args(["--dir=w", "--output=urls"])
        self.assertEqual(args["folder"], "w")
        self.assertEqual(args["style"], "urls")

    def test_chinese_value_flags(self) -> None:
        args = main._parse_upload_args(["目录", "wall", "格式", "plain", "命名", "index"])
        self.assertEqual(args["folder"], "wall")
        self.assertEqual(args["style"], "plain")
        self.assertEqual(args["name_type"], "index")

    def test_flags_are_case_insensitive(self) -> None:
        args = main._parse_upload_args(["-F", "wall", "--FORMAT=plain"])
        self.assertEqual(args["folder"], "wall")
        self.assertEqual(args["style"], "plain")

    def test_value_flag_table_space_form(self) -> None:
        for token, key in main._VALUE_FLAGS.items():
            with self.subTest(token=token, form="space"):
                self.assertEqual(main._parse_upload_args([token, "zz"])[key], "zz")

    def test_value_flag_table_equals_form(self) -> None:
        for token, key in main._VALUE_FLAGS.items():
            with self.subTest(token=token, form="equals"):
                self.assertEqual(main._parse_upload_args([f"{token}=zz"])[key], "zz")

    def test_dangling_value_flag_is_ignored(self) -> None:
        args = main._parse_upload_args(["-f"])
        self.assertEqual(args["folder"], "")
        self.assertEqual(args["item_ids"], [])
        self.assertEqual(args["target"], "auto")

    def test_pending_flag_swallows_next_token(self) -> None:
        # 已知取舍：`-f` 后面紧跟开关时，开关被当成目录名吃掉。
        args = main._parse_upload_args(["-f", "--no-extract"])
        self.assertEqual(args["folder"], "--no-extract")
        self.assertIsNone(args["extract"])

    def test_empty_equals_value_is_not_consumed(self) -> None:
        # tail 为空时不算赋值，token 原样落进 item_ids。
        args = main._parse_upload_args(["--folder="])
        self.assertEqual(args["folder"], "")
        self.assertEqual(args["item_ids"], ["--folder="])
        self.assertEqual(args["target"], "index")

    def test_target_modes_are_accepted(self) -> None:
        for mode in main.TARGET_MODES:
            with self.subTest(mode=mode):
                self.assertEqual(main._parse_upload_args([mode])["target"], mode)

    def test_target_mode_is_case_insensitive(self) -> None:
        self.assertEqual(main._parse_upload_args(["LATEST"])["target"], "latest")

    def test_target_alias_table(self) -> None:
        for token, mode in main._TARGET_ALIASES.items():
            with self.subTest(token=token):
                self.assertEqual(main._parse_upload_args([token])["target"], mode)

    def test_switch_flag_table(self) -> None:
        for token, (key, value) in main._SWITCH_FLAGS.items():
            other = "compress" if key == "extract" else "extract"
            with self.subTest(token=token):
                args = main._parse_upload_args([token])
                self.assertEqual(args[key], value)
                self.assertIsNone(args[other])

    def test_indexes(self) -> None:
        args = main._parse_upload_args(["1", "#3", "-1"])
        self.assertEqual(args["indexes"], [1, 3, -1])
        self.assertEqual(args["target"], "index")

    def test_signed_and_hashed_indexes(self) -> None:
        args = main._parse_upload_args(["+2", "#-1", "9999"])
        self.assertEqual(args["indexes"], [2, -1, 9999])

    def test_long_digits_are_item_ids(self) -> None:
        # _INDEX_TOKEN 只吃 1~4 位，更长的当成素材 id。
        args = main._parse_upload_args(["12345"])
        self.assertEqual(args["indexes"], [])
        self.assertEqual(args["item_ids"], ["12345"])
        self.assertEqual(args["target"], "index")

    def test_unknown_token_becomes_item_id(self) -> None:
        args = main._parse_upload_args(["--nope"])
        self.assertEqual(args["item_ids"], ["--nope"])
        self.assertEqual(args["target"], "index")

    def test_urls(self) -> None:
        tokens = ["https://a.com/x.png", "http://b.com/y.zip"]
        args = main._parse_upload_args(tokens)
        self.assertEqual(args["urls"], tokens)
        self.assertEqual(args["folder"], "")
        # 只给外链时不算按序号取件，target 保持 auto。
        self.assertEqual(args["target"], "auto")

    def test_url_scheme_is_case_insensitive_but_value_is_kept(self) -> None:
        args = main._parse_upload_args(["HTTPS://A.com/X.PNG"])
        self.assertEqual(args["urls"], ["HTTPS://A.com/X.PNG"])

    def test_url_with_query_equals(self) -> None:
        # head 不是已知选项，所以带 = 的外链不会被误当成赋值。
        args = main._parse_upload_args(["https://a.com/x.png?token=1"])
        self.assertEqual(args["urls"], ["https://a.com/x.png?token=1"])
        self.assertEqual(args["item_ids"], [])

    def test_bare_path_is_folder(self) -> None:
        args = main._parse_upload_args(["wall/2026"])
        self.assertEqual(args["folder"], "wall/2026")
        self.assertEqual(args["target"], "auto")

    def test_second_path_is_item_id(self) -> None:
        args = main._parse_upload_args(["wall/2026", "a/b"])
        self.assertEqual(args["folder"], "wall/2026")
        self.assertEqual(args["item_ids"], ["a/b"])
        self.assertEqual(args["target"], "index")

    def test_index_overrides_target_mode(self) -> None:
        args = main._parse_upload_args(["全部", "2"])
        self.assertEqual(args["indexes"], [2])
        self.assertEqual(args["target"], "index")

    def test_realistic_mixed_command(self) -> None:
        args = main._parse_upload_args(
            ["引用", "--no-extract", "压缩", "-f", "wall/{date}", "--format=plain"]
        )
        self.assertEqual(args["target"], "reply")
        self.assertFalse(args["extract"])
        self.assertTrue(args["compress"])
        self.assertEqual(args["folder"], "wall/{date}")
        self.assertEqual(args["style"], "plain")
        self.assertEqual(args["indexes"], [])
        self.assertEqual(args["item_ids"], [])

    def test_last_value_flag_wins(self) -> None:
        args = main._parse_upload_args(["-f", "a", "--folder=b"])
        self.assertEqual(args["folder"], "b")

    def test_result_lists_are_independent(self) -> None:
        first = main._parse_upload_args(["1"])
        second = main._parse_upload_args([])
        first["indexes"].append(99)
        self.assertEqual(second["indexes"], [])


class ConstantsTest(unittest.TestCase):
    """常量之间的一致性，改名或改版本时这些断言会先炸。"""

    def test_plugin_identity(self) -> None:
        self.assertEqual(main.PLUGIN_ID, "astrbot_plugin_imgbed_ferry")
        self.assertEqual(main.PLUGIN_ID, _PLUGIN_ROOT.name)
        self.assertTrue(main.PLUGIN_REPO.startswith("https://github.com/"))
        self.assertTrue(main.PLUGIN_REPO.endswith(main.PLUGIN_ID))

    def test_version_matches_metadata(self) -> None:
        text = (_PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn(f"version: v{main.PLUGIN_VERSION}", text)

    def test_changelog_mentions_version(self) -> None:
        text = (_PLUGIN_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(main.PLUGIN_VERSION, text)

    def test_target_aliases_resolve_to_known_modes(self) -> None:
        for token, mode in main._TARGET_ALIASES.items():
            with self.subTest(token=token):
                self.assertIn(mode, main.TARGET_MODES)

    def test_value_flag_keys_exist_in_parse_result(self) -> None:
        keys = set(main._parse_upload_args([]))
        for token, key in main._VALUE_FLAGS.items():
            with self.subTest(token=token):
                self.assertIn(key, keys)

    def test_tool_switch_table_covers_every_tool(self) -> None:
        names = [tool_class().name for tool_class in TOOL_CLASSES]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), set(main._TOOL_SWITCHES))

    def test_limits_are_sane(self) -> None:
        self.assertGreater(main.MAX_DELETE_TARGETS, 0)
        self.assertGreater(main.MAX_BATCH_SESSIONS, 0)
        self.assertGreaterEqual(main.MAX_PREVIEW_IMAGES, 1)
        self.assertIn("aiocqhttp", main.FORWARD_PLATFORMS)

    def test_hints_are_non_empty(self) -> None:
        self.assertTrue(main.NO_MATERIAL_HINT.strip())
        self.assertTrue(main.RELAY_HINT.strip())


if __name__ == "__main__":
    unittest.main()
