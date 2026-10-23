"""清风清理 — 界面内置演示数据（引擎缺失时的后备）。

当 `qingfeng.core` 尚未就绪（ImportError）时，界面使用这里的数据，保证
GUI 可以独立开发、演示与截图。数据结构与 docs/ARCHITECTURE.md §2 对齐，
属性名称完全兼容，但**不会触碰任何真实文件**。
"""

from __future__ import annotations

import ctypes
import math
import time
from dataclasses import dataclass, field

CATEGORY_ORDER = ["系统", "浏览器", "应用", "隐私痕迹", "大文件", "系统维护"]


@dataclass
class MockRule:
    id: str
    name: str
    category: str
    description: str
    action: str = "delete"
    paths: list[str] = field(default_factory=list)
    risk: str = "safe"
    requires_admin: bool = False
    default_enabled: bool = True
    report_note: str = ""
    source: str = "mock"


@dataclass
class MockScanItem:
    rule_id: str
    path: str
    is_dir: bool
    size_bytes: int
    file_count: int
    mtime: float
    risk: str = "safe"


@dataclass
class MockRuleResult:
    rule: MockRule
    items: list[MockScanItem] = field(default_factory=list)
    error: str | None = None
    skipped_reason: str | None = None

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.items)

    @property
    def file_count(self) -> int:
        return sum(item.file_count for item in self.items)


@dataclass
class MockScanReport:
    results: list[MockRuleResult] = field(default_factory=list)
    started_at: float = 0.0
    duration: float = 0.0
    drive_free: int = 0
    drive_total: int = 0
    cancelled: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(r.total_bytes for r in self.results)

    @property
    def cleanable_bytes(self) -> int:
        return sum(r.total_bytes for r in self.results if r.rule.action != "report")


@dataclass
class MockCleanResult:
    deleted_files: int = 0
    deleted_dirs: int = 0
    freed_bytes: int = 0
    recycled: bool = True
    failed: list[tuple[str, str]] = field(default_factory=list)
    per_rule: dict[str, int] = field(default_factory=dict)
    duration: float = 0.0


# ---------------------------------------------------------------------------
# 演示规则：6 个分类 / 31 条规则 / 可清理约 11 GB
# ---------------------------------------------------------------------------

_MB = 1024 * 1024

