"""`imgbed_ferry.onebot` OneBot 取件兜底测试。

这一层的价值全在「适配器拿不到时我还能拿到」，所以测试重点是：

1. 各种畸形 payload / 响应结构都不能把流程炸掉，只能降级；
2. action 兜底链的顺序与参数要和协议端实现对得上；
3. `file://` URI 与百分号编码路径必须能还原成真实文件。
"""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote

from imgbed_ferry import onebot
from imgbed_ferry.onebot import (
    build_file_attempts,
    build_image_attempts,
    event_group_id,
    event_user_id,
    extract_onebot_segments,
    fetch_file_payload,
    fetch_image_payload,
    fetch_reply_segments,
    is_http_url,
    normalize_id,
    payload_base64,
    payload_is_usable,
    payload_local_path,
    payload_url,
    resolve_call_action,
    run_action_attempts,
    safe_get,
    segment_payload,
    strip_file_uri,
    unwrap_action_response,
)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class Recorder:
    """按调用顺序吐出预置结果的假 call_action，Exception 会被抛出。"""

    def __init__(self, results: list[Any] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, *, action: str, **params: Any) -> Any:
        self.calls.append((action, dict(params)))
        value = self.results.pop(0) if self.results else {}
        if isinstance(value, Exception):
            raise value
        return value

    @property
    def actions(self) -> list[str]:
        return [action for action, _ in self.calls]


def make_event(
    *,
    group_id: Any = "",
    user_id: Any = "",
    raw: Any = None,
    results: list[Any] | None = None,
    with_bot: bool = True,
) -> Any:
    recorder = Recorder(results) if with_bot else None
    event = SimpleNamespace(
        message_obj=SimpleNamespace(raw_message=raw),
        get_group_id=lambda: group_id,
        get_sender_id=lambda: user_id,
    )
    if recorder is not None:
        event.bot = SimpleNamespace(call_action=recorder)
        event.recorder = recorder
    return event


class IsHttpUrlTest(unittest.TestCase):
    def test_accepts_http_and_https_any_case(self) -> None:
        for value in ("http://a/b", "https://a/b", "HTTPS://A", "  http://a  "):
            with self.subTest(value=value):
                self.assertTrue(is_http_url(value))

    def test_rejects_other_schemes_and_types(self) -> None:
        for value in ("", "ftp://a", "base64://abc", "file:///a", None, 123, b"http://a"):
            with self.subTest(value=value):
                self.assertFalse(is_http_url(value))


class SafeGetTest(unittest.TestCase):
    def test_mapping(self) -> None:
        self.assertEqual(safe_get({"a": 1}, "a"), 1)
        self.assertEqual(safe_get({}, "a", "d"), "d")

    def test_object_with_callable_get(self) -> None:
        class Holder:
            def get(self, key: str, default: Any = None) -> Any:
                return {"a": 2}.get(key, default)

        self.assertEqual(safe_get(Holder(), "a"), 2)
        self.assertEqual(safe_get(Holder(), "zz", "d"), "d")

    def test_get_raising_falls_back_to_default(self) -> None:
        class Broken:
            def get(self, key: str, default: Any = None) -> Any:
                raise RuntimeError("boom")

        self.assertEqual(safe_get(Broken(), "a", "d"), "d")

    def test_plain_attribute(self) -> None:
        self.assertEqual(safe_get(SimpleNamespace(a=5), "a"), 5)
        self.assertEqual(safe_get(SimpleNamespace(a=5), "b", "d"), "d")

    def test_non_callable_get_attribute_ignored(self) -> None:
        self.assertEqual(safe_get(SimpleNamespace(get=5, a=1), "a"), 1)


class NormalizeIdTest(unittest.TestCase):
    def test_digit_strings_become_int(self) -> None:
        self.assertEqual(normalize_id("12345"), 12345)
        self.assertEqual(normalize_id(12345), 12345)
        self.assertEqual(normalize_id("  678  "), 678)

    def test_blank_values(self) -> None:
        for value in ("", "   ", None, 0, False):
            with self.subTest(value=value):
                self.assertEqual(normalize_id(value), "")

    def test_non_digit_kept_as_string(self) -> None:
        self.assertEqual(normalize_id("-100"), "-100")
        self.assertEqual(normalize_id("abc"), "abc")


