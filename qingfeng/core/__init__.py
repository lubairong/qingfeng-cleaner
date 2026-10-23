"""清风清理 —— 核心引擎包。

v1 对外暴露 ``models`` / ``rules`` / ``safety`` / ``winapi`` / ``scanner`` / ``cleaner``，
接口定义见 ``docs/ARCHITECTURE.md`` §4；
v2 新增 ``space``（空间索引/系统占用探测）与 ``decide``（大文件决策/跨盘迁移），
见 ``docs/ARCHITECTURE-V2.md`` §2/§3。

注意：本包不依赖 tkinter，可在无界面环境（如 ``--scan-only``、单元测试）中安全导入。
"""

from __future__ import annotations

from . import cleaner, decide, models, rules, safety, scanner, space, winapi
from .cleaner import clean, execute_rule_action
from .decide import (
    ActionPolicy,
    MoveResult,
    ReviewGroup,
    ReviewItem,
    TargetOption,
    action_policy,
    collect_review_items,
    delete_many,
    group_review_items,
    heuristics,
    move_many,
    suggest_targets,
)
from .models import (
    ACTION_VALUES,
    CATEGORIES,
    RISK_VALUES,
    Action,
    CleanResult,
    Risk,
    Rule,
    RuleScanResult,
    ScanItem,
    ScanReport,
)
from .rules import RuleError, expand_paths, load_rules, validate_rule
from .safety import UnsafePathError, assert_deletable, expand, is_protected, within_root
from .scanner import scan
from .space import (
    SpaceIndex,
    SpaceNode,
    build_index,
    cache_path,
    load_cached_index,
    probe_system_space,
    save_index,
)
from .winapi import (
    drive_type,
    drive_usage,
    empty_recycle_bin,
    human_size,
    is_admin,
    open_in_explorer,
    recycle_bin_size,
    recycle_paths,
    relaunch_command,
    restart_as_admin,
)

__all__ = [
    "ACTION_VALUES",
    "ActionPolicy",
    "CATEGORIES",
    "RISK_VALUES",
    "Action",
    "CleanResult",
    "MoveResult",
    "ReviewGroup",
    "ReviewItem",
    "Risk",
    "Rule",
    "RuleError",
    "RuleScanResult",
    "ScanItem",
    "ScanReport",
    "SpaceIndex",
    "SpaceNode",
    "TargetOption",
    "UnsafePathError",
    "action_policy",
    "assert_deletable",
    "build_index",
    "cache_path",
    "clean",
    "cleaner",
    "collect_review_items",
    "decide",
    "delete_many",
    "drive_type",
    "drive_usage",
    "empty_recycle_bin",
    "execute_rule_action",
    "expand",
    "expand_paths",
    "group_review_items",
    "heuristics",
    "human_size",
    "is_admin",
    "is_protected",
    "load_cached_index",
    "load_rules",
    "models",
    "move_many",
    "open_in_explorer",
    "probe_system_space",
    "recycle_bin_size",
    "recycle_paths",
    "relaunch_command",
    "restart_as_admin",
    "rules",
    "safety",
    "save_index",
    "scan",
    "scanner",
    "space",
    "suggest_targets",
    "validate_rule",
    "winapi",
    "within_root",
]
