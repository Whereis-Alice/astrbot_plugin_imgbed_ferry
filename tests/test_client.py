"""`imgbed_ferry.client` 单元测试：全部走注入的假 transport，不碰网络。"""

from __future__ import annotations

import json
import unittest
from typing import Any

from imgbed_ferry.client import (
    ERROR_HINTS,
    MAX_BATCH_DELETE,
    UPLOAD_FIELD_NAME,
    FilePart,
    ImgBedClient,
    ImgBedError,
    RawResponse,
    UploadOutcome,
    classify_status,
    clean_error_body,
    encode_file_id_for_path,
    extract_file_id,
    parse_upload_response,
    sanitize_upload_folder,
)
from imgbed_ferry.config import EndpointConfig


class FakeTransport:
    """记录每次调用参数，并按队列返回预置响应。"""

    def __init__(self, *responses: RawResponse | Exception) -> None:
        self.queue: list[RawResponse | Exception] = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        headers: Any = None,
        json_body: Any = None,
        file_part: FilePart | None = None,
        timeout: float = 180.0,
        verify_ssl: bool = True,
    ) -> RawResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params or {}),
                "headers": dict(headers or {}),
                "json_body": json_body,
                "file_part": file_part,
                "timeout": timeout,
                "verify_ssl": verify_ssl,
            }
        )
        if not self.queue:
            raise AssertionError("transport 被调用的次数超过预置响应数")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]


def ok(payload: Any, status: int = 200) -> RawResponse:
    return RawResponse(status=status, text=json.dumps(payload, ensure_ascii=False))


def endpoint(**kwargs: Any) -> EndpointConfig:
    base: dict[str, Any] = {
        "base_url": "https://img.example.com",
        "api_token": "tok-abc",
        "channel_name": "HF-Buckets",
    }
    base.update(kwargs)
    return EndpointConfig(**base)


class ImgBedErrorTest(unittest.TestCase):
    def test_hint_follows_kind(self) -> None:
        self.assertEqual(ImgBedError("x", kind="auth").hint, ERROR_HINTS["auth"])
        self.assertEqual(ImgBedError("x", kind="不存在的分类").hint, "")

    def test_describe_joins_message_status_hint(self) -> None:
        exc = ImgBedError("上传失败", kind="auth", status=401)
        parts = exc.describe().split(" | ")
        self.assertEqual(parts[0], "上传失败")
        self.assertEqual(parts[1], "HTTP 401")
        self.assertEqual(parts[2], ERROR_HINTS["auth"])

    def test_describe_without_status_and_hint(self) -> None:
        exc = ImgBedError("裸消息", kind="unknown")
        self.assertEqual(exc.describe(), "裸消息")
        self.assertEqual(str(exc), "裸消息")

    def test_status_zero_is_omitted(self) -> None:
        # status=0 是假值，describe 不应该拼出 "HTTP 0"
        self.assertNotIn("HTTP", ImgBedError("x", status=0).describe())


class RawResponseTest(unittest.TestCase):
    def test_json_roundtrip(self) -> None:
        self.assertEqual(RawResponse(200, '{"a": 1}').json(), {"a": 1})

    def test_json_raises_on_garbage(self) -> None:
        with self.assertRaises(ValueError):
            RawResponse(200, "<html>").json()

    def test_headers_default_is_independent(self) -> None:
        a = RawResponse(200, "")
        b = RawResponse(200, "")
        a.headers["x"] = "1"
        self.assertEqual(b.headers, {})