class UnwrapActionResponseTest(unittest.TestCase):
    def test_unwraps_nested_data(self) -> None:
        payload = unwrap_action_response({"status": "ok", "retcode": 0, "data": {"url": "u"}})
        self.assertEqual(payload, {"url": "u"})

    def test_flat_mapping_passthrough(self) -> None:
        self.assertEqual(unwrap_action_response({"url": "u"}), {"url": "u"})

    def test_non_mapping_data_keeps_outer(self) -> None:
        self.assertEqual(unwrap_action_response({"data": "raw"}), {"data": "raw"})

    def test_dict_convertible_sequence(self) -> None:
        self.assertEqual(unwrap_action_response([("data", {"url": "u"})]), {"url": "u"})
        self.assertEqual(unwrap_action_response([("url", "u")]), {"url": "u"})

    def test_unconvertible_values(self) -> None:
        for value in (None, "text", 123, object()):
            with self.subTest(value=value):
                self.assertEqual(unwrap_action_response(value), {})

    def test_returns_copy(self) -> None:
        source = {"data": {"url": "u"}}
        unwrapped = unwrap_action_response(source)
        unwrapped["url"] = "changed"
        self.assertEqual(source["data"], {"url": "u"})


class ResolveCallActionTest(unittest.TestCase):
    def test_on_bot(self) -> None:
        recorder = Recorder()
        event = SimpleNamespace(bot=SimpleNamespace(call_action=recorder))
        self.assertIs(resolve_call_action(event), recorder)

    def test_on_bot_api(self) -> None:
        recorder = Recorder()
        bot = SimpleNamespace(api=SimpleNamespace(call_action=recorder))
        self.assertIs(resolve_call_action(SimpleNamespace(bot=bot)), recorder)

    def test_missing(self) -> None:
        cases = (
            SimpleNamespace(),
            SimpleNamespace(bot=None),
            SimpleNamespace(bot=SimpleNamespace()),
            SimpleNamespace(bot=SimpleNamespace(call_action="not-callable")),
            SimpleNamespace(bot=SimpleNamespace(api=SimpleNamespace())),
        )
        for event in cases:
            with self.subTest(event=event):
                self.assertIsNone(resolve_call_action(event))


class ExtractOnebotSegmentsTest(unittest.TestCase):
    def test_direct_list(self) -> None:
        raw = [{"type": "image", "data": {"file": "a.png"}}]
        segments = extract_onebot_segments(raw)
        self.assertEqual(segments, raw)
        segments[0]["type"] = "changed"
        self.assertEqual(raw[0]["type"], "image")

    def test_tuple_accepted(self) -> None:
        segments = extract_onebot_segments(({"type": "file"},))
        self.assertEqual(segments, [{"type": "file"}])

    def test_message_key(self) -> None:
        raw = {"message_id": 1, "message": [{"type": "file", "data": {"file_id": "F"}}]}
        self.assertEqual(extract_onebot_segments(raw), [{"type": "file", "data": {"file_id": "F"}}])

    def test_message_attribute(self) -> None:
        raw = SimpleNamespace(message=[{"type": "image"}])
        self.assertEqual(extract_onebot_segments(raw), [{"type": "image"}])

    def test_cq_code_string_rejected(self) -> None:
        for raw in ("[CQ:image,file=a.png]", {"message": "[CQ:image,file=a.png]"}):
            with self.subTest(raw=raw):
                self.assertEqual(extract_onebot_segments(raw), [])

    def test_non_iterable_and_none(self) -> None:
        for raw in (None, 123, {"message": 5}, object()):
            with self.subTest(raw=raw):
                self.assertEqual(extract_onebot_segments(raw), [])

    def test_drops_non_mapping_items(self) -> None:
        raw = ["text", None, {"type": "image"}, 7]
        self.assertEqual(extract_onebot_segments(raw), [{"type": "image"}])


class SegmentPayloadTest(unittest.TestCase):
    def test_copies_data(self) -> None:
        segment = {"type": "image", "data": {"file": "a.png"}}
        payload = segment_payload(segment)
        payload["file"] = "b.png"
        self.assertEqual(segment["data"], {"file": "a.png"})

    def test_missing_or_bad_data(self) -> None:
        for segment in ({"type": "image"}, {"type": "image", "data": None}, {"data": "x"}):
            with self.subTest(segment=segment):
                self.assertEqual(segment_payload(segment), {})


