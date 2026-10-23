"""清风清理 v2 —— 大文件决策助手与跨盘迁移。

职责（对应 ``docs/ARCHITECTURE-V2.md`` §3）：

- 基于 :class:`qingfeng.core.space.SpaceIndex` 抽取 ≥ ``min_size`` 的大文件/目录，
  按 ``qingfeng/data/heuristics.json`` 分类并给出「可删 / 待定 / 保留 / 迁移」建议；
- :func:`suggest_targets` 为迁移推荐可写的固定磁盘目标目录；
- :func:`move_many` 执行跨盘迁移：**复制成功才删源**，任何失败/取消都保留源文件。

迁移安全规则（§3 硬性要求）：

1. 逐个路径过 :func:`qingfeng.core.safety.assert_deletable`，受保护路径拒绝；
2. 目标盘必须是固定磁盘（``GetDriveTypeW == 3``）且与源不同盘；
3. 同盘 / 目标在源内部 → 拒绝（防递归）；
4. 目标保留原目录结构：``<dest_root>\\<盘符>_mirror\\<原相对路径>``，重名不覆盖（`` (2)`` 后缀）；
5. 先复制并校验大小，成功后才删除源；失败/取消保留源文件并清理半成品；
6. 不跟随 reparse point。
"""

from __future__ import annotations

import fnmatch
import json
import ntpath
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import cleaner, models, safety, space, winapi

#: 进度回调：``progress(当前路径, 已完成项数, 总项数)``
ProgressCallback = Callable[[str, int, int], None]
#: 取消回调：返回 ``True`` 时尽快停止
CancelCallback = Callable[[], bool]

#: 复制缓冲区大小
_COPY_BUFFER = 4 * 1024 * 1024
#: 目标盘空间安全余量（字节）
_SPACE_MARGIN = 64 * 1024 * 1024
#: suggest_targets 最多实试的顶级目录数
_MAX_TARGET_PROBE = 8
#: 目标目录筛选时优先尝试的关键字（提高"能找到用户可写目录"的概率）
_PREFERRED_DIR_KEYWORDS = (
    "download",
    "下载",
    "tmp",
    "temp",
    "doc",
    "文档",
    "file",
    "文件",
    "data",
    "资料",
    "backup",
    "备份",
    "move",
    "迁移",
)
#: 枚举目标盘时直接跳过的系统目录名（小写）
_SKIP_TARGET_DIRS = frozenset(
    {
        "windows",
        "program files",
        "program files (x86)",
        "programdata",
        "recovery",
        "system volume information",
        "$recycle.bin",
        "$recycler.bin",
        "config.msi",
        "msocache",
        "perflogs",
        "intel",
        "amd",
        "nvidia",
        "documents and settings",
    }
)

#: 分组展示顺序与默认建议（§3.2）
GROUP_ORDER: tuple[str, ...] = (
    "installer",
    "app-version-dup",
    "backup",
    "devcache",
    "video",
    "archive",
    "vm",
    "large-misc",
    "keep",
)

#: group key -> (中文标题, 组级建议)
GROUP_LABELS: dict[str, tuple[str, str]] = {
    "installer": ("安装包 / 升级残留", "放心清理（回收站）"),
    "app-version-dup": ("程序的旧版本副本", "可清理（确认程序未使用旧版）"),
    "backup": ("备份 / 旧副本", "可清理（确认无回滚需求）"),
    "devcache": ("开发缓存 / 构建产物", "可清理（下次构建会重建）"),
    "video": ("视频 / 录屏", "你来判断（可迁移到 D/E 盘）"),
    "archive": ("压缩包", "你来判断（可迁移到 D/E 盘）"),
    "vm": ("虚拟机磁盘 / 镜像", "你来判断（可迁移到 D/E 盘）"),
    "large-misc": ("其它大文件", "你来判断"),
    "keep": ("建议保留", "不要删"),
}

#: 允许作为「目录级迁移项」的分组（其它大目录不进大文件列表，避免刷屏）
DIR_REVIEW_GROUPS: frozenset[str] = frozenset({"devcache", "app-version-dup", "backup"})


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class ReviewItem:
    """一条大文件/大目录的决策项。"""

    path: str
    name: str
    is_dir: bool
    size: int
    mtime: float
    age_days: int
    ext: str
    category: str
    advice: str  # "safe_delete" | "review" | "keep" | "move"
    reason: str
    risk: str  # "safe" | "caution" | "danger"
    group: str


@dataclass
class ReviewGroup:
    """按 group 聚合后的决策组。"""

    key: str
    label: str
    items: list[ReviewItem]
    total_bytes: int
    advice: str


@dataclass
class TargetOption:
    """迁移目标盘选项（由 :func:`suggest_targets` 探测得到）。"""

    drive: str
    path: str
    free_bytes: int
    writable: bool
    note: str


@dataclass
class MoveResult:
    """一次迁移操作的结果统计。"""

    moved: int = 0
    moved_bytes: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    dest_root: str = ""
    duration: float = 0.0