# (id, 名称, 分类, 说明, action, risk, requires_admin, enabled, 兆字节, 文件数)
_RAW_RULES: list[tuple] = [
    # 系统
    ("user-temp", "用户临时文件", "系统", "当前用户 %TEMP% 下的临时文件与安装残留",
     "delete", "safe", False, True, 1200, 12480),
    ("windows-temp", "Windows 临时文件", "系统", "C:\\Windows\\Temp 中的临时文件",
     "delete", "safe", False, True, 1600, 8260),
    ("system-logs", "系统日志文件", "系统", "Windows 日志与 CBS 记录（CBS 日志可能较大）",
     "delete", "safe", False, True, 420, 1840),
    ("crash-dumps", "崩溃转储文件", "系统", "内存转储与 WER 错误报告",
     "delete", "caution", False, True, 640, 132),
    ("thumbnails", "缩略图缓存", "系统", "资源管理器缩略图与图标缓存，会自动重建",
     "delete", "safe", False, True, 210, 2430),
    ("font-cache", "字体缓存", "系统", "字体缓存文件，重启资源管理器后自动重建",
     "delete", "safe", False, True, 78, 350),
    ("update-cache", "Windows 更新缓存", "系统", "已下载的更新包，清理后可能需要重新下载",
     "delete", "safe", True, True, 1500, 5400),
    # 浏览器
    ("edge-cache", "Microsoft Edge 缓存", "浏览器", "Edge 网页缓存与 GPU 缓存（不含密码/书签）",
     "delete", "safe", False, True, 680, 5120),
    ("chrome-cache", "Google Chrome 缓存", "浏览器", "Chrome 网页缓存与代码缓存（不含 Cookie）",
     "delete", "safe", False, True, 430, 3340),
    ("firefox-cache", "Firefox 缓存", "浏览器", "Firefox 缓存目录，不影响书签与密码",
     "delete", "safe", False, True, 220, 1910),
    ("browser-update-cache", "浏览器更新缓存", "浏览器", "旧版本浏览器安装包缓存",
     "delete", "caution", False, True, 220, 160),
    ("edge-history", "Edge 历史记录", "浏览器", "浏览历史数据库（会记录访问痕迹）",
     "delete", "advanced", False, False, 160, 920),
    ("chrome-session", "Chrome 会话恢复文件", "浏览器", "未关闭标签页的会话快照",
     "delete", "advanced", False, False, 90, 410),
    # 应用
    ("wechat-cache", "微信缓存", "应用", "微信聊天图片、视频与文件缓存（不删聊天记录）",
     "delete", "safe", False, True, 980, 8420),
    ("qq-cache", "QQ 缓存", "应用", "QQ 接收的图片、表情与临时文件",
     "delete", "safe", False, True, 620, 4710),
    ("dingtalk-cache", "钉钉缓存", "应用", "钉钉图片、文件与更新缓存",
     "delete", "safe", False, True, 320, 2650),
    ("office-temp", "Office 临时文件", "应用", "Office 自动保存与临时文档碎片",
     "delete", "caution", False, True, 280, 1930),
    ("teams-cache", "Teams 缓存", "应用", "Teams 缓存与日志（重新登录后恢复）",
     "delete", "caution", False, True, 380, 2820),
    ("steam-cache", "Steam 下载缓存", "应用", "已安装游戏的下载缓存与着色器缓存",
     "delete", "caution", False, True, 1020, 1640),
    # 隐私痕迹（默认关闭）
    ("recent-files", "最近文件记录", "隐私痕迹", "资源管理器最近访问的文件列表",
     "delete", "advanced", False, False, 120, 640),
    ("jump-lists", "跳转列表", "隐私痕迹", "任务栏跳转列表缓存",
     "delete", "advanced", False, False, 80, 330),
    ("clipboard-history", "剪贴板历史", "隐私痕迹", "Win+V 剪贴板历史记录",
     "delete", "advanced", False, False, 20, 42),
    ("explorer-address-history", "地址栏历史", "隐私痕迹", "资源管理器地址栏输入记录",
     "delete", "advanced", False, False, 15, 120),
    ("run-dialog-history", "运行对话框历史", "隐私痕迹", "Win+R 运行记录",
     "delete", "advanced", False, False, 5, 30),
    # 大文件（仅报告）
    ("big-files-top", "C 盘大文件 Top 100", "大文件", "扫描 C 盘最大的文件，仅报告不删除",
     "report", "safe", False, False, 8400, 100),
    ("downloads-big", "下载目录大文件", "大文件", "下载目录中超过 100 MB 的文件，仅报告",
     "report", "safe", False, False, 2100, 62),
    ("video-cache-dir", "视频缓存目录", "大文件", "视频软件缓存目录占用，仅报告",
     "report", "caution", False, False, 3200, 44),
    # 系统维护（仅报告 / 需要管理员）
    ("recycle-bin", "回收站", "系统维护", "回收站占用空间，请手动确认后清空",
     "empty_recycle_bin", "caution", False, False, 1600, 820),
    ("hiberfil", "休眠文件", "系统维护", "hiberfil.sys，可通过 powercfg 关闭休眠释放",
     "report", "caution", True, False, 6400, 1),
    ("restore-points", "系统还原点", "系统维护", "系统还原占用的空间，需在系统属性中管理",
     "report", "caution", True, False, 4200, 6),
    ("winsxs", "组件存储 WinSxS", "系统维护", "Windows 组件存储，请使用 DISM 清理",
     "report", "advanced", True, False, 12000, 1),
    ("pagefile", "页面文件", "系统维护", "pagefile.sys，由系统管理，不建议手动删除",
     "report", "advanced", True, False, 2048, 1),
]

# 演示用 report_note：多行 PowerShell 命令，用于验证详情面板的换行与复制
_REPORT_NOTES: dict[str, str] = {
    "big-files-top": (
        "只读命令（PowerShell，需管理员）：\n"
        "Get-ChildItem C:\\ -Recurse -File -ErrorAction SilentlyContinue |\n"
        "  Sort-Object Length -Descending |\n"
        "  Select-Object -First 100 FullName, "
        "@{n='GB';e={[math]::Round($_.Length/1GB,2)}}"
    ),
    "downloads-big": (
        "只读命令（PowerShell）：\n"
        "Get-ChildItem $env:USERPROFILE\\Downloads -Recurse -File |\n"
        "  Sort-Object Length -Descending | Select-Object -First 30 "
        "FullName, @{n='MB';e={[math]::Round($_.Length/1MB)}}"
    ),
    "video-cache-dir": (
        "只读命令（PowerShell）：\n"
        "Get-ChildItem C:\\ -Directory | ForEach-Object {\n"
        "  $s = (Get-ChildItem $_.FullName -Recurse -File "
        "-ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum;\n"
        "  [pscustomobject]@{Path=$_.FullName; GB=[math]::Round($s/1GB,2)}\n"
        "} | Sort-Object GB -Descending | Select-Object -First 20"
    ),
}