class EventIdTest(unittest.TestCase):
    def test_payload_wins(self) -> None:
        event = make_event(group_id="222", user_id="333")
        self.assertEqual(event_group_id(event, {"group_id": 111}), "111")
        self.assertEqual(event_user_id(event, {"user_id": " 444 "}), "444")

    def test_blank_payload_falls_through_to_getter(self) -> None:
        event = make_event(group_id="222", user_id="333")
        for payload in ({"group_id": None}, {"group_id": "   "}, {}):
            with self.subTest(payload=payload):
                self.assertEqual(event_group_id(event, payload), "222")
                self.assertEqual(event_user_id(event, {}), "333")

    def test_getter_exception_falls_back_to_raw(self) -> None:
        def boom() -> str:
            raise RuntimeError("no session")

        event = SimpleNamespace(
            message_obj=SimpleNamespace(raw_message={"group_id": 555, "user_id": 666}),
            get_group_id=boom,
            get_sender_id=boom,
        )
        self.assertEqual(event_group_id(event), "555")
        self.assertEqual(event_user_id(event), "666")

    def test_raw_used_when_getter_blank(self) -> None:
        event = SimpleNamespace(
            message_obj=SimpleNamespace(raw_message={"group_id": "777", "user_id": "888"}),
            get_group_id=lambda: "",
            get_sender_id=lambda: None,
        )
        self.assertEqual(event_group_id(event), "777")
        self.assertEqual(event_user_id(event), "888")

    def test_nothing_available(self) -> None:
        event = SimpleNamespace()
        self.assertEqual(event_group_id(event), "")
        self.assertEqual(event_user_id(event), "")

    def test_private_chat_has_no_group(self) -> None:
        event = make_event(group_id="", user_id="999")
        self.assertEqual(event_group_id(event, {"file_id": "F"}), "")
        self.assertEqual(event_user_id(event, {"file_id": "F"}), "999")


class PayloadUrlTest(unittest.TestCase):
    def test_key_priority(self) -> None:
        payload = {"file_url": "https://c", "download_url": "https://b", "url": "https://a"}
        self.assertEqual(payload_url(payload), "https://a")

    def test_skips_non_http_values(self) -> None:
        payload = {"url": "/local/a.png", "download_url": "", "file_url": " https://c "}
        self.assertEqual(payload_url(payload), "https://c")

    def test_empty(self) -> None:
        self.assertEqual(payload_url({"file_id": "F", "url": None}), "")


class StripFileUriTest(unittest.TestCase):
    def test_posix_absolute_keeps_leading_slash(self) -> None:
        self.assertEqual(strip_file_uri("file:///data/a.png"), "/data/a.png")

    def test_windows_drive_drops_leading_slash(self) -> None:
        self.assertEqual(strip_file_uri("file:///C:/tmp/a.png"), "C:/tmp/a.png")
        self.assertEqual(strip_file_uri("file:///C:"), "C:")

    def test_host_form_kept(self) -> None:
        self.assertEqual(strip_file_uri("file://host/share/a.png"), "host/share/a.png")

    def test_scheme_case_insensitive_and_trimmed(self) -> None:
        self.assertEqual(strip_file_uri("  FILE:///data/A.png  "), "/data/A.png")

    def test_non_uri_passthrough(self) -> None:
        for value in ("/data/a.png", "C:/tmp/a.png", "a.png", "", "file:/data/a.png"):
            with self.subTest(value=value):
                self.assertEqual(strip_file_uri(value), value.strip())

    def test_degenerate_uri(self) -> None:
        self.assertEqual(strip_file_uri("file://"), "")


class PathCandidatesTest(unittest.TestCase):
    def test_plain_path_single_candidate(self) -> None:
        self.assertEqual(onebot._path_candidates("/data/a.png"), ["/data/a.png"])

    def test_percent_encoded_adds_decoded(self) -> None:
        self.assertEqual(
            onebot._path_candidates("/data/a%20b.png"),
            ["/data/a%20b.png", "/data/a b.png"],
        )

    def test_percent_without_escape_stays_single(self) -> None:
        self.assertEqual(onebot._path_candidates("/data/100%.png"), ["/data/100%.png"])


class PayloadLocalPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.plain = self.root / "plain.png"
        self.plain.write_bytes(b"plain")
        self.spaced = self.root / "a b.png"
        self.spaced.write_bytes(b"spaced")

    def test_existing_path_resolved(self) -> None:
        self.assertEqual(
            payload_local_path({"path": str(self.plain)}),
            str(self.plain.resolve()),
        )

    def test_key_order_skips_missing_file(self) -> None:
        payload = {"path": str(self.root / "nope.png"), "file": str(self.plain)}
        self.assertEqual(payload_local_path(payload), str(self.plain.resolve()))

    def test_file_uri(self) -> None:
        self.assertEqual(
            payload_local_path({"file": Path(self.plain).as_uri()}),
            str(self.plain.resolve()),
        )

    def test_percent_encoded_file_uri(self) -> None:
        uri = Path(self.spaced).as_uri()
        self.assertIn("%20", uri)
        self.assertEqual(payload_local_path({"path": uri}), str(self.spaced.resolve()))

    def test_unencoded_file_uri_with_space(self) -> None:
        uri = unquote(Path(self.spaced).as_uri())
        self.assertNotIn("%20", uri)
        self.assertEqual(payload_local_path({"path": uri}), str(self.spaced.resolve()))

    def test_skips_urls_and_inline_data(self) -> None:
        payload = {
            "path": "https://cdn.example.com/a.png",
            "file": "base64://" + b64(b"0123456789abcdef"),
            "local_path": str(self.plain),
        }
        self.assertEqual(payload_local_path(payload), str(self.plain.resolve()))

    def test_directory_is_not_a_file(self) -> None:
        self.assertEqual(payload_local_path({"path": str(self.root)}), "")

    def test_ignores_blank_and_non_string(self) -> None:
        payload = {"path": "   ", "file": 123, "local_path": None, "filename": ""}
        self.assertEqual(payload_local_path(payload), "")

    def test_file_id_is_not_treated_as_path(self) -> None:
        payload = {"file": "31D1B4C0F0A9E7B2.png", "file_id": "31D1B4C0F0A9E7B2"}
        self.assertEqual(payload_local_path(payload), "")

    def test_hostile_value_does_not_raise(self) -> None:
        self.assertEqual(payload_local_path({"path": "bad\x00name.png"}), "")
        self.assertEqual(payload_local_path({"path": "x" * 5000}), "")


class PayloadBase64Test(unittest.TestCase):
    def test_base64_prefix(self) -> None:
        payload = {"base64": "base64://" + b64(b"hello world payload")}
        self.assertEqual(payload_base64(payload), b"hello world payload")

    def test_bare_base64_value(self) -> None:
        payload = {"file_base64": b64(b"bare value payload")}
        self.assertEqual(payload_base64(payload), b"bare value payload")

    def test_data_key_requires_prefix(self) -> None:
        self.assertEqual(payload_base64({"data": b64(b"structured payload")}), b"")
        self.assertEqual(
            payload_base64({"data": "base64://" + b64(b"prefixed payload")}),
            b"prefixed payload",
        )

    def test_data_uri(self) -> None:
        payload = {"data": "data:image/png;base64," + b64(b"data uri payload")}
        self.assertEqual(payload_base64(payload), b"data uri payload")

    def test_short_and_non_string_skipped(self) -> None:
        payload = {"base64": "abc", "file_base64": 123, "data": None}
        self.assertEqual(payload_base64(payload), b"")

    def test_broken_value_falls_through(self) -> None:
        payload = {"base64": "YWJjZGU!!!!!!!!!!", "file_base64": b64(b"fallback payload")}
        self.assertEqual(payload_base64(payload), b"fallback payload")

    def test_nothing_usable(self) -> None:
        self.assertEqual(payload_base64({"file_id": "F", "url": "https://a"}), b"")


class PayloadIsUsableTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name) / "u.png"
        self.target.write_bytes(b"u")

    def test_usable_shapes(self) -> None:
        cases = (
            {"url": "https://cdn.example.com/a.png"},
            {"path": str(self.target)},
            {"base64": "base64://" + b64(b"usable payload here")},
        )
        for payload in cases:
            with self.subTest(payload=sorted(payload)):
                self.assertTrue(payload_is_usable(payload))

    def test_unusable_shapes(self) -> None:
        cases = ({}, {"file_id": "F"}, {"file": "abcd.image"}, {"url": "not-a-url"})
        for payload in cases:
            with self.subTest(payload=sorted(payload)):
                self.assertFalse(payload_is_usable(payload))


