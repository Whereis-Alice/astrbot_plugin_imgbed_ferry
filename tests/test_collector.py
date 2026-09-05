"""`imgbed_ferry.collector` 素材收集测试。

重点覆盖两件事：

1. 组件链 / 引用消息 / 原始段三个来源的合并与去重；
2. 适配器丢掉 `File` 组件时，原始段兜底能不能把文件捞回来。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from imgbed_ferry import collector
from imgbed_ferry.collector import (
    KIND_FILE,
    KIND_IMAGE,
    PAYLOAD_KEEP_KEYS,
    SOURCE_MESSAGE,
    SOURCE_RAW,
    SOURCE_REPLY,
    SOURCE_SESSION,
    Material,
    collect_materials,
    component_type,
    material_from_component,
    material_from_segment,
    name_from_url,
    payload_name,
    trim_payload,
)
from imgbed_ferry.media import short_hash


def seg(seg_type: str, **data: Any) -> dict[str, Any]:
    """造一个 OneBot 消息段。"""
    return {"type": seg_type, "data": dict(data)}


class TypeValue:
    """模拟 `ComponentType` 这类 str 枚举：真正的类型名在 `.value` 上。"""

    def __init__(self, value: str) -> None:
        self.value = value


class FakeImage:
    type = "Image"

    def __init__(self, *, url: str = "", file: str = "", path: str = "") -> None:
        self.url = url
        self.file = file
        self.path = path


class FakeFile:
    type = "File"

    def __init__(self, *, file_: str = "", name: str = "", url: str = "") -> None:
        self.file_ = file_
        self.name = name
        self.url = url

    @property
    def file(self) -> str:
        # 真实的 `File.file` 在异步上下文里会返回空串并打 warning，收集阶段不该碰它。
        raise AssertionError("File.file 不应在收集阶段被读取")


class FakeReply:
    type = "Reply"

    def __init__(self, *, message_id: Any = "", chain: list[Any] | None = None) -> None:
        self.id = message_id
        self.chain = list(chain or [])


class FakePlain:
    type = "Plain"

    def __init__(self, text: str = "") -> None:
        self.text = text


class FakeBot:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = dict(responses or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_action(self, action: str, **params: Any) -> Any:
        self.calls.append((action, dict(params)))
        if action not in self.responses:
            raise RuntimeError(f"未实现的 action: {action}")
        value = self.responses[action]
        if isinstance(value, Exception):
            raise value
        return value


class FakeEvent:
    def __init__(
        self,
        *,
        chain: list[Any] | None = None,
        raw: Any = None,
        responses: dict[str, Any] | None = None,
        chain_error: bool = False,
    ) -> None:
        self._chain = list(chain or [])
        self._chain_error = chain_error
        self.message_obj = SimpleNamespace(raw_message=raw)
        self.bot = FakeBot(responses)

    def get_messages(self) -> list[Any]:
        if self._chain_error:
            raise RuntimeError("组件链不可用")
        return list(self._chain)


class ComponentTypeTest(unittest.TestCase):
    def test_plain_string_type(self) -> None:
        self.assertEqual(component_type(FakeImage()), "Image")

    def test_enum_like_type(self) -> None:
        self.assertEqual(component_type(SimpleNamespace(type=TypeValue("File"))), "File")

    def test_missing_or_empty(self) -> None:
        for component in (object(), SimpleNamespace(type=None), SimpleNamespace(type="")):
            with self.subTest(component=component):
                self.assertEqual(component_type(component), "")


class PayloadNameTest(unittest.TestCase):
    def test_key_priority(self) -> None:
        payload = {"file": "z.bin", "filename": "y.bin", "name": "b.png", "file_name": "a.png"}
        self.assertEqual(payload_name(payload), "a.png")

    def test_falls_through_blank_and_non_string(self) -> None:
        self.assertEqual(payload_name({"file_name": "   ", "name": 123, "file": "c.png"}), "c.png")

    def test_strips_directories(self) -> None:
        self.assertEqual(payload_name({"name": "C:\\data\\sub\\a.png"}), "a.png")
        self.assertEqual(payload_name({"name": "sub/dir/c.png"}), "c.png")

    def test_skips_url_and_inline_values(self) -> None:
        cases = (
            {"name": "https://cdn.example.com/a.png", "file": "b.png"},
            {"name": "HTTP://cdn.example.com/a.png", "file": "b.png"},
            {"name": "base64://QUJD", "file": "b.png"},
            {"name": "data:image/png;base64,QUJD", "file": "b.png"},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(payload_name(payload), "b.png")

    def test_inline_only_payload_has_no_name(self) -> None:
        self.assertEqual(payload_name({"file": "base64://QUJD"}), "")
        self.assertEqual(payload_name({}), "")


class NameFromUrlTest(unittest.TestCase):
    def test_takes_tail_with_extension(self) -> None:
        self.assertEqual(name_from_url("https://cdn.example.com/a/b.png"), "b.png")

    def test_drops_query_and_fragment(self) -> None:
        self.assertEqual(name_from_url("https://cdn.example.com/a/b.png?sig=1#x"), "b.png")

    def test_ignores_trailing_slash(self) -> None:
        self.assertEqual(name_from_url("https://cdn.example.com/a/b.png/"), "b.png")

    def test_rejects_names_without_extension(self) -> None:
        cases = (
            "https://cdn.example.com/8f3a9c2b7d",
            "https://cdn.example.com/.png",
            "https://cdn.example.com/b.verylongextension",
            "not-a-url/a.png",
            "",
        )
        for url in cases:
            with self.subTest(url=url):
                self.assertEqual(name_from_url(url), "")


class InlineBase64Test(unittest.TestCase):
    def test_base64_prefix(self) -> None:
        self.assertEqual(collector._decode_inline_base64("base64://aGVsbG8="), b"hello")

    def test_data_uri(self) -> None:
        self.assertEqual(
            collector._decode_inline_base64("data:image/png;base64,aGVsbG8="), b"hello"
        )

    def test_tolerates_surrounding_whitespace(self) -> None:
        self.assertEqual(collector._decode_inline_base64("  base64://aGVsbG8=  "), b"hello")

    def test_rejects_other_shapes(self) -> None:
        for value in ("aGVsbG8=", "data:image/png", "file:///tmp/a.png", ""):
            with self.subTest(value=value):
                self.assertEqual(collector._decode_inline_base64(value), b"")

    def test_broken_payload_returns_empty(self) -> None:
        self.assertEqual(collector._decode_inline_base64("base64://a"), b"")


class TrimPayloadTest(unittest.TestCase):
    def test_keeps_only_whitelisted_keys(self) -> None:
        payload = {
            "file_id": "F1",
            "file_name": "a.zip",
            "url": "https://cdn.example.com/a.zip",
            "extra": {"deep": "value"},
        }
        self.assertEqual(trim_payload(payload), {"file_id": "F1", "file_name": "a.zip"})

    def test_drops_none_and_empty_string(self) -> None:
        trimmed = trim_payload({"file_id": None, "name": "", "file": "a.png"})
        self.assertEqual(trimmed, {"file": "a.png"})

    def test_keeps_zero_size(self) -> None:
        self.assertEqual(trim_payload({"size": 0}), {"size": 0})

    def test_result_is_detached_copy(self) -> None:
        payload = {"file_id": "F1"}
        trimmed = trim_payload(payload)
        trimmed["file_id"] = "changed"
        self.assertEqual(payload["file_id"], "F1")

    def test_whitelist_is_stable(self) -> None:
        self.assertEqual(
            PAYLOAD_KEEP_KEYS,
            ("file_id", "file", "file_name", "name", "busid", "group_id", "user_id", "size"),
        )


class MaterialTest(unittest.TestCase):
    def test_defaults(self) -> None:
        material = Material()
        self.assertEqual(material.kind, KIND_FILE)
        self.assertEqual(material.source, SOURCE_MESSAGE)
        self.assertEqual(material.payload, {})
        self.assertTrue(material.material_id)

    def test_size_derived_from_data(self) -> None:
        self.assertEqual(Material(data=b"abcd").size, 4)

    def test_explicit_size_wins(self) -> None:
        self.assertEqual(Material(data=b"abc", size=99).size, 99)

    def test_signature_priority(self) -> None:
        material = Material(url="https://cdn/a.png", path="/tmp/a.png", file_id="F1")
        self.assertEqual(material.signature, "https://cdn/a.png")
        self.assertEqual(Material(path="/tmp/a.png", file_id="F1").signature, "/tmp/a.png")
        self.assertEqual(Material(file_id="F1").signature, "F1")

    def test_inline_signature(self) -> None:
        data = b"abcd"
        expected = f"inline:4:{short_hash(str(data[:64]))}"
        self.assertEqual(Material(data=data).signature, expected)

    def test_inline_signature_only_reads_head(self) -> None:
        head = b"x" * 64
        self.assertNotEqual(Material(data=head).signature, Material(data=head + b"y").signature)

    def test_descriptive_signature_fallback(self) -> None:
        self.assertEqual(Material(kind=KIND_FILE, name="a.txt", size=7).signature, "file:a.txt:7")

    def test_id_prefix_by_kind_and_extension(self) -> None:
        cases = {
            "img": Material(kind=KIND_IMAGE, name="a.png", url="https://cdn/a.png"),
            "arc": Material(name="a.zip", url="https://cdn/a.zip"),
            "doc": Material(name="a.txt", url="https://cdn/a.txt"),
        }
        for prefix, material in cases.items():
            with self.subTest(prefix=prefix):
                self.assertTrue(material.material_id.startswith(f"{prefix}_"))

    def test_image_kind_beats_archive_extension(self) -> None:
        self.assertTrue(Material(kind=KIND_IMAGE, name="a.zip").material_id.startswith("img_"))

    def test_explicit_id_is_kept(self) -> None:
        self.assertEqual(Material(name="a.png", material_id="fixed").material_id, "fixed")

    def test_resolvable_matrix(self) -> None:
        cases = (
            (Material(url="https://cdn/a.png"), True),
            (Material(path="/tmp/a.png"), True),
            (Material(data=b"abc"), True),
            (Material(file_id="F1"), True),
            (Material(component=FakeImage()), True),
            (Material(name="a.png"), False),
        )
        for material, expected in cases:
            with self.subTest(signature=material.signature):
                self.assertIs(material.resolvable, expected)

    def test_cacheable_needs_a_portable_handle(self) -> None:
        cases = (
            (Material(url="https://cdn/a.png"), True),
            (Material(path="/tmp/a.png"), True),
            (Material(file_id="F1"), True),
            (Material(data=b"abc"), False),
            (Material(component=FakeImage()), False),
        )
        for material, expected in cases:
            with self.subTest(signature=material.signature):
                self.assertIs(material.cacheable, expected)

    def test_display_name(self) -> None:
        self.assertEqual(Material(name="a.png").display_name(), "a.png")
        self.assertEqual(Material(kind=KIND_IMAGE).display_name(), "image")
        self.assertEqual(Material(kind=KIND_FILE).display_name(), "file")


class DescriptorTest(unittest.TestCase):
    def sample(self) -> Material:
        return Material(
            kind=KIND_IMAGE,
            name="a.png",
            source=SOURCE_REPLY,
            url="https://cdn.example.com/a.png",
            path="/tmp/a.png",
            size=12,
            mime="image/png",
            file_id="F1",
            payload={"file_id": "F1"},
            component=FakeImage(),
        )

    def test_descriptor_keys(self) -> None:
        descriptor = self.sample().descriptor()
        self.assertEqual(
            set(descriptor),
            {
                "material_id",
                "kind",
                "name",
                "source",
                "url",
                "path",
                "size",
                "mime",
                "file_id",
                "payload",
            },
        )

    def test_descriptor_drops_bytes_and_component(self) -> None:
        descriptor = self.sample().descriptor()
        self.assertNotIn("data", descriptor)
        self.assertNotIn("component", descriptor)

    def test_descriptor_payload_is_copied(self) -> None:
        material = self.sample()
        descriptor = material.descriptor()
        descriptor["payload"]["file_id"] = "changed"
        self.assertEqual(material.payload["file_id"], "F1")

    def test_round_trip(self) -> None:
        material = self.sample()
        restored = Material.from_descriptor(material.descriptor())
        self.assertEqual(restored, material)
        self.assertEqual(restored.material_id, material.material_id)
        self.assertIsNone(restored.component)
        self.assertEqual(restored.data, b"")

    def test_from_descriptor_defaults(self) -> None:
        restored = Material.from_descriptor({})
        self.assertEqual(restored.kind, KIND_FILE)
        self.assertEqual(restored.source, SOURCE_SESSION)
        self.assertEqual(restored.payload, {})
        self.assertTrue(restored.material_id.startswith("doc_"))

    def test_from_descriptor_coerces_types(self) -> None:
        raw = {"size": "12", "payload": "not-a-mapping", "url": None}
        restored = Material.from_descriptor(raw)
        self.assertEqual(restored.size, 12)
        self.assertEqual(restored.payload, {})
        self.assertEqual(restored.url, "")


class MaterialFromComponentTest(unittest.TestCase):
    def test_rejects_other_components(self) -> None:
        for component in (FakePlain("hi"), FakeReply(), object()):
            with self.subTest(component=component):
                self.assertIsNone(material_from_component(component, source=SOURCE_MESSAGE))

    def test_image_url(self) -> None:
        material = material_from_component(
            FakeImage(url="https://cdn.example.com/a/b.png"), source=SOURCE_MESSAGE
        )
        self.assertIsNotNone(material)
        assert material is not None
        self.assertEqual(material.kind, KIND_IMAGE)
        self.assertEqual(material.url, "https://cdn.example.com/a/b.png")
        self.assertEqual(material.name, "b.png")
        self.assertEqual(material.source, SOURCE_MESSAGE)

    def test_image_url_from_file_field(self) -> None:
        material = material_from_component(
            FakeImage(file=" https://cdn.example.com/c.jpg "), source=SOURCE_REPLY
        )
        assert material is not None
        self.assertEqual(material.url, "https://cdn.example.com/c.jpg")
        self.assertEqual(material.name, "c.jpg")

    def test_image_inline_base64(self) -> None:
        material = material_from_component(FakeImage(file="base64://aGVsbG8="), source=SOURCE_RAW)
        assert material is not None
        self.assertEqual(material.data, b"hello")
        self.assertEqual(material.url, "")
        self.assertEqual(material.size, 5)
        self.assertEqual(material.name, "")
        self.assertFalse(material.cacheable)
        self.assertTrue(material.resolvable)

    def test_image_local_path(self) -> None:
        material = material_from_component(FakeImage(path="/tmp/pic.png"), source=SOURCE_MESSAGE)
        assert material is not None
        self.assertEqual(material.path, "/tmp/pic.png")
        self.assertEqual(material.url, "")

    def test_image_http_path_is_ignored(self) -> None:
        material = material_from_component(
            FakeImage(url="https://cdn.example.com/a.png", path="https://cdn.example.com/a.png"),
            source=SOURCE_MESSAGE,
        )
        assert material is not None
        self.assertEqual(material.path, "")

    def test_image_file_uri(self) -> None:
        cases = {
            "file:///data/a.png": "/data/a.png",
            "file:///C:/pics/a.png": "C:/pics/a.png",
            "file://host/a.png": "host/a.png",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                material = material_from_component(FakeImage(file=raw), source=SOURCE_MESSAGE)
                assert material is not None
                self.assertEqual(material.path, expected)

    def test_file_component_url(self) -> None:
        material = material_from_component(
            FakeFile(file_="https://cdn.example.com/pack.zip", name="pack.zip"),
            source=SOURCE_MESSAGE,
        )
        assert material is not None
        self.assertEqual(material.kind, KIND_FILE)
        self.assertEqual(material.url, "https://cdn.example.com/pack.zip")
        self.assertEqual(material.path, "")
        self.assertEqual(material.name, "pack.zip")
        self.assertTrue(material.material_id.startswith("arc_"))

    def test_file_component_path(self) -> None:
        material = material_from_component(FakeFile(file_="/data/pack.zip"), source=SOURCE_REPLY)
        assert material is not None
        self.assertEqual(material.path, "/data/pack.zip")
        self.assertEqual(material.url, "")
        self.assertEqual(material.name, "")

    def test_file_name_is_sanitized(self) -> None:
        material = material_from_component(
            FakeFile(file_="/data/x", name="../../etc/passwd"), source=SOURCE_MESSAGE
        )
        assert material is not None
        self.assertEqual(material.name, "passwd")

    def test_file_name_falling_back_to_blank(self) -> None:
        material = material_from_component(FakeFile(file_="/data/x", name=".."), source=SOURCE_RAW)
        assert material is not None
        self.assertEqual(material.name, "")

    def test_component_reference_is_kept(self) -> None:
        component = FakeImage(url="https://cdn.example.com/a.png")
        material = material_from_component(component, source=SOURCE_MESSAGE)
        assert material is not None
        self.assertIs(material.component, component)


class MaterialFromSegmentTest(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def test_rejects_other_segment_types(self) -> None:
        for segment in (seg("text", text="hi"), seg("reply", id="1"), {"data": {}}):
            with self.subTest(segment=segment):
                self.assertIsNone(material_from_segment(segment, source=SOURCE_RAW))

    def test_image_segment(self) -> None:
        material = material_from_segment(
            seg("image", file="a.png", file_id="IMG-1", url="https://cdn.example.com/a.png"),
            source=SOURCE_RAW,
        )
        assert material is not None
        self.assertEqual(material.kind, KIND_IMAGE)
        self.assertEqual(material.url, "https://cdn.example.com/a.png")
        self.assertEqual(material.file_id, "IMG-1")
        self.assertEqual(material.name, "a.png")
        self.assertEqual(material.payload, {"file": "a.png", "file_id": "IMG-1"})

    def test_file_segment(self) -> None:
        material = material_from_segment(
            seg("file", file_id="FID-1", file_name="report.pdf", size=1234), source=SOURCE_REPLY
        )
        assert material is not None
        self.assertEqual(material.kind, KIND_FILE)
        self.assertEqual(material.name, "report.pdf")
        self.assertEqual(material.file_id, "FID-1")
        self.assertEqual(material.source, SOURCE_REPLY)
        self.assertEqual(material.payload["size"], 1234)

    def test_segment_type_is_case_insensitive(self) -> None:
        material = material_from_segment(seg("IMAGE", file_id="IMG-2"), source=SOURCE_RAW)
        assert material is not None
        self.assertEqual(material.kind, KIND_IMAGE)

    def test_local_path_must_exist(self) -> None:
        real = self.tmp / "on-disk.png"
        real.write_bytes(b"png-bytes")
        material = material_from_segment(
            seg("image", file=str(real), file_name="on-disk.png"), source=SOURCE_RAW
        )
        assert material is not None
        self.assertEqual(material.path, str(real.resolve()))
        missing = material_from_segment(seg("image", file="/no/such/file.png"), source=SOURCE_RAW)
        assert missing is not None
        self.assertEqual(missing.path, "")

    def test_empty_segment_is_unresolvable(self) -> None:
        material = material_from_segment(seg("image"), source=SOURCE_RAW)
        assert material is not None
        self.assertFalse(material.resolvable)


class SegmentPairingTest(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def test_iter_media_segments_filters_and_keeps_order(self) -> None:
        segments = [
            seg("text", text="hi"),
            seg("IMAGE", file_id="A"),
            seg("at", qq="1"),
            seg("File", file_id="B"),
        ]
        pending = collector._iter_media_segments(segments)
        self.assertEqual([kind for kind, _ in pending], ["image", "file"])
        self.assertEqual(pending[0][1]["data"]["file_id"], "A")

    def test_take_file_segment_by_name(self) -> None:
        pending = collector._iter_media_segments(
            [
                seg("file", file_name="other.zip", file_id="A"),
                seg("file", file_name="want.zip", file_id="B"),
            ]
        )
        taken = collector._take_segment(pending, KIND_FILE, "WANT.ZIP")
        self.assertEqual(taken["data"]["file_id"], "B")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][1]["data"]["file_id"], "A")

    def test_take_file_segment_falls_back_to_first(self) -> None:
        pending = collector._iter_media_segments(
            [
                seg("file", file_name="a.zip", file_id="A"),
                seg("file", file_name="b.zip", file_id="B"),
            ]
        )
        taken = collector._take_segment(pending, KIND_FILE, "missing.zip")
        self.assertEqual(taken["data"]["file_id"], "A")
        self.assertEqual(len(pending), 1)

    def test_nameless_file_segment_matches_immediately(self) -> None:
        pending = collector._iter_media_segments([seg("file", file_id="A")])
        taken = collector._take_segment(pending, KIND_FILE, "want.zip")
        self.assertEqual(taken["data"]["file_id"], "A")
        self.assertEqual(pending, [])

    def test_image_segments_ignore_names(self) -> None:
        pending = collector._iter_media_segments([seg("image", file="b.png", file_id="B")])
        taken = collector._take_segment(pending, KIND_IMAGE, "a.png")
        self.assertEqual(taken["data"]["file_id"], "B")

    def test_take_segment_returns_empty_when_no_match(self) -> None:
        pending = collector._iter_media_segments([seg("image", file_id="A")])
        self.assertEqual(collector._take_segment(pending, KIND_FILE, "a.zip"), {})
        self.assertEqual(len(pending), 1)
        self.assertEqual(collector._take_segment([], KIND_IMAGE, ""), {})

    def test_merge_empty_segment_is_noop(self) -> None:
        material = Material(kind=KIND_IMAGE, url="https://cdn.example.com/a.png")
        before = material.material_id
        collector._merge_segment(material, {})
        self.assertEqual(material.material_id, before)
        self.assertEqual(material.payload, {})

    def test_merge_fills_missing_fields(self) -> None:
        material = Material(kind=KIND_FILE, component=FakeFile())
        before = material.material_id
        collector._merge_segment(material, seg("file", file_id="F1", file_name="a.zip", size=12))
        self.assertEqual(material.file_id, "F1")
        self.assertEqual(material.name, "a.zip")
        self.assertEqual(material.size, 12)
        self.assertEqual(material.payload, {"file_id": "F1", "file_name": "a.zip", "size": 12})
        self.assertNotEqual(material.material_id, before)
        self.assertTrue(material.material_id.startswith("arc_"))

    def test_merge_keeps_existing_values(self) -> None:
        material = Material(
            kind=KIND_IMAGE,
            name="keep.png",
            url="https://cdn.example.com/keep.png",
            file_id="OLD",
            size=5,
        )
        collector._merge_segment(
            material,
            seg(
                "image",
                file_id="NEW",
                file="new.png",
                url="https://cdn.example.com/new.png",
                size=9,
            ),
        )
        self.assertEqual(material.file_id, "OLD")
        self.assertEqual(material.name, "keep.png")
        self.assertEqual(material.url, "https://cdn.example.com/keep.png")
        self.assertEqual(material.size, 5)
        self.assertEqual(material.payload, {"file_id": "NEW", "file": "new.png", "size": 9})

    def test_merge_resolves_existing_local_path(self) -> None:
        real = self.tmp / "merged.bin"
        real.write_bytes(b"data")
        material = Material(kind=KIND_FILE, component=FakeFile())
        collector._merge_segment(material, seg("file", path=str(real), file_name="merged.bin"))
        self.assertEqual(material.path, str(real.resolve()))
        self.assertNotIn("path", material.payload)

    def test_merge_tolerates_bad_size(self) -> None:
        material = Material(kind=KIND_FILE, component=FakeFile())
        collector._merge_segment(material, seg("file", file_id="F1", size="约 3 MB"))
        self.assertEqual(material.size, 0)


class CollectMaterialsTest(unittest.IsolatedAsyncioTestCase):
    async def test_component_chain(self) -> None:
        event = FakeEvent(
            chain=[FakePlain("传一下"), FakeImage(url="https://cdn.example.com/a.png")]
        )
        materials = await collect_materials(event)
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0].kind, KIND_IMAGE)
        self.assertEqual(materials[0].name, "a.png")
        self.assertEqual(materials[0].source, SOURCE_MESSAGE)
        self.assertEqual(event.bot.calls, [])

    async def test_raw_file_segment_is_recovered(self) -> None:
        event = FakeEvent(
            chain=[FakePlain("上传这个")],
            raw={"message": [seg("file", file_id="FID-1", file_name="report.pdf", size=1234)]},
        )
        materials = await collect_materials(event)
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0].source, SOURCE_RAW)
        self.assertEqual(materials[0].kind, KIND_FILE)
        self.assertEqual(materials[0].file_id, "FID-1")
        self.assertEqual(materials[0].name, "report.pdf")

    async def test_component_merges_matching_raw_segment(self) -> None:
        event = FakeEvent(
            chain=[FakeImage(url="https://cdn.example.com/a.png")],
            raw={"message": [seg("image", file="a.png", file_id="IMG-1")]},
        )
        materials = await collect_materials(event)
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0].file_id, "IMG-1")
        self.assertEqual(materials[0].url, "https://cdn.example.com/a.png")
        self.assertEqual(materials[0].source, SOURCE_MESSAGE)

    async def test_leftover_raw_segments_come_after_chain(self) -> None:
        event = FakeEvent(
            chain=[FakeImage(url="https://cdn.example.com/a.png")],
            raw={
                "message": [
                    seg("image", file="a.png", file_id="IMG-1"),
                    seg("file", file_id="FID-2", file_name="b.zip"),
                ]
            },
        )
        materials = await collect_materials(event)
        self.assertEqual([m.source for m in materials], [SOURCE_MESSAGE, SOURCE_RAW])
        self.assertEqual([m.kind for m in materials], [KIND_IMAGE, KIND_FILE])

    async def test_file_components_pair_by_name(self) -> None:
        event = FakeEvent(
            chain=[
                FakeFile(file_="/data/a.zip", name="a.zip"),
                FakeFile(file_="/data/b.zip", name="b.zip"),
            ],
            raw={
                "message": [
                    seg("file", file_name="b.zip", file_id="B"),
                    seg("file", file_name="a.zip", file_id="A"),
                ]
            },
        )
        materials = await collect_materials(event)
        pairs = [(m.name, m.file_id) for m in materials]
        self.assertEqual(pairs, [("a.zip", "A"), ("b.zip", "B")])

    async def test_reply_chain_and_extra_segments(self) -> None:
        reply = FakeReply(message_id="9527", chain=[FakeImage(url="https://cdn.example.com/r.png")])
        event = FakeEvent(
            chain=[reply],
            responses={
                "get_msg": {
                    "message": [
                        seg("image", file="r.png", file_id="RIMG"),
                        seg("file", file_id="RFID", file_name="r.zip"),
                    ]
                }
            },
        )
        materials = await collect_materials(event)
        self.assertEqual(len(materials), 2)
        self.assertEqual(materials[0].file_id, "RIMG")
        self.assertEqual(materials[0].url, "https://cdn.example.com/r.png")
        self.assertEqual(materials[1].name, "r.zip")
        self.assertEqual({m.source for m in materials}, {SOURCE_REPLY})
        self.assertEqual(event.bot.calls, [("get_msg", {"message_id": 9527})])

    async def test_reply_file_dropped_by_adapter_is_recovered(self) -> None:
        event = FakeEvent(
            chain=[FakeReply(message_id="42", chain=[FakePlain("看这个")])],
            responses={
                "get_msg": {
                    "data": {"message": [seg("file", file_id="F42", file_name="big.zip", size=999)]}
                }
            },
        )
        materials = await collect_materials(event)
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0].source, SOURCE_REPLY)
        self.assertEqual(materials[0].file_id, "F42")
        self.assertEqual(materials[0].name, "big.zip")

    async def test_reply_lookup_can_be_disabled(self) -> None:
        reply = FakeReply(message_id="9527", chain=[FakeImage(url="https://cdn.example.com/r.png")])
        event = FakeEvent(
            chain=[reply],
            responses={"get_msg": {"message": [seg("file", file_id="RFID", file_name="r.zip")]}},
        )
        materials = await collect_materials(event, resolve_reply=False)
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0].source, SOURCE_REPLY)
        self.assertEqual(event.bot.calls, [])

    async def test_reply_without_id_skips_action(self) -> None:
        event = FakeEvent(chain=[FakeReply(chain=[FakeImage(url="https://cdn.example.com/r.png")])])
        materials = await collect_materials(event)
        self.assertEqual(len(materials), 1)
        self.assertEqual(event.bot.calls, [])

    async def test_reply_action_failure_is_tolerated(self) -> None:
        event = FakeEvent(
            chain=[
                FakeReply(message_id="7", chain=[FakeImage(url="https://cdn.example.com/r.png")])
            ],
            responses={"get_msg": RuntimeError("接口不可用")},
        )
        materials = await collect_materials(event)
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0].source, SOURCE_REPLY)

    async def test_dedupes_repeated_components(self) -> None:
        event = FakeEvent(
            chain=[
                FakeImage(url="https://cdn.example.com/dup.png"),
                FakeImage(url="https://cdn.example.com/dup.png"),
            ]
        )
        self.assertEqual(len(await collect_materials(event)), 1)

    async def test_dedupes_repeated_raw_segments(self) -> None:
        event = FakeEvent(
            raw={"message": [seg("file", file_id="SAME"), seg("file", file_id="SAME")]}
        )
        self.assertEqual(len(await collect_materials(event)), 1)

    async def test_unresolvable_segments_are_dropped(self) -> None:
        event = FakeEvent(raw={"message": [seg("image"), seg("file")]})
        self.assertEqual(await collect_materials(event), [])

    async def test_broken_chain_falls_back_to_raw(self) -> None:
        event = FakeEvent(
            chain_error=True,
            raw={"message": [seg("image", url="https://cdn.example.com/x.png")]},
        )
        materials = await collect_materials(event)
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0].source, SOURCE_RAW)

    async def test_event_without_raw_message(self) -> None:
        class BareEvent:
            def get_messages(self) -> list[Any]:
                return [FakeImage(url="https://cdn.example.com/only.png")]

        materials = await collect_materials(BareEvent())
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0].name, "only.png")

    async def test_event_without_get_messages(self) -> None:
        raw = [seg("image", url="https://cdn.example.com/y.png")]
        event = SimpleNamespace(message_obj=SimpleNamespace(raw_message=raw))
        materials = await collect_materials(event)
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0].source, SOURCE_RAW)

    async def test_empty_event(self) -> None:
        self.assertEqual(await collect_materials(FakeEvent()), [])


if __name__ == "__main__":
    unittest.main()
