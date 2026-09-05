"""压缩包解压安全性测试：zip slip、解压炸弹、额度截断、乱码名、加密包。"""

from __future__ import annotations

import gzip
import importlib.util
import io
import tarfile
import unittest
import zipfile

from imgbed_ferry.archive import (
    ArchiveError,
    detect_archive_kind,
    extract_archive,
    is_archive,
    repair_zip_name,
    safe_member_name,
)
from imgbed_ferry.config import ArchiveConfig

HAS_PY7ZR = importlib.util.find_spec("py7zr") is not None
HAS_RARFILE = importlib.util.find_spec("rarfile") is not None


class _Cp437Info(zipfile.ZipInfo):
    """强制按 cp437 写文件名，模拟 GBK / Shift-JIS 压出来的老 zip。"""

    def _encodeFilenameFlags(self) -> tuple[bytes, int]:
        return self.filename.encode("cp437"), self.flag_bits


def make_zip(members: dict[str, bytes], *, legacy_names: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in members.items():
            if legacy_names:
                zf.writestr(_Cp437Info(name.encode("gbk").decode("cp437")), payload)
            else:
                zf.writestr(name, payload)
    return buffer.getvalue()


def mark_zip_encrypted(data: bytes) -> bytes:
    """翻起 zip 的加密标志位；zipfile 只能读加密包不能写，所以手工打标。"""
    buffer = bytearray(data)
    for signature, offset in ((b"PK\x01\x02", 8), (b"PK\x03\x04", 6)):
        index = buffer.find(signature)
        while index != -1:
            start = index + offset
            flags = int.from_bytes(buffer[start : start + 2], "little") | 0x1
            buffer[start : start + 2] = flags.to_bytes(2, "little")
            index = buffer.find(signature, index + 1)
    return bytes(buffer)


def make_tar(members: dict[str, bytes], *, links: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tf:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    return buffer.getvalue()


class SafeMemberNameTest(unittest.TestCase):
    def test_rejects_traversal(self) -> None:
        for raw in (
            "../evil.txt",
            "a/../../evil.txt",
            "..",
            "/etc/passwd",
            "//host/share/x.txt",
            r"C:\Windows\evil.exe",
            "D:/x.txt",
            "~/.ssh/id_rsa",
            "",
            "   ",
            "dir/",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(safe_member_name(raw), "")

    def test_normalizes_separators(self) -> None:
        self.assertEqual(safe_member_name(r"dir\sub\a.png"), "dir/sub/a.png")
        self.assertEqual(safe_member_name("./dir/a.png"), "dir/a.png")
        self.assertEqual(safe_member_name("dir//a.png"), "dir/a.png")

    def test_sanitizes_each_segment(self) -> None:
        self.assertEqual(safe_member_name('di<r>/a"b.png'), "di_r_/a_b.png")

    def test_flatten_keeps_last_segment(self) -> None:
        self.assertEqual(safe_member_name("dir/sub/a.png", flatten=True), "a.png")
        self.assertEqual(safe_member_name("a.png", flatten=True), "a.png")


class DetectArchiveKindTest(unittest.TestCase):
    def test_zip_family(self) -> None:
        blob = make_zip({"a.txt": b"hi"})
        self.assertEqual(detect_archive_kind("pack.zip", blob), "zip")
        self.assertEqual(detect_archive_kind("pack.cbz", blob), "zip")
        self.assertEqual(detect_archive_kind("noextension", blob), "zip")
        self.assertTrue(is_archive("pack.zip", blob))

    def test_office_and_apk_are_not_archives(self) -> None:
        blob = make_zip({"a.txt": b"hi"})
        for name in ("report.docx", "book.xlsx", "deck.pptx", "app.apk", "lib.jar", "b.epub"):
            with self.subTest(name=name):
                self.assertEqual(detect_archive_kind(name, blob), "")
                self.assertFalse(is_archive(name, blob))

    def test_zip_with_foreign_extension_is_left_alone(self) -> None:
        blob = make_zip({"a.txt": b"hi"})
        self.assertEqual(detect_archive_kind("weird.png", blob), "")

    def test_gzip_vs_tar_gz(self) -> None:
        blob = gzip.compress(b"hello")
        self.assertEqual(detect_archive_kind("notes.txt.gz", blob), "gzip")
        self.assertEqual(detect_archive_kind("bundle.tar.gz", blob), "tar")
        self.assertEqual(detect_archive_kind("bundle.tgz", blob), "tar")

    def test_bzip2_and_xz(self) -> None:
        self.assertEqual(detect_archive_kind("a.bz2", b"BZh9payload"), "bzip2")
        self.assertEqual(detect_archive_kind("a.tar.bz2", b"BZh9payload"), "tar")
        self.assertEqual(detect_archive_kind("a.xz", b"\xfd7zXZ\x00rest"), "xz")
        self.assertEqual(detect_archive_kind("a.txz", b"\xfd7zXZ\x00rest"), "tar")

    def test_plain_tar_by_ustar_magic(self) -> None:
        blob = make_tar({"a.txt": b"hi"})
        self.assertEqual(detect_archive_kind("bundle.tar", blob), "tar")
        self.assertEqual(detect_archive_kind("noextension", blob), "tar")

    def test_rar_and_7z_magic(self) -> None:
        self.assertEqual(detect_archive_kind("a.rar", b"Rar!\x1a\x07\x00rest"), "rar")
        self.assertEqual(detect_archive_kind("a.7z", b"7z\xbc\xaf\x27\x1crest"), "7z")

    def test_not_an_archive(self) -> None:
        self.assertEqual(detect_archive_kind("a.png", b"\x89PNG\r\n\x1a\n"), "")
        self.assertEqual(detect_archive_kind("a.txt", b"plain text"), "")
        self.assertEqual(detect_archive_kind("", b""), "")


class ExtractZipTest(unittest.TestCase):
    def test_extracts_and_keeps_inner_structure(self) -> None:
        blob = make_zip({"dir/a.txt": b"aaa", "dir/sub/b.txt": b"bb", "c.txt": b"c"})
        result = extract_archive("pack.zip", blob, ArchiveConfig())
        self.assertEqual(result.kind, "zip")
        self.assertEqual(
            [entry.name for entry in result.entries],
            ["dir/a.txt", "dir/sub/b.txt", "c.txt"],
        )
        self.assertEqual(result.total_bytes, 6)
        self.assertFalse(result.truncated)
        self.assertEqual(result.skipped, [])

    def test_flatten_names(self) -> None:
        blob = make_zip({"dir/sub/b.txt": b"bb"})
        result = extract_archive("pack.zip", blob, ArchiveConfig(flatten_names=True))
        self.assertEqual([entry.name for entry in result.entries], ["b.txt"])

    def test_zip_slip_members_are_dropped(self) -> None:
        blob = make_zip({"../evil.txt": b"x", "ok.txt": b"y"})
        result = extract_archive("pack.zip", blob, ArchiveConfig())
        self.assertEqual([entry.name for entry in result.entries], ["ok.txt"])
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0][0], "../evil.txt")
        self.assertIn("不安全", result.skipped[0][1])

    def test_directory_entries_are_ignored(self) -> None:
        blob = make_zip({"dir/": b"", "dir/a.txt": b"a"})
        result = extract_archive("pack.zip", blob, ArchiveConfig())
        self.assertEqual([entry.name for entry in result.entries], ["dir/a.txt"])

    def test_max_entries_truncates(self) -> None:
        blob = make_zip({f"f{index}.txt": b"x" for index in range(6)})
        result = extract_archive("pack.zip", blob, ArchiveConfig(max_entries=2))
        self.assertEqual(len(result.entries), 2)
        self.assertTrue(result.truncated)

    def test_byte_budget_skips_oversized_member(self) -> None:
        blob = make_zip({"a.bin": b"a" * 1000, "b.bin": b"b" * 500})
        result = extract_archive(
            "pack.zip", blob, ArchiveConfig(max_total_mb=0.001, max_ratio=10000.0)
        )
        self.assertEqual([entry.name for entry in result.entries], ["a.bin"])
        self.assertTrue(result.truncated)
        self.assertEqual(result.skipped, [("b.bin", "超出解压额度")])

    def test_ratio_budget_blocks_bomb_members(self) -> None:
        blob = make_zip({"bomb.bin": b"\x00" * (2 * 1024 * 1024)})
        result = extract_archive("pack.zip", blob, ArchiveConfig(max_ratio=2.0))
        self.assertEqual(result.entries, [])
        self.assertTrue(result.truncated)
        self.assertEqual(result.skipped[0][1], "超出解压额度")

    def test_empty_archive_raises(self) -> None:
        blob = make_zip({"dir/": b""})
        with self.assertRaises(ArchiveError) as ctx:
            extract_archive("pack.zip", blob, ArchiveConfig())
        self.assertEqual(ctx.exception.kind, "empty")

    def test_broken_zip_raises(self) -> None:
        with self.assertRaises(ArchiveError) as ctx:
            extract_archive("pack.zip", b"PK\x03\x04 not really a zip", ArchiveConfig())
        self.assertEqual(ctx.exception.kind, "broken")

    def test_unsupported_raises(self) -> None:
        with self.assertRaises(ArchiveError) as ctx:
            extract_archive("a.txt", b"plain text", ArchiveConfig())
        self.assertEqual(ctx.exception.kind, "unsupported")

    def test_encrypted_zip_raises_password(self) -> None:
        blob = mark_zip_encrypted(make_zip({"secret.txt": b"hello world"}))
        for password in ("", "wrong-password"):
            with self.subTest(password=password), self.assertRaises(ArchiveError) as ctx:
                extract_archive("pack.zip", blob, ArchiveConfig(password=password))
            self.assertEqual(ctx.exception.kind, "password")
            self.assertIn("解压密码", ctx.exception.message)


class LegacyZipNameTest(unittest.TestCase):
    def test_repair_zip_name_gbk(self) -> None:
        info = zipfile.ZipInfo("测试目录/文件.txt".encode("gbk").decode("cp437"))
        info.flag_bits = 0
        self.assertEqual(repair_zip_name(info), "测试目录/文件.txt")

    def test_repair_zip_name_shift_jis(self) -> None:
        # 半角片假名在 GBK 下解不出来，才能确定命中 shift_jis 分支；
        # 汉字名在 GBK / Shift-JIS 下天然歧义，插件按 _ZIP_NAME_ENCODINGS 顺序取第一个能解的。
        name = "ｽｸﾘｰﾝ.png"
        info = zipfile.ZipInfo(name.encode("shift_jis").decode("cp437"))
        info.flag_bits = 0
        self.assertEqual(repair_zip_name(info), name)

    def test_undecodable_name_is_left_as_is(self) -> None:
        info = zipfile.ZipInfo("plain-ascii.txt")
        info.flag_bits = 0
        self.assertEqual(repair_zip_name(info), "plain-ascii.txt")

    def test_utf8_flag_short_circuits(self) -> None:
        info = zipfile.ZipInfo("已经是 UTF-8.txt")
        info.flag_bits = 0x800
        self.assertEqual(repair_zip_name(info), "已经是 UTF-8.txt")

    def test_extract_repairs_names(self) -> None:
        blob = make_zip({"图片/照片.png": b"\x89PNG\r\n\x1a\n"}, legacy_names=True)
        result = extract_archive("pack.zip", blob, ArchiveConfig())
        self.assertEqual([entry.name for entry in result.entries], ["图片/照片.png"])


class ExtractTarTest(unittest.TestCase):
    def test_extracts_plain_tar(self) -> None:
        blob = make_tar({"dir/a.txt": b"aaa", "b.txt": b"b"})
        result = extract_archive("bundle.tar", blob, ArchiveConfig())
        self.assertEqual(result.kind, "tar")
        self.assertEqual([entry.name for entry in result.entries], ["dir/a.txt", "b.txt"])

    def test_extracts_tar_gz(self) -> None:
        blob = gzip.compress(make_tar({"a.txt": b"aaa"}))
        result = extract_archive("bundle.tar.gz", blob, ArchiveConfig())
        self.assertEqual([entry.name for entry in result.entries], ["a.txt"])

    def test_drops_unsafe_and_link_members(self) -> None:
        blob = make_tar({"../evil.txt": b"x", "ok.txt": b"y"}, links={"link.txt": "/etc/passwd"})
        result = extract_archive("bundle.tar", blob, ArchiveConfig())
        self.assertEqual([entry.name for entry in result.entries], ["ok.txt"])
        reasons = dict(result.skipped)
        self.assertIn("不安全", reasons["../evil.txt"])
        self.assertEqual(reasons["link.txt"], "链接成员，已跳过")

    def test_broken_tar_raises(self) -> None:
        payload = b"\x00" * 257 + b"ustar" + b"\x00" * 100
        with self.assertRaises(ArchiveError) as ctx:
            extract_archive("bundle.tar", payload, ArchiveConfig())
        self.assertEqual(ctx.exception.kind, "broken")


class SingleStreamTest(unittest.TestCase):
    def test_gzip_single_file(self) -> None:
        blob = gzip.compress(b"hello world" * 10)
        result = extract_archive("notes.txt.gz", blob, ArchiveConfig())
        self.assertEqual(result.kind, "gzip")
        self.assertEqual([entry.name for entry in result.entries], ["notes.txt"])
        self.assertEqual(result.entries[0].data, b"hello world" * 10)

    def test_gzip_inner_name_drops_only_last_suffix(self) -> None:
        blob = gzip.compress(b"hi")
        result = extract_archive("backup.tar.txt.gz", blob, ArchiveConfig())
        self.assertEqual([entry.name for entry in result.entries], ["backup.tar.txt"])
        # 只有 ".gz" 这种没有主干的名字，兜底成扩展名本身
        result = extract_archive(".gz", blob, ArchiveConfig())
        self.assertEqual([entry.name for entry in result.entries], ["gz"])

    def test_gzip_bomb_raises(self) -> None:
        blob = gzip.compress(b"\x00" * (4 * 1024 * 1024))
        with self.assertRaises(ArchiveError) as ctx:
            extract_archive("bomb.gz", blob, ArchiveConfig(max_ratio=2.0))
        self.assertEqual(ctx.exception.kind, "bomb")
        self.assertIn("解压炸弹", ctx.exception.message)

    def test_broken_gzip_raises(self) -> None:
        with self.assertRaises(ArchiveError) as ctx:
            extract_archive("a.gz", b"\x1f\x8b\x08 garbage", ArchiveConfig())
        self.assertEqual(ctx.exception.kind, "broken")

    def test_bzip2_single_file(self) -> None:
        import bz2

        result = extract_archive("a.txt.bz2", bz2.compress(b"payload"), ArchiveConfig())
        self.assertEqual(result.entries[0].data, b"payload")

    def test_xz_single_file(self) -> None:
        import lzma

        result = extract_archive("a.txt.xz", lzma.compress(b"payload"), ArchiveConfig())
        self.assertEqual(result.entries[0].data, b"payload")


class OptionalBackendTest(unittest.TestCase):
    @unittest.skipIf(HAS_RARFILE, "已安装 rarfile，跳过缺依赖分支")
    def test_rar_without_backend(self) -> None:
        with self.assertRaises(ArchiveError) as ctx:
            extract_archive("a.rar", b"Rar!\x1a\x07\x00rest", ArchiveConfig())
        self.assertEqual(ctx.exception.kind, "missing_backend")
        self.assertIn("rarfile", ctx.exception.message)

    @unittest.skipIf(HAS_PY7ZR, "已安装 py7zr，跳过缺依赖分支")
    def test_7z_without_backend(self) -> None:
        with self.assertRaises(ArchiveError) as ctx:
            extract_archive("a.7z", b"7z\xbc\xaf\x27\x1crest", ArchiveConfig())
        self.assertEqual(ctx.exception.kind, "missing_backend")
        self.assertIn("py7zr", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