class ClassifyStatusTest(unittest.TestCase):
    def test_mapping(self) -> None:
        cases = {
            401: "auth",
            403: "forbidden",
            413: "too_large",
            507: "too_large",
            429: "rate_limit",
            500: "server",
            502: "server",
            400: "bad_request",
            404: "bad_request",
            200: "unknown",
            302: "unknown",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.assertEqual(classify_status(status), expected)


class CleanErrorBodyTest(unittest.TestCase):
    def test_empty_body(self) -> None:
        self.assertEqual(clean_error_body(""), "图床未返回错误详情")
        self.assertEqual(clean_error_body("   \n "), "图床未返回错误详情")

    def test_strips_error_prefix_case_insensitive(self) -> None:
        self.assertEqual(clean_error_body("Error: 渠道不可用"), "渠道不可用")
        self.assertEqual(clean_error_body("ERROR:  boom"), "boom")

    def test_collapses_whitespace_and_truncates(self) -> None:
        self.assertEqual(clean_error_body("a\n  b\tc"), "a b c")
        self.assertEqual(clean_error_body("x" * 500), "x" * 240)
        self.assertEqual(clean_error_body("y" * 50, limit=10), "y" * 10)

    def test_json_dict_prefers_known_keys(self) -> None:
        self.assertEqual(clean_error_body('{"error": "no channel"}'), "no channel")
        self.assertEqual(clean_error_body('{"message": "m"}'), "m")
        self.assertEqual(clean_error_body('{"msg": "s"}'), "s")
        self.assertEqual(clean_error_body('{"detail": "d"}'), "d")

    def test_json_dict_key_priority(self) -> None:
        body = '{"detail": "d", "message": "m", "error": "e"}'
        self.assertEqual(clean_error_body(body), "e")

    def test_json_dict_without_known_keys_dumps_whole(self) -> None:
        out = clean_error_body('{"code": 7}')
        self.assertEqual(json.loads(out), {"code": 7})

    def test_json_dict_ignores_blank_and_non_str_values(self) -> None:
        out = clean_error_body('{"error": "   ", "message": 5, "msg": "命中"}')
        self.assertEqual(out, "命中")

    def test_json_array_falls_back_to_raw_text(self) -> None:
        self.assertEqual(clean_error_body('["a", "b"]'), '["a", "b"]')

    def test_broken_json_keeps_raw(self) -> None:
        self.assertEqual(clean_error_body("{oops"), "{oops")

    def test_nested_error_prefix_inside_json(self) -> None:
        self.assertEqual(clean_error_body('{"error": "Error: inner"}'), "inner")


class FileIdTest(unittest.TestCase):
    def test_encode_replaces_slash_with_comma(self) -> None:
        self.assertEqual(encode_file_id_for_path("a/b/c.png"), "a,b,c.png")
        self.assertEqual(encode_file_id_for_path("  /a/b/  "), "a,b")
        self.assertEqual(encode_file_id_for_path(""), "")
        self.assertEqual(encode_file_id_for_path("///"), "")

    def test_extract_from_various_shapes(self) -> None:
        cases = {
            "": "",
            "  ": "",
            "/file/a.png": "a.png",
            "file/a.png": "a.png",
            "a.png": "a.png",
            "https://img.example.com/file/x/y.png": "x/y.png",
            "http://h/file/a.png?authCode=1": "a.png",
            "https://h/file/a.png#frag": "a.png",
            "https://h/other/a.png": "other/a.png",
            "https://h": "",
        }
        for src, expected in cases.items():
            with self.subTest(src=src):
                self.assertEqual(extract_file_id(src), expected)


class UploadOutcomeTest(unittest.TestCase):
    def test_prefers_public_url(self) -> None:
        out = UploadOutcome(src="/file/a.png", public_url="https://cdn/a.png")
        self.assertEqual(out.best_url(base_url="https://b"), "https://cdn/a.png")

    def test_absolute_src_when_public_disabled(self) -> None:
        out = UploadOutcome(src="https://h/file/a.png", public_url="https://cdn/a.png")
        self.assertEqual(
            out.best_url(base_url="https://b", prefer_public=False),
            "https://h/file/a.png",
        )

    def test_joins_base_url_and_relative_src(self) -> None:
        out = UploadOutcome(src="/file/a.png")
        self.assertEqual(out.best_url(base_url="https://b/"), "https://b/file/a.png")

    def test_relative_src_without_base(self) -> None:
        self.assertEqual(UploadOutcome(src="/file/a.png").best_url(), "/file/a.png")

    def test_public_only_when_prefer_disabled(self) -> None:
        out = UploadOutcome(public_url="https://cdn/a.png")
        self.assertEqual(out.best_url(prefer_public=False), "https://cdn/a.png")

    def test_all_empty(self) -> None:
        self.assertEqual(UploadOutcome().best_url(base_url="https://b"), "")


class ParseUploadResponseTest(unittest.TestCase):
    def test_array_shape(self) -> None:
        out = parse_upload_response([{"src": "/file/a.png", "publicUrl": "https://cdn/a.png"}])
        self.assertEqual(out.src, "/file/a.png")
        self.assertEqual(out.public_url, "https://cdn/a.png")
        self.assertEqual(out.file_id, "a.png")

    def test_bare_object_shape(self) -> None:
        self.assertEqual(parse_upload_response({"src": "/file/b.png"}).file_id, "b.png")

    def test_data_wrapper(self) -> None:
        payload = {"data": [{"src": "/file/c.png"}]}
        self.assertEqual(parse_upload_response(payload).file_id, "c.png")

    def test_src_wins_over_data_wrapper(self) -> None:
        payload = {"src": "/file/outer.png", "data": [{"src": "/file/inner.png"}]}
        self.assertEqual(parse_upload_response(payload).file_id, "outer.png")

    def test_file_id_falls_back_to_public_url(self) -> None:
        out = parse_upload_response([{"publicUrl": "https://cdn/file/d.png"}])
        self.assertEqual(out.src, "")
        self.assertEqual(out.file_id, "d.png")

    def test_raw_is_copied(self) -> None:
        node = {"src": "/file/e.png", "extra": 1}
        out = parse_upload_response([node])
        out.raw["extra"] = 2
        self.assertEqual(node["extra"], 1)

    def test_empty_array(self) -> None:
        with self.assertRaises(ImgBedError) as ctx:
            parse_upload_response([])
        self.assertEqual(ctx.exception.kind, "parse")
        self.assertEqual(ctx.exception.message, "图床返回体不含上传结果")

    def test_non_mapping_payloads(self) -> None:
        for payload in (None, "plain text", 42, b"bytes", [1, 2]):
            with self.subTest(payload=payload), self.assertRaises(ImgBedError) as ctx:
                parse_upload_response(payload)
            self.assertEqual(ctx.exception.kind, "parse")

    def test_missing_src_and_public_url(self) -> None:
        with self.assertRaises(ImgBedError) as ctx:
            parse_upload_response([{"other": 1}])
        self.assertEqual(ctx.exception.message, "图床返回体缺少 src 字段")

    def test_blank_src_is_treated_as_missing(self) -> None:
        with self.assertRaises(ImgBedError):
            parse_upload_response([{"src": "   ", "publicUrl": ""}])


class SanitizeUploadFolderTest(unittest.TestCase):
    def test_cases(self) -> None:
        cases = {
            "": "",
            "   ": "",
            "astrbot/2026": "astrbot/2026",
            "astrbot\\2026": "astrbot/2026",
            "/a//b/": "a/b",
            "./a/./b": "a/b",
            "../etc/passwd": "_/etc/passwd",
            "a/../../b": "a/_/_/b",
            "  sp ace ": "sp_ace",
            "a<b>c": "a_b_c",
            "图床/相册": "图床/相册",
            "user@host-1_2.x": "user@host-1_2.x",
            "...": "_",
            "._a_.": "a",
            "!!!": "_",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(sanitize_upload_folder(raw), expected)


class ClientPlumbingTest(unittest.TestCase):
    def test_build_url(self) -> None:
        client = ImgBedClient(endpoint(base_url="https://img.example.com/"))
        self.assertEqual(client.build_url("/upload"), "https://img.example.com/upload")
        self.assertEqual(client.build_url("upload"), "https://img.example.com/upload")

    def test_build_url_adds_scheme(self) -> None:
        client = ImgBedClient(endpoint(base_url="img.example.com"))
        self.assertEqual(client.build_url("/upload"), "https://img.example.com/upload")

    def test_build_url_without_base(self) -> None:
        client = ImgBedClient(EndpointConfig())
        with self.assertRaises(ImgBedError) as ctx:
            client.build_url("/upload")
        self.assertEqual(ctx.exception.kind, "not_configured")

    def test_auth_headers_token(self) -> None:
        client = ImgBedClient(endpoint(auth_mode="token", api_token="tok"))
        self.assertEqual(client.auth_headers(), {"Authorization": "Bearer tok"})
        self.assertEqual(client.auth_query(), {})

    def test_auth_headers_auth_code(self) -> None:
        client = ImgBedClient(endpoint(auth_mode="auth_code", api_token="", auth_code="code"))
        self.assertEqual(client.auth_headers(), {"authCode": "code"})
        self.assertEqual(client.auth_query(), {"authCode": "code"})

    def test_auth_headers_none_mode(self) -> None:
        # 显式选 none：即使配置里残留 token 也不能发出去
        client = ImgBedClient(endpoint(auth_mode="none", api_token="stale", auth_code="stale"))
        self.assertEqual(client.auth_headers(), {})
        self.assertEqual(client.auth_query(), {})

    def test_auth_mode_falls_back_to_filled_credential(self) -> None:
        # 模式写了 token 但只填了 authCode，按 authCode 走
        client = ImgBedClient(endpoint(auth_mode="token", api_token="", auth_code="code"))
        self.assertEqual(client.auth_headers(), {"authCode": "code"})

    def test_upload_params_defaults(self) -> None:
        client = ImgBedClient(endpoint())
        params = client.upload_params()
        self.assertEqual(params["uploadChannel"], "s3")
        self.assertEqual(params["channelName"], "HF-Buckets")
        self.assertEqual(params["uploadNameType"], "default")
        self.assertEqual(params["returnFormat"], "full")
        self.assertEqual(params["autoRetry"], "true")
        self.assertEqual(params["serverCompress"], "true")
        self.assertNotIn("uploadFolder", params)

    def test_upload_params_overrides(self) -> None:
        client = ImgBedClient(
            endpoint(channel_name="", server_compress=False, auto_retry=False, name_type="short")
        )
        params = client.upload_params(folder="../wall/2026", name_type="origin")
        self.assertNotIn("channelName", params)
        self.assertEqual(params["autoRetry"], "false")
        self.assertEqual(params["serverCompress"], "false")
        self.assertEqual(params["uploadNameType"], "origin")
        self.assertEqual(params["uploadFolder"], "_/wall/2026")

    def test_upload_params_name_type_falls_back_to_endpoint(self) -> None:
        client = ImgBedClient(endpoint(name_type="short"))
        self.assertEqual(client.upload_params()["uploadNameType"], "short")

    def test_upload_params_drops_folder_that_sanitizes_to_empty(self) -> None:
        client = ImgBedClient(endpoint())
        self.assertNotIn("uploadFolder", client.upload_params(folder="  /  "))


class ClientUploadTest(unittest.IsolatedAsyncioTestCase):
    async def test_upload_happy_path(self) -> None:
        transport = FakeTransport(ok([{"src": "/file/a.png", "publicUrl": "https://cdn/a.png"}]))
        client = ImgBedClient(endpoint(timeout_seconds=42.0, verify_ssl=False), transport=transport)
        out = await client.upload(
            filename="a.png",
            data=b"\x89PNG",
            content_type="image/png",
            folder="astrbot/2026",
        )
        self.assertEqual(out.file_id, "a.png")
        call = transport.last
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://img.example.com/upload")
        self.assertEqual(call["headers"], {"Authorization": "Bearer tok-abc"})
        self.assertEqual(call["params"]["uploadFolder"], "astrbot/2026")
        self.assertNotIn("authCode", call["params"])
        self.assertEqual(call["timeout"], 42.0)
        self.assertFalse(call["verify_ssl"])
        part = call["file_part"]
        self.assertIsInstance(part, FilePart)
        self.assertEqual(part.field_name, UPLOAD_FIELD_NAME)
        self.assertEqual(part.filename, "a.png")
        self.assertEqual(part.data, b"\x89PNG")
        self.assertEqual(part.content_type, "image/png")

    async def test_upload_does_not_leak_auth_code_into_query(self) -> None:
        transport = FakeTransport(ok([{"src": "/file/a.png"}]))
        client = ImgBedClient(
            endpoint(auth_mode="auth_code", api_token="", auth_code="secret"),
            transport=transport,
        )
        await client.upload(filename="a.png", data=b"1")
        self.assertNotIn("authCode", transport.last["params"])
        self.assertEqual(transport.last["headers"], {"authCode": "secret"})

    async def test_upload_error_status_is_classified(self) -> None:
        transport = FakeTransport(RawResponse(413, "Error: file too large"))
        client = ImgBedClient(endpoint(), transport=transport)
        with self.assertRaises(ImgBedError) as ctx:
            await client.upload(filename="a.png", data=b"1")
        self.assertEqual(ctx.exception.kind, "too_large")
        self.assertEqual(ctx.exception.status, 413)
        self.assertEqual(ctx.exception.message, "上传失败：file too large")

    async def test_upload_non_json_body(self) -> None:
        transport = FakeTransport(RawResponse(200, "<html>blocked</html>"))
        client = ImgBedClient(endpoint(), transport=transport)
        with self.assertRaises(ImgBedError) as ctx:
            await client.upload(filename="a.png", data=b"1")
        self.assertEqual(ctx.exception.kind, "parse")
        self.assertIn("上传返回非 JSON", ctx.exception.message)

    async def test_transport_exception_propagates(self) -> None:
        boom = ImgBedError("请求图床超时", kind="timeout")
        client = ImgBedClient(endpoint(), transport=FakeTransport(boom))
        with self.assertRaises(ImgBedError) as ctx:
            await client.upload(filename="a.png", data=b"1")
        self.assertEqual(ctx.exception.kind, "timeout")


class ClientDeleteTest(unittest.IsolatedAsyncioTestCase):
    async def test_delete_encodes_path_and_sends_auth_query(self) -> None:
        transport = FakeTransport(ok({"success": True}))
        client = ImgBedClient(
            endpoint(auth_mode="auth_code", api_token="", auth_code="code"),
            transport=transport,
        )
        self.assertEqual(await client.delete("astrbot/2026/a.png"), {"success": True})
        call = transport.last
        self.assertEqual(call["method"], "DELETE")
        self.assertEqual(
            call["url"],
            "https://img.example.com/api/manage/delete/astrbot,2026,a.png",
        )
        self.assertEqual(call["params"], {"authCode": "code"})

    async def test_delete_wraps_non_dict_payload(self) -> None:
        client = ImgBedClient(endpoint(), transport=FakeTransport(ok("done")))
        self.assertEqual(await client.delete("a.png"), {"success": True, "raw": "done"})

    async def test_delete_requires_file_id(self) -> None:
        transport = FakeTransport()
        client = ImgBedClient(endpoint(), transport=transport)
        for bad in ("", "   ", "/"):
            with self.subTest(bad=bad), self.assertRaises(ImgBedError) as ctx:
                await client.delete(bad)
            self.assertEqual(ctx.exception.kind, "bad_request")
        self.assertEqual(transport.calls, [])

    async def test_delete_batch(self) -> None:
        transport = FakeTransport(ok({"deleted": 2}))
        client = ImgBedClient(endpoint(), transport=transport)
        self.assertEqual(await client.delete_batch([" a ", "b", "", "  "]), {"deleted": 2})
        call = transport.last
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://img.example.com/api/manage/delete/batch")
        self.assertEqual(call["json_body"], {"fileIds": ["a", "b"]})

    async def test_delete_batch_rejects_empty(self) -> None:
        client = ImgBedClient(endpoint(), transport=FakeTransport())
        with self.assertRaises(ImgBedError) as ctx:
            await client.delete_batch(["", "   "])
        self.assertEqual(ctx.exception.message, "缺少要删除的 fileId 列表")

    async def test_delete_batch_rejects_oversize(self) -> None:
        client = ImgBedClient(endpoint(), transport=FakeTransport())
        ids = [f"f{i}.png" for i in range(MAX_BATCH_DELETE + 1)]
        with self.assertRaises(ImgBedError) as ctx:
            await client.delete_batch(ids)
        self.assertEqual(ctx.exception.kind, "bad_request")
        self.assertIn(str(MAX_BATCH_DELETE), ctx.exception.message)

    async def test_delete_batch_accepts_exactly_max(self) -> None:
        transport = FakeTransport(ok({"deleted": MAX_BATCH_DELETE}))
        client = ImgBedClient(endpoint(), transport=transport)
        ids = [f"f{i}.png" for i in range(MAX_BATCH_DELETE)]
        await client.delete_batch(ids)
        self.assertEqual(len(transport.last["json_body"]["fileIds"]), MAX_BATCH_DELETE)


class ClientListTest(unittest.IsolatedAsyncioTestCase):
    async def test_list_files_defaults(self) -> None:
        transport = FakeTransport(ok({"files": []}))
        client = ImgBedClient(endpoint(), transport=transport)
        await client.list_files()
        call = transport.last
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://img.example.com/api/manage/list")
        self.assertEqual(call["params"], {"count": 20, "start": 0})

    async def test_list_files_all_options(self) -> None:
        transport = FakeTransport(ok({"files": [1]}))
        client = ImgBedClient(endpoint(), transport=transport)
        await client.list_files(
            count=-1,
            directory="astrbot",
            recursive=True,
            search="cat",
            channel="S3",
            file_type="image",
            start=5,
        )
        self.assertEqual(
            transport.last["params"],
            {
                "count": -1,
                "start": 5,
                "dir": "astrbot",
                "recursive": "true",
                "search": "cat",
                "channel": "S3",
                "fileType": "image",
            },
        )

    async def test_list_files_wraps_array_payload(self) -> None:
        client = ImgBedClient(endpoint(), transport=FakeTransport(ok([{"name": "a"}])))
        self.assertEqual(await client.list_files(), {"files": [{"name": "a"}]})

    async def test_list_files_error(self) -> None:
        client = ImgBedClient(endpoint(), transport=FakeTransport(RawResponse(500, "boom")))
        with self.assertRaises(ImgBedError) as ctx:
            await client.list_files()
        self.assertEqual(ctx.exception.kind, "server")
        self.assertIn("拉取文件列表失败", ctx.exception.message)


class ClientQuotaTest(unittest.IsolatedAsyncioTestCase):
    async def test_quota_ok(self) -> None:
        client = ImgBedClient(endpoint(), transport=FakeTransport(ok({"used": 1})))
        self.assertEqual(await client.quota(), {"used": 1})

    async def test_quota_401_is_permission(self) -> None:
        client = ImgBedClient(endpoint(), transport=FakeTransport(RawResponse(401, "Unauthorized")))
        with self.assertRaises(ImgBedError) as ctx:
            await client.quota()
        self.assertEqual(ctx.exception.kind, "permission")
        self.assertEqual(ctx.exception.status, 401)

    async def test_quota_wraps_non_dict(self) -> None:
        client = ImgBedClient(endpoint(), transport=FakeTransport(ok([1, 2])))
        self.assertEqual(await client.quota(), {"raw": [1, 2]})


class ClientProbeTest(unittest.IsolatedAsyncioTestCase):
    async def test_probe_uploads_then_cleans(self) -> None:
        transport = FakeTransport(
            ok([{"src": "/file/astrbot/_probe/p.png", "publicUrl": "https://cdn/p.png"}]),
            ok({"success": True}),
        )
        client = ImgBedClient(endpoint(), transport=transport)
        report = await client.probe()
        self.assertTrue(report["uploaded"])
        self.assertTrue(report["cleaned"])
        self.assertEqual(report["clean_error"], "")
        self.assertEqual(report["url"], "https://cdn/p.png")
        self.assertEqual(report["file_id"], "astrbot/_probe/p.png")
        upload_call, delete_call = transport.calls
        self.assertEqual(upload_call["params"]["uploadFolder"], "astrbot/probe")
        self.assertEqual(upload_call["file_part"].filename, "astrbot-imgbed-ferry-probe.png")
        self.assertEqual(upload_call["file_part"].content_type, "image/png")
        self.assertTrue(upload_call["file_part"].data.startswith(b"\x89PNG"))
        self.assertEqual(delete_call["method"], "DELETE")

    async def test_probe_records_clean_error(self) -> None:
        transport = FakeTransport(
            ok([{"src": "/file/p.png"}]),
            RawResponse(403, "Error: nope"),
        )
        client = ImgBedClient(endpoint(), transport=transport)
        report = await client.probe()
        self.assertTrue(report["uploaded"])
        self.assertFalse(report["cleaned"])
        self.assertIn("删除失败：nope", report["clean_error"])
        self.assertIn("HTTP 403", report["clean_error"])

    async def test_probe_skips_delete_without_file_id(self) -> None:
        transport = FakeTransport(ok([{"publicUrl": "https://cdn/"}]))
        client = ImgBedClient(endpoint(), transport=transport)
        report = await client.probe()
        self.assertEqual(report["file_id"], "")
        self.assertFalse(report["cleaned"])
        self.assertEqual(len(transport.calls), 1)

    async def test_probe_upload_failure_bubbles(self) -> None:
        transport = FakeTransport(RawResponse(401, "Error: bad token"))
        client = ImgBedClient(endpoint(), transport=transport)
        with self.assertRaises(ImgBedError) as ctx:
            await client.probe()
        self.assertEqual(ctx.exception.kind, "auth")

    async def test_probe_respects_prefer_public_false(self) -> None:
        transport = FakeTransport(
            ok([{"src": "/file/p.png", "publicUrl": "https://cdn/p.png"}]),
            ok({"success": True}),
        )
        client = ImgBedClient(endpoint(prefer_public_url=False), transport=transport)
        report = await client.probe()
        self.assertEqual(report["url"], "https://img.example.com/file/p.png")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
