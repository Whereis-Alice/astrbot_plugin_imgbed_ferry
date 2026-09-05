"""`imgbed_ferry.formatting` 与 `imgbed_ferry.results` 的渲染测试。"""

from __future__ import annotations

import unittest

from imgbed_ferry.formatting import (
    STYLE_BBCODE,
    STYLE_HTML,
    STYLE_MARKDOWN,
    STYLE_PLAIN,
    format_item,
    format_link,
    format_results,
    format_urls,
    normalize_style,
    summary_line,
)
from imgbed_ferry.results import FailedItem, UploadedItem, UploadReport


def image(name: str = "a.png", url: str = "https://cdn/a.png", **kwargs: object) -> UploadedItem:
    return UploadedItem(name=name, url=url, kind="image", **kwargs)  # type: ignore[arg-type]


def doc(name: str = "a.pdf", url: str = "https://cdn/a.pdf", **kwargs: object) -> UploadedItem:
    return UploadedItem(name=name, url=url, kind="file", **kwargs)  # type: ignore[arg-type]


class NormalizeStyleTest(unittest.TestCase):
    def test_aliases(self) -> None:
        cases = {
            "": STYLE_MARKDOWN,
            "text": STYLE_PLAIN,
            "raw": STYLE_PLAIN,
            "url": STYLE_PLAIN,
            "plain": STYLE_PLAIN,
            "md": STYLE_MARKDOWN,
            "markdown": STYLE_MARKDOWN,
            "html": STYLE_HTML,
            "bbcode": STYLE_BBCODE,
            "bb": STYLE_BBCODE,
            "  MarkDown  ": STYLE_MARKDOWN,
            "HTML": STYLE_HTML,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_style(raw), expected)

    def test_unknown_uses_default(self) -> None:
        self.assertEqual(normalize_style("wat"), STYLE_MARKDOWN)
        self.assertEqual(normalize_style("wat", STYLE_PLAIN), STYLE_PLAIN)

    def test_none_like_input(self) -> None:
        # None / 空串都会命中 "" 这个显式别名，因此 default 参数不生效
        self.assertEqual(normalize_style(None), STYLE_MARKDOWN)  # type: ignore[arg-type]
        self.assertEqual(normalize_style(None, STYLE_BBCODE), STYLE_MARKDOWN)  # type: ignore[arg-type]
        self.assertEqual(normalize_style("", STYLE_BBCODE), STYLE_MARKDOWN)


class FormatLinkTest(unittest.TestCase):
    def test_plain_ignores_name_and_image(self) -> None:
        self.assertEqual(format_link("https://u", "n", STYLE_PLAIN), "https://u")
        self.assertEqual(format_link("https://u", "n", STYLE_PLAIN, image=True), "https://u")

    def test_markdown(self) -> None:
        self.assertEqual(format_link("https://u", "n", STYLE_MARKDOWN), "[n](https://u)")
        self.assertEqual(
            format_link("https://u", "n", STYLE_MARKDOWN, image=True), "![n](https://u)"
        )

    def test_markdown_escapes_brackets_in_label(self) -> None:
        self.assertEqual(
            format_link("https://u", "a[b]c", STYLE_MARKDOWN), "[a\\[b\\]c](https://u)"
        )

    def test_markdown_label_defaults_to_url(self) -> None:
        self.assertEqual(format_link("https://u", "", STYLE_MARKDOWN), "[https://u](https://u)")

    def test_html_escapes(self) -> None:
        out = format_link("https://u?a=1&b=2", '<x>"y"', STYLE_HTML)
        self.assertEqual(out, '<a href="https://u?a=1&amp;b=2">&lt;x&gt;&quot;y&quot;</a>')

    def test_html_image(self) -> None:
        out = format_link("https://u", "n", STYLE_HTML, image=True)
        self.assertEqual(out, '<img src="https://u" alt="n">')

    def test_bbcode(self) -> None:
        self.assertEqual(format_link("https://u", "n", STYLE_BBCODE), "[url=https://u]n[/url]")
        self.assertEqual(
            format_link("https://u", "n", STYLE_BBCODE, image=True), "[img]https://u[/img]"
        )

    def test_style_alias_is_normalized_inside(self) -> None:
        self.assertEqual(format_link("https://u", "n", "bb"), "[url=https://u]n[/url]")


