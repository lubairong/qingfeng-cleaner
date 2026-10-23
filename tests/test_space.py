"""v2 空间索引单元测试（tests/test_space.py）。"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import temp_dir  # noqa: E402

from qingfeng.core import space  # noqa: E402


def _write_file(path: Path, size: int, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _walk_nodes(index: space.SpaceIndex) -> list[space.SpaceNode]:
    nodes: list[space.SpaceNode] = []

    def visit(node: space.SpaceNode) -> None:
        nodes.append(node)
        for child in node.children:
            visit(child)

    visit(index.root_node)
    return nodes


class TestBuildIndex(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_totals_and_tree(self) -> None:
        _write_file(self.root / "a.bin", 1000)
        _write_file(self.root / "sub" / "b.bin", 2000)
        _write_file(self.root / "sub" / "deep" / "c.bin", 3000)
        index = space.build_index(str(self.root), min_child_size=512, max_nodes=1000)
        self.assertFalse(index.truncated)
        self.assertFalse(index.cancelled)
        self.assertEqual(index.accessible_bytes, 6000)
        self.assertEqual(index.root_node.size, 6000)
        self.assertEqual(index.root_node.file_count, 3)
        # 子节点按大小降序
        sizes = [child.size for child in index.root_node.children]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        # find() 可定位已物化节点
        found = index.find(str(self.root / "sub"))
        self.assertIsNotNone(found)
        self.assertEqual(found.size, 5000)
        self.assertIsNone(index.find(str(self.root / "no-such-path")))

    def test_min_child_size_aggregation(self) -> None:
        _write_file(self.root / "a.bin", 100)
        _write_file(self.root / "b.bin", 200)
        index = space.build_index(str(self.root), min_child_size=10 * 1024 * 1024)
        self.assertEqual(index.accessible_bytes, 300)
        self.assertEqual(index.root_node.children, [])
        self.assertEqual(index.root_node.other_size, 300)
        self.assertTrue(index.root_node.truncated)

        small = space.build_index(str(self.root), min_child_size=1)
        self.assertEqual(len(small.root_node.children), 2)

    def test_files_below_threshold_not_materialized(self) -> None:
        _write_file(self.root / "big.bin", 4096)
        _write_file(self.root / "tiny.bin", 10)
        index = space.build_index(str(self.root), min_child_size=1024)
        file_nodes = [node for node in index.root_node.children if not node.is_dir]
        self.assertEqual([node.name for node in file_nodes], ["big.bin"])
        self.assertGreaterEqual(index.root_node.other_size, 10)

    def test_max_nodes_truncation(self) -> None:
        for index_name in range(30):
            _write_file(self.root / f"d{index_name}" / "f.bin", 4096)
        index = space.build_index(str(self.root), min_child_size=1, max_nodes=4)
        self.assertTrue(index.truncated)
        self.assertLessEqual(len(index._path_map), 8)

    def test_cancel(self) -> None:
        for index_name in range(20):
            _write_file(self.root / f"d{index_name}" / "f.bin", 4096)
        started = time.time()
        index = space.build_index(str(self.root), cancel=lambda: True)
        self.assertTrue(index.cancelled)
        self.assertLess(time.time() - started, 10)

    def test_progress_callback(self) -> None:
        _write_file(self.root / "a.bin", 100)
        _write_file(self.root / "sub" / "b.bin", 200)
        calls: list[tuple[str, int]] = []
        space.build_index(str(self.root), progress=lambda path, count: calls.append((path, count)))
        self.assertTrue(calls)
        self.assertTrue(all(isinstance(path, str) and count >= 0 for path, count in calls))
        self.assertGreaterEqual(calls[-1][1], 1)

    def test_scan_is_read_only(self) -> None:
        _write_file(self.root / "a.bin", 100, mtime=1_600_000_000)
        _write_file(self.root / "sub" / "b.bin", 200, mtime=1_600_000_100)
        before = {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.root.rglob("*")
        }
        space.build_index(str(self.root))
        after = {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.root.rglob("*")
        }
        self.assertEqual(before, after)

    def test_hardlink_dedupe(self) -> None:
        first = self.root / "hard-a.bin"
        _write_file(first, 1024 * 1024)
        second = self.root / "hard-b.bin"
        try:
            os.link(first, second)
        except OSError:
            self.skipTest("当前环境不支持硬链接")
        original = space.DEDUPE_HARDLINKS
        try:
            space.DEDUPE_HARDLINKS = True
            deduped = space.build_index(str(self.root), min_child_size=1024)
            self.assertEqual(deduped.accessible_bytes, 1024 * 1024)
            self.assertEqual(deduped.root_node.file_count, 2)  # 文件数按逻辑条目计
            space.DEDUPE_HARDLINKS = False
            apparent = space.build_index(str(self.root), min_child_size=1024)
            self.assertEqual(apparent.accessible_bytes, 2 * 1024 * 1024)
        finally:
            space.DEDUPE_HARDLINKS = original

    def test_enum_matches_scandir(self) -> None:
        _write_file(self.root / "a.bin", 100)
        _write_file(self.root / "sub" / "b.bin", 200)
        fast = space._enum_dir_fast(str(self.root))
        if fast is None:
            self.skipTest("快速枚举不可用（回退 os.scandir）")
        fast_map = {name: (size, is_dir) for name, size, _fid, _attr, is_dir, _mt in fast}
        slow_map = {}
        with os.scandir(self.root) as it:
            for entry in it:
                st = entry.stat(follow_symlinks=False)
                slow_map[entry.name] = (st.st_size, entry.is_dir(follow_symlinks=False))
        self.assertEqual(fast_map, slow_map)

    def test_scan_tree_hardlink_dedupe(self) -> None:
        first = self.root / "probe-a.bin"
        _write_file(first, 1024 * 1024)
        try:
            os.link(first, self.root / "probe-b.bin")
        except OSError:
            self.skipTest("当前环境不支持硬链接")
        total, _special, raw_total, _raw_special = space._scan_tree(str(self.root))
        self.assertEqual(total, 1024 * 1024)  # 去重口径
        self.assertEqual(raw_total, 2 * 1024 * 1024)  # 原始口径
        total2, special, _raw2, raw_special = space._scan_tree(
            str(self.root), special=str(self.root)
        )
        self.assertEqual(total2, 1024 * 1024)
        self.assertEqual(special, 1024 * 1024)
        self.assertEqual(raw_special, 2 * 1024 * 1024)


class TestCache(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.root = Path(self._tmp.name)
        self.cache = self.root / "cache" / "space_index.json"
        _write_file(self.root / "data" / "a.bin", 2048)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cache_roundtrip(self) -> None:
        index = space.build_index(str(self.root), min_child_size=1024)
        with mock.patch.object(space, "cache_path", return_value=self.cache):
            space.save_index(index)
            loaded = space.load_cached_index(max_age_hours=24)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.accessible_bytes, index.accessible_bytes)
        self.assertEqual(loaded.total_bytes, index.total_bytes)
        self.assertEqual(loaded.root, index.root)
        self.assertIsNotNone(loaded.find(str(self.root / "data")))
        # 缓存 json 体积应远小于 15 MB
        self.assertLess(self.cache.stat().st_size, 15 * 1024 * 1024)

    def test_cache_expired(self) -> None:
        index = space.build_index(str(self.root), min_child_size=1024)
        index.built_at = time.time() - 48 * 3600
        with mock.patch.object(space, "cache_path", return_value=self.cache):
            space.save_index(index)
            self.assertIsNone(space.load_cached_index(max_age_hours=24))

    def test_cache_corrupt_returns_none(self) -> None:
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text("{broken json", encoding="utf-8")
        with mock.patch.object(space, "cache_path", return_value=self.cache):
            self.assertIsNone(space.load_cached_index())
        self.cache.write_text(json.dumps({"version": 999}), encoding="utf-8")
        with mock.patch.object(space, "cache_path", return_value=self.cache):
            self.assertIsNone(space.load_cached_index())

    def test_save_failure_is_silent(self) -> None:
        index = space.build_index(str(self.root), min_child_size=1024)
        bad = Path(os.environ.get("WINDIR", r"C:\Windows")) / "no-such-dir-qf" / "x.json"
        with mock.patch.object(space, "cache_path", return_value=bad):
            space.save_index(index)  # 不应抛异常

    def test_cache_path_ends_with_expected_name(self) -> None:
        path = space.cache_path()
        self.assertEqual(path.name, "space_index.json")
        self.assertEqual(path.parent.name, "QingFengCleaner")

    def test_json_roundtrip_node_error(self) -> None:
        index = space.build_index(str(self.root), min_child_size=1024)
        index.root_node.error = "示例错误"
        data = index.to_json()
        restored = space.SpaceIndex.from_json(data)
        self.assertEqual(restored.root_node.error, "示例错误")
        self.assertIsNotNone(restored.find(str(self.root)))


class TestProbeSystemSpace(unittest.TestCase):
    def test_structure_and_sizes(self) -> None:
        with temp_dir() as tmp:
            fake_root = Path(tmp)
            _write_file(fake_root / "hiberfil.sys", 4096)
            _write_file(fake_root / "pagefile.sys", 8192)
            _write_file(fake_root / "Windows" / "System32" / "x.dll", 1024)
            _write_file(fake_root / "Windows" / "WinSxS" / "comp.dll", 2048)
            _write_file(fake_root / "Windows" / "Installer" / "a.msi", 512)
            with mock.patch.dict(os.environ, {"SystemDrive": tmp}):
                probe = space.probe_system_space()
        expected_keys = {
            "recycle_bin",
            "hiberfil",
            "pagefile",
            "swapfile",
            "winsxs",
            "windows_old",
            "restore_points",
            "windows",
            "installer_cache",
        }
        self.assertEqual(set(probe), expected_keys)
        self.assertTrue(probe["hiberfil"]["exists"])
        self.assertEqual(probe["hiberfil"]["bytes"], 4096)
        self.assertEqual(probe["pagefile"]["bytes"], 8192)
        self.assertTrue(probe["windows"]["available"])
        self.assertEqual(probe["windows"]["bytes"], 1024 + 2048 + 512)
        self.assertEqual(probe["winsxs"]["bytes"], 2048)
        self.assertEqual(probe["installer_cache"]["bytes"], 512)
        # 同时返回不去重口径（无硬链接时应相等）
        self.assertIn("bytes_raw", probe["windows"])
        self.assertIn("bytes_raw", probe["winsxs"])
        self.assertEqual(probe["windows"]["bytes_raw"], probe["windows"]["bytes"])
        self.assertEqual(probe["winsxs"]["bytes_raw"], probe["winsxs"]["bytes"])
        self.assertIn("count", probe["recycle_bin"])
        self.assertIn("bytes", probe["recycle_bin"])
        self.assertIn("available", probe["restore_points"])

    def test_probe_with_index_does_not_rescan(self) -> None:
        """传 index 时应直接从索引树取数，不再调用 _scan_tree（<0.2s 级别）。"""
        with temp_dir() as tmp:
            fake_root = Path(tmp)
            _write_file(fake_root / "hiberfil.sys", 4096)
            _write_file(fake_root / "Windows" / "System32" / "x.dll", 1024)
            _write_file(fake_root / "Windows" / "WinSxS" / "comp.dll", 2048)
            _write_file(fake_root / "Windows" / "Installer" / "a.msi", 512)
            index = space.build_index(str(fake_root), min_child_size=1)
            started = time.time()
            with mock.patch.dict(os.environ, {"SystemDrive": tmp}), mock.patch.object(
                space, "_scan_tree", side_effect=AssertionError("传入 index 时不应重新扫描")
            ):
                probe = space.probe_system_space(index)
            elapsed = time.time() - started
        self.assertLess(elapsed, 2.0)
        self.assertTrue(probe["windows"]["available"])
        self.assertEqual(probe["windows"]["bytes"], 1024 + 2048 + 512)
        self.assertEqual(probe["winsxs"]["bytes"], 2048)
        self.assertEqual(probe["installer_cache"]["bytes"], 512)
        self.assertEqual(probe["windows"]["source"], "index")
        self.assertIn("bytes_raw", probe["windows"])
        self.assertIn("bytes_raw", probe["winsxs"])
        self.assertEqual(probe["hiberfil"]["bytes"], 4096)

    def test_probe_degrades_on_error(self) -> None:
        with mock.patch.object(space, "_scan_tree", side_effect=OSError("mock")):
            probe = space.probe_system_space()
        self.assertFalse(probe["windows"]["available"])
        self.assertFalse(probe["winsxs"]["available"])
        self.assertFalse(probe["installer_cache"]["available"])
        self.assertIn("error", probe["windows"])


if __name__ == "__main__":
    unittest.main()