MOCK_RULES: list[MockRule] = [
    MockRule(
        id=rid,
        name=name,
        category=category,
        description=desc,
        action=action,
        risk=risk,
        requires_admin=admin,
        default_enabled=enabled and risk != "advanced",
        report_note=(
            _REPORT_NOTES.get(rid, "仅报告，不会删除")
            if action == "report"
            else ("清空后不可恢复；建议先确认回收站占用再让用户决定"
                  if action == "empty_recycle_bin" else "")
        ),
    )
    for rid, name, category, desc, action, risk, admin, enabled, _mb, _count in _RAW_RULES
]

_RULE_META = {
    rid: (size_mb, count) for rid, _n, _c, _d, _a, _r, _ad, _e, size_mb, count in _RAW_RULES
}


# ---------------------------------------------------------------------------
# 系统信息（真实，不依赖 qingfeng.core）
# ---------------------------------------------------------------------------


def _drive_usage(drive: str = "C:") -> tuple[int, int]:
    try:
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        total_free = ctypes.c_ulonglong(0)
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(drive + "\\"),
            ctypes.byref(free),
            ctypes.byref(total),
            ctypes.byref(total_free),
        )
        if ok:
            return int(free.value), int(total.value)
    except Exception:
        pass
    total = 512 * 1024**3
    return int(total * 0.27), total


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _human_size(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(max(0, n))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(n)} B"


# ---------------------------------------------------------------------------
# 演示扫描 / 清理
# ---------------------------------------------------------------------------


def load_rules() -> list[MockRule]:
    return list(MOCK_RULES)


def scan(
    rules: list[MockRule],
    *,
    progress=None,
    cancel=None,
    max_depth: int = 24,
) -> MockScanReport:
    started = time.time()
    report = MockScanReport(started_at=started)
    total = len(rules)
    for index, rule in enumerate(rules, start=1):
        if cancel is not None and cancel():
            report.cancelled = True
            break
        if progress is not None:
            try:
                progress(rule.name, index, total)
            except Exception:
                pass
        size_mb, count = _RULE_META.get(rule.id, (64, 120))
        # 让每条规则耗时 1%~3% 秒，总时长约 2 秒，便于观察进度条
        time.sleep(0.015 + min(0.06, size_mb / 22000.0))
        # 用正弦扰动让演示数字看起来有"真实感"，但保持确定性
        wobble = 0.94 + 0.06 * math.sin(index * 1.7)
        size_bytes = int(size_mb * _MB * wobble)
        item = MockScanItem(
            rule_id=rule.id,
            path=f"%DEMO%\\{rule.id}",
            is_dir=True,
            size_bytes=size_bytes,
            file_count=max(1, int(count * wobble)),
            mtime=started - 86400 * (1 + index % 7),
            risk=rule.risk,
        )
        result = MockRuleResult(rule=rule, items=[item])
        if rule.requires_admin and not _is_admin():
            result.skipped_reason = "需要管理员权限"
        report.results.append(result)
    report.duration = time.time() - started
    report.drive_free, report.drive_total = _drive_usage()
    return report


def clean(
    results: list[MockRuleResult],
    *,
    mode: str = "recycle",
    progress=None,
    cancel=None,
) -> MockCleanResult:
    started = time.time()
    out = MockCleanResult(recycled=(mode == "recycle"))
    total = len(results)
    for index, result in enumerate(results, start=1):
        if cancel is not None and cancel():
            break
        if progress is not None:
            try:
                progress(result.rule.name, index, total)
            except Exception:
                pass
        time.sleep(0.06)
        for item in result.items:
            out.deleted_files += item.file_count
            out.freed_bytes += item.size_bytes
            out.per_rule[result.rule.id] = out.per_rule.get(result.rule.id, 0) + item.size_bytes
        if result.items and result.items[0].is_dir:
            out.deleted_dirs += 1
    out.duration = time.time() - started
    return out
