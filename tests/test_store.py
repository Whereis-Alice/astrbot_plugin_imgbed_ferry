"""`imgbed_ferry.store` KV 持久化测试：KV 用假 owner 顶替。"""

from __future__ import annotations

import time
import unittest
from typing import Any
from unittest import mock

from imgbed_ferry import store as store_module
from imgbed_ferry.config import BehaviorConfig
from imgbed_ferry.store import (
    KEY_DEDUPE,
    KEY_QUOTA,
    KEY_RECORDS,
    SCOPE_GROUP,
    SCOPE_USER,
    FerryStore,
)


class FakeOwner:
    """最小可用的 KV owner。"""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = dict(initial or {})
        self.puts: list[tuple[str, Any]] = []

    async def get_kv_data(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    async def put_kv_data(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.puts.append((key, value))


class BrokenOwner:
    async def get_kv_data(self, key: str, default: Any = None) -> Any:
        raise RuntimeError("kv down")

    async def put_kv_data(self, key: str, value: Any) -> None:
        raise RuntimeError("kv down")


class NoKvOwner:
    pass


class DedupeTest(unittest.IsolatedAsyncioTestCase):
    async def test_remember_then_lookup(self) -> None:
        owner = FakeOwner()
        store = FerryStore(owner)
        await store.remember("d1", url="https://cdn/a.png", file_id="f/a.png", name="a.png", size=7)
        entry = await store.lookup("d1")
        assert entry is not None
        self.assertEqual(entry["url"], "https://cdn/a.png")
        self.assertEqual(entry["file_id"], "f/a.png")
        self.assertEqual(entry["name"], "a.png")
        self.assertEqual(entry["size"], 7)
        self.assertGreater(entry["ts"], 0)
        self.assertIn(KEY_DEDUPE, owner.data)

    async def test_lookup_returns_a_copy(self) -> None:
        store = FerryStore(FakeOwner())
        await store.remember("d1", url="https://cdn/a.png")
        entry = await store.lookup("d1")
        assert entry is not None
        entry["url"] = "tampered"
        again = await store.lookup("d1")
        assert again is not None
        self.assertEqual(again["url"], "https://cdn/a.png")

    async def test_lookup_miss(self) -> None:
        store = FerryStore(FakeOwner())
        self.assertIsNone(await store.lookup("nope"))

    async def test_blank_digest(self) -> None:
        store = FerryStore(FakeOwner())
        self.assertIsNone(await store.lookup(""))

    async def test_disabled_dedupe(self) -> None:
        owner = FakeOwner()
        store = FerryStore(owner, BehaviorConfig(dedupe_enabled=False))
        await store.remember("d1", url="https://cdn/a.png")
        self.assertIsNone(await store.lookup("d1"))
        self.assertEqual(owner.puts, [])

    async def test_remember_needs_url(self) -> None:
        store = FerryStore(FakeOwner())
        await store.remember("d1", url="")
        self.assertIsNone(await store.lookup("d1"))

    async def test_remember_needs_digest(self) -> None:
        owner = FakeOwner()
        store = FerryStore(owner)
        await store.remember("", url="https://cdn/a.png")
        self.assertEqual(owner.puts, [])

    async def test_loads_existing_index(self) -> None:
        owner = FakeOwner(
            {KEY_DEDUPE: {"d1": {"url": "https://cdn/a.png", "ts": time.time()}, "bad": "x"}}
        )
        store = FerryStore(owner)
        self.assertIsNotNone(await store.lookup("d1"))
        self.assertIsNone(await store.lookup("bad"))

    async def test_entry_without_url_is_ignored(self) -> None:
        owner = FakeOwner({KEY_DEDUPE: {"d1": {"ts": time.time()}}})
        store = FerryStore(owner)
        self.assertIsNone(await store.lookup("d1"))

    async def test_ttl_expiry_drops_entry(self) -> None:
        stale = time.time() - 31 * 86400
        owner = FakeOwner({KEY_DEDUPE: {"d1": {"url": "https://cdn/a.png", "ts": stale}}})
        store = FerryStore(owner, BehaviorConfig(dedupe_ttl_days=30))
        self.assertIsNone(await store.lookup("d1"))
        self.assertEqual((await store.stats())["dedupe"], 0)

    async def test_ttl_zero_never_expires(self) -> None:
        owner = FakeOwner({KEY_DEDUPE: {"d1": {"url": "https://cdn/a.png", "ts": 1.0}}})
        store = FerryStore(owner, BehaviorConfig(dedupe_ttl_days=0))
        self.assertIsNotNone(await store.lookup("d1"))

    async def test_broken_timestamp_is_treated_as_expired(self) -> None:
        owner = FakeOwner({KEY_DEDUPE: {"d1": {"url": "https://cdn/a.png", "ts": "bad"}}})
        store = FerryStore(owner, BehaviorConfig(dedupe_ttl_days=30))
        self.assertIsNone(await store.lookup("d1"))

    async def test_max_entries_evicts_oldest(self) -> None:
        store = FerryStore(FakeOwner(), BehaviorConfig(dedupe_max_entries=2))
        base = time.time()
        for index in range(3):
            with mock.patch.object(time, "time", return_value=base + index):
                await store.remember(f"d{index}", url=f"https://cdn/{index}.png")
        self.assertIsNone(await store.lookup("d0"))
        self.assertIsNotNone(await store.lookup("d1"))
        self.assertIsNotNone(await store.lookup("d2"))

    async def test_drop(self) -> None:
        owner = FakeOwner()
        store = FerryStore(owner)
        await store.remember("d1", url="https://cdn/a.png")
        await store.drop("d1")
        self.assertIsNone(await store.lookup("d1"))
        await store.drop("")
        await store.drop("missing")
        self.assertEqual([key for key, _ in owner.puts].count(KEY_DEDUPE), 2)

    async def test_drop_by_file_id(self) -> None:
        store = FerryStore(FakeOwner())
        await store.remember("d1", url="https://cdn/a.png", file_id="f/a.png")
        await store.remember("d2", url="https://cdn/b.png", file_id="f/a.png")
        await store.remember("d3", url="https://cdn/c.png", file_id="f/c.png")
        self.assertEqual(await store.drop_by_file_id("f/a.png"), 2)
        self.assertIsNone(await store.lookup("d1"))
        self.assertIsNone(await store.lookup("d2"))
        self.assertIsNotNone(await store.lookup("d3"))

    async def test_drop_by_file_id_blank_and_miss(self) -> None:
        store = FerryStore(FakeOwner())
        self.assertEqual(await store.drop_by_file_id(""), 0)
        self.assertEqual(await store.drop_by_file_id("   "), 0)
        self.assertEqual(await store.drop_by_file_id("nope"), 0)


class RecordsTest(unittest.IsolatedAsyncioTestCase):
    async def test_add_and_recent_order(self) -> None:
        store = FerryStore(FakeOwner())
        await store.add_records([{"n": 1}, {"n": 2}])
        await store.add_records([{"n": 3}])
        self.assertEqual([item["n"] for item in await store.recent(10)], [3, 2, 1])

    async def test_recent_respects_limit(self) -> None:
        store = FerryStore(FakeOwner())
        await store.add_records([{"n": index} for index in range(5)])
        self.assertEqual([item["n"] for item in await store.recent(2)], [4, 3])

    async def test_recent_limit_lower_bound(self) -> None:
        store = FerryStore(FakeOwner())
        await store.add_records([{"n": 1}, {"n": 2}])
        self.assertEqual([item["n"] for item in await store.recent(0)], [2])

    async def test_records_are_copies(self) -> None:
        store = FerryStore(FakeOwner())
        await store.add_records([{"n": 1}])
        rows = await store.recent()
        rows[0]["n"] = 99
        self.assertEqual((await store.recent())[0]["n"], 1)

    async def test_limit_truncates_to_newest(self) -> None:
        store = FerryStore(FakeOwner(), BehaviorConfig(record_limit=3))
        await store.add_records([{"n": index} for index in range(6)])
        self.assertEqual([item["n"] for item in await store.recent(10)], [5, 4, 3])

    async def test_empty_or_disabled(self) -> None:
        owner = FakeOwner()
        store = FerryStore(owner, BehaviorConfig(record_limit=0))
        await store.add_records([{"n": 1}])
        self.assertEqual(owner.puts, [])
        store2 = FerryStore(owner)
        await store2.add_records([])
        self.assertEqual(owner.puts, [])

    async def test_loads_existing_records(self) -> None:
        owner = FakeOwner({KEY_RECORDS: [{"n": 1}, "junk", {"n": 2}]})
        store = FerryStore(owner)
        self.assertEqual([item["n"] for item in await store.recent(10)], [2, 1])

    async def test_clear_records(self) -> None:
        owner = FakeOwner()
        store = FerryStore(owner)
        await store.add_records([{"n": 1}, {"n": 2}])
        self.assertEqual(await store.clear_records(), 2)
        self.assertEqual(await store.recent(), [])
        self.assertEqual(owner.data[KEY_RECORDS], [])
        self.assertEqual(await store.clear_records(), 0)


class QuotaTest(unittest.IsolatedAsyncioTestCase):
    async def test_add_and_used(self) -> None:
        owner = FakeOwner()
        store = FerryStore(owner)
        self.assertEqual(await store.quota_add(SCOPE_USER, "u1", 3), 3)
        self.assertEqual(await store.quota_add(SCOPE_USER, "u1", 2), 5)
        self.assertEqual(await store.quota_used(SCOPE_USER, "u1"), 5)
        self.assertEqual(await store.quota_used(SCOPE_GROUP, "u1"), 0)
        self.assertIn(KEY_QUOTA, owner.data)

    async def test_ignores_non_positive_and_blank(self) -> None:
        owner = FakeOwner()
        store = FerryStore(owner)
        self.assertEqual(await store.quota_add(SCOPE_USER, "u1", 0), 0)
        self.assertEqual(await store.quota_add(SCOPE_USER, "u1", -5), 0)
        self.assertEqual(await store.quota_add(SCOPE_USER, "", 5), 0)
        self.assertEqual(await store.quota_used(SCOPE_USER, ""), 0)
        self.assertEqual(owner.puts, [])

    async def test_unknown_scope_is_created(self) -> None:
        store = FerryStore(FakeOwner())
        self.assertEqual(await store.quota_add("channel", "c1", 2), 2)
        self.assertEqual(await store.quota_used("channel", "c1"), 2)

    async def test_snapshot_shape(self) -> None:
        store = FerryStore(FakeOwner())
        await store.quota_add(SCOPE_USER, "u1", 1)
        await store.quota_add(SCOPE_GROUP, "g1", 2)
        snapshot = await store.quota_snapshot()
        self.assertEqual(snapshot[SCOPE_USER], {"u1": 1})
        self.assertEqual(snapshot[SCOPE_GROUP], {"g1": 2})
        self.assertEqual(snapshot["date"], store_module._today())

    async def test_snapshot_is_a_copy(self) -> None:
        store = FerryStore(FakeOwner())
        await store.quota_add(SCOPE_USER, "u1", 1)
        snapshot = await store.quota_snapshot()
        snapshot[SCOPE_USER]["u1"] = 99
        self.assertEqual(await store.quota_used(SCOPE_USER, "u1"), 1)

    async def test_loads_today_quota(self) -> None:
        owner = FakeOwner(
            {KEY_QUOTA: {"date": store_module._today(), SCOPE_USER: {"u1": 4}, SCOPE_GROUP: {}}}
        )
        store = FerryStore(owner)
        self.assertEqual(await store.quota_used(SCOPE_USER, "u1"), 4)

    async def test_stale_quota_is_discarded(self) -> None:
        owner = FakeOwner({KEY_QUOTA: {"date": "2000-01-01", SCOPE_USER: {"u1": 4}}})
        store = FerryStore(owner)
        self.assertEqual(await store.quota_used(SCOPE_USER, "u1"), 0)

    async def test_day_rollover_resets(self) -> None:
        store = FerryStore(FakeOwner())
        await store.quota_add(SCOPE_USER, "u1", 3)
        with mock.patch.object(store_module, "_today", return_value="2999-12-31"):
            self.assertEqual(await store.quota_used(SCOPE_USER, "u1"), 0)
            self.assertEqual(await store.quota_add(SCOPE_USER, "u1", 1), 1)

    async def test_broken_counter_value(self) -> None:
        owner = FakeOwner(
            {KEY_QUOTA: {"date": store_module._today(), SCOPE_USER: {"u1": "bad"}, SCOPE_GROUP: {}}}
        )
        store = FerryStore(owner)
        self.assertEqual(await store.quota_used(SCOPE_USER, "u1"), 0)
        self.assertEqual(await store.quota_add(SCOPE_USER, "u1", 2), 2)


class DegradationTest(unittest.IsolatedAsyncioTestCase):
    async def test_owner_without_kv_methods(self) -> None:
        store = FerryStore(NoKvOwner())
        await store.remember("d1", url="https://cdn/a.png")
        self.assertIsNotNone(await store.lookup("d1"))
        await store.add_records([{"n": 1}])
        self.assertEqual(len(await store.recent()), 1)
        self.assertEqual(await store.quota_add(SCOPE_USER, "u1", 1), 1)

    async def test_owner_raising_is_swallowed(self) -> None:
        store = FerryStore(BrokenOwner())
        await store.remember("d1", url="https://cdn/a.png")
        self.assertIsNotNone(await store.lookup("d1"))
        self.assertEqual(await store.stats(), {"dedupe": 1, "records": 0})

    async def test_owner_is_none(self) -> None:
        store = FerryStore(None)
        await store.remember("d1", url="https://cdn/a.png")
        self.assertIsNotNone(await store.lookup("d1"))

    async def test_kv_returning_none_uses_default(self) -> None:
        owner = FakeOwner({KEY_DEDUPE: None, KEY_RECORDS: None})
        store = FerryStore(owner)
        self.assertEqual(await store.stats(), {"dedupe": 0, "records": 0})

    async def test_kv_returning_wrong_types(self) -> None:
        owner = FakeOwner({KEY_DEDUPE: ["a"], KEY_RECORDS: {"a": 1}, KEY_QUOTA: 7})
        store = FerryStore(owner)
        self.assertEqual(await store.stats(), {"dedupe": 0, "records": 0})

    async def test_update_config_takes_effect(self) -> None:
        store = FerryStore(FakeOwner())
        store.update_config(BehaviorConfig(dedupe_enabled=False))
        await store.remember("d1", url="https://cdn/a.png")
        self.assertIsNone(await store.lookup("d1"))


class StatsTest(unittest.IsolatedAsyncioTestCase):
    async def test_counts(self) -> None:
        store = FerryStore(FakeOwner())
        self.assertEqual(await store.stats(), {"dedupe": 0, "records": 0})
        await store.remember("d1", url="https://cdn/a.png")
        await store.add_records([{"n": 1}, {"n": 2}])
        self.assertEqual(await store.stats(), {"dedupe": 1, "records": 2})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