class FormatItemTest(unittest.TestCase):
    def test_plain_two_lines_with_size(self) -> None:
        out = format_item(image(size=2048), STYLE_PLAIN)
        self.assertEqual(out, "a.png（2.0KB）\nhttps://cdn/a.png")

    def test_plain_hides_size_when_disabled(self) -> None:
        self.assertEqual(
            format_item(image(size=2048), STYLE_PLAIN, show_size=False), "a.png\nhttps://cdn/a.png"
        )

    def test_plain_hides_size_when_zero(self) -> None:
        self.assertEqual(format_item(image(), STYLE_PLAIN), "a.png\nhttps://cdn/a.png")

    def test_plain_name_fallback(self) -> None:
        out = format_item(image(name=""), STYLE_PLAIN)
        self.assertTrue(out.startswith("file\n"))

    def test_markdown_image_gets_bang_and_size(self) -> None:
        self.assertEqual(
            format_item(image(size=1024), STYLE_MARKDOWN),
            "![a.png](https://cdn/a.png) `1.0KB`",
        )

    def test_markdown_non_image_has_no_bang(self) -> None:
        self.assertEqual(format_item(doc(), STYLE_MARKDOWN), "[a.pdf](https://cdn/a.pdf)")

    def test_size_suffix_only_in_markdown(self) -> None:
        self.assertEqual(
            format_item(image(size=1024), STYLE_HTML), '<img src="https://cdn/a.png" alt="a.png">'
        )
        self.assertEqual(
            format_item(image(size=1024), STYLE_BBCODE), "[img]https://cdn/a.png[/img]"
        )


class SummaryLineTest(unittest.TestCase):
    def test_minimal(self) -> None:
        self.assertEqual(summary_line(UploadReport(items=[image()])), "成功 1")

    def test_all_parts_in_order(self) -> None:
        report = UploadReport(
            items=[image(size=1024), doc(name="b.pdf", size=2048, reused=True)],
            failures=[FailedItem("c.png", "太大了")],
            skipped=[("d.exe", "扩展名不允许")],
            elapsed=1.25,
        )
        self.assertEqual(
            summary_line(report),
            "成功 2、命中去重 1、失败 1、跳过 1、新上传 1.0KB、1.2s",
        )

    def test_show_size_false_drops_bytes(self) -> None:
        report = UploadReport(items=[image(size=1024)], elapsed=0.5)
        self.assertEqual(summary_line(report, show_size=False), "成功 1、0.5s")

    def test_reused_items_do_not_count_bytes(self) -> None:
        report = UploadReport(items=[image(size=1024, reused=True)])
        self.assertEqual(report.uploaded_bytes, 0)
        self.assertEqual(summary_line(report), "成功 1、命中去重 1")

    def test_empty_report(self) -> None:
        self.assertEqual(summary_line(UploadReport()), "成功 0")


class FormatUrlsTest(unittest.TestCase):
    def test_one_per_line_and_skips_blank(self) -> None:
        report = UploadReport(items=[image(), doc(url=""), doc(name="c.pdf", url="https://cdn/c")])
        self.assertEqual(format_urls(report), "https://cdn/a.png\nhttps://cdn/c")

    def test_empty(self) -> None:
        self.assertEqual(format_urls(UploadReport()), "")


class FormatResultsTest(unittest.TestCase):
    def test_single_item_is_not_numbered(self) -> None:
        out = format_results(UploadReport(items=[image()]))
        self.assertEqual(out, "📤 成功 1\n\n![a.png](https://cdn/a.png)")

    def test_multiple_items_are_numbered(self) -> None:
        report = UploadReport(items=[image(), doc()])
        lines = format_results(report).splitlines()
        self.assertTrue(lines[2].startswith("1. "))
        self.assertTrue(lines[3].startswith("2. "))

    def test_numbered_false(self) -> None:
        report = UploadReport(items=[image(), doc()])
        lines = format_results(report, numbered=False).splitlines()
        self.assertFalse(lines[2].startswith("1. "))

    def test_marks_are_joined(self) -> None:
        item = image(reused=True, archive="pack.zip", note="已压缩")
        out = format_results(UploadReport(items=[item]))
        self.assertIn("（已存在，复用；来自 pack.zip；已压缩）", out)

    def test_no_items(self) -> None:
        self.assertEqual(format_results(UploadReport()), "📤 没有文件上传成功。")

    def test_failures_block(self) -> None:
        report = UploadReport(
            failures=[FailedItem("a.png", "上传失败", "检查 token"), FailedItem("", "未知")]
        )
        out = format_results(report)
        self.assertIn("⚠️ 失败：", out)
        self.assertIn("- a.png：上传失败（检查 token）", out)
        self.assertIn("- 未命名：未知", out)

    def test_skipped_block(self) -> None:
        report = UploadReport(items=[image()], skipped=[("a.exe", "黑名单"), ("", "空文件")])
        out = format_results(report)
        self.assertIn("↩️ 跳过：", out)
        self.assertIn("- a.exe：黑名单", out)
        self.assertIn("- 未命名：空文件", out)

    def test_notes_block(self) -> None:
        out = format_results(UploadReport(items=[image()], notes=["pack.zip 解压出 3 个文件。"]))
        self.assertIn("ℹ️ pack.zip 解压出 3 个文件。", out)

    def test_truncated_block(self) -> None:
        out = format_results(UploadReport(items=[image()], truncated=True))
        self.assertTrue(out.endswith("ℹ️ 达到数量或体积上限，剩余文件未处理。"))

    def test_plain_style_end_to_end(self) -> None:
        report = UploadReport(items=[image(size=1024)], elapsed=0.4)
        out = format_results(report, STYLE_PLAIN)
        self.assertEqual(out, "📤 成功 1、新上传 1.0KB、0.4s\n\na.png（1.0KB）\nhttps://cdn/a.png")

    def test_output_is_stripped(self) -> None:
        out = format_results(UploadReport(items=[image()]))
        self.assertEqual(out, out.strip())


