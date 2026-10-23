"""清风清理 —— 只读扫描器。

职责：把 ``Rule`` 展开成具体的 ``ScanItem`` 列表，供 GUI 展示与 cleaner 清理。

关键约束：

- **绝对只读**：只调用 ``os.scandir`` / ``os.stat`` / ``glob``，不创建、不修改、不删除；
- 使用 ``os.scandir`` 一次遍历拿到 ``DirEntry``，尽量复用其缓存的 stat 信息；
- 遇到 ``PermissionError``（如深入 ``C:\\Windows`` 内部目录）只记录到
  ``RuleScanResult.error``，绝不向上抛异常，保证整盘扫描不中断；
- 跳过符号链接 / junction / 其他 reparse point，防止目录环和越界扫描；
- 单条规则异常被隔离，不影响其他规则；
- 支持 ``cancel`` 回调与 ``max_depth`` 深度限制，进度通过 ``progress`` 回调上报。

聚合策略：当规则的 ``globs`` 为空（或等价于 ``*``）时，扫描器会把
「整棵子树都合格」的顶层子目录聚合成**一个** ``ScanItem(is_dir=True)``，
使清理阶段只需一次 Shell 操作即可删除大量文件；否则（配置了具体 glob）按文件粒度上报。
"""

from __future__ import annotations

import fnmatch
import ntpath
import os
import time
from typing import Callable

from . import rules as rules_mod
from . import safety, winapi
from .models import Rule, RuleScanResult, ScanItem, ScanReport

#: 进度回调：``progress(名称, 已完成, 总数)``，会从工作线程调用
ProgressCallback = Callable[[str, int, int], None]
#: 取消回调：返回 ``True`` 表示尽快停止
CancelCallback = Callable[[], bool]

#: 每扫描这么多目录项检查一次取消标记
_CANCEL_CHECK_INTERVAL = 256
#: FILE_ATTRIBUTE_REPARSE_POINT
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
#: 等价于「整棵目录」的 glob 集合
_WHOLE_TREE_PATTERNS = frozenset({"*", "**", "**/*", "*.*", "*\\*", "**\\*"})


class _Cancelled(Exception):
    """内部取消信号，用于快速解开递归。"""


# ---------------------------------------------------------------------------
# glob / 路径匹配
# ---------------------------------------------------------------------------


def _lower(text: str) -> str:
    return text.replace("/", "\\").lower()


def _matches_globs(patterns: list[str], name: str, rel: str) -> bool:
    """判断文件名/相对路径是否匹配 glob 列表（大小写不敏感）。

    - 不含路径分隔符的模式（如 ``*.log``）匹配文件名；
    - 含分隔符的模式（如 ``sub\\*.tmp``）匹配相对规则根的路径。
    """
    if not patterns:
        return False
    name_l = _lower(name)
    rel_l = _lower(rel)
    for pattern in patterns:
        pat = _lower(pattern)
        if "\\" in pat:
            if fnmatch.fnmatchcase(rel_l, pat):
                return True
        elif fnmatch.fnmatchcase(name_l, pat):
            return True
    return False


def _is_excluded(rule: Rule, name: str, rel: str) -> bool:
    """命中 ``exclude_globs`` / ``protected_globs`` 即跳过。"""
    return _matches_globs(rule.exclude_globs, name, rel) or _matches_globs(
        rule.protected_globs, name, rel
    )


def _is_whole_tree(rule: Rule) -> bool:
    """规则是否把整棵目录当作删除单元（globs 为空或全为 ``*``）。"""
    if not rule.globs:
        return True
    return all(pattern.strip() in _WHOLE_TREE_PATTERNS for pattern in rule.globs)


# ---------------------------------------------------------------------------
# 扫描状态
# ---------------------------------------------------------------------------


class _ScanState:
    """单条规则扫描过程中的可变状态。"""

    def __init__(self, rule: Rule, *, cancel: CancelCallback | None, max_depth: int, now: float):
        self.rule = rule
        self.cancel = cancel
        self.max_depth = max(1, int(max_depth))
        self.now = now
        self.whole_tree = _is_whole_tree(rule)
        self.cutoff = now - rule.min_age_days * 86400.0
        self.items: list[ScanItem] = []
        self.errors: list[str] = []
        self.error_count = 0
        self.cancelled = False
        self._checks = 0

    # -- 取消 ---------------------------------------------------------
    def check_cancel(self) -> None:
        self._checks += 1
        if self._checks % _CANCEL_CHECK_INTERVAL != 0:
            return
        if _should_cancel(self.cancel):
            self.cancelled = True
            raise _Cancelled

    # -- 错误 ---------------------------------------------------------
    def note_error(self, exc: BaseException, path: str = "") -> None:
        """记录一处可恢复错误（权限不足等），最多保留 3 个样例并截断长度。"""
        self.error_count += 1
        if len(self.errors) < 3:
            if isinstance(exc, PermissionError) and path:
                detail = path
            else:
                detail = str(exc) or path
            self.errors.append(f"{type(exc).__name__}: {_shorten(detail, 110)}")

    def error_message(self) -> str | None:
        if not self.error_count:
            return None
        samples = "；".join(self.errors)
        return f"部分路径读取失败（{self.error_count} 处，例：{samples}）"

    # -- 判断 ---------------------------------------------------------
    def file_eligible(self, mtime: float) -> bool:
        """``min_age_days`` 过滤：只保留修改时间早于 N 天前的文件。"""
        if self.rule.min_age_days <= 0:
            return True
        return mtime < self.cutoff


