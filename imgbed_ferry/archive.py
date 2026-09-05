"""压缩包解析：安全地把压缩包里的文件取出来。

解压是最容易被打的入口，这里做了四层防护：

1. **目录穿越（zip slip）**：成员名一律按 POSIX 归一化，出现绝对路径、盘符、`..`
   直接丢弃，而不是「清洗后继续用」。
2. **解压炸弹**：同时限制成员数量、解压后总字节数，以及「解压后总量 / 压缩包体积」的
   膨胀比。
3. **谎报体积**：读取时按剩余额度 `read(limit + 1)`，不相信 header 里的 `file_size`。
4. **非普通文件**：软链接、硬链接、设备节点、目录全部跳过。

内置支持 zip 与 tar 全家桶；rar 需要可选依赖 `rarfile`（且依赖系统 unrar），
7z 需要 `py7zr`。缺依赖时给出明确提示，而不是静默失败。
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import tarfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import IO, Any

from .config import ArchiveConfig, normalize_extension
from .media import sanitize_filename

# 明确当作压缩包处理的扩展名。OOXML（docx/xlsx/pptx）与 apk 虽然本质是 zip，
# 但语义上是单个文档 / 安装包，不解压。
ARCHIVE_EXTENSIONS: frozenset[str] = frozenset(
    {
        "zip",
        "cbz",
        "tar",
        "tgz",
        "tbz",
        "tbz2",
        "txz",
        "gz",
        "bz2",
        "xz",
        "rar",
        "cbr",
        "7z",
    }
)

NON_ARCHIVE_ZIP_EXTENSIONS: frozenset[str] = frozenset(
    {"docx", "xlsx", "pptx", "apk", "jar", "epub", "odt", "ods", "odp", "ipa", "whl"}
)

# zip 未设 UTF-8 标志位时 zipfile 会用 cp437 解码，需要按东亚编码回捞。
_ZIP_NAME_ENCODINGS = ("utf-8", "gbk", "shift_jis", "big5", "euc-kr")

_TAR_DOUBLE_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.z")


class ArchiveError(Exception):
    """压缩包整体处理失败，`kind` 用于挑选给用户的提示。"""

    def __init__(self, message: str, *, kind: str = "archive") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind

    def __str__(self) -> str:  # pragma: no cover
        return self.message


@dataclass(slots=True)
class ArchiveEntry:
    """压缩包里的一个普通文件。"""

    name: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(slots=True)
class ArchiveExtraction:
    """一次解压的完整结果，含被跳过的原因清单。"""

    kind: str = ""
    entries: list[ArchiveEntry] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries)


class _BudgetExceeded(Exception):
    """内部信号：读取时撞到了解压额度。"""


class _Budget:
    """成员数 / 总字节 / 膨胀比三重额度。"""

    def __init__(self, cfg: ArchiveConfig, archive_size: int) -> None:
        ratio_cap = int(max(1, archive_size) * cfg.max_ratio)
        self.limit = min(cfg.max_total_bytes, ratio_cap)
        self.max_entries = cfg.max_entries
        self.used = 0
        self.count = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def entries_exhausted(self) -> bool:
        return self.count >= self.max_entries

    def take(self, size: int) -> None:
        self.used += size
        self.count += 1


def detect_archive_kind(name: str, data: bytes) -> str:
    """返回 zip/tar/gzip/bzip2/xz/rar/7z；不是压缩包则返回空串。"""
    lowered = (name or "").lower()
    extension = normalize_extension(PurePosixPath(lowered).suffix)
    if extension in NON_ARCHIVE_ZIP_EXTENSIONS:
        return ""

    head = data[:8]
    tar_wrapped = lowered.endswith(_TAR_DOUBLE_SUFFIXES) or extension in {
        "tgz",
        "tbz",
        "tbz2",
        "txz",
    }

    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        # 带明确非压缩后缀的 zip 容器不擅自解压。
        return "" if extension and extension not in ARCHIVE_EXTENSIONS else "zip"
    if head.startswith(b"\x1f\x8b"):
        return "tar" if tar_wrapped else "gzip"
    if head.startswith(b"BZh"):
        return "tar" if tar_wrapped else "bzip2"
    if head.startswith(b"\xfd7zXZ\x00"):
        return "tar" if tar_wrapped else "xz"
    if extension == "tar" or data[257:262] == b"ustar":
        return "tar"
    return ""


def is_archive(name: str, data: bytes) -> bool:
    return bool(detect_archive_kind(name, data))


def safe_member_name(raw: str, *, flatten: bool = False) -> str:
    """把成员名归一化成安全的相对路径；不安全则返回空串。"""
    text = (raw or "").replace("\\", "/").strip()
    if not text or text.endswith("/"):
        return ""
    if text.startswith(("/", "~")):
        return ""
    if len(text) >= 2 and text[1] == ":":  # C:/... 盘符
        return ""
    parts: list[str] = []
    for chunk in PurePosixPath(text).parts:
        if chunk in {"", ".", "/"}:
            continue
        if chunk == "..":
            return ""
        parts.append(sanitize_filename(chunk, fallback="_"))
    if not parts:
        return ""
    return parts[-1] if flatten else "/".join(parts)


def repair_zip_name(info: zipfile.ZipInfo) -> str:
    """修复 zip 里的乱码文件名（GBK / Shift-JIS 压出来的包很常见）。"""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for encoding in _ZIP_NAME_ENCODINGS:
        try:
            decoded = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\ufffd" not in decoded:
            return decoded
    return name


def _read_limited(stream: IO[bytes], budget: _Budget) -> bytes:
    limit = budget.remaining
    if limit <= 0:
        raise _BudgetExceeded
    payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise _BudgetExceeded
    return payload


def _password_error(exc: Exception) -> bool:
    lowered = str(exc).lower()
    return "password" in lowered or "encrypt" in lowered


def _iter_zip(
    data: bytes, cfg: ArchiveConfig, budget: _Budget, result: ArchiveExtraction
) -> Iterator[ArchiveEntry]:
    password = cfg.password.encode("utf-8") if cfg.password else None
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"zip 文件损坏或不完整：{exc}", kind="broken") from exc
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if budget.entries_exhausted():
                result.truncated = True
                return
            raw_name = repair_zip_name(info)
            name = safe_member_name(raw_name, flatten=cfg.flatten_names)
            if not name:
                result.skipped.append((raw_name or "<空名>", "成员名不安全，已丢弃"))
                continue
            if info.file_size > budget.remaining:
                result.skipped.append((name, "超出解压额度"))
                result.truncated = True
                continue
            try:
                with archive.open(info, pwd=password) as stream:
                    payload = _read_limited(stream, budget)
            except _BudgetExceeded:
                result.skipped.append((name, "超出解压额度"))
                result.truncated = True
                continue
            except (RuntimeError, zipfile.BadZipFile) as exc:
                if _password_error(exc):
                    raise ArchiveError(
                        "压缩包已加密，请在插件配置里填写解压密码", kind="password"
                    ) from exc
                result.skipped.append((name, f"读取失败：{exc}"))
                continue
            except Exception as exc:
                result.skipped.append((name, f"读取失败：{exc}"))
                continue
            budget.take(len(payload))
            yield ArchiveEntry(name=name, data=payload)


def _iter_tar(
    data: bytes, cfg: ArchiveConfig, budget: _Budget, result: ArchiveExtraction
) -> Iterator[ArchiveEntry]:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as exc:
        raise ArchiveError(f"tar 文件无法解析：{exc}", kind="broken") from exc
    with archive:
        for member in archive:
            if not member.isfile():
                if member.issym() or member.islnk():
                    result.skipped.append((member.name, "链接成员，已跳过"))
                continue
            if budget.entries_exhausted():
                result.truncated = True
                return
            name = safe_member_name(member.name, flatten=cfg.flatten_names)
            if not name:
                result.skipped.append((member.name or "<空名>", "成员名不安全，已丢弃"))
                continue
            if member.size > budget.remaining:
                result.skipped.append((name, "超出解压额度"))
                result.truncated = True
                continue
            stream = archive.extractfile(member)
            if stream is None:
                result.skipped.append((name, "无法读取成员"))
                continue
            try:
                payload = _read_limited(stream, budget)
            except _BudgetExceeded:
                result.skipped.append((name, "超出解压额度"))
                result.truncated = True
                continue
            finally:
                stream.close()
            budget.take(len(payload))
            yield ArchiveEntry(name=name, data=payload)


def _iter_rar(
    data: bytes, cfg: ArchiveConfig, budget: _Budget, result: ArchiveExtraction
) -> Iterator[ArchiveEntry]:
    try:
        import rarfile
    except ImportError as exc:
        raise ArchiveError(
            "解压 rar 需要可选依赖 rarfile，并且宿主机要有 unrar 或 bsdtar 命令",
            kind="missing_backend",
        ) from exc
    try:
        archive = rarfile.RarFile(io.BytesIO(data))
    except Exception as exc:
        if _password_error(exc):
            raise ArchiveError("压缩包已加密，请在插件配置里填写解压密码", kind="password") from exc
        raise ArchiveError(f"rar 文件无法解析：{exc}", kind="broken") from exc
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if budget.entries_exhausted():
                result.truncated = True
                return
            raw_name = getattr(info, "filename", "")
            name = safe_member_name(raw_name, flatten=cfg.flatten_names)
            if not name:
                result.skipped.append((raw_name or "<空名>", "成员名不安全，已丢弃"))
                continue
            declared = int(getattr(info, "file_size", 0) or 0)
            if declared > budget.remaining:
                result.skipped.append((name, "超出解压额度"))
                result.truncated = True
                continue
            try:
                with archive.open(info, pwd=cfg.password or None) as stream:
                    payload = _read_limited(stream, budget)
            except _BudgetExceeded:
                result.skipped.append((name, "超出解压额度"))
                result.truncated = True
                continue
            except Exception as exc:
                if _password_error(exc):
                    raise ArchiveError(
                        "压缩包已加密，请在插件配置里填写解压密码", kind="password"
                    ) from exc
                result.skipped.append((name, f"读取失败：{exc}"))
                continue
            budget.take(len(payload))
            yield ArchiveEntry(name=name, data=payload)


def _iter_7z(
    data: bytes, cfg: ArchiveConfig, budget: _Budget, result: ArchiveExtraction
) -> Iterator[ArchiveEntry]:
    try:
        import py7zr
    except ImportError as exc:
        raise ArchiveError(
            "解压 7z 需要可选依赖 py7zr（pip install py7zr）", kind="missing_backend"
        ) from exc
    try:
        archive = py7zr.SevenZipFile(io.BytesIO(data), mode="r", password=cfg.password or None)
    except Exception as exc:
        if _password_error(exc):
            raise ArchiveError("压缩包已加密，请在插件配置里填写解压密码", kind="password") from exc
        raise ArchiveError(f"7z 文件无法解析：{exc}", kind="broken") from exc
    with archive:
        try:
            members: dict[str, Any] = archive.readall() or {}
        except Exception as exc:
            if _password_error(exc):
                raise ArchiveError(
                    "压缩包已加密，请在插件配置里填写解压密码", kind="password"
                ) from exc
            raise ArchiveError(f"7z 解压失败：{exc}", kind="broken") from exc
        for raw_name, stream in members.items():
            if budget.entries_exhausted():
                result.truncated = True
                return
            name = safe_member_name(raw_name, flatten=cfg.flatten_names)
            if not name:
                result.skipped.append((raw_name or "<空名>", "成员名不安全，已丢弃"))
                continue
            try:
                payload = _read_limited(stream, budget)
            except _BudgetExceeded:
                result.skipped.append((name, "超出解压额度"))
                result.truncated = True
                continue
            except Exception as exc:
                result.skipped.append((name, f"读取失败：{exc}"))
                continue
            budget.take(len(payload))
            yield ArchiveEntry(name=name, data=payload)


def _single_stream_entry(kind: str, name: str, data: bytes, budget: _Budget) -> ArchiveEntry:
    factories = {
        "gzip": lambda: gzip.GzipFile(fileobj=io.BytesIO(data)),
        "bzip2": lambda: bz2.BZ2File(io.BytesIO(data)),
        "xz": lambda: lzma.LZMAFile(io.BytesIO(data)),
    }
    try:
        with factories[kind]() as stream:
            payload = _read_limited(stream, budget)
    except _BudgetExceeded as exc:
        raise ArchiveError("解压后体积超出上限，已中止（疑似解压炸弹）", kind="bomb") from exc
    except (OSError, EOFError, lzma.LZMAError) as exc:
        raise ArchiveError(f"解压失败：{exc}", kind="broken") from exc
    inner = PurePosixPath(sanitize_filename(name)).stem or "extracted"
    budget.take(len(payload))
    return ArchiveEntry(name=inner, data=payload)


_ITERATORS = {
    "zip": _iter_zip,
    "tar": _iter_tar,
    "rar": _iter_rar,
    "7z": _iter_7z,
}


def extract_archive(name: str, data: bytes, cfg: ArchiveConfig) -> ArchiveExtraction:
    """解压并返回全部安全成员；抛 `ArchiveError` 表示整个包处理不了。"""
    kind = detect_archive_kind(name, data)
    if not kind:
        raise ArchiveError("这个文件不是受支持的压缩包", kind="unsupported")

    result = ArchiveExtraction(kind=kind)
    budget = _Budget(cfg, len(data))

    if kind in {"gzip", "bzip2", "xz"}:
        result.entries.append(_single_stream_entry(kind, name, data, budget))
        return result

    for entry in _ITERATORS[kind](data, cfg, budget, result):
        result.entries.append(entry)

    if not result.entries and not result.skipped:
        raise ArchiveError("压缩包里没有可上传的文件", kind="empty")
    return result
