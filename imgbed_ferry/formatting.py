"""把上传结果渲染成用户想要的文本形式。

四种风格覆盖了常见的粘贴场景：聊天里直接看（plain）、写文档（markdown）、
贴进网页（html）、发论坛（bbcode）。
"""

from __future__ import annotations

import html as html_lib

from .media import human_size
from .results import UploadedItem, UploadReport

STYLE_PLAIN = "plain"
STYLE_MARKDOWN = "markdown"
STYLE_HTML = "html"
STYLE_BBCODE = "bbcode"

_STYLE_ALIASES = {
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
}


def normalize_style(style: str, default: str = STYLE_MARKDOWN) -> str:
    return _STYLE_ALIASES.get(str(style or "").strip().lower(), default)


def _md_escape(text: str) -> str:
    return text.replace("[", "\\[").replace("]", "\\]")


def format_link(
    url: str, name: str = "", style: str = STYLE_MARKDOWN, *, image: bool = False
) -> str:
    """把一条 URL 渲染成指定风格的链接。"""
    resolved = normalize_style(style)
    label = name or url
    if resolved == STYLE_PLAIN:
        return url
    if resolved == STYLE_MARKDOWN:
        prefix = "!" if image else ""
        return f"{prefix}[{_md_escape(label)}]({url})"
    if resolved == STYLE_HTML:
        safe_url = html_lib.escape(url, quote=True)
        safe_label = html_lib.escape(label, quote=True)
        if image:
            return f'<img src="{safe_url}" alt="{safe_label}">'
        return f'<a href="{safe_url}">{safe_label}</a>'
    if image:
        return f"[img]{url}[/img]"
    return f"[url={url}]{label}[/url]"


def format_item(item: UploadedItem, style: str = STYLE_MARKDOWN, *, show_size: bool = True) -> str:
    resolved = normalize_style(style)
    link = format_link(item.url, item.name, resolved, image=item.is_image)
    if resolved == STYLE_PLAIN:
        head = item.name or "file"
        tail = f"（{human_size(item.size)}）" if show_size and item.size else ""
        return f"{head}{tail}\n{link}"
    suffix = (
        f" `{human_size(item.size)}`"
        if show_size and item.size and resolved == STYLE_MARKDOWN
        else ""
    )
    return f"{link}{suffix}"


def format_urls(report: UploadReport) -> str:
    """只要 URL，一行一条 —— 方便直接复制。"""
    return "\n".join(item.url for item in report.items if item.url)


def summary_line(report: UploadReport, *, show_size: bool = True) -> str:
    """一句话总览，放在链接列表前面。"""
    parts = [f"成功 {len(report.items)}"]
    if report.reused_count:
        parts.append(f"命中去重 {report.reused_count}")
    if report.failures:
        parts.append(f"失败 {len(report.failures)}")
    if report.skipped:
        parts.append(f"跳过 {len(report.skipped)}")
    if show_size and report.uploaded_bytes:
        parts.append(f"新上传 {human_size(report.uploaded_bytes)}")
    if report.elapsed:
        parts.append(f"{report.elapsed:.1f}s")
    return "、".join(parts)


def format_results(
    report: UploadReport,
    style: str = STYLE_MARKDOWN,
    *,
    show_size: bool = True,
    numbered: bool = True,
) -> str:
    """完整回执：总览 + 链接列表 + 失败与跳过说明。"""
    resolved = normalize_style(style)
    lines: list[str] = []
    if report.items:
        lines.append(f"📤 {summary_line(report, show_size=show_size)}")
        lines.append("")
        for index, item in enumerate(report.items, start=1):
            body = format_item(item, resolved, show_size=show_size)
            prefix = f"{index}. " if numbered and len(report.items) > 1 else ""
            marks: list[str] = []
            if item.reused:
                marks.append("已存在，复用")
            if item.archive:
                marks.append(f"来自 {item.archive}")
            if item.note:
                marks.append(item.note)
            note = f"（{'；'.join(marks)}）" if marks else ""
            lines.append(f"{prefix}{body}{note}")
    else:
        lines.append("📤 没有文件上传成功。")

    if report.failures:
        lines.append("")
        lines.append("⚠️ 失败：")
        lines.extend(f"- {item.name or '未命名'}：{item.describe()}" for item in report.failures)

    if report.skipped:
        lines.append("")
        lines.append("↩️ 跳过：")
        lines.extend(f"- {name or '未命名'}：{reason}" for name, reason in report.skipped)

    if report.notes:
        lines.append("")
        lines.extend(f"ℹ️ {note}" for note in report.notes)

    if report.truncated:
        lines.append("")
        lines.append("ℹ️ 达到数量或体积上限，剩余文件未处理。")

    return "\n".join(lines).strip()