@dataclass
class ActionPolicy:
    """某个大文件/目录允许执行的动作（空间透视页按钮状态直接读它）。"""

    can_delete: bool
    can_move: bool
    deny_reason: str = ""


# ---------------------------------------------------------------------------
# 分类词典
# ---------------------------------------------------------------------------


_DEFAULT_HEURISTICS: dict = {
    "version": 1,
    "categories": [],
    "age_rules": [],
    "never_touch": [],
}

_heuristics_cache: dict = {"data": None, "mtime": None, "path": None}


def _heuristics_path() -> Path:
    import qingfeng

    return Path(qingfeng.PACKAGE_DIR) / "data" / "heuristics.json"


def heuristics() -> dict:
    """读取 ``qingfeng/data/heuristics.json``（按 mtime 缓存）。

    文件缺失/损坏时返回内置最小词典（不抛异常，GUI 不会因此崩溃）。
    """
    path = _heuristics_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _heuristics_cache["data"] or _DEFAULT_HEURISTICS
    if _heuristics_cache["data"] is not None and _heuristics_cache["mtime"] == mtime:
        return _heuristics_cache["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("heuristics 顶层必须是对象")
    except (OSError, json.JSONDecodeError, ValueError):
        return _heuristics_cache["data"] or _DEFAULT_HEURISTICS
    _heuristics_cache.update({"data": data, "mtime": mtime, "path": str(path)})
    return data


def _norm_globs(globs: list) -> list[str]:
    normalized: list[str] = []
    for raw in globs or []:
        text = str(raw).replace("/", "\\").lower()
        if text.endswith("\\"):
            text += "*"
        normalized.append(text)
    return normalized


def _path_matches(path: str, globs: list) -> bool:
    expanded = safety.expand(path) or path
    target = expanded.replace("/", "\\").lower()
    # 目录路径本身不带结尾分隔符，但词典里的 pattern 常以 "\*" 结尾，
    # 因此同时用带结尾分隔符的形式匹配。
    candidates = [target]
    if not target.endswith("\\"):
        candidates.append(target + "\\")
    return any(
        fnmatch.fnmatchcase(candidate, pattern)
        for candidate in candidates
        for pattern in _norm_globs(globs)
    )


def _file_ext(path: str, is_dir: bool) -> str:
    if is_dir:
        return ""
    return ntpath.splitext(path)[1].lower().lstrip(".")


def _render_reason(template: object, *, size: int, age_days: int, path: str) -> str:
    """渲染词典里的 reason 模板（占位符 ``{size}`` / ``{age}`` / ``{path}`` / ``{name}``）。"""
    if not template:
        return ""
    text = str(template)
    values = {
        "size": winapi.human_size(int(size)),
        "age": int(age_days),
        "path": path,
        "name": ntpath.basename(path),
    }
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):
        for key, value in values.items():
            text = text.replace("{" + key + "}", str(value))
        return text


def _group_for(category_key: str, advice: str) -> str:
    """把词典 category 映射到界面分组（§3.2 的 7 组）。"""
    if category_key in GROUP_LABELS:
        return category_key
    if advice == "keep":
        return "keep"
    return "large-misc"


def _category_result(
    category: dict, *, size: int, age_days: int, path: str
) -> tuple[str, str, str, str, str]:
    key = str(category.get("key") or "").strip()
    label = str(category.get("label") or key)
    advice = str(category.get("advice") or "review")
    risk = str(category.get("risk") or "caution")
    reason = _render_reason(category.get("reason"), size=size, age_days=age_days, path=path)
    if not reason:
        reason = _default_reason_for(label, advice)
    return key, advice, risk, reason, _group_for(key, advice)


