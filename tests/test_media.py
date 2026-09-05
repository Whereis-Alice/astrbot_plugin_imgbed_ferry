"""文件名规范化、魔数嗅探与本地图片压缩测试。"""

from __future__ import annotations

import io
import unittest
import zipfile

from imgbed_ferry.config import CompressConfig
from imgbed_ferry.media import (
    NormalizedFile,
    compress_image,
    guess_mime,
    human_size,
    is_image_extension,
    normalize_file,
    sanitize_filename,
    sha256_hex,
    short_hash,
    sniff_extension,
    split_extension,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_bytes(width: int = 8, height: int = 8, *, alpha: bool = False) -> bytes:
    from PIL import Image

    mode = "RGBA" if alpha else "RGB"
    image = Image.new(mode, (width, height), (10, 120, 200, 255) if alpha else (10, 120, 200))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def noise_bytes(
    width: int, height: int, *, fmt: str = "PNG", alpha: bool = False, **kwargs: object
) -> bytes:
    """生成不可压缩的噪点图：PNG 体积大，缩放后转码必然小很多。"""
    import random

    from PIL import Image

    rng = random.Random(20260905)
    mode = "RGBA" if alpha else "RGB"
    channels = 4 if alpha else 3
    image = Image.frombytes(mode, (width, height), rng.randbytes(width * height * channels))
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def ooxml_bytes(prefix: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(f"{prefix}/document.xml", "<x/>")
    return buffer.getvalue()


class HashAndSizeTest(unittest.TestCase):
    def test_sha256_hex(self) -> None:
        self.assertEqual(len(sha256_hex(b"abc")), 64)
        self.assertEqual(sha256_hex(b"abc"), sha256_hex(b"abc"))
        self.assertNotEqual(sha256_hex(b"abc"), sha256_hex(b"abd"))

    def test_short_hash(self) -> None:
        self.assertEqual(len(short_hash("a", "b")), 10)
        # 分隔符保证 ("ab","") 与 ("a","b") 不撞车
        self.assertNotEqual(short_hash("ab", ""), short_hash("a", "b"))

    def test_human_size(self) -> None:
        self.assertEqual(human_size(0), "0B")
        self.assertEqual(human_size(512), "512B")
        self.assertEqual(human_size(1024), "1.0KB")
        self.assertEqual(human_size(1536), "1.5KB")
        self.assertEqual(human_size(5 * 1024 * 1024), "5.0MB")
        self.assertEqual(human_size(3 * 1024**3), "3.0GB")
        self.assertEqual(human_size(-10), "0B")


class NameHelperTest(unittest.TestCase):
    def test_sanitize_filename_strips_path(self) -> None:
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename(r"C:\temp\a.png"), "a.png")
        self.assertEqual(sanitize_filename("dir/sub/b.jpg"), "b.jpg")

    def test_sanitize_filename_illegal_chars(self) -> None:
        self.assertEqual(sanitize_filename('a<b>c:"d?.png'), "a_b_c__d_.png")
        self.assertEqual(sanitize_filename("a\x00b.png"), "a_b.png")
        self.assertEqual(sanitize_filename("a_____b.png"), "a__b.png")

    def test_sanitize_filename_fallback(self) -> None:
        self.assertEqual(sanitize_filename(""), "file")
        self.assertEqual(sanitize_filename("..."), "file")
        self.assertEqual(sanitize_filename("   "), "file")
        self.assertEqual(sanitize_filename("/", fallback="_"), "_")

    def test_sanitize_filename_length_cap(self) -> None:
        name = f"{'x' * 200}.png"
        result = sanitize_filename(name)
        self.assertEqual(len(result), 120)
        self.assertTrue(result.endswith(".png"))

        long_ext = f"{'x' * 200}.{'y' * 30}"
        self.assertEqual(len(sanitize_filename(long_ext)), 120)

    def test_split_extension(self) -> None:
        self.assertEqual(split_extension("a.PNG"), ("a", "png"))
        self.assertEqual(split_extension("archive.tar.gz"), ("archive.tar", "gz"))
        self.assertEqual(split_extension("noext"), ("noext", ""))
        self.assertEqual(split_extension("a.verylongextension"), ("a.verylongextension", ""))
        self.assertEqual(split_extension("dir.name/file"), ("dir.name/file", ""))
        self.assertEqual(split_extension(""), ("", ""))

    def test_guess_mime(self) -> None:
        self.assertEqual(guess_mime("PNG"), "image/png")
        self.assertEqual(guess_mime("zip"), "application/zip")
        self.assertEqual(guess_mime(""), "application/octet-stream")
        self.assertEqual(guess_mime("nosuchext"), "application/octet-stream")

    def test_is_image_extension(self) -> None:
        self.assertTrue(is_image_extension(".JPG"))
        self.assertTrue(is_image_extension("webp"))
        self.assertFalse(is_image_extension("pdf"))
        self.assertFalse(is_image_extension(""))


class SniffTest(unittest.TestCase):
    def test_sniff_common_magics(self) -> None:
        cases = {
            PNG_MAGIC + b"rest": "png",
            b"\xff\xd8\xff\xe0": "jpg",
            b"GIF89a...": "gif",
            b"%PDF-1.7": "pdf",
            b"Rar!\x1a\x07\x00": "rar",
            b"7z\xbc\xaf\x27\x1c": "7z",
            b"\x1f\x8b\x08": "gz",
            b"BZh9": "bz2",
            b"\xfd7zXZ\x00": "xz",
        }
        for data, extension in cases.items():
            with self.subTest(extension=extension):
                self.assertEqual(sniff_extension(data)[0], extension)

    def test_sniff_riff_family(self) -> None:
        self.assertEqual(
            sniff_extension(b"RIFF" + b"\x00" * 4 + b"WEBPxxxx"), ("webp", "image/webp")
        )
        self.assertEqual(sniff_extension(b"RIFF" + b"\x00" * 4 + b"WAVEfmt ")[0], "wav")
        self.assertEqual(sniff_extension(b"RIFF" + b"\x00" * 4 + b"AVI LIST")[0], "avi")

    def test_sniff_mp4(self) -> None:
        self.assertEqual(sniff_extension(b"\x00\x00\x00 ftypisom" + b"\x00" * 16)[0], "mp4")

    def test_sniff_ooxml_vs_plain_zip(self) -> None:
        self.assertEqual(sniff_extension(ooxml_bytes("word"))[0], "docx")
        self.assertEqual(sniff_extension(ooxml_bytes("xl"))[0], "xlsx")
        self.assertEqual(sniff_extension(ooxml_bytes("ppt"))[0], "pptx")

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("a.txt", "hi")
        self.assertEqual(sniff_extension(buffer.getvalue())[0], "zip")

    def test_sniff_unknown(self) -> None:
        self.assertEqual(sniff_extension(b"just plain text"), ("", ""))
        self.assertEqual(sniff_extension(b""), ("", ""))


class NormalizeFileTest(unittest.TestCase):
    def test_keeps_existing_extension(self) -> None:
        result = normalize_file("photo.png", PNG_MAGIC + b"x")
        self.assertEqual(result.filename, "photo.png")
        self.assertEqual(result.extension, "png")
        self.assertFalse(result.sniffed)
        self.assertEqual(result.mime, "image/png")
        self.assertEqual(result.size, len(PNG_MAGIC) + 1)

    def test_fills_extension_from_magic(self) -> None:
        result = normalize_file("tmp_image", PNG_MAGIC + b"x")
        self.assertEqual(result.filename, "tmp_image.png")
        self.assertEqual(result.extension, "png")
        self.assertTrue(result.sniffed)

    def test_fills_extension_from_declared_mime(self) -> None:
        result = normalize_file("clip", b"nothing recognizable", declared_mime="text/plain")
        self.assertEqual(result.extension, "txt")
        self.assertTrue(result.filename.endswith(".txt"))
        self.assertTrue(result.sniffed)

    def test_falls_back_to_bin(self) -> None:
        result = normalize_file("blob", b"nothing recognizable")
        self.assertEqual(result.filename, "blob.bin")
        self.assertEqual(result.extension, "bin")
        self.assertTrue(result.sniffed)
        self.assertEqual(result.mime, "application/octet-stream")

    def test_empty_name_gets_fallback_stem(self) -> None:
        result = normalize_file("", PNG_MAGIC)
        self.assertEqual(result.filename, "file.png")

    def test_strips_directories(self) -> None:
        result = normalize_file("../../evil/x.jpg", b"\xff\xd8\xff\x00")
        self.assertEqual(result.filename, "x.jpg")

    def test_magic_wins_over_declared_mime(self) -> None:
        result = normalize_file("noext", PNG_MAGIC, declared_mime="text/plain")
        self.assertEqual(result.extension, "png")
        self.assertEqual(result.mime, "image/png")


class CompressImageTest(unittest.TestCase):
    def _file(self, name: str, data: bytes) -> NormalizedFile:
        return normalize_file(name, data)

    def test_disabled_returns_source(self) -> None:
        source = self._file("a.png", noise_bytes(120, 120))
        outcome = compress_image(source, CompressConfig(enabled=False, trigger_mb=0.0001))
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.note, "")
        self.assertIs(outcome.file, source)
        self.assertEqual(outcome.original_size, source.size)

    def test_non_image_returns_source(self) -> None:
        source = self._file("a.pdf", b"%PDF-1.7" + b"0" * 200000)
        outcome = compress_image(source, CompressConfig(trigger_mb=0.0001))
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.note, "")

    def test_gif_skipped_by_config(self) -> None:
        source = self._file("a.gif", b"GIF89a" + b"0" * 200000)
        outcome = compress_image(source, CompressConfig(trigger_mb=0.0001, skip_gif=True))
        self.assertFalse(outcome.changed)
        self.assertIn("GIF", outcome.note)

    def test_below_trigger_returns_source(self) -> None:
        source = self._file("a.png", png_bytes())
        outcome = compress_image(source, CompressConfig(trigger_mb=1.0))
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.note, "")

    def test_compresses_and_renames(self) -> None:
        source = self._file("shot.png", noise_bytes(400, 300))
        outcome = compress_image(
            source,
            CompressConfig(trigger_mb=0.0001, max_edge=100, quality=80, target_format="webp"),
        )
        self.assertTrue(outcome.changed)
        self.assertEqual(outcome.file.filename, "shot.webp")
        self.assertEqual(outcome.file.extension, "webp")
        self.assertEqual(outcome.file.mime, "image/webp")
        self.assertLess(outcome.file.size, source.size)
        self.assertEqual(outcome.original_size, source.size)
        self.assertIn("→", outcome.note)
        self.assertIn("-", outcome.note)

    def test_jpeg_target_extension(self) -> None:
        source = self._file("shot.png", noise_bytes(400, 300))
        outcome = compress_image(
            source,
            CompressConfig(trigger_mb=0.0001, max_edge=100, target_format="jpeg"),
        )
        self.assertTrue(outcome.changed)
        self.assertEqual(outcome.file.filename, "shot.jpg")
        self.assertEqual(outcome.file.mime, "image/jpeg")

    def test_alpha_forces_webp(self) -> None:
        source = self._file("logo.png", noise_bytes(400, 300, alpha=True))
        outcome = compress_image(
            source,
            CompressConfig(
                trigger_mb=0.0001,
                max_edge=100,
                target_format="jpeg",
                keep_transparency=True,
            ),
        )
        self.assertTrue(outcome.changed)
        self.assertEqual(outcome.file.extension, "webp")

    def test_keep_format_maps_png_to_jpeg(self) -> None:
        source = self._file("shot.png", noise_bytes(400, 300))
        outcome = compress_image(
            source,
            CompressConfig(trigger_mb=0.0001, max_edge=100, target_format="keep"),
        )
        self.assertTrue(outcome.changed)
        self.assertEqual(outcome.file.extension, "jpg")

    def test_keeps_original_when_result_is_bigger(self) -> None:
        # 源图已经是低质量 WebP，用 q86 重编码必然更大
        data = noise_bytes(200, 200, fmt="WEBP", quality=1)
        source = self._file("tiny.webp", data)
        self.assertEqual(source.extension, "webp")
        outcome = compress_image(
            source,
            CompressConfig(trigger_mb=0.0001, max_edge=8192, quality=95, target_format="keep"),
        )
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.note, "压缩后反而更大，保留原图")
        self.assertIs(outcome.file, source)

    def test_broken_image_falls_back(self) -> None:
        source = NormalizedFile(
            filename="broken.png",
            data=PNG_MAGIC + b"garbage" * 5000,
            mime="image/png",
            extension="png",
        )
        outcome = compress_image(source, CompressConfig(trigger_mb=0.0001))
        self.assertFalse(outcome.changed)
        self.assertTrue(outcome.note.startswith("压缩失败，改为原图上传："))
        self.assertIs(outcome.file, source)


if __name__ == "__main__":
    unittest.main()
