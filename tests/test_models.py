"""数据结构单元测试（tests/test_models.py）。"""

from __future__ import annotations

import unittest

from qingfeng.core.models import (
    ACTION_VALUES,
    CATEGORIES,
    RISK_VALUES,
    CleanResult,
    Rule,
    RuleScanResult,
    ScanItem,
    ScanReport,
)


def _rule(**kwargs) -> Rule:
    base = dict(
        id="demo-rule",
        name="示例",
        category="系统",
        description="示例规则",
        action="delete",
    )
    base.update(kwargs)
    return Rule(**base)  # type: ignore[arg-type]


class TestModels(unittest.TestCase):
    def test_rule_defaults(self) -> None:
        rule = _rule()
        self.assertEqual(rule.paths, [])
        self.assertEqual(rule.globs, [])
        self.assertEqual(rule.exclude_globs, [])
        self.assertEqual(rule.risk, "safe")
        self.assertFalse(rule.requires_admin)
        self.assertTrue(rule.default_enabled)
        self.assertEqual(rule.min_age_days, 0)
        self.assertTrue(rule.follow_dirs)
        self.assertEqual(rule.protected_globs, [])
        self.assertEqual(rule.report_note, "")
        self.assertEqual(rule.source, "")

    def test_rule_scan_result_totals(self) -> None:
        rule = _rule()
        result = RuleScanResult(rule=rule)
        self.assertEqual(result.total_bytes, 0)
        self.assertEqual(result.file_count, 0)
        result.items = [
            ScanItem(rule_id=rule.id, path="C:\\a", is_dir=False, size_bytes=100, file_count=1, mtime=1.0),
            ScanItem(rule_id=rule.id, path="C:\\b", is_dir=True, size_bytes=900, file_count=9, mtime=1.0),
        ]
        self.assertEqual(result.total_bytes, 1000)
        self.assertEqual(result.file_count, 10)

    def test_scan_report_cleanable_excludes_report_action(self) -> None:
        delete_rule = _rule(id="a", action="delete")
        report_rule = _rule(id="b", action="report", report_note="命令")
        empty_rule = _rule(id="c", action="empty_recycle_bin")
        results = [
            RuleScanResult(
                rule=delete_rule,
                items=[ScanItem(rule_id="a", path="x", is_dir=False, size_bytes=10, file_count=1, mtime=0)],
            ),
            RuleScanResult(
                rule=report_rule,
                items=[ScanItem(rule_id="b", path="y", is_dir=False, size_bytes=20, file_count=1, mtime=0)],
            ),
            RuleScanResult(
                rule=empty_rule,
                items=[ScanItem(rule_id="c", path="z", is_dir=False, size_bytes=30, file_count=1, mtime=0)],
            ),
        ]
        report = ScanReport(results=results, started_at=0.0, duration=0.1, drive_free=1, drive_total=2)
        self.assertEqual(report.total_bytes, 60)
        self.assertEqual(report.cleanable_bytes, 40)  # 排除 report 规则的 20
        self.assertFalse(report.cancelled)

    def test_clean_result_defaults(self) -> None:
        result = CleanResult()
        self.assertEqual(result.deleted_files, 0)
        self.assertEqual(result.deleted_dirs, 0)
        self.assertEqual(result.freed_bytes, 0)
        self.assertTrue(result.recycled)
        self.assertEqual(result.failed, [])
        self.assertEqual(result.per_rule, {})
        self.assertEqual(result.duration, 0.0)

    def test_risk_action_category_constants(self) -> None:
        self.assertEqual(RISK_VALUES, ("safe", "caution", "advanced"))
        self.assertEqual(ACTION_VALUES, ("delete", "empty_recycle_bin", "report"))
        self.assertEqual(CATEGORIES[0], "系统")
        self.assertEqual(CATEGORIES[-1], "系统维护")


if __name__ == "__main__":
    unittest.main()