def classify(
    path: str, *, size: int, mtime: float, is_dir: bool, age_days: int
) -> tuple[str, str, str, str, str]:
    """对单个路径分类，返回 ``(category, advice, risk, reason, group)``。

    优先级（v2 契约 §3.1）：``never_touch`` → categories 顺序 → ``age_rules`` →
    ``large-misc``。``match="all"`` 要求扩展名与路径模式**同时**命中，
    ``match="any"`` 命中其一即可（缺省 ``all``）。
    """
    data = heuristics()
    # 1) never_touch 最高优先级：命中即 keep/danger
    if _path_matches(path, data.get("never_touch") or []):
        return (
            "keep",
            "keep",
            "danger",
            "系统/程序目录，请勿手动删除",
            "keep",
        )

    ext = _file_ext(path, is_dir)
    fallback_category: dict | None = None

    # 2) categories 顺序（large-misc / 空规则类别留作最终兜底）
    for category in data.get("categories") or []:
        if not isinstance(category, dict):
            continue
        key = str(category.get("key") or "").strip()
        if not key:
            continue
        exts = {str(item).lower().lstrip(".") for item in (category.get("exts") or [])}
        globs = list(category.get("path_globs") or [])
        if key == "large-misc" or (not exts and not globs):
            if fallback_category is None:
                fallback_category = category
            continue
        ext_hit = bool(ext) and ext in exts
        path_hit = _path_matches(path, globs)
        mode = str(category.get("match") or "all").lower()
        if mode == "any":
            matched = ext_hit or path_hit
        else:  # all：扩展名且路径都要命中（只有一边有规则时另一边视为通过）
            matched = (ext_hit if exts else True) and (path_hit if globs else True)
        if matched:
            return _category_result(category, size=size, age_days=age_days, path=path)

    # 3) age_rules：按 min_size 从大到小取第一条命中
    age_rules = sorted(
        (rule for rule in (data.get("age_rules") or []) if isinstance(rule, dict)),
        key=lambda rule: int(rule.get("min_size", 0)),
        reverse=True,
    )
    for rule in age_rules:
        if size >= int(rule.get("min_size", 0)) and age_days >= int(rule.get("min_age_days", 0)):
            reason = _render_reason(
                rule.get("reason"), size=size, age_days=age_days, path=path
            )
            return (
                "large-misc",
                str(rule.get("advice") or "review"),
                "caution",
                reason or "大文件，请确认是否保留/迁移",
                "large-misc",
            )

    # 4) 兜底 large-misc
    if fallback_category is not None:
        return _category_result(fallback_category, size=size, age_days=age_days, path=path)
    return (
        "large-misc",
        "review",
        "caution",
        "未识别的大文件，请确认后处理",
        "large-misc",
    )


def _default_reason_for(label: str, advice: str) -> str:
    if advice == "safe_delete":
        return f"{label}：通常可安全清理（建议走回收站）"
    if advice == "keep":
        return f"{label}：建议保留，不要删除"
    return f"{label}：请确认后再决定"


# ---------------------------------------------------------------------------
# 动作授权矩阵（ARCHITECTURE-V2 §3.4，删除/迁移前必须过的第一道闸）
# ---------------------------------------------------------------------------


#: 永不触碰的系统目录段（任意盘）
_SPECIAL_SEGMENTS: frozenset[str] = frozenset(
    {"$recycle.bin", "system volume information", "recovery", "efi", "boot"}
)
#: 系统盘根目录下的系统文件
_ROOT_SYSTEM_FILES: frozenset[str] = frozenset(
    {"hiberfil.sys", "pagefile.sys", "swapfile.sys", "dumpstack.log.tmp"}
)


def _is_root_level_system_file(path: str) -> bool:
    """是否系统盘根目录下的 hiberfil/pagefile/swapfile/DumpStack.log* 文件。

    这些文件在「系统占用卡片」里专门展示，不应混入大文件决策表。
    """
    expanded = safety.expand(path) if path else ""
    if not expanded:
        return False
    key = ntpath.normcase(expanded)
    name = ntpath.basename(key)
    if name not in _ROOT_SYSTEM_FILES and not name.startswith("dumpstack.log"):
        return False
    drive, _tail = ntpath.splitdrive(key)
    return ntpath.dirname(key).rstrip("\\") == drive


def _policy_roots() -> dict[str, str]:
    """返回授权矩阵需要的区域根目录（规范化小写 key）。"""
    system_drive = (os.environ.get("SystemDrive") or "C:").rstrip("\\")
    if not system_drive.endswith(":"):
        system_drive = "C:"
    root = system_drive + "\\"
    return {
        "system_drive": system_drive.upper(),
        "windir": ntpath.normcase(safety.expand(os.environ.get("WINDIR") or root + "Windows")),
        "program_files": ntpath.normcase(
            safety.expand(os.environ.get("ProgramFiles") or root + "Program Files")
        ),
        "program_files_x86": ntpath.normcase(
            safety.expand(os.environ.get("ProgramFiles(x86)") or root + "Program Files (x86)")
        ),
        "programdata": ntpath.normcase(
            safety.expand(os.environ.get("ProgramData") or root + "ProgramData")
        ),
        "profile": ntpath.normcase(safety.expand(os.environ.get("USERPROFILE") or "")),
        "users": ntpath.normcase(ntpath.join(root, "Users")),
        "root": ntpath.normcase(root),
    }


def _is_under_key(key: str, root_key: str) -> bool:
    if not key or not root_key:
        return False
    if key == root_key:
        return True
    prefix = root_key if root_key.endswith("\\") else root_key + "\\"
    return key.startswith(prefix)


