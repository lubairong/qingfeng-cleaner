"""清理执行器单元测试（tests/test_cleaner.py）。

红线：只允许在工作区内的临时目录（tests/_tmp）中造数据，绝不删除真实系统目录。
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

from qingfeng.core import cleaner  # noqa: E402
from qingfeng.core.cleaner import clean, execute_rule_action  # noqa: E402
from qingfeng.core.models import Rule, RuleScanResult, ScanItem  # noqa: E402
from qingfeng.core.scanner import scan  # noqa: E402


def _rule(paths: list[str], *, rule_id: str = "clean-rule", **kwargs) -> Rule:
    base = dict(
        id=rule_id,
        name="清理测试规则",
        category="系统",
        description="测试用",
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


def _item(path: Path, size: int = 10, *, is_dir: bool = False, rule_id: str = "fake") -> ScanItem:
    return ScanItem(
        rule_id=rule_id,
        path=str(path),
        is_dir=is_dir,
        size_bytes=size,
        file_count=1 if not is_dir else 3,
        mtime=time.time(),
    )


class TestCleanDeleteMode(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_permanent_delete_removes_tree(self) -> None:
        _write_file(self.root / "a.txt", 100)
        _write_file(self.root / "sub" / "b.log", 200)
        _write_file(self.root / "sub" / "deep" / "c.tmp", 300)
        rule = _rule([self.root])
        report = scan([rule])
        self.assertEqual(report.results[0].total_bytes, 600)

        result = clean([report.results[0]], mode="delete")
        self.assertFalse(result.recycled)
        self.assertEqual(result.freed_bytes, 600)
        self.assertEqual(result.per_rule.get(rule.id), 600)
        self.assertEqual(result.failed, [])
        self.assertGreaterEqual(result.deleted_files, 3)
        self.assertGreaterEqual(result.deleted_dirs, 1)
        # 规则根目录本身不应被删除
        self.assertTrue(self.root.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_delete_mode_readonly_file(self) -> None:
        target = self.root / "readonly.txt"
        _write_file(target, 50)
        os.chmod(target, 0o444)
        result = clean([scan([_rule([self.root])]).results[0]], mode="delete")
        self.assertFalse(target.exists())
        self.assertEqual(result.freed_bytes, 50)


class TestCleanRecycleMode(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_recycle_single_file(self) -> None:
        target = self.root / "to-recycle.txt"
        _write_file(target, 77)
        result = clean([scan([_rule([self.root])]).results[0]], mode="recycle")
        if result.failed:
            self.skipTest(f"当前环境回收站不可用: {result.failed}")
        self.assertTrue(result.recycled)
        self.assertFalse(target.exists())
        self.assertEqual(result.freed_bytes, 77)

    def test_recycle_directory_aggregate(self) -> None:
        _write_file(self.root / "sub" / "a.bin", 100)
        _write_file(self.root / "sub" / "nested" / "b.bin", 200)
        result = clean([scan([_rule([self.root])]).results[0]], mode="recycle")
        if result.failed:
            self.skipTest(f"当前环境回收站不可用: {result.failed}")
        self.assertTrue(result.recycled)
        self.assertFalse((self.root / "sub").exists())
        self.assertEqual(result.freed_bytes, 300)


class TestCleanSafetyGuards(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_protected_path_rejected(self) -> None:
        windows = Path(os.environ.get("SystemDrive", "C:") + "\\Windows\\System32")
        target = windows / "kernel32.dll"
        if not target.exists():
            self.skipTest("kernel32.dll 不存在，无法验证保护逻辑")
        rule = _rule([self.root])
        fake = RuleScanResult(rule=rule, items=[_item(target, 1024)])
        result = clean([fake], mode="delete")
        self.assertTrue(result.failed)
        self.assertIn("受保护", result.failed[0][1])
        self.assertTrue(target.exists(), "受保护文件绝不能被删除")
        self.assertEqual(result.freed_bytes, 0)

    def test_out_of_rule_scope_rejected(self) -> None:
        outside = self.root / "outside"
        _write_file(outside / "keep.txt", 10)
        rule = _rule([self.root / "inside"])
        (self.root / "inside").mkdir()
        fake = RuleScanResult(rule=rule, items=[_item(outside / "keep.txt", 10)])
        result = clean([fake], mode="delete")
        self.assertTrue(result.failed)
        self.assertIn("不在规则声明范围内", result.failed[0][1])
        self.assertTrue((outside / "keep.txt").exists())

    def test_traversal_item_rejected(self) -> None:
        rule = _rule([self.root])
        evil_path = os.environ.get("SystemDrive", "C:") + "\\Windows\\Temp\\..\\System32\\evil.dll"
        evil = ScanItem(
            rule_id=rule.id,
            path=evil_path,
            is_dir=False,
            size_bytes=1,
            file_count=1,
            mtime=time.time(),
        )
        result = clean([RuleScanResult(rule=rule, items=[evil])], mode="delete")
        self.assertTrue(result.failed)
        self.assertIn("受保护", result.failed[0][1])

    def test_requires_admin_rule_skipped(self) -> None:
        target = self.root / "admin-only.txt"
        _write_file(target, 10)
        rule = _rule([self.root], requires_admin=True)
        report = scan([rule])
        with mock.patch.object(cleaner.winapi, "is_admin", return_value=False):
            result = clean([report.results[0]], mode="delete")
        self.assertTrue(target.exists())
        self.assertTrue(result.failed)
        self.assertIn("管理员", result.failed[0][1])

    def test_requires_admin_records_skipped_admin(self) -> None:
        target = self.root / "admin-record.txt"
        _write_file(target, 12)
        rule = _rule([self.root], rule_id="admin-clean", requires_admin=True)
        item = _item(target, 12)
        scanned = RuleScanResult(rule=rule, items=[item], requires_elevation=True)
        with mock.patch.object(cleaner.winapi, "is_admin", return_value=False):
            result = clean([scanned], mode="recycle")
        self.assertEqual(result.skipped_admin, [rule.name])
        self.assertEqual(result.freed_bytes, 0)
        self.assertTrue(target.exists())

    def test_requires_admin_admin_deletes_and_no_skip(self) -> None:
        target = self.root / "admin-delete.txt"
        _write_file(target, 12)
        rule = _rule([self.root], rule_id="admin-clean", requires_admin=True)
        scanned = RuleScanResult(rule=rule, items=[_item(target, 12)])
        with mock.patch.object(cleaner.winapi, "is_admin", return_value=True):
            result = clean([scanned], mode="delete")
        self.assertEqual(result.skipped_admin, [])
        self.assertFalse(target.exists())
        self.assertGreaterEqual(result.freed_bytes, 12)

    def test_min_age_rechecked_before_delete(self) -> None:
        target = self.root / "aged.txt"
        _write_file(target, 10, age_days=60)
        rule = _rule([self.root], min_age_days=30)
        report = scan([rule])
        self.assertEqual(report.results[0].total_bytes, 10)
        # 扫描到清理之间文件被更新 → 二次校验应跳过
        os.utime(target, None)
        result = clean([report.results[0]], mode="delete")
        self.assertTrue(target.exists())
        self.assertEqual(result.freed_bytes, 0)

    def test_missing_item_ignored(self) -> None:
        rule = _rule([self.root])
        fake = RuleScanResult(rule=rule, items=[_item(self.root / "gone.txt", 10)])
        result = clean([fake], mode="delete")
        self.assertEqual(result.failed, [])
        self.assertEqual(result.freed_bytes, 0)

    def test_report_rule_ignored(self) -> None:
        rule = _rule([], rule_id="report", action="report", report_note="命令")
        result = clean([RuleScanResult(rule=rule)], mode="delete")
        self.assertEqual(result.failed, [])
        self.assertEqual(result.freed_bytes, 0)

    def test_empty_recycle_bin_action(self) -> None:
        rule = _rule([], rule_id="recycle", action="empty_recycle_bin")
        with mock.patch.object(cleaner.winapi, "empty_recycle_bin", return_value=True):
            result = clean([RuleScanResult(rule=rule)], mode="recycle")
        self.assertEqual(result.failed, [])

        with mock.patch.object(cleaner.winapi, "empty_recycle_bin", return_value=False):
            result = clean([RuleScanResult(rule=rule)], mode="recycle")
        self.assertTrue(result.failed)
        self.assertIn("回收站", result.failed[0][0])


class TestCleanFallbackAndCancel(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_recycle_failure_no_fallback_by_default(self) -> None:
        target = self.root / "a.txt"
        _write_file(target, 10)
        report = scan([_rule([self.root])])
        with mock.patch.object(
            cleaner.winapi, "recycle_paths", return_value=[(str(target), "mock 回收站失败")]
        ):
            result = clean([report.results[0]], mode="recycle")
        self.assertTrue(target.exists())
        self.assertTrue(result.failed)
        self.assertEqual(result.freed_bytes, 0)

    def test_recycle_failure_can_fallback_when_enabled(self) -> None:
        target = self.root / "a.txt"
        _write_file(target, 10)
        report = scan([_rule([self.root])])
        original = cleaner.ALLOW_PERMANENT_FALLBACK
        try:
            cleaner.set_permanent_fallback(True)
            with mock.patch.object(
                cleaner.winapi, "recycle_paths", return_value=[(str(target), "mock 回收站失败")]
            ):
                result = clean([report.results[0]], mode="recycle")
            self.assertFalse(target.exists())
            self.assertFalse(result.recycled)  # 发生了永久删除降级
            self.assertEqual(result.freed_bytes, 10)
            self.assertEqual(result.failed, [])
        finally:
            cleaner.set_permanent_fallback(original)

    def test_cancel_prevents_deletion(self) -> None:
        target = self.root / "a.txt"
        _write_file(target, 10)
        report = scan([_rule([self.root])])
        result = clean([report.results[0]], mode="delete", cancel=lambda: True)
        self.assertTrue(target.exists())
        self.assertEqual(result.freed_bytes, 0)

    def test_invalid_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            clean([], mode="shred")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            execute_rule_action(
                _rule([self.root]), RuleScanResult(rule=_rule([self.root])), mode="shred"
            )

    def test_progress_callback_invoked(self) -> None:
        _write_file(self.root / "a.txt", 10)
        report = scan([_rule([self.root])])
        calls: list[tuple[str, int, int]] = []
        clean(
            [report.results[0]],
            mode="delete",
            progress=lambda name, done, total: calls.append((name, done, total)),
        )
        self.assertTrue(calls)
        self.assertEqual(calls[-1][1], calls[-1][2])

    def test_execute_rule_action_single_rule(self) -> None:
        target = self.root / "single.txt"
        _write_file(target, 33)
        rule = _rule([self.root])
        report = scan([rule])
        result = execute_rule_action(rule, report.results[0], mode="delete")
        self.assertFalse(target.exists())
        self.assertEqual(result.per_rule.get(rule.id), 33)

    def test_clean_result_duration(self) -> None:
        result = clean([], mode="delete")
        self.assertGreaterEqual(result.duration, 0.0)
        self.assertEqual(result.freed_bytes, 0)


if __name__ == "__main__":
    unittest.main()
