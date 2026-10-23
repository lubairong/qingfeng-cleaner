"""清风清理 —— 核心数据结构。

本模块定义引擎与界面之间的契约数据类，纯标准库实现，禁止依赖 tkinter。
字段定义见 ``docs/ARCHITECTURE.md`` §2，任何字段/签名变更都必须先同步架构文档。

设计要点：
- 全部使用 ``dataclass``，方便 GUI 直接构造 mock 数据；
- 所有列表字段使用 ``field(default_factory=...)``，避免可变默认值共享；
- ``RuleScanResult`` / ``ScanReport`` 的统计值是**派生属性**，不落盘、不缓存，
  保证 GUI 侧无论怎样修改 ``items`` 都能得到一致的合计值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: 规则风险级别；``advanced`` 默认禁用（见 §0.4）
Risk = Literal["safe", "caution", "advanced"]
#: 规则动作；``report`` 只展示建议命令，不做任何删除
Action = Literal["delete", "empty_recycle_bin", "report"]

#: 合法取值元组，供 rules.py 校验与 GUI 下拉框使用（顺序即优先级）
RISK_VALUES: tuple[str, ...] = ("safe", "caution", "advanced")
ACTION_VALUES: tuple[str, ...] = ("delete", "empty_recycle_bin", "report")
#: 分类顺序即 GUI 展示排序（docs/ARCHITECTURE.md §3）
CATEGORIES: tuple[str, ...] = ("系统", "浏览器", "应用", "隐私痕迹", "大文件", "系统维护")


@dataclass
class Rule:
    """一条清理规则。

    ``paths`` 中的环境变量（``%TEMP%`` / ``%LOCALAPPDATA%`` / ``%WINDIR%`` 等）
    由 ``safety.expand()`` 展开；路径中允许出现 ``*`` / ``?`` 通配符
    （例如浏览器多用户配置目录 ``...\\User Data\\*\\Cache``），扫描器会展开它们。
    """

    id: str
    name: str
    category: str
    description: str
    action: Action
    paths: list[str] = field(default_factory=list)
    globs: list[str] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=list)
    risk: Risk = "safe"
    requires_admin: bool = False
    default_enabled: bool = True
    min_age_days: int = 0
    follow_dirs: bool = True
    protected_globs: list[str] = field(default_factory=list)
    report_note: str = ""
    source: str = ""


@dataclass
class ScanItem:
    """扫描命中的单个可清理条目。

    - ``is_dir=True`` 表示「整棵目录」都被判定为可清理（递归大小已聚合）；
    - ``is_dir=False`` 表示单个文件；
    - ``size_bytes`` / ``file_count`` 对于目录条目是**递归聚合值**；
    - ``mtime`` 为文件（目录条目取目录自身）的修改时间戳。
    """

    rule_id: str
    path: str
    is_dir: bool
    size_bytes: int
    file_count: int
    mtime: float
    risk: Risk = "safe"


@dataclass
class RuleScanResult:
    """单条规则的扫描结果。"""

    rule: Rule
    items: list[ScanItem] = field(default_factory=list)
    error: str | None = None
    skipped_reason: str | None = None  # 如 "需要管理员权限"
    #: 该规则因需要管理员权限而未实际扫描/执行（GUI 用于分组/统计）
    requires_elevation: bool = False

    @property
    def total_bytes(self) -> int:
        """本条规则命中的可清理字节数合计。"""
        return sum(int(item.size_bytes) for item in self.items)

    @property
    def file_count(self) -> int:
        """本条规则命中的文件数合计（目录条目按递归文件数计）。"""
        return sum(int(item.file_count) for item in self.items)


@dataclass
class ScanReport:
    """一次完整扫描的报告。"""

    results: list[RuleScanResult]
    started_at: float
    duration: float
    drive_free: int
    drive_total: int
    cancelled: bool = False

    @property
    def total_bytes(self) -> int:
        """所有规则命中字节数合计（含 report 类规则）。"""
        return sum(res.total_bytes for res in self.results)

    @property
    def cleanable_bytes(self) -> int:
        """真正可清理的字节数：排除 ``action == "report"`` 的只报告规则。"""
        return sum(res.total_bytes for res in self.results if res.rule.action != "report")


@dataclass
class CleanResult:
    """一次清理操作的结果统计。"""

    deleted_files: int = 0
    deleted_dirs: int = 0
    freed_bytes: int = 0
    recycled: bool = True
    failed: list[tuple[str, str]] = field(default_factory=list)  # (path, error)
    per_rule: dict[str, int] = field(default_factory=dict)  # rule_id -> freed_bytes
    #: 因需要管理员权限而被跳过的规则名（GUI 汇总 "X 条系统规则因权限跳过"）
    skipped_admin: list[str] = field(default_factory=list)
    duration: float = 0.0