def _should_cancel(cancel: CancelCallback | None) -> bool:
    if cancel is None:
        return False
    try:
        return bool(cancel())
    except Exception:  # noqa: BLE001 - 回调异常绝不能影响扫描
        return False


def _notify(
    progress: ProgressCallback | None, name: str, done: int, total: int
) -> None:
    if progress is None:
        return
    try:
        progress(name, done, total)
    except Exception:  # noqa: BLE001 - UI 回调异常不能中断扫描
        return


def _shorten(text: str, limit: int) -> str:
    """截断过长的错误文本，避免 GUI/日志被单条错误刷屏。"""
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)] + "…"


def _is_reparse_point(st: os.stat_result) -> bool:
    """是否为符号链接 / junction / 其他 reparse point（一律不跟随）。"""
    attributes = getattr(st, "st_file_attributes", 0) or 0
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


# ---------------------------------------------------------------------------
# 递归扫描
# ---------------------------------------------------------------------------


def _file_item(rule: Rule, path: str, st: os.stat_result) -> ScanItem:
    return ScanItem(
        rule_id=rule.id,
        path=path,
        is_dir=False,
        size_bytes=int(getattr(st, "st_size", 0)),
        file_count=1,
        mtime=float(getattr(st, "st_mtime", 0.0)),
        risk=rule.risk,
    )


def _dir_item(rule: Rule, path: str, size: int, count: int, mtime: float) -> ScanItem:
    return ScanItem(
        rule_id=rule.id,
        path=path,
        is_dir=True,
        size_bytes=int(size),
        file_count=int(count),
        mtime=float(mtime),
        risk=rule.risk,
    )


def _walk_entries(
    state: _ScanState,
    directory: str,
    depth: int,
    rel_prefix: str,
) -> tuple[list[ScanItem], int, int, bool]:
    """递归扫描 ``directory`` 内的条目。

    返回 ``(items, 合格总大小, 合格文件数, 整棵子树是否都合格)``。
    ``depth`` 是 ``directory`` 相对规则根的层级（规则根为 0，其直接子项为 1）。
    """
    items: list[ScanItem] = []
    size = 0
    count = 0
    fully = True
    rule = state.rule

    try:
        with os.scandir(directory) as it:
            entries = list(it)
    except FileNotFoundError:
        return items, size, count, False
    except PermissionError as exc:
        state.note_error(exc, directory)
        return items, size, count, False
    except OSError as exc:
        state.note_error(exc, directory)
        return items, size, count, False

    for entry in entries:
        state.check_cancel()
        name = entry.name
        rel = f"{rel_prefix}{name}"

        if _is_excluded(rule, name, rel):
            fully = False
            continue

        try:
            st = entry.stat(follow_symlinks=False)
        except OSError as exc:
            state.note_error(exc, entry.path)
            fully = False
            continue

        # 绝不跟随 reparse point（symlink/junction，含跨卷 reparse），避免目录环与越界
        try:
            if entry.is_symlink():
                fully = False
                continue
        except OSError:  # pragma: no cover - 极少见
            fully = False
            continue
        if _is_reparse_point(st):
            fully = False
            continue

        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:  # pragma: no cover - 极少见
            fully = False
            continue

        entry_depth = depth + 1

        if is_dir:
            if not rule.follow_dirs:
                fully = False
                continue
            if entry_depth >= state.max_depth:
                fully = False
                continue
            sub_items, sub_size, sub_count, sub_fully = _walk_entries(
                state, entry.path, entry_depth, f"{rel}\\"
            )
            size += sub_size
            count += sub_count
            if state.whole_tree and sub_fully:
                # 整棵子树都合格：聚合成一个目录条目，清理时一次 Shell 操作搞定
                items.append(_dir_item(rule, entry.path, sub_size, sub_count, st.st_mtime))
            else:
                items.extend(sub_items)
                fully = False
            continue

        # 普通文件
        if not state.whole_tree and not _matches_globs(rule.globs, name, rel):
            fully = False
            continue
        if not state.file_eligible(st.st_mtime):
            fully = False
            continue
        items.append(_file_item(rule, entry.path, st))
        size += int(getattr(st, "st_size", 0))
        count += 1

    return items, size, count, fully


