"""文件名、MIME、扩展名与本地图片压缩。

图床服务端会按扩展名做白名单校验（`isExtValid`），而 OneBot 传过来的文件名经常是
`image`、`tmp_xxx` 这种没有后缀的东西，所以这里统一做「魔数嗅探 → MIME → 扩展名」
的兜底链，让上传的文件名尽量带上正确后缀。
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .config import CompressConfig, normalize_extension

_ILLEGAL_NAME_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_MULTI_UNDERSCORE = re.compile(r"_{3,}")

# 魔数 → (扩展名, MIME)。只放常见类型，够用且不引入额外依赖。
MAGIC_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
    (b"BM", "bmp", "image/bmp"),
    (b"\x00\x00\x01\x00", "ico", "image/x-icon"),
    (b"%PDF-", "pdf", "application/pdf"),
    (b"PK\x03\x04", "zip", "application/zip"),
    (b"Rar!\x1a\x07", "rar", "application/vnd.rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z", "application/x-7z-compressed"),
    (b"\x1f\x8b", "gz", "application/gzip"),
    (b"BZh", "bz2", "application/x-bzip2"),
    (b"\xfd7zXZ\x00", "xz", "application/x-xz"),
    (b"\x28\xb5\x2f\xfd", "zst", "application/zstd"),
    (b"ID3", "mp3", "audio/mpeg"),
    (b"fLaC", "flac", "audio/flac"),
    (b"OggS", "ogg", "audio/ogg"),
    (b"\x1aE\xdf\xa3", "mkv", "video/x-matroska"),
    (b"\x25\x21PS", "eps", "application/postscript"),
    (b"8BPS", "psd", "image/vnd.adobe.photoshop"),
)

EXTENSION_MIME: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "ico": "image/x-icon",
    "svg": "image/svg+xml",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "mov": "video/quicktime",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
    "m4a": "audio/mp4",
    "pdf": "application/pdf",
    "zip": "application/zip",
    "rar": "application/vnd.rar",
    "7z": "application/x-7z-compressed",
    "tar": "application/x-tar",
    "gz": "application/gzip",
    "bz2": "application/x-bzip2",
    "xz": "application/x-xz",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "json": "application/json",
    "csv": "text/csv; charset=utf-8",
}

IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif"})

# 只用于「这看起来像压缩包」的轻量判断（素材 id 前缀等），真正的识别走 archive.py 的魔数。
ARCHIVE_HINT_EXTENSIONS: frozenset[str] = frozenset(
    {
        "zip",
        "cbz",
        "rar",
        "cbr",
        "7z",
        "tar",
        "tgz",
        "tbz",
        "tbz2",
        "txz",
        "gz",
        "bz2",
        "xz",
        "zst",
    }
)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def short_hash(*parts: str) -> str:
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8", "replace"))
    return digest.hexdigest()[:10]


def human_size(size: float) -> str:
    value = float(max(0.0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def sniff_extension(data: bytes) -> tuple[str, str]:
    """按魔数猜 (扩展名, MIME)，猜不出返回 ("", "")。"""
    head = data[:32]
    for signature, extension, mime in MAGIC_SIGNATURES:
        if head.startswith(signature):
            if extension == "zip" and _looks_like_ooxml(data):
                return _ooxml_kind(data)
            return extension, mime
    if head[4:12] in (b"ftypisom", b"ftypmp42", b"ftypM4V ", b"ftypavc1"):
        return "mp4", "video/mp4"
    if head[4:8] == b"ftyp":
        return "mp4", "video/mp4"
    if head[:4] == b"RIFF":
        if data[8:12] == b"WEBP":
            return "webp", "image/webp"
        if data[8:12] == b"WAVE":
            return "wav", "audio/wav"
        if data[8:12] == b"AVI ":
            return "avi", "video/x-msvideo"
    return "", ""


def _looks_like_ooxml(data: bytes) -> bool:
    return b"[Content_Types].xml" in data[:4096]


def _ooxml_kind(data: bytes) -> tuple[str, str]:
    head = data[:8192]
    if b"word/" in head:
        return (
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    if b"xl/" in head:
        return "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if b"ppt/" in head:
        return (
            "pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
    return "zip", "application/zip"


def sanitize_filename(name: str, *, fallback: str = "file") -> str:
    """裁掉路径、控制字符与平台非法字符，只留一个安全的文件名。"""
    text = (name or "").replace("\\", "/")
    text = PurePosixPath(text).name
    text = _ILLEGAL_NAME_CHARS.sub("_", text).strip().strip(".")
    text = _MULTI_UNDERSCORE.sub("__", text)
    if not text or text in {".", ".."}:
        return fallback
    if len(text) > 120:
        stem, dot, ext = text.rpartition(".")
        text = f"{stem[: 120 - len(ext) - 1]}.{ext}" if dot and len(ext) <= 8 else text[:120]
    return text


def split_extension(name: str) -> tuple[str, str]:
    """返回 (主干, 小写扩展名)。多重后缀如 `.tar.gz` 只取最后一段。"""
    stem, dot, ext = (name or "").rpartition(".")
    if not dot or not ext or len(ext) > 12 or "/" in ext:
        return name or "", ""
    return stem, normalize_extension(ext)


def guess_mime(extension: str, *, default: str = "application/octet-stream") -> str:
    ext = normalize_extension(extension)
    if ext in EXTENSION_MIME:
        return EXTENSION_MIME[ext]
    guessed, _ = mimetypes.guess_type(f"x.{ext}") if ext else (None, None)
    return guessed or default


@dataclass(slots=True)
class NormalizedFile:
    """规范化之后准备上传的文件。"""

    filename: str
    data: bytes
    mime: str
    extension: str
    sniffed: bool = False

    @property
    def size(self) -> int:
        return len(self.data)


def normalize_file(name: str, data: bytes, *, declared_mime: str = "") -> NormalizedFile:
    """补齐文件名后缀与 MIME，尽量让图床白名单能认。"""
    safe_name = sanitize_filename(name)
    stem, extension = split_extension(safe_name)
    sniffed_ext, sniffed_mime = sniff_extension(data)
    used_sniff = False

    if not extension and sniffed_ext:
        extension = sniffed_ext
        safe_name = f"{stem or 'file'}.{extension}"
        used_sniff = True
    elif not extension and declared_mime:
        guessed = mimetypes.guess_extension(declared_mime.split(";")[0].strip() or "")
        if guessed:
            extension = normalize_extension(guessed)
            safe_name = f"{stem or 'file'}.{extension}"
            used_sniff = True

    if not extension:
        extension = "bin"
        safe_name = f"{stem or 'file'}.bin"
        used_sniff = True

    mime = sniffed_mime or declared_mime or guess_mime(extension)
    return NormalizedFile(
        filename=safe_name,
        data=data,
        mime=mime,
        extension=extension,
        sniffed=used_sniff,
    )


def is_image_extension(extension: str) -> bool:
    return normalize_extension(extension) in IMAGE_EXTENSIONS


@dataclass(slots=True)
class CompressOutcome:
    """压缩结果。`changed=False` 表示原样返回。"""

    file: NormalizedFile
    changed: bool = False
    note: str = ""
    original_size: int = 0


def compress_image(source: NormalizedFile, cfg: CompressConfig) -> CompressOutcome:
    """按配置对图片做「限长边 + 转码降质量」的本地压缩。

    压缩只在能明确变小的时候才生效；Pillow 缺失、格式不支持或结果更大都会原样返回，
    这样即便压缩链路出问题也不会阻断上传。
    """
    original_size = source.size
    if not cfg.enabled:
        return CompressOutcome(file=source, original_size=original_size)
    if not is_image_extension(source.extension):
        return CompressOutcome(file=source, original_size=original_size)
    if cfg.skip_gif and source.extension == "gif":
        return CompressOutcome(file=source, note="GIF 按配置跳过压缩", original_size=original_size)
    if original_size <= cfg.trigger_bytes:
        return CompressOutcome(file=source, original_size=original_size)

    try:
        from PIL import Image
    except ImportError:
        return CompressOutcome(
            file=source, note="未安装 Pillow，跳过本地压缩", original_size=original_size
        )

    try:
        with Image.open(io.BytesIO(source.data)) as image:
            image.load()
            has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
            work = image.convert("RGBA" if has_alpha else "RGB")
            if max(work.size) > cfg.max_edge:
                ratio = cfg.max_edge / float(max(work.size))
                target = (
                    max(1, int(work.width * ratio)),
                    max(1, int(work.height * ratio)),
                )
                work = work.resize(target, Image.LANCZOS)

            target_format = cfg.target_format
            if target_format == "keep":
                target_format = "webp" if source.extension == "webp" else "jpeg"
            if has_alpha and cfg.keep_transparency:
                target_format = "webp"
            if target_format == "jpeg" and work.mode != "RGB":
                work = work.convert("RGB")

            buffer = io.BytesIO()
            save_kwargs: dict[str, object] = {"quality": cfg.quality}
            if target_format == "webp":
                save_kwargs["method"] = 4
            else:
                save_kwargs["optimize"] = True
                save_kwargs["progressive"] = True
            work.save(buffer, format="WEBP" if target_format == "webp" else "JPEG", **save_kwargs)
            payload = buffer.getvalue()
    except Exception as exc:  # 坏图、动图、不支持的模式都别把上传拖死
        return CompressOutcome(
            file=source, note=f"压缩失败，改为原图上传：{exc}", original_size=original_size
        )

    if len(payload) >= original_size:
        return CompressOutcome(
            file=source, note="压缩后反而更大，保留原图", original_size=original_size
        )

    extension = "webp" if target_format == "webp" else "jpg"
    stem, _ = split_extension(source.filename)
    compressed = NormalizedFile(
        filename=f"{stem or 'image'}.{extension}",
        data=payload,
        mime=guess_mime(extension),
        extension=extension,
        sniffed=source.sniffed,
    )
    saved = 100.0 * (1 - len(payload) / max(1, original_size))
    return CompressOutcome(
        file=compressed,
        changed=True,
        note=f"{human_size(original_size)} → {human_size(len(payload))}（-{saved:.0f}%）",
        original_size=original_size,
    )