class RunActionAttemptsTest(unittest.IsolatedAsyncioTestCase):
    async def test_first_hit_stops_the_chain(self) -> None:
        recorder = Recorder([{"data": {"url": "https://cdn.example.com/a.png"}}])
        payload = await run_action_attempts(
            recorder,
            [("get_group_file_url", {"file_id": "F"}), ("get_file", {"file_id": "F"})],
        )
        self.assertEqual(payload, {"url": "https://cdn.example.com/a.png"})
        self.assertEqual(recorder.actions, ["get_group_file_url"])

    async def test_skips_exceptions(self) -> None:
        recorder = Recorder(
            [RuntimeError("不支持该 action"), {"url": "https://cdn.example.com/b.png"}]
        )
        payload = await run_action_attempts(
            recorder,
            [("get_group_file_url", {"file_id": "F"}), ("get_private_file_url", {"file_id": "F"})],
        )
        self.assertEqual(payload, {"url": "https://cdn.example.com/b.png"})
        self.assertEqual(recorder.actions, ["get_group_file_url", "get_private_file_url"])

    async def test_skips_unusable_responses(self) -> None:
        recorder = Recorder(
            [
                {"status": "ok", "data": {"file_id": "F"}},
                None,
                {"data": {"url": "https://cdn.example.com/c.png"}},
            ]
        )
        attempts = [
            ("get_group_file_url", {"file_id": "F"}),
            ("get_private_file_url", {"file_id": "F"}),
            ("get_file", {"file_id": "F"}),
        ]
        payload = await run_action_attempts(recorder, attempts)
        self.assertEqual(payload, {"url": "https://cdn.example.com/c.png"})
        self.assertEqual(len(recorder.calls), 3)

    async def test_all_failed(self) -> None:
        recorder = Recorder([RuntimeError("x"), {"retcode": 1404}])
        payload = await run_action_attempts(
            recorder,
            [("get_image", {"file": "a"}), ("get_file", {"file": "a"})],
        )
        self.assertEqual(payload, {})

    async def test_params_are_forwarded(self) -> None:
        recorder = Recorder([{"url": "https://cdn.example.com/d.png"}])
        await run_action_attempts(recorder, [("get_group_file_url", {"group_id": 1, "busid": 102})])
        self.assertEqual(recorder.calls, [("get_group_file_url", {"group_id": 1, "busid": 102})])

    async def test_empty_attempts(self) -> None:
        recorder = Recorder()
        self.assertEqual(await run_action_attempts(recorder, []), {})
        self.assertEqual(recorder.calls, [])


class BuildFileAttemptsTest(unittest.TestCase):
    def test_group_chat_full_chain(self) -> None:
        event = make_event(group_id="12345", user_id="999")
        payload = {"file_id": "F1", "file": "raw.bin", "busid": 102}
        self.assertEqual(
            build_file_attempts(event, payload),
            [
                ("get_group_file_url", {"group_id": 12345, "file_id": "F1", "busid": 102}),
                ("get_group_file_url", {"group_id": 12345, "file_id": "F1"}),
                ("get_private_file_url", {"file_id": "F1", "user_id": 999}),
                ("get_private_file_url", {"file_id": "F1"}),
                ("get_file", {"file_id": "F1"}),
                ("get_file", {"file": "raw.bin"}),
            ],
        )

    def test_payload_group_id_overrides_event(self) -> None:
        event = make_event(group_id="12345", user_id="999")
        attempts = build_file_attempts(event, {"file_id": "F1", "group_id": "54321"})
        self.assertEqual(attempts[0], ("get_group_file_url", {"group_id": 54321, "file_id": "F1"}))

    def test_blank_busid_skipped(self) -> None:
        event = make_event(group_id="1", user_id="")
        attempts = build_file_attempts(event, {"file_id": "F1", "busid": "   "})
        self.assertEqual(
            attempts,
            [
                ("get_group_file_url", {"group_id": 1, "file_id": "F1"}),
                ("get_private_file_url", {"file_id": "F1"}),
                ("get_file", {"file_id": "F1"}),
            ],
        )

    def test_private_chat(self) -> None:
        event = make_event(group_id="", user_id="888")
        self.assertEqual(
            build_file_attempts(event, {"file_id": "F1"}),
            [
                ("get_private_file_url", {"file_id": "F1", "user_id": 888}),
                ("get_private_file_url", {"file_id": "F1"}),
                ("get_file", {"file_id": "F1"}),
            ],
        )

    def test_id_key_fallback(self) -> None:
        event = make_event()
        attempts = build_file_attempts(event, {"id": "F2"})
        self.assertEqual(attempts[0], ("get_private_file_url", {"file_id": "F2"}))

    def test_same_raw_file_not_appended(self) -> None:
        event = make_event()
        attempts = build_file_attempts(event, {"file_id": "F1", "file": "F1"})
        self.assertEqual([params for _, params in attempts].count({"file": "F1"}), 0)

    def test_only_raw_file(self) -> None:
        event = make_event()
        self.assertEqual(
            build_file_attempts(event, {"file": "raw.bin"}),
            [("get_file", {"file": "raw.bin"})],
        )

    def test_no_identifiers(self) -> None:
        event = make_event(group_id="1", user_id="2")
        self.assertEqual(build_file_attempts(event, {"file_name": "a.bin"}), [])