def _scan_rule(state: _ScanState, roots: list[str]) -> bool:
    """扫描单条规则展开后的全部具体路径；结果累积到 ``state.items``。

    返回是否至少命中一个真实存在的扫描根（用于生成 skipped_reason）。
    """
    rule = state.rule
    scanned_any = False
    for root in roots:
        state.check_cancel()
        try:
            is_dir = os.path.isdir(root)
            is_file = os.path.isfile(root)
        except OSError:
            is_dir = is_file = False
        if is_dir:
            # 运行期兜底：规则根若受保护则整条路径跳过（rules.validate 已拦截，双保险）
            if safety.is_protected(root):
                continue
            scanned_any = True
            try:
                items, _size, _count, _fully = _walk_entries(state, root, 0, "")
            except _Cancelled:
                raise
            except OSError as exc:  # pragma: no cover - 上级已隔离
                state.note_error(exc, root)
                continue
            state.items.extend(items)
        elif is_file:
            scanned_any = True
            name = ntpath.basename(root)
            rel = name
            if _is_excluded(rule, name, rel):
                continue
            try:
                st = os.stat(root, follow_symlinks=False)
            except OSError as exc:
                state.note_error(exc, root)
                continue
            if _is_reparse_point(st):
                continue
            if not state.whole_tree and not _matches_globs(rule.globs, name, rel):
                continue
            if not state.file_eligible(st.st_mtime):
                continue
            state.items.append(_file_item(rule, root, st))
    return scanned_any


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def scan(
    rules: list[Rule],
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
    max_depth: int = 24,
) -> ScanReport:
    """扫描规则列表，返回 :class:`ScanReport`（只读，不删除任何文件）。

    :param rules: 待扫描规则（GUI 可先按勾选/默认状态过滤）
    :param progress: ``progress(rule_name, done, total)``，会从工作线程调用
    :param cancel: 返回 ``True`` 时尽快结束，``report.cancelled`` 置 True
    :param max_depth: 最大递归深度（相对规则根，默认 24）
    """
    started_at = time.time()
    if max_depth is None or int(max_depth) < 1:
        max_depth = 24
    max_depth = int(max_depth)

    total = len(rules)
    results: list[RuleScanResult] = []
    cancelled = False

    for index, rule in enumerate(rules):
        _notify(progress, rule.name, index, total)
        if not cancelled and _should_cancel(cancel):
            cancelled = True
        if cancelled:
            results.append(RuleScanResult(rule=rule, skipped_reason="扫描已取消"))
            continue

        result = RuleScanResult(rule=rule)

        if rule.action == "report":
            result.skipped_reason = rule.report_note or "只报告，不执行扫描/删除"
            results.append(result)
            _notify(progress, rule.name, index + 1, total)
            continue
        if rule.action == "empty_recycle_bin":
            result.skipped_reason = rule.report_note or "通过系统回收站操作执行"
            results.append(result)
            _notify(progress, rule.name, index + 1, total)
            continue

        # 非管理员：不扫描 requires_admin 规则，明确提示「需要管理员权限」
        # （避免把 PermissionError 噪音伪装成"扫描出错"）
        if rule.requires_admin and not winapi.is_admin():
            result.skipped_reason = "需要管理员权限：点「以管理员身份重启」后可清理"
            result.requires_elevation = True
            results.append(result)
            _notify(progress, rule.name, index + 1, total)
            continue

        # §4.1：路径展开（环境变量 + 单个 * 段）由 rules.expand_paths 完成
        roots = rules_mod.expand_paths(rule)
        if not roots:
            result.skipped_reason = "未找到可扫描路径（对应软件可能未安装或未产生缓存）"
            results.append(result)
            _notify(progress, rule.name, index + 1, total)
            continue

        state = _ScanState(rule, cancel=cancel, max_depth=max_depth, now=time.time())
        scanned_any = False
        try:
            scanned_any = _scan_rule(state, roots)
        except _Cancelled:
            cancelled = True
        except Exception as exc:  # noqa: BLE001 - 单条规则异常必须被隔离
            result.error = f"扫描规则 {rule.id} 失败: {type(exc).__name__}: {exc}"
        finally:
            result.items = state.items
            error_message = state.error_message()
            if error_message:
                result.error = (
                    f"{result.error}；{error_message}" if result.error else error_message
                )
            if state.cancelled:
                cancelled = True

        if not scanned_any and not result.items and not cancelled:
            result.skipped_reason = "未找到可扫描路径（对应软件可能未安装或未产生缓存）"

        results.append(result)
        _notify(progress, rule.name, index + 1, total)
        if cancelled:
            # 剩余规则补空结果（带 skipped_reason），方便 GUI 按规则对齐展示
            for rest in rules[index + 1 :]:
                results.append(RuleScanResult(rule=rest, skipped_reason="扫描已取消"))
            break

    try:
        system_drive = os.environ.get("SystemDrive", "C:")
        drive_free, drive_total = winapi.drive_usage(system_drive)
    except Exception:  # noqa: BLE001 - 磁盘信息失败不影响扫描结果
        drive_free, drive_total = 0, 0

    return ScanReport(
        results=results,
        started_at=started_at,
        duration=time.time() - started_at,
        drive_free=drive_free,
        drive_total=drive_total,
        cancelled=cancelled,
    )


__all__ = ["CancelCallback", "ProgressCallback", "scan"]