def action_policy(item: ReviewItem) -> ActionPolicy:
    """按授权矩阵给出删除/迁移许可（§3.4）。

    - ``C:\\Windows`` / ``Program Files*``：不可删、不可移；
    - ``C:\\ProgramData``：仅 ``category=="installer"`` 且路径含 ``\\Downloads\\``
      可删（不可移），其余 keep；
    - 当前用户目录（含 AppData，``AppData\\Local\\Packages`` 除外）：可删可移；
    - 非系统盘：可删可移；
    - ``$Recycle.Bin`` / ``System Volume Information`` / ``Recovery`` / ``EFI`` / ``Boot``：
      永不触碰；系统盘根目录的 hiberfil/pagefile/swapfile 同样拒绝；
    - 系统盘下其它未知区域：保守拒绝，提示手动处理。
    """
    expanded = safety.expand(item.path) if item.path else ""
    if not expanded or not ntpath.isabs(expanded):
        return ActionPolicy(False, False, "路径非法，已拒绝")
    key = ntpath.normcase(expanded)
    roots = _policy_roots()

    segments = [segment for segment in key.split("\\") if segment]
    if any(segment in _SPECIAL_SEGMENTS for segment in segments):
        return ActionPolicy(
            False, False, "系统受保护目录（回收站/还原点/恢复/引导），永不触碰"
        )
    if ntpath.dirname(key) == roots["root"] and ntpath.basename(key) in _ROOT_SYSTEM_FILES:
        return ActionPolicy(False, False, "系统文件，请用设置 / DISM / powercfg 处理，不要直接删除")

    if _is_under_key(key, roots["windir"]):
        return ActionPolicy(False, False, "系统目录，请勿手动删除（只展示/请用管理员命令处理）")

    if _is_under_key(key, roots["program_files"]) or _is_under_key(
        key, roots["program_files_x86"]
    ):
        return ActionPolicy(False, False, "程序安装目录，请在程序设置或卸载中处理")

    if _is_under_key(key, roots["programdata"]):
        has_downloads = "\\downloads\\" in key
        if item.category == "installer" and has_downloads:
            return ActionPolicy(
                True, False, "ProgramData 下的残留安装包仅支持删除（回收站），不支持迁移"
            )
        return ActionPolicy(False, False, "系统程序数据目录，请勿手删（请用程序自带卸载/设置处理）")

    profile = roots["profile"]
    under_user_tree = bool(profile) and _is_under_key(key, profile)
    if not under_user_tree and _is_under_key(key, roots["users"]):
        under_user_tree = True
    if under_user_tree:
        if "\\appdata\\local\\packages\\" in key:
            return ActionPolicy(
                False, False, "应用包数据（AppData\\Local\\Packages），请用系统设置/卸载处理"
            )
        return ActionPolicy(True, True, "")

    if _drive_of(expanded) and _drive_of(expanded) != roots["system_drive"]:
        return ActionPolicy(True, True, "")

    return ActionPolicy(False, False, "该区域不在自动清理/迁移授权范围内，请手动处理")


def _downgrade_by_policy(item: ReviewItem) -> bool:
    """命中完全拒绝的区域时把条目降级为 ``keep/danger``，返回是否降级。"""
    policy = action_policy(item)
    if policy.can_delete or policy.can_move:
        return False
    item.advice = "keep"
    item.risk = "danger"
    item.group = "keep"
    item.reason = policy.deny_reason or item.reason or "该区域不允许自动删除/迁移"
    return True


def _policy_for_path(path: str) -> ActionPolicy:
    """按路径快速求授权（不依赖索引；用于 delete_many / move_many）。"""
    expanded = safety.expand(path) if path else ""
    if not expanded or not ntpath.isabs(expanded):
        return ActionPolicy(False, False, "路径非法，已拒绝")
    try:
        is_dir = os.path.isdir(expanded)
    except OSError:
        is_dir = False
    category, _advice, _risk, _reason, _group = classify(
        expanded, size=0, mtime=0.0, is_dir=is_dir, age_days=0
    )
    item = ReviewItem(
        path=expanded,
        name=ntpath.basename(expanded),
        is_dir=is_dir,
        size=0,
        mtime=0.0,
        age_days=0,
        ext=_file_ext(expanded, is_dir),
        category=category,
        advice="review",
        reason="",
        risk="caution",
        group=_group,
    )
    return action_policy(item)


# ---------------------------------------------------------------------------
# 大文件收集 / 分组
# ---------------------------------------------------------------------------


