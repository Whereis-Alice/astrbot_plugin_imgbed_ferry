"""`imgbed_ferry.session` 会话素材缓存测试。"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from imgbed_ferry.collector import KIND_FILE, KIND_IMAGE, Material
from imgbed_ferry.config import BehaviorConfig
from imgbed_ferry.session import SessionMaterials

SESSION = "aiocqhttp:GroupMessage:123"
OTHER = "aiocqhttp:GroupMessage:456"


def img(index: int) -> Material:
    return Material(kind=KIND_IMAGE, name=f"{index}.png", url=f"https://cdn/{index}.png")


def doc(index: int) -> Material:
    return Material(kind=KIND_FILE, name=f"{index}.zip", file_id=f"fid-{index}")


class ConfigTest(unittest.TestCase):
    def test_defaults(self) -> None:
        cache = SessionMaterials()
        self.assertEqual(cache.ttl_seconds, 180 * 60)
        self.assertEqual(cache.limit, 40)

    def test_lower_bounds(self) -> None:
        cache = SessionMaterials(BehaviorConfig(session_ttl_minutes=0, session_material_limit=0))
        self.assertEqual(cache.ttl_seconds, 60.0)
        self.assertEqual(cache.limit, 1)

    def test_update_config_reprunes(self) -> None:
        cache = SessionMaterials()
        cache.remember(SESSION, [img(1), img(2), img(3)])
        cache.update_config(BehaviorConfig(session_material_limit=1))
        snapshot = cache.snapshot(SESSION)
        self.assertEqual([item.name for item in snapshot], ["3.png"])


class RememberTest(unittest.TestCase):
    def test_returns_touched_count(self) -> None:
        cache = SessionMaterials()
        self.assertEqual(cache.remember(SESSION, [img(1), doc(2)]), 2)
        self.assertEqual(len(cache.snapshot(SESSION)), 2)

    def test_blank_session_id_is_ignored(self) -> None:
        cache = SessionMaterials()
        for bad in ("", "   ", None):
            with self.subTest(bad=bad):
                self.assertEqual(cache.remember(bad, [img(1)]), 0)  # type: ignore[arg-type]
        self.assertEqual(cache.stats(), {"sessions": 0, "materials": 0})

    def test_skips_unresolvable(self) -> None:
        cache = SessionMaterials()
        self.assertEqual(cache.remember(SESSION, [Material(name="x.png")]), 0)

    def test_skips_inline_only_material(self) -> None:
        # 只有 bytes 的素材进了缓存也取不回来，直接不收
        inline = Material(kind=KIND_IMAGE, name="a.png", data=b"\x89PNG")
        self.assertTrue(inline.resolvable)
        self.assertFalse(inline.cacheable)
        cache = SessionMaterials()
        self.assertEqual(cache.remember(SESSION, [inline]), 0)

    def test_skips_component_only_material(self) -> None:
        cache = SessionMaterials()
        self.assertEqual(cache.remember(SESSION, [Material(component=object())]), 0)

    def test_repeat_refreshes_lru_order(self) -> None:
        cache = SessionMaterials()
        cache.remember(SESSION, [img(1), img(2)])
        cache.remember(SESSION, [img(1)])
        self.assertEqual([item.name for item in cache.snapshot(SESSION)], ["2.png", "1.png"])
        self.assertEqual(cache.stats()["materials"], 2)

    def test_sessions_are_isolated(self) -> None:
        cache = SessionMaterials()
        cache.remember(SESSION, [img(1)])
        cache.remember(OTHER, [img(2)])
        self.assertEqual([i.name for i in cache.snapshot(SESSION)], ["1.png"])
        self.assertEqual([i.name for i in cache.snapshot(OTHER)], ["2.png"])
        self.assertEqual(cache.stats(), {"sessions": 2, "materials": 2})

    def test_limit_evicts_oldest(self) -> None:
        cache = SessionMaterials(BehaviorConfig(session_material_limit=3))
        cache.remember(SESSION, [img(1), img(2), img(3), img(4), img(5)])
        self.assertEqual([i.name for i in cache.snapshot(SESSION)], ["3.png", "4.png", "5.png"])

    def test_descriptor_roundtrip_keeps_fields(self) -> None:
        cache = SessionMaterials()
        source = Material(
            kind=KIND_IMAGE,
            name="a.png",
            url="https://cdn/a.png",
            path="/tmp/a.png",
            size=99,
            mime="image/png",
            file_id="fid",
            payload={"file_id": "fid", "size": 99},
        )
        cache.remember(SESSION, [source])
        restored = cache.snapshot(SESSION)[0]
        self.assertEqual(restored.material_id, source.material_id)
        self.assertEqual(restored.kind, source.kind)
        self.assertEqual(restored.name, source.name)
        self.assertEqual(restored.url, source.url)
        self.assertEqual(restored.path, source.path)
        self.assertEqual(restored.size, 99)
        self.assertEqual(restored.mime, "image/png")
        self.assertEqual(restored.file_id, "fid")
        self.assertEqual(restored.payload, {"file_id": "fid", "size": 99})
        self.assertEqual(restored.data, b"")
        self.assertIsNone(restored.component)


class ForgetTest(unittest.TestCase):
    def test_forget_returns_count_and_clears(self) -> None:
        cache = SessionMaterials()
        cache.remember(SESSION, [img(1), img(2)])
        self.assertEqual(cache.forget(SESSION), 2)
        self.assertEqual(cache.snapshot(SESSION), [])
        self.assertEqual(cache.forget(SESSION), 0)

    def test_clear_drops_all_sessions(self) -> None:
        cache = SessionMaterials()
        cache.remember(SESSION, [img(1)])
        cache.remember(OTHER, [img(2)])
        cache.clear()
        self.assertEqual(cache.stats(), {"sessions": 0, "materials": 0})


class SnapshotTest(unittest.TestCase):
    def test_unknown_session(self) -> None:
        self.assertEqual(SessionMaterials().snapshot("nope"), [])

    def test_blank_session(self) -> None:
        self.assertEqual(SessionMaterials().snapshot(""), [])

    def test_latest_is_last(self) -> None:
        cache = SessionMaterials()
        cache.remember(SESSION, [img(1), img(2), img(3)])
        latest = cache.latest(SESSION)
        assert latest is not None
        self.assertEqual(latest.name, "3.png")

    def test_latest_on_empty(self) -> None:
        self.assertIsNone(SessionMaterials().latest(SESSION))

    def test_ttl_expires_entries(self) -> None:
        cache = SessionMaterials(BehaviorConfig(session_ttl_minutes=1))
        base = time.time()
        with mock.patch.object(time, "time", return_value=base):
            cache.remember(SESSION, [img(1)])
        with mock.patch.object(time, "time", return_value=base + 59):
            self.assertEqual(len(cache.snapshot(SESSION)), 1)
        with mock.patch.object(time, "time", return_value=base + 61):
            self.assertEqual(cache.snapshot(SESSION), [])
        self.assertEqual(cache.stats(), {"sessions": 0, "materials": 0})

    def test_ttl_only_expires_old_entries(self) -> None:
        cache = SessionMaterials(BehaviorConfig(session_ttl_minutes=1))
        base = time.time()
        with mock.patch.object(time, "time", return_value=base):
            cache.remember(SESSION, [img(1)])
        with mock.patch.object(time, "time", return_value=base + 40):
            cache.remember(SESSION, [img(2)])
        with mock.patch.object(time, "time", return_value=base + 70):
            self.assertEqual([i.name for i in cache.snapshot(SESSION)], ["2.png"])


class PickTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = SessionMaterials()
        self.items = [img(1), img(2), img(3)]
        self.cache.remember(SESSION, self.items)

    def test_positive_index_is_oldest_first(self) -> None:
        picked, missing = self.cache.pick(SESSION, indexes=[1])
        self.assertEqual([i.name for i in picked], ["1.png"])
        self.assertEqual(missing, [])

    def test_negative_index_is_newest_first(self) -> None:
        picked, missing = self.cache.pick(SESSION, indexes=[-1, -3])
        self.assertEqual([i.name for i in picked], ["3.png", "1.png"])
        self.assertEqual(missing, [])

    def test_zero_index_is_missing(self) -> None:
        picked, missing = self.cache.pick(SESSION, indexes=[0])
        self.assertEqual(picked, [])
        self.assertEqual(missing, ["0"])

    def test_out_of_range(self) -> None:
        picked, missing = self.cache.pick(SESSION, indexes=[4, -4])
        self.assertEqual(picked, [])
        self.assertEqual(missing, ["4", "-4"])

    def test_non_numeric_index(self) -> None:
        picked, missing = self.cache.pick(SESSION, indexes=["abc"])  # type: ignore[list-item]
        self.assertEqual(picked, [])
        self.assertEqual(missing, ["abc"])

    def test_numeric_string_index_works(self) -> None:
        picked, _ = self.cache.pick(SESSION, indexes=["2"])  # type: ignore[list-item]
        self.assertEqual([i.name for i in picked], ["2.png"])

    def test_pick_by_item_id(self) -> None:
        target = self.items[1].material_id
        picked, missing = self.cache.pick(SESSION, item_ids=[target])
        self.assertEqual([i.name for i in picked], ["2.png"])
        self.assertEqual(missing, [])

    def test_unknown_item_id(self) -> None:
        picked, missing = self.cache.pick(SESSION, item_ids=["img_nope"])
        self.assertEqual(picked, [])
        self.assertEqual(missing, ["img_nope"])

    def test_blank_item_ids_are_ignored(self) -> None:
        picked, missing = self.cache.pick(SESSION, item_ids=["", "   "])
        self.assertEqual(picked, [])
        self.assertEqual(missing, [])

    def test_dedupes_across_id_and_index(self) -> None:
        target = self.items[2].material_id
        picked, missing = self.cache.pick(SESSION, item_ids=[target], indexes=[-1, 3])
        self.assertEqual([i.name for i in picked], ["3.png"])
        self.assertEqual(missing, [])

    def test_ids_come_before_indexes(self) -> None:
        picked, _ = self.cache.pick(SESSION, item_ids=[self.items[2].material_id], indexes=[1])
        self.assertEqual([i.name for i in picked], ["3.png", "1.png"])

    def test_empty_session_reports_missing(self) -> None:
        cache = SessionMaterials()
        picked, missing = cache.pick(SESSION, item_ids=["x"], indexes=[1])
        self.assertEqual(picked, [])
        self.assertEqual(missing, ["x", "1"])

    def test_no_selectors_returns_nothing(self) -> None:
        self.assertEqual(self.cache.pick(SESSION), ([], []))


class StatsTest(unittest.TestCase):
    def test_counts(self) -> None:
        cache = SessionMaterials()
        self.assertEqual(cache.stats(), {"sessions": 0, "materials": 0})
        cache.remember(SESSION, [img(1), img(2)])
        cache.remember(OTHER, [doc(3)])
        self.assertEqual(cache.stats(), {"sessions": 2, "materials": 3})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
