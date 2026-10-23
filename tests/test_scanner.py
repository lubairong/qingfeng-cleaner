"""扫描器单元测试（tests/test_scanner.py）。

全部在工作区内的临时目录（tests/_tmp）中造数据，绝不触碰真实系统目录。
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import temp_dir  # noqa: E402

from qingfeng.core import scanner  # noqa: E402
from qingfeng.core.models import Rule  # noqa: E402
from qingfeng.core.scanner import scan  # noqa: E402


def _rule(paths: list[str], *, rule_id: str = "test-rule", **kwargs) -> Rule:
    base = dict(
        id=rule_id,
        name="测试规则",
        category="系统",
        description="测试用规则",
        action="delete",
        paths=[str(path) for path in paths],
        globs=[],
        exclude_globs=[],
        risk="safe",
        requires_admin=False,
        default_enabled=True,
        min_age_days=0,
        follow_dirs=True,
    )
    base.update(kwargs)
    return Rule(**base)  # type: ignore[arg-type]


def _write_file(path: Path, size: int, age_days: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if age_days:
        timestamp = time.time() - age_days * 86400
        os.utime(path, (timestamp, timestamp))


def _item_paths(result) -> set[str]:
    return {os.path.normcase(item.path) for item in result.items}


class TestScanBasics(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_whole_tree_aggregation(self) -> None:
        _write_file(self.root / "a.txt", 100)
        _write_file(self.root / "sub" / "c.log", 200)
        _write_file(self.root / "sub" / "deep" / "d.tmp", 300)

        report = scan([_rule([self.root])])
        result = report.results[0]
        self.assertIsNone(result.error)
        self.assertEqual(result.total_bytes, 600)
        self.assertEqual(result.file_count, 3)
        self.assertEqual(len(result.items), 2)  # a.txt + 聚合的 sub 目录
        dir_items = [item for item in result.items if item.is_dir]
        self.assertEqual(len(dir_items), 1)
        self.assertEqual(dir_items[0].size_bytes, 500)
        self.assertEqual(dir_items[0].file_count, 2)

    def test_min_age_days_filter(self) -> None:
        _write_file(self.root / "a.txt", 100, age_days=40)
        _write_file(self.root / "sub" / "c.log", 200, age_days=40)
        _write_file(self.root / "sub" / "deep" / "d.tmp", 300, age_days=40)
        _write_file(self.root / "new" / "e.txt", 50, age_days=0)

        report = scan([_rule([self.root], min_age_days=30)])
        result = report.results[0]
        self.assertEqual(result.total_bytes, 600)
        self.assertEqual(result.file_count, 3)
        self.assertNotIn(os.path.normcase(str(self.root / "new")), _item_paths(result))

    def test_globs_matching(self) -> None:
        _write_file(self.root / "a.txt", 100)
        _write_file(self.root / "sub" / "c.log", 200)
        _write_file(self.root / "sub" / "other.log", 50)
        _write_file(self.root / "sub" / "deep" / "x.log", 25)

        report = scan([_rule([self.root], globs=["*.log"])])
        result = report.results[0]
        self.assertEqual(result.file_count, 3)
        self.assertEqual(result.total_bytes, 275)
        for item in result.items:
            self.assertTrue(item.path.lower().endswith(".log"))

    def test_exclude_globs(self) -> None:
        _write_file(self.root / "a.txt", 100)
        _write_file(self.root / "b.sys", 10)
        _write_file(self.root / "sub" / "c.log", 200)
        _write_file(self.root / "sub" / "bad.sys", 10)

        report = scan([_rule([self.root], exclude_globs=["*.sys"])])
        result = report.results[0]
        paths = _item_paths(result)
        self.assertNotIn(os.path.normcase(str(self.root / "b.sys")), paths)
        self.assertNotIn(os.path.normcase(str(self.root / "sub" / "bad.sys")), paths)
        # sub 目录不再是"整棵合格"，退化为文件粒度，但仍能扫到 c.log
        self.assertIn(os.path.normcase(str(self.root / "sub" / "c.log")), paths)
        self.assertNotIn(os.path.normcase(str(self.root / "sub")), paths)

    def test_protected_globs_skip(self) -> None:
        _write_file(self.root / "keep.me", 10)
        _write_file(self.root / "drop.tmp", 20)
        report = scan([_rule([self.root], protected_globs=["*.me"])])
        paths = _item_paths(report.results[0])
        self.assertNotIn(os.path.normcase(str(self.root / "keep.me")), paths)
        self.assertIn(os.path.normcase(str(self.root / "drop.tmp")), paths)

    def test_follow_dirs_false(self) -> None:
        _write_file(self.root / "a.txt", 100)
        _write_file(self.root / "sub" / "c.log", 200)
        report = scan([_rule([self.root], follow_dirs=False)])
        result = report.results[0]
        self.assertEqual(result.total_bytes, 100)
        self.assertEqual(result.file_count, 1)
        for item in result.items:
            self.assertEqual(Path(item.path).parent, self.root)

    def test_max_depth_limit(self) -> None:
        _write_file(self.root / "l1" / "l2" / "l3" / "f.dat", 400)
        shallow = scan([_rule([self.root], rule_id="d2")], max_depth=2)
        self.assertEqual(shallow.results[0].total_bytes, 0)
        deep = scan([_rule([self.root], rule_id="d10")], max_depth=10)
        self.assertEqual(deep.results[0].total_bytes, 400)

    def test_file_as_rule_root(self) -> None:
        target = self.root / "single.txt"
        _write_file(target, 42)
        report = scan([_rule([target])])
        result = report.results[0]
        self.assertEqual(result.total_bytes, 42)
        self.assertEqual(result.file_count, 1)
        self.assertFalse(result.items[0].is_dir)

    def test_wildcard_path_expansion(self) -> None:
        _write_file(self.root / "profiles" / "alpha" / "cache.bin", 100)
        _write_file(self.root / "profiles" / "beta" / "cache.bin", 200)
        report = scan([_rule([self.root / "profiles" / "*"])])
        self.assertEqual(report.results[0].total_bytes, 300)

    def test_duplicate_paths_not_double_counted(self) -> None:
        _write_file(self.root / "a.txt", 100)
        rule = _rule([self.root, self.root])
        report = scan([rule])
        self.assertEqual(report.results[0].total_bytes, 100)

    def test_report_rule_not_scanned(self) -> None:
        rule = _rule([], rule_id="report-rule", action="report", report_note="建议命令")
        report = scan([rule])
        result = report.results[0]
        self.assertEqual(result.items, [])
        self.assertEqual(result.total_bytes, 0)
        self.assertEqual(result.skipped_reason, "建议命令")

    def test_empty_recycle_bin_rule_not_scanned(self) -> None:
        rule = _rule([], rule_id="recycle-rule", action="empty_recycle_bin", report_note="清空")
        report = scan([rule])
        result = report.results[0]
        self.assertEqual(result.items, [])
        self.assertEqual(result.skipped_reason, "清空")

    def test_missing_path_sets_skipped_reason(self) -> None:
        rule = _rule([self.root / "definitely-not-exists"])
        report = scan([rule])
        result = report.results[0]
        self.assertEqual(result.items, [])
        self.assertTrue(result.skipped_reason)

    def test_report_fields(self) -> None:
        _write_file(self.root / "a.txt", 100)
        started = time.time()
        report = scan([_rule([self.root])])
        self.assertGreaterEqual(report.started_at, started - 1)
        self.assertGreaterEqual(report.duration, 0.0)
        self.assertIsInstance(report.drive_free, int)
        self.assertIsInstance(report.drive_total, int)
        self.assertFalse(report.cancelled)
        self.assertEqual(report.cleanable_bytes, report.total_bytes)


class TestScanControl(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.root = Path(self._tmp.name)
        _write_file(self.root / "a.txt", 100)
        _write_file(self.root / "sub" / "b.log", 200)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_progress_callback(self) -> None:
        calls: list[tuple[str, int, int]] = []
        rules = [_rule([self.root], rule_id="r1"), _rule([self.root], rule_id="r2")]
        report = scan(rules, progress=lambda name, done, total: calls.append((name, done, total)))
        self.assertTrue(calls)
        self.assertTrue(all(total == 2 for _, _, total in calls))
        self.assertEqual(calls[-1][1], 2)
        self.assertEqual(len(report.results), 2)

    def test_cancel_returns_quickly(self) -> None:
        rules = [_rule([self.root], rule_id="r1"), _rule([self.root], rule_id="r2")]
        report = scan(rules, cancel=lambda: True)
        self.assertTrue(report.cancelled)
        self.assertEqual(len(report.results), 2)
        for result in report.results:
            self.assertEqual(result.items, [])
            self.assertEqual(result.skipped_reason, "扫描已取消")

    def test_rule_exception_isolated(self) -> None:
        rules = [_rule([self.root], rule_id="r1"), _rule([self.root], rule_id="r2")]
        with mock.patch.object(scanner, "_scan_rule", side_effect=RuntimeError("boom")):
            report = scan(rules)
        self.assertEqual(len(report.results), 2)
        for result in report.results:
            self.assertIsNotNone(result.error)
            self.assertIn("boom", result.error or "")

    def test_read_only_scan_does_not_modify_files(self) -> None:
        before = {}
        for path in self.root.rglob("*"):
            before[path] = (path.stat().st_size, path.stat().st_mtime_ns)
        scan([_rule([self.root])])
        after = {}
        for path in self.root.rglob("*"):
            after[path] = (path.stat().st_size, path.stat().st_mtime_ns)
        self.assertEqual(before, after)

    @unittest.skipUnless(os.name == "nt", "仅 Windows 验证 junction/reparse 跳过")
    def test_symlink_not_followed(self) -> None:
        link = self.root / "link-to-temp"
        outside_tmp = temp_dir()
        outside = Path(outside_tmp.name)
        try:
            _write_file(outside / "outside.bin", 999)
            try:
                os.symlink(outside, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("当前环境无法创建目录符号链接")
            report = scan([_rule([self.root])])
            paths = _item_paths(report.results[0])
            link_key = os.path.normcase(str(link))
            self.assertNotIn(link_key, paths)
            self.assertFalse(any(path.startswith(link_key) for path in paths))
        finally:
            try:
                os.unlink(link)
            except OSError:
                pass
            outside_tmp.cleanup()


class TestScanPermissionIsolation(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "仅 Windows 验证权限异常")
    def test_permission_error_recorded_not_raised(self) -> None:
        denied = Path(os.environ.get("SystemDrive", "C:") + "\\System Volume Information")
        if not denied.exists():
            self.skipTest("System Volume Information 不存在")
        with temp_dir() as tmp:
            rule = _rule([tmp], rule_id="perm-rule")
            state = scanner._ScanState(rule, cancel=None, max_depth=24, now=time.time())
            scanner._walk_entries(state, str(denied), 0, "")
        if state.error_count == 0:
            self.skipTest("当前用户可读取该目录，无法验证权限异常")
        self.assertIsNotNone(state.error_message())
        self.assertIn("读取失败", state.error_message() or "")

    def test_huge_dir_cancel_checks(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            for index in range(300):
                _write_file(root / f"f{index}.tmp", 1)
            state = scanner._ScanState(_rule([root]), cancel=lambda: True, max_depth=24, now=time.time())
            with self.assertRaises(scanner._Cancelled):
                scanner._walk_entries(state, str(root), 0, "")
            self.assertTrue(state.cancelled)


class TestRequiresAdminSkip(unittest.TestCase):
    """非管理员时 requires_admin 规则应明确跳过，而不是报扫描错误。"""

    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.root = Path(self._tmp.name)
        _write_file(self.root / "a.tmp", 100)
        _write_file(self.root / "sub" / "b.tmp", 200)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_non_admin_skips_scan_and_marks_elevation(self) -> None:
        rule = _rule([self.root], rule_id="admin-rule", requires_admin=True)
        with mock.patch.object(scanner.winapi, "is_admin", return_value=False):
            report = scan([rule])
        result = report.results[0]
        self.assertIn("需要管理员权限", result.skipped_reason or "")
        self.assertIn("以管理员身份重启", result.skipped_reason or "")
        self.assertTrue(result.requires_elevation)
        self.assertIsNone(result.error)
        self.assertEqual(result.items, [])

    def test_admin_scans_normally(self) -> None:
        rule = _rule([self.root], rule_id="admin-rule", requires_admin=True)
        with mock.patch.object(scanner.winapi, "is_admin", return_value=True):
            report = scan([rule])
        result = report.results[0]
        self.assertTrue(result.items)
        self.assertFalse(result.requires_elevation)
        self.assertIsNone(result.error)

    def test_skip_does_not_affect_other_rules(self) -> None:
        admin_rule = _rule([self.root], rule_id="admin-rule", requires_admin=True)
        normal_rule = _rule([self.root], rule_id="normal-rule")
        with mock.patch.object(scanner.winapi, "is_admin", return_value=False):
            report = scan([admin_rule, normal_rule])
        by_id = {result.rule.id: result for result in report.results}
        self.assertEqual(by_id["admin-rule"].items, [])
        self.assertTrue(by_id["normal-rule"].items)
        self.assertFalse(by_id["normal-rule"].requires_elevation)


if __name__ == "__main__":
    unittest.main()
