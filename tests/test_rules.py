"""规则加载与校验单元测试（tests/test_rules.py）。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import temp_dir  # noqa: E402

from qingfeng.core import safety  # noqa: E402
from qingfeng.core.models import CATEGORIES  # noqa: E402
from qingfeng.core.rules import RuleError, expand_paths, load_rules, validate_rule  # noqa: E402

#: 危险路径规则必须 action=report 的规则 id（Lead 硬性要求）
_REPORT_ONLY_IDS = {
    "winsxs-component-store",
    "windows-old",
    "hibernation-file",
    "pagefile",
    "restore-points",
    "windows-installer-cache",
}

#: 浏览器敏感文件关键字（非 advanced 规则不得出现）
_BROWSER_SECRETS = ("local state", "login data", "cookies", "bookmarks", "web data")


def _write_rules_file(directory: Path, name: str, payload: dict) -> None:
    (directory / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _minimal_rule(**kwargs) -> dict:
    base = {
        "id": "demo-rule",
        "name": "示例规则",
        "category": "系统",
        "description": "示例",
        "action": "delete",
        "paths": ["%TEMP%"],
    }
    base.update(kwargs)
    return base


class TestLoadRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = load_rules()

    def test_rule_count_at_least_38(self) -> None:
        # Lead 要求 ≥38 条（researcher 建议集 + 引擎补充）
        self.assertGreaterEqual(len(self.rules), 38)

    def test_ids_unique_and_kebab_case(self) -> None:
        ids = [rule.id for rule in self.rules]
        self.assertEqual(len(ids), len(set(ids)))
        for rule in self.rules:
            self.assertRegex(rule.id, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

    def test_categories_valid_and_sorted(self) -> None:
        for rule in self.rules:
            self.assertIn(rule.category, CATEGORIES)
        order = [CATEGORIES.index(rule.category) for rule in self.rules]
        self.assertEqual(order, sorted(order))

    def test_all_six_categories_present(self) -> None:
        present = {rule.category for rule in self.rules}
        self.assertEqual(present, set(CATEGORIES))

    def test_advanced_rules_disabled_by_default(self) -> None:
        for rule in self.rules:
            if rule.risk == "advanced":
                self.assertFalse(rule.default_enabled, f"{rule.id} 是 advanced 但默认启用")

    def test_no_delete_rule_targets_protected_paths(self) -> None:
        for rule in self.rules:
            if rule.action != "delete":
                continue
            for raw_path in rule.paths:
                expanded = safety.expand(raw_path)
                if not any(ch in expanded for ch in "*?["):
                    self.assertFalse(
                        safety.is_protected(expanded),
                        f"规则 {rule.id} 指向受保护路径: {raw_path}",
                    )
                else:
                    for path in expand_paths(rule):
                        self.assertFalse(
                            safety.is_protected(path),
                            f"规则 {rule.id} 展开到受保护路径: {path}",
                        )

    def test_no_rule_targets_user_document_roots(self) -> None:
        protected_names = (
            "documents",
            "desktop",
            "downloads",
            "pictures",
            "videos",
            "music",
            "onedrive",
        )
        # Lead 批准的窄例外：仅微信经典版缓存目录允许位于 Documents 之下
        document_exception_ids = {"wechat-cache"}
        for rule in self.rules:
            if rule.action != "delete" or rule.id in document_exception_ids:
                continue
            for raw_path in rule.paths:
                expanded = safety.expand(raw_path)
                normalized = expanded.replace("/", "\\").lower()
                for name in protected_names:
                    self.assertNotRegex(
                        normalized,
                        rf"\\users\\[^\\]+\\{name}(\\|$)",
                        f"规则 {rule.id} 直接指向用户 {name} 目录: {raw_path}",
                    )

    def test_browser_rules_do_not_touch_secrets(self) -> None:
        for rule in self.rules:
            blob = " ".join(rule.paths + rule.globs).lower()
            if rule.id.startswith(("edge-", "chrome-", "firefox-", "chromium-", "opera-")):
                for secret in _BROWSER_SECRETS:
                    self.assertNotIn(secret, blob, f"浏览器规则 {rule.id} 触碰敏感文件: {secret}")
                if "history" in blob:
                    # History 只允许单列 advanced 且默认关闭
                    self.assertEqual(rule.risk, "advanced", rule.id)
                    self.assertFalse(rule.default_enabled, rule.id)

    def test_dangerous_targets_are_report_only(self) -> None:
        rules = {rule.id: rule for rule in self.rules}
        for rule_id in _REPORT_ONLY_IDS:
            self.assertIn(rule_id, rules, f"缺少必须的只报告规则: {rule_id}")
            self.assertEqual(rules[rule_id].action, "report", f"{rule_id} 必须是 action=report")

    def test_report_rules_have_note_and_no_paths(self) -> None:
        for rule in self.rules:
            if rule.action != "report":
                continue
            self.assertTrue(rule.report_note, f"{rule.id} 缺少 report_note")
            self.assertEqual(rule.paths, [], f"{rule.id} 报告规则不应声明 paths")

    def test_rules_have_description_and_source(self) -> None:
        for rule in self.rules:
            self.assertTrue(rule.description.strip(), rule.id)
            self.assertTrue(rule.source.strip(), f"{rule.id} 缺少 source")

    def test_wechat_cache_rule_spec(self) -> None:
        """Lead 批准的微信缓存规则：唯一穿透 Documents 保护的规则（窄例外）。"""
        rules = {rule.id: rule for rule in self.rules}
        self.assertIn("wechat-cache", rules)
        rule = rules["wechat-cache"]
        self.assertEqual(rule.category, "应用")
        self.assertEqual(rule.action, "delete")
        self.assertEqual(rule.risk, "caution")
        self.assertFalse(rule.default_enabled)
        self.assertEqual(
            rule.paths,
            [r"%USERPROFILE%\Documents\WeChat Files\*\FileStorage\Cache"],
        )
        self.assertIn("聊天记录", rule.description)
        self.assertIn("缓存", rule.description)


class TestValidateRule(unittest.TestCase):
    def test_fill_defaults(self) -> None:
        rule = validate_rule(_minimal_rule())
        self.assertEqual(rule.risk, "safe")
        self.assertTrue(rule.default_enabled)
        self.assertEqual(rule.min_age_days, 0)
        self.assertTrue(rule.follow_dirs)
        self.assertFalse(rule.requires_admin)
        self.assertEqual(rule.report_note, "")

    def test_advanced_forced_disabled(self) -> None:
        rule = validate_rule(_minimal_rule(id="adv-rule", risk="advanced", default_enabled=True))
        self.assertEqual(rule.risk, "advanced")
        self.assertFalse(rule.default_enabled)

    def test_invalid_values_rejected(self) -> None:
        bad_cases = [
            _minimal_rule(action="rm-rf"),
            _minimal_rule(risk="danger"),
            _minimal_rule(category="不存在的分类"),
            _minimal_rule(min_age_days=-1),
            _minimal_rule(follow_dirs="yes"),
            _minimal_rule(id="Bad_ID"),
            {**_minimal_rule(), "name": ""},
        ]
        for raw in bad_cases:
            with self.subTest(raw=raw):
                with self.assertRaises(RuleError):
                    validate_rule(raw)

    def test_delete_rule_requires_paths(self) -> None:
        with self.assertRaises(RuleError):
            validate_rule(_minimal_rule(paths=[]))

    def test_delete_rule_rejects_protected_path(self) -> None:
        for dangerous in (
            "%WINDIR%\\System32",
            "%USERPROFILE%\\Documents",
            "%USERPROFILE%\\AppData",
            "%SYSTEMDRIVE%\\",
        ):
            with self.subTest(path=dangerous):
                with self.assertRaises(RuleError):
                    validate_rule(_minimal_rule(paths=[dangerous]))

    def test_report_rule_requires_note(self) -> None:
        with self.assertRaises(RuleError):
            validate_rule(
                {
                    "id": "report-rule",
                    "name": "报告",
                    "category": "系统维护",
                    "description": "说明",
                    "action": "report",
                    "paths": [],
                }
            )

    def test_report_rule_may_have_empty_paths(self) -> None:
        rule = validate_rule(
            {
                "id": "report-rule",
                "name": "报告",
                "category": "系统维护",
                "description": "说明",
                "action": "report",
                "paths": [],
                "report_note": "建议命令",
            }
        )
        self.assertEqual(rule.action, "report")
        self.assertEqual(rule.paths, [])

    def test_wildcard_constraints(self) -> None:
        # 不允许 **
        with self.assertRaises(RuleError):
            validate_rule(_minimal_rule(paths=["%TEMP%\\**\\*.log"]))
        # 不允许通配符出现在盘符/根段
        with self.assertRaises(RuleError):
            validate_rule(_minimal_rule(paths=["%SYSTEMDRIVE%\\*"]))
        # 通配符必须独占一段
        with self.assertRaises(RuleError):
            validate_rule(_minimal_rule(paths=["%TEMP%\\*cache"]))
        # 合法的单层 * 段
        rule = validate_rule(_minimal_rule(paths=["%LOCALAPPDATA%\\Microsoft\\Edge\\User Data\\*\\Cache"]))
        self.assertEqual(len(rule.paths), 1)

    def test_wechat_cache_path_allowed(self) -> None:
        """唯一的文档目录例外：微信经典版缓存路径可通过校验。"""
        rule = validate_rule(
            _minimal_rule(paths=[r"%USERPROFILE%\Documents\WeChat Files\*\FileStorage\Cache"])
        )
        self.assertEqual(
            rule.paths, [r"%USERPROFILE%\Documents\WeChat Files\*\FileStorage\Cache"]
        )

    def test_documents_wildcards_still_rejected(self) -> None:
        """除微信 Cache 外，Documents 下的通配路径一律禁止加载。"""
        rejected = [
            r"%USERPROFILE%\Documents\*",
            r"%USERPROFILE%\Documents\WeChat Files\*",
            r"%USERPROFILE%\Documents\WeChat Files\*\FileStorage",
            r"%USERPROFILE%\Documents\WeChat Files\*\FileStorage\MsgAttach",
            r"%USERPROFILE%\Documents\WeChat Files\*\Msg",
        ]
        for path in rejected:
            with self.subTest(path=path):
                with self.assertRaises(RuleError):
                    validate_rule(_minimal_rule(paths=[path]))

    def test_duplicate_ids_rejected(self) -> None:
        payload_a = {"version": 1, "category": "系统", "rules": [_minimal_rule(id="dup-rule")]}
        payload_b = {"version": 1, "category": "系统", "rules": [_minimal_rule(id="dup-rule")]}
        with temp_dir() as tmp:
            directory = Path(tmp)
            _write_rules_file(directory, "a.json", payload_a)
            _write_rules_file(directory, "b.json", payload_b)
            with self.assertRaises(RuleError):
                load_rules(directory)

    def test_invalid_json_rejected(self) -> None:
        with temp_dir() as tmp:
            (Path(tmp) / "broken.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(RuleError):
                load_rules(Path(tmp))

    def test_missing_directory_rejected(self) -> None:
        with temp_dir() as tmp:
            with self.assertRaises(RuleError):
                load_rules(Path(tmp) / "not-exists")

    def test_category_mismatch_rejected(self) -> None:
        payload = {"version": 1, "category": "浏览器", "rules": [_minimal_rule(category="系统")]}
        with temp_dir() as tmp:
            _write_rules_file(Path(tmp), "x.json", payload)
            with self.assertRaises(RuleError):
                load_rules(Path(tmp))

    def test_custom_rules_dir_loaded(self) -> None:
        payload = {"version": 1, "category": "系统", "rules": [_minimal_rule(id="custom-rule")]}
        with temp_dir() as tmp:
            _write_rules_file(Path(tmp), "x.json", payload)
            rules = load_rules(Path(tmp))
            self.assertEqual([rule.id for rule in rules], ["custom-rule"])


class TestExpandPaths(unittest.TestCase):
    def _rule_with_paths(self, paths: list[str]):
        return validate_rule(_minimal_rule(id="expand-rule", paths=paths))

    def test_plain_path_expansion(self) -> None:
        with temp_dir() as tmp:
            rule = self._rule_with_paths([tmp])
            self.assertEqual([str(Path(path)) for path in expand_paths(rule)], [str(Path(tmp))])

    def test_single_star_expands_directories_only(self) -> None:
        with temp_dir() as tmp:
            base = Path(tmp)
            (base / "profile-a").mkdir()
            (base / "profile-b").mkdir()
            (base / "a-file.txt").write_text("x", encoding="utf-8")
            rule = self._rule_with_paths([str(base / "*")])
            expanded = expand_paths(rule)
            names = sorted(Path(path).name for path in expanded)
            self.assertEqual(names, ["profile-a", "profile-b"])

    def test_unexpanded_wildcard_returns_empty(self) -> None:
        rule = self._rule_with_paths(["%LOCALAPPDATA%\\ThisAppDoesNotExist\\*\\Cache"])
        self.assertEqual(expand_paths(rule), [])

    def test_middle_star_expands_files_too(self) -> None:
        """M2 回归：``*`` 在中间时，``User Data\\*\\History`` 类路径必须能展开到文件。"""
        with temp_dir() as tmp:
            base = Path(tmp)
            for profile in ("Default", "Profile 1"):
                history = base / "User Data" / profile / "History"
                history.parent.mkdir(parents=True)
                history.write_bytes(b"history")
            rule = self._rule_with_paths([str(base / "User Data" / "*" / "History")])
            expanded = expand_paths(rule)
            self.assertEqual(len(expanded), 2)
            self.assertTrue(all(Path(path).is_file() for path in expanded))
            self.assertTrue(all(Path(path).name == "History" for path in expanded))

    def test_middle_star_expands_directories_too(self) -> None:
        """M2 回归：``*`` 在中间、后接目录时，仍需展开目录（微信缓存语义不变）。"""
        with temp_dir() as tmp:
            base = Path(tmp)
            for account in ("wxid_a", "wxid_b"):
                cache = base / "WeChat Files" / account / "FileStorage" / "Cache"
                cache.mkdir(parents=True)
            rule = self._rule_with_paths(
                [str(base / "WeChat Files" / "*" / "FileStorage" / "Cache")]
            )
            expanded = expand_paths(rule)
            self.assertEqual(len(expanded), 2)
            self.assertTrue(all(Path(path).is_dir() for path in expanded))
            self.assertTrue(all(Path(path).name == "Cache" for path in expanded))

    def test_missing_plain_path_still_returned(self) -> None:
        # 不存在的普通路径由 scanner 判断存在性，expand_paths 原样返回
        rule = self._rule_with_paths(["%TEMP%\\qingfeng-not-exists-12345"])
        self.assertEqual(len(expand_paths(rule)), 1)


if __name__ == "__main__":
    unittest.main()