def _make_review_item(node: space.SpaceNode, *, now: float) -> ReviewItem:
    age_days = max(0, int((now - node.mtime) // 86400)) if node.mtime else 0
    ext = _file_ext(node.path, node.is_dir)
    category, advice, risk, reason, group = classify(
        node.path, size=node.size, mtime=node.mtime, is_dir=node.is_dir, age_days=age_days
    )
    return ReviewItem(
        path=node.path,
        name=node.name,
        is_dir=node.is_dir,
        size=int(node.size),
        mtime=float(node.mtime),
        age_days=age_days,
        ext=ext,
        category=category,
        advice=advice,
        reason=reason,
        risk=risk,
        group=group,
    )


def collect_review_items(
    index: space.SpaceIndex,
    *,
    min_size: int = 100 * 1024 * 1024,
    limit: int = 800,
) -> list[ReviewItem]:
    """从空间索引中收集 ≥ ``min_size`` 的大文件/可迁移目录，按大小降序。

    安全约束（§3.4）：

    - **剪枝嵌套**：父目录一旦纳入，其全部子项跳过——保证大小不重复计、
      删除/迁移顺序不冲突；
    - 每条都过 :func:`action_policy`，被完全拒绝的区域降级为
      ``keep/danger`` 并在 ``reason`` 里说明（如程序安装目录）。

    注意：索引只物化 ≥ ``build_index(min_child_size)`` 的节点，若要收集更小的文件，
    请以更小的 ``min_child_size`` 重建索引。
    """
    min_size = max(0, int(min_size))
    limit = max(1, int(limit))
    now = time.time()
    items: list[ReviewItem] = []

    def visit(node: space.SpaceNode) -> None:
        for child in node.children:
            if child.is_dir:
                if child.size < min_size:
                    visit(child)
                    continue
                item = _make_review_item(child, now=now)
                original_group = item.group
                _downgrade_by_policy(item)
                # 原本属于「可迁移目录」组（含被授权矩阵降级为 keep 的）才作为
                # 目录级条目展示；一旦纳入就剪掉子树，避免父子重复计数。
                if original_group in DIR_REVIEW_GROUPS:
                    items.append(item)
                    continue
                visit(child)
            elif child.size >= min_size:
                if _is_root_level_system_file(child.path):
                    # 根级系统文件由「系统占用卡片」展示，不进大文件决策表
                    continue
                item = _make_review_item(child, now=now)
                _downgrade_by_policy(item)
                items.append(item)

    visit(index.root_node)
    items.sort(key=lambda item: item.size, reverse=True)
    return items[:limit]


def group_review_items(items: list[ReviewItem]) -> list[ReviewGroup]:
    """按 §3.2 的顺序分组，并计算组级建议与合计。"""
    buckets: dict[str, list[ReviewItem]] = {}
    for item in items:
        buckets.setdefault(item.group, []).append(item)

    groups: list[ReviewGroup] = []
    for key in GROUP_ORDER:
        bucket = buckets.pop(key, None)
        if not bucket:
            continue
        label, advice = GROUP_LABELS[key]
        bucket.sort(key=lambda item: item.size, reverse=True)
        groups.append(
            ReviewGroup(
                key=key,
                label=label,
                items=bucket,
                total_bytes=sum(item.size for item in bucket),
                advice=advice,
            )
        )
    # 词典里出现未知分组时兜底归入 large-misc
    for key, bucket in buckets.items():
        label, advice = GROUP_LABELS["large-misc"]
        bucket.sort(key=lambda item: item.size, reverse=True)
        groups.append(
            ReviewGroup(
                key=key,
                label=label,
                items=bucket,
                total_bytes=sum(item.size for item in bucket),
                advice=advice,
            )
        )
    return groups


# ---------------------------------------------------------------------------
# 迁移目标探测
# ---------------------------------------------------------------------------


def _try_write_dir(path: str) -> bool:
    """目录存在（必要时创建）且真实可写 → True。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".qf_write_probe_{os.getpid()}_{int(time.time() * 1000) % 100000}")
        with open(probe, "wb") as handle:
            handle.write(b"qf")
        os.remove(probe)
        return True
    except OSError:
        return False


def _is_writable_dir(path: str) -> bool:
    """已存在目录是否可写：只认「真实建临时文件再删除」，不用 os.access 猜测。"""
    if not path or not os.path.isdir(path):
        return False
    return _try_write_dir(path)


def _enumerate_fixed_drives() -> list[str]:
    drives: list[str] = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = f"{letter}:"
        try:
            if not os.path.exists(drive + "\\"):
                continue
        except OSError:
            continue
        if winapi.drive_type(drive) == winapi.DRIVE_FIXED:
            drives.append(drive)
    return drives


def _list_target_subdirs(root: str) -> list[str]:
    """列出目标盘根目录下可考虑的用户目录（跳过系统目录/reparse）。"""
    candidates: list[str] = []
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return candidates
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            stat = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if getattr(stat, "st_file_attributes", 0) & 0x400:  # reparse
            continue
        name = entry.name.lower()
        if name.startswith("$") or name in _SKIP_TARGET_DIRS:
            continue
        if safety.is_protected(entry.path):
            continue
        candidates.append(entry.path)
    return candidates


def _preferred_rank(path: str) -> int:
    name = ntpath.basename(path).lower()
    for index, keyword in enumerate(_PREFERRED_DIR_KEYWORDS):
        if keyword in name:
            return index
    return len(_PREFERRED_DIR_KEYWORDS)


def suggest_targets(exclude_drive: str = "C:") -> list[TargetOption]:
    """推荐可写的迁移目标（默认排除 C: 与源盘）。

    先尝试 ``<盘根>\\QingFengMoved``；若根目录不可写（常见于 D:），
    就在该盘用户目录里找第一个可写目录，用 ``<该目录>\\QingFengMoved``，
    并在 ``note`` 里说明原因。返回按「可写优先、剩余空间大优先」排序。
    """
    exclude = str(exclude_drive or "").strip().upper()
    if exclude and not exclude.endswith(":"):
        exclude += ":"
    options: list[TargetOption] = []
    for drive in _enumerate_fixed_drives():
        if drive.upper() == exclude:
            continue
        try:
            free, _total = winapi.drive_usage(drive)
        except Exception:  # noqa: BLE001
            free = 0
        root = drive + "\\"
        root_candidate = ntpath.join(root, "QingFengMoved")
        if _try_write_dir(root_candidate):
            options.append(
                TargetOption(drive=drive, path=root_candidate, free_bytes=int(free), writable=True, note="")
            )
            continue
        # 根目录不可写：在该盘用户目录里找第一个可写的
        subdirs = _list_target_subdirs(root)
        subdirs.sort(key=lambda path: (_preferred_rank(path), path.lower()))
        chosen = ""
        for subdir in subdirs[:_MAX_TARGET_PROBE]:
            candidate = ntpath.join(subdir, "QingFengMoved")
            if _try_write_dir(candidate):
                chosen = candidate
                break
        if chosen:
            options.append(
                TargetOption(
                    drive=drive,
                    path=chosen,
                    free_bytes=int(free),
                    writable=True,
                    note=f"{drive} 根目录不允许普通用户写入，已改用 {chosen}",
                )
            )
        else:
            options.append(
                TargetOption(
                    drive=drive,
                    path=root_candidate,
                    free_bytes=int(free),
                    writable=False,
                    note="根目录与常见用户目录均不可写，需要管理员权限",
                )
            )
    options.sort(key=lambda option: (option.writable, option.free_bytes), reverse=True)
    return options


# ---------------------------------------------------------------------------
# 跨盘迁移
# ---------------------------------------------------------------------------


def _drive_of(path: str) -> str:
    drive, _tail = ntpath.splitdrive(path or "")
    return drive.upper()


def _is_fixed_drive(drive: str) -> bool:
    return winapi.drive_type(drive) == winapi.DRIVE_FIXED


def _dest_free_bytes(drive: str) -> int:
    try:
        return int(winapi.drive_usage(drive)[0])
    except Exception:  # noqa: BLE001
        return 0


def _should_cancel(cancel: CancelCallback | None) -> bool:
    if cancel is None:
        return False
    try:
        return bool(cancel())
    except Exception:  # noqa: BLE001
        return False


def _notify(progress: ProgressCallback | None, path: str, done: int, total: int) -> None:
    if progress is None:
        return
    try:
        progress(path, done, total)
    except Exception:  # noqa: BLE001 - UI 回调异常不能中断迁移
        return


def _unique_dest(dest: str) -> str:
    """目标已存在时追加 `` (2)`` 等后缀，绝不覆盖。"""
    if not os.path.lexists(dest):
        return dest
    base, ext = ntpath.splitext(dest)
    for index in range(2, 1000):
        candidate = f"{base} ({index}){ext}"
        if not os.path.lexists(candidate):
            return candidate
    return f"{dest} ({int(time.time())})"


def _dest_path_for(source: str, dest_root: str) -> str:
    drive, tail = ntpath.splitdrive(source)
    letter = (drive[:1] or "X").upper()
    relative = tail.lstrip("\\")
    return ntpath.join(dest_root, f"{letter}_mirror", relative)


def _is_reparse(path: str) -> bool:
    try:
        stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return True
    if getattr(stat, "st_file_attributes", 0) & 0x400:
        return True
    try:
        return os.path.islink(path)
    except OSError:
        return True


def _copy_file_cancellable(
    source: str,
    dest: str,
    cancel: CancelCallback | None,
) -> tuple[bool, bool, str]:
    """复制单个文件；返回 ``(成功, 是否取消, 错误信息)``。取消/失败会清理半成品。"""
    try:
        source_stat = os.stat(source, follow_symlinks=False)
    except OSError as exc:
        return False, False, f"无法读取源文件: {exc}"
    try:
        os.makedirs(ntpath.dirname(dest), exist_ok=True)
        with open(source, "rb") as src, open(dest, "xb") as dst:
            while True:
                if _should_cancel(cancel):
                    raise _CopyCancelled
                chunk = src.read(_COPY_BUFFER)
                if not chunk:
                    break
                dst.write(chunk)
        if os.path.getsize(dest) != int(source_stat.st_size):
            raise OSError("复制后大小不一致")
        try:
            os.utime(dest, (source_stat.st_atime, source_stat.st_mtime))
        except OSError:
            pass
        return True, False, ""
    except _CopyCancelled:
        _safe_remove_file(dest)
        return False, True, "已取消（源文件未动）"
    except OSError as exc:
        _safe_remove_file(dest)
        return False, False, f"复制失败: {exc}"


class _CopyCancelled(Exception):
    pass


def _safe_remove_file(path: str) -> None:
    try:
        if os.path.isfile(path) and not os.path.islink(path):
            os.remove(path)
    except OSError:
        pass


def _safe_remove_tree(path: str) -> None:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _copy_tree_cancellable(
    source: str,
    dest: str,
    cancel: CancelCallback | None,
) -> tuple[bool, bool, str]:
    """递归复制目录；遇到 reparse point 视为失败（不跟随、不删源）。"""
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as exc:
        return False, False, f"无法创建目标目录: {exc}"
    stack: list[tuple[str, str]] = [(source, dest)]
    total_source = 0
    total_copied = 0
    try:
        while stack:
            src_dir, dst_dir = stack.pop()
            try:
                with os.scandir(src_dir) as it:
                    entries = list(it)
            except OSError as exc:
                raise OSError(f"无法读取源目录 {src_dir}: {exc}") from exc
            for entry in entries:
                if _should_cancel(cancel):
                    raise _CopyCancelled
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise OSError(f"无法读取 {entry.path}: {exc}") from exc
                if getattr(entry_stat, "st_file_attributes", 0) & 0x400:
                    raise OSError(f"源目录包含符号链接/挂载点，已跳过: {entry.path}")
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False
                if is_dir:
                    os.makedirs(ntpath.join(dst_dir, entry.name), exist_ok=True)
                    stack.append((entry.path, ntpath.join(dst_dir, entry.name)))
                    continue
                os.makedirs(dst_dir, exist_ok=True)
                target = ntpath.join(dst_dir, entry.name)
                with open(entry.path, "rb") as src, open(target, "xb") as dst:
                    while True:
                        if _should_cancel(cancel):
                            raise _CopyCancelled
                        chunk = src.read(_COPY_BUFFER)
                        if not chunk:
                            break
                        dst.write(chunk)
                copied = int(entry_stat.st_size)
                if os.path.getsize(target) != copied:
                    raise OSError(f"复制后大小不一致: {entry.path}")
                try:
                    os.utime(target, (entry_stat.st_atime, entry_stat.st_mtime))
                except OSError:
                    pass
                total_source += copied
                total_copied += copied
        return True, False, ""
    except _CopyCancelled:
        _safe_remove_tree(dest)
        return False, True, "已取消（源文件未动）"
    except OSError as exc:
        _safe_remove_tree(dest)
        return False, False, f"复制失败: {exc}"


def _source_size(path: str) -> tuple[int, bool]:
    """返回 ``(字节数, 是否为目录)``；包含 reparse 的子项不计。"""
    try:
        stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return 0, False
    if not os.path.isdir(path):
        return int(stat.st_size), False
    total = 0
    stack = [path]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if getattr(entry_stat, "st_file_attributes", 0) & 0x400:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                else:
                    total += int(entry_stat.st_size)
            except OSError:
                continue
    return total, True


def move_many(
    paths: list[str],
    dest_root: str,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
) -> MoveResult:
    """把 ``paths`` 迁移到 ``dest_root``（跨固定磁盘）。

    安全承诺：**复制并校验成功后才删除源文件**；取消/失败保留源文件；
    目标重名自动加 `` (2)`` 后缀，绝不覆盖。
    """
    started = time.time()
    dest_root = safety.expand(dest_root) if dest_root else ""
    result = MoveResult(dest_root=dest_root)
    total = max(1, len(paths or []))
    done = 0

    def finish() -> MoveResult:
        result.duration = time.time() - started
        _notify(progress, dest_root, total, total)
        return result

    if not dest_root or not ntpath.isabs(dest_root):
        result.failed.append((str(dest_root), "目标目录无效"))
        return finish()
    if not os.path.isdir(dest_root):
        result.failed.append((dest_root, "目标目录不存在，请先选择或创建可写目录"))
        return finish()
    if not _is_writable_dir(dest_root):
        result.failed.append((dest_root, "目标目录不可写（权限不足）"))
        return finish()
    dest_drive = _drive_of(dest_root)
    if not _is_fixed_drive(dest_drive):
        result.failed.append((dest_root, "目标盘不是固定磁盘（不支持网络盘/光驱）"))
        return finish()

    # 1) 逐项校验 + 估算空间
    candidates: list[tuple[str, bool, int]] = []
    total_bytes = 0
    for raw_path in paths or []:
        source = safety.expand(raw_path) if raw_path else ""
        if not source:
            result.skipped.append((str(raw_path), "空路径"))
            continue
        # 第一道闸：授权矩阵（Windows/Program Files/ProgramData 一律不能迁出）
        policy = _policy_for_path(source)
        if not policy.can_move:
            result.skipped.append(
                (source, policy.deny_reason or "该区域不允许自动迁移，请手动处理")
            )
            continue
        # 第二道闸：safety 保护路径
        try:
            safety.assert_deletable(source)
        except safety.UnsafePathError as exc:
            result.skipped.append((source, str(exc)))
            continue
        if _is_reparse(source):
            result.skipped.append((source, "源路径是符号链接/reparse point，已跳过"))
            continue
        source_drive = _drive_of(source)
        if source_drive == dest_drive:
            result.skipped.append((source, "同盘迁移不释放空间，暂不支持（需选择不同磁盘）"))
            continue
        if safety.within_root(dest_root, source):
            result.skipped.append((source, "目标目录位于源目录内部，已拒绝（防递归复制）"))
            continue
        size, is_dir = _source_size(source)
        candidates.append((source, is_dir, size))
        total_bytes += size

    if candidates:
        free_bytes = _dest_free_bytes(dest_drive)
        if free_bytes < total_bytes + _SPACE_MARGIN:
            for source, _is_dir, _size in candidates:
                result.failed.append((source, "目标盘空间不足，已全部取消（源文件未动）"))
            return finish()

    # 2) 逐个复制 → 校验 → 删源
    for source, is_dir, size in candidates:
        if _should_cancel(cancel):
            result.skipped.append((source, "已取消（源文件未动）"))
            done += 1
            continue
        dest = _unique_dest(_dest_path_for(source, dest_root))
        try:
            if is_dir:
                ok, was_cancelled, error = _copy_tree_cancellable(source, dest, cancel)
            else:
                ok, was_cancelled, error = _copy_file_cancellable(source, dest, cancel)
        except Exception as exc:  # noqa: BLE001 - 单文件异常不能中断整批迁移
            _safe_remove_tree(dest)
            ok, was_cancelled, error = False, False, f"迁移异常: {exc.__class__.__name__}: {exc}"
        done += 1
        if was_cancelled:
            result.skipped.append((source, error))
            _notify(progress, source, done, total)
            continue
        if not ok:
            result.failed.append((source, error))
            _notify(progress, source, done, total)
            continue
        # 复制成功 → 删除源；删源失败则回滚目标，保证"源文件仍在"
        try:
            if is_dir:
                shutil.rmtree(source)
            else:
                os.remove(source)
        except OSError as exc:
            _safe_remove_tree(dest)
            result.failed.append((source, f"复制成功但删除源失败（已回滚目标，源仍保留）: {exc}"))
            _notify(progress, source, done, total)
            continue
        result.moved += 1
        result.moved_bytes += size
        _notify(progress, source, done, total)
    return finish()


def delete_many(
    paths: list[str],
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
) -> models.CleanResult:
    """删除选中的大文件/目录（空间透视页的唯一删除入口，默认回收站）。

    逐条过两道闸：:func:`action_policy`（授权矩阵，§3.4）+ ``safety.assert_deletable``；
    未通过的路径记入 ``CleanResult.failed``（含拒绝原因），绝不静默删除。
    实际删除复用 v1 :func:`qingfeng.core.cleaner.clean`（回收站、可撤销、可取消）。
    """
    started = time.time()
    total = max(1, len(paths or []))
    denied: list[tuple[str, str]] = []
    scan_items: list[models.ScanItem] = []
    drives: set[str] = set()

    for index, raw_path in enumerate(paths or []):
        if _should_cancel(cancel):
            break
        _notify(progress, str(raw_path), index, total)
        source = safety.expand(raw_path) if raw_path else ""
        if not source:
            denied.append((str(raw_path), "空路径"))
            continue
        policy = _policy_for_path(source)
        if not policy.can_delete:
            denied.append((source, policy.deny_reason or "该区域不允许自动删除"))
            continue
        try:
            safety.assert_deletable(source)
        except safety.UnsafePathError as exc:
            denied.append((source, str(exc)))
            continue
        try:
            source_stat = os.stat(source, follow_symlinks=False)
        except FileNotFoundError:
            denied.append((source, "路径不存在"))
            continue
        except OSError as exc:
            denied.append((source, f"无法访问: {exc}"))
            continue
        if _is_reparse(source):
            denied.append((source, "符号链接/reparse point，已跳过"))
            continue
        is_dir = os.path.isdir(source)
        size, _is_dir_flag = _source_size(source)
        scan_items.append(
            models.ScanItem(
                rule_id="space-delete",
                path=source,
                is_dir=is_dir,
                size_bytes=size,
                file_count=0 if is_dir else 1,
                mtime=float(source_stat.st_mtime or 0.0),
                risk="caution",
            )
        )
        drive = _drive_of(source)
        if drive:
            drives.add(drive)

    cleaned = models.CleanResult(recycled=True)
    if scan_items:
        rule = models.Rule(
            id="space-delete",
            name="空间透视删除",
            category="系统",
            description="空间透视页选中的大文件/目录（回收站）",
            action="delete",
            paths=[f"{drive}\\" for drive in sorted(drives)],
            globs=[],
            follow_dirs=True,
        )
        cleaned = cleaner.clean(
            [models.RuleScanResult(rule=rule, items=scan_items)],
            mode="recycle",
            progress=progress,
            cancel=cancel,
        )
    cleaned.failed = denied + cleaned.failed
    cleaned.duration = time.time() - started
    _notify(progress, "空间透视删除", total, total)
    return cleaned


__all__ = [
    "ActionPolicy",
    "DIR_REVIEW_GROUPS",
    "GROUP_LABELS",
    "GROUP_ORDER",
    "MoveResult",
    "ReviewGroup",
    "ReviewItem",
    "TargetOption",
    "action_policy",
    "classify",
    "collect_review_items",
    "delete_many",
    "group_review_items",
    "heuristics",
    "move_many",
    "suggest_targets",
]