class BuildImageAttemptsTest(unittest.TestCase):
    def test_full_chain(self) -> None:
        self.assertEqual(
            build_image_attempts({"file": "abc.image", "file_id": "FID"}),
            [
                ("get_image", {"file": "abc.image"}),
                ("get_image", {"file_id": "FID"}),
                ("get_file", {"file_id": "FID"}),
                ("get_file", {"file": "abc.image"}),
            ],
        )

    def test_only_file(self) -> None:
        self.assertEqual(
            build_image_attempts({"file": "abc.image"}),
            [("get_image", {"file": "abc.image"}), ("get_file", {"file": "abc.image"})],
        )

    def test_only_file_id(self) -> None:
        self.assertEqual(
            build_image_attempts({"file_id": "FID"}),
            [("get_image", {"file_id": "FID"}), ("get_file", {"file_id": "FID"})],
        )

    def test_identical_ids_deduped(self) -> None:
        attempts = build_image_attempts({"file": "FID", "file_id": "FID"})
        self.assertEqual(len(attempts), 3)

    def test_empty_payload(self) -> None:
        self.assertEqual(build_image_attempts({"url": "https://a"}), [])


class FetchFilePayloadTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name) / "doc.bin"
        self.target.write_bytes(b"doc")

    async def test_direct_url_short_circuits(self) -> None:
        event = make_event(group_id="12345", results=[{"url": "https://should.not/be/used"}])
        payload = await fetch_file_payload(
            event, {"url": "https://cdn.example.com/a.bin", "file_id": "F"}
        )
        self.assertEqual(payload, {"url": "https://cdn.example.com/a.bin"})
        self.assertEqual(event.recorder.calls, [])

    async def test_local_path_short_circuits(self) -> None:
        event = make_event(results=[{"url": "https://should.not/be/used"}])
        payload = await fetch_file_payload(event, {"path": str(self.target), "file_id": "F"})
        self.assertEqual(payload, {"path": str(self.target.resolve())})
        self.assertEqual(event.recorder.calls, [])

    async def test_group_action_chain(self) -> None:
        event = make_event(
            group_id="12345",
            user_id="999",
            results=[{"status": "ok", "data": {"url": "https://cdn.example.com/g.bin"}}],
        )
        payload = await fetch_file_payload(event, {"file_id": "F1", "busid": 102})
        self.assertEqual(payload, {"url": "https://cdn.example.com/g.bin"})
        self.assertEqual(event.recorder.actions, ["get_group_file_url"])
        self.assertEqual(event.recorder.calls[0][1]["busid"], 102)

    async def test_falls_through_to_get_file(self) -> None:
        event = make_event(
            group_id="12345",
            user_id="999",
            results=[
                RuntimeError("busid 不支持"),
                {"retcode": 1404},
                RuntimeError("private 接口不可用"),
                {"data": {}},
                {"data": {"base64": "base64://" + b64(b"final fallback payload")}},
            ],
        )
        payload = await fetch_file_payload(event, {"file_id": "F1", "busid": 102})
        self.assertEqual(
            event.recorder.actions,
            [
                "get_group_file_url",
                "get_group_file_url",
                "get_private_file_url",
                "get_private_file_url",
                "get_file",
            ],
        )
        self.assertEqual(payload_base64(payload), b"final fallback payload")

    async def test_without_bot(self) -> None:
        event = make_event(group_id="12345", with_bot=False)
        self.assertEqual(await fetch_file_payload(event, {"file_id": "F1"}), {})

    async def test_without_identifiers(self) -> None:
        event = make_event(results=[{"url": "https://should.not/be/used"}])
        self.assertEqual(await fetch_file_payload(event, {"file_name": "a.bin"}), {})
        self.assertEqual(event.recorder.calls, [])


class FetchImagePayloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_direct_url_short_circuits(self) -> None:
        event = make_event(results=[{"url": "https://should.not/be/used"}])
        payload = await fetch_image_payload(event, {"url": "https://cdn.example.com/a.png"})
        self.assertEqual(payload, {"url": "https://cdn.example.com/a.png"})
        self.assertEqual(event.recorder.calls, [])

    async def test_action_chain_order(self) -> None:
        event = make_event(
            results=[
                {"retcode": 1404},
                {"data": {"url": "https://cdn.example.com/i.png"}},
            ]
        )
        payload = await fetch_image_payload(event, {"file": "abc.image", "file_id": "FID"})
        self.assertEqual(payload, {"url": "https://cdn.example.com/i.png"})
        self.assertEqual(event.recorder.calls[0], ("get_image", {"file": "abc.image"}))
        self.assertEqual(event.recorder.calls[1], ("get_image", {"file_id": "FID"}))

    async def test_without_bot(self) -> None:
        event = make_event(with_bot=False)
        self.assertEqual(await fetch_image_payload(event, {"file": "abc.image"}), {})

    async def test_without_identifiers(self) -> None:
        event = make_event(results=[{"url": "https://should.not/be/used"}])
        self.assertEqual(await fetch_image_payload(event, {"summary": "[图片]"}), {})
        self.assertEqual(event.recorder.calls, [])


class FetchReplySegmentsTest(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_id_converted(self) -> None:
        segments = [{"type": "file", "data": {"file_id": "F1", "file_name": "a.zip"}}]
        event = make_event(results=[{"status": "ok", "data": {"message": segments}}])
        self.assertEqual(await fetch_reply_segments(event, "123456"), segments)
        self.assertEqual(event.recorder.calls, [("get_msg", {"message_id": 123456})])

    async def test_negative_numeric_id_converted(self) -> None:
        event = make_event(results=[{"message": []}])
        await fetch_reply_segments(event, -98765)
        self.assertEqual(event.recorder.calls, [("get_msg", {"message_id": -98765})])

    async def test_non_numeric_id_kept(self) -> None:
        event = make_event(results=[{"message": []}])
        await fetch_reply_segments(event, "abc-1")
        self.assertEqual(event.recorder.calls, [("get_msg", {"message_id": "abc-1"})])

    async def test_flat_message_response(self) -> None:
        segments = [{"type": "image", "data": {"file": "a.png"}}]
        event = make_event(results=[{"message": segments}])
        self.assertEqual(await fetch_reply_segments(event, 1), segments)

    async def test_blank_id_skips_call(self) -> None:
        for message_id in ("", "   ", None, 0):
            event = make_event(results=[{"message": [{"type": "image"}]}])
            with self.subTest(message_id=message_id):
                self.assertEqual(await fetch_reply_segments(event, message_id), [])
                self.assertEqual(event.recorder.calls, [])

    async def test_action_error_is_swallowed(self) -> None:
        event = make_event(results=[RuntimeError("消息已过期")])
        self.assertEqual(await fetch_reply_segments(event, 1), [])

    async def test_unusable_response(self) -> None:
        event = make_event(results=[{"status": "failed", "retcode": 1404}])
        self.assertEqual(await fetch_reply_segments(event, 1), [])

    async def test_without_bot(self) -> None:
        event = make_event(with_bot=False)
        self.assertEqual(await fetch_reply_segments(event, 1), [])


if __name__ == "__main__":
    unittest.main()