class UploadedItemTest(unittest.TestCase):
    def test_is_image(self) -> None:
        self.assertTrue(image().is_image)
        self.assertFalse(doc().is_image)

    def test_to_dict_minimal(self) -> None:
        self.assertEqual(
            doc(size=5).to_dict(),
            {"name": "a.pdf", "url": "https://cdn/a.pdf", "size": 5},
        )

    def test_to_dict_optional_fields(self) -> None:
        item = doc(size=5, file_id="f/a.pdf", archive="p.zip", reused=True, note="n")
        self.assertEqual(
            item.to_dict(),
            {
                "name": "a.pdf",
                "url": "https://cdn/a.pdf",
                "size": 5,
                "file_id": "f/a.pdf",
                "from_archive": "p.zip",
                "reused": True,
                "note": "n",
            },
        )


class FailedItemTest(unittest.TestCase):
    def test_describe(self) -> None:
        self.assertEqual(FailedItem("a", "坏了").describe(), "坏了")
        self.assertEqual(FailedItem("a", "坏了", "重试").describe(), "坏了（重试）")

    def test_to_dict(self) -> None:
        self.assertEqual(FailedItem("a", "坏了").to_dict(), {"name": "a", "reason": "坏了"})
        self.assertEqual(
            FailedItem("a", "坏了", "重试").to_dict(),
            {"name": "a", "reason": "坏了", "hint": "重试"},
        )


class UploadReportTest(unittest.TestCase):
    def test_ok_and_counters(self) -> None:
        self.assertFalse(UploadReport().ok)
        report = UploadReport(items=[image(size=10), doc(size=20, reused=True)])
        self.assertTrue(report.ok)
        self.assertEqual(report.uploaded_bytes, 10)
        self.assertEqual(report.reused_count, 1)

    def test_default_lists_are_independent(self) -> None:
        a = UploadReport()
        b = UploadReport()
        a.items.append(image())
        a.notes.append("x")
        self.assertEqual(b.items, [])
        self.assertEqual(b.notes, [])

    def test_merge(self) -> None:
        left = UploadReport(items=[image()], notes=["l"], elapsed=1.0)
        right = UploadReport(
            items=[doc()],
            failures=[FailedItem("c", "r")],
            skipped=[("d", "s")],
            notes=["r"],
            elapsed=2.0,
            truncated=True,
        )
        left.merge(right)
        self.assertEqual(len(left.items), 2)
        self.assertEqual(len(left.failures), 1)
        self.assertEqual(left.skipped, [("d", "s")])
        self.assertEqual(left.notes, ["l", "r"])
        self.assertEqual(left.elapsed, 3.0)
        self.assertTrue(left.truncated)

    def test_merge_keeps_truncated_true(self) -> None:
        left = UploadReport(truncated=True)
        left.merge(UploadReport(truncated=False))
        self.assertTrue(left.truncated)

    def test_to_dict_minimal(self) -> None:
        self.assertEqual(
            UploadReport(items=[image()]).to_dict(),
            {
                "success": True,
                "uploaded": 1,
                "files": [{"name": "a.png", "url": "https://cdn/a.png", "size": 0}],
            },
        )

    def test_to_dict_full(self) -> None:
        report = UploadReport(
            failures=[FailedItem("c", "r")],
            skipped=[("d", "s")],
            notes=["n"],
            truncated=True,
        )
        data = report.to_dict()
        self.assertFalse(data["success"])
        self.assertEqual(data["failed"], [{"name": "c", "reason": "r"}])
        self.assertEqual(data["skipped"], [{"name": "d", "reason": "s"}])
        self.assertEqual(data["notes"], ["n"])
        self.assertTrue(data["truncated"])

    def test_to_dict_notes_are_copied(self) -> None:
        report = UploadReport(notes=["n"])
        data = report.to_dict()
        data["notes"].append("x")
        self.assertEqual(report.notes, ["n"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
