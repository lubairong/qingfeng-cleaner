"""清风清理 —— 清理执行器。

职责：对 ``RuleScanResult`` 中的 ``ScanItem`` 执行删除。

安全设计（与 ``safety.py`` 双保险）：

1. 逐个条目先过 :func:`safety.assert_deletable`，受保护路径直接记入 ``failed``，**绝不删除**；
2. 再校验条目确实位于该规则 ``paths`` 展开范围内（防止伪造 ``ScanItem`` 越权删除）；
3. 默认走回收站（``winapi.recycle_paths``，可撤销）；永久删除仅在显式
   ``mode="delete"`` 时执行；
4. 回收站删除失败默认**不降级**为永久删除；只有显式设置
   :data:`ALLOW_PERMANENT_FALLBACK` 为 ``True``（GUI 设置项）才会降级；
5. 单条规则、单个路径的失败只记录不中断；``requires_admin`` 规则在非管理员下跳过；
6. 支持 ``cancel`` 回调尽快停止。

``ScanItem.is_dir=True`` 表示整棵目录聚合条目；``is_dir=False`` 表示单个文件。
"""

from __future__ import annotations

import fnmatch
import ntpath
import os
import shutil
import stat
import time
from typing import Callable, Literal

from . import safety, winapi
from .models import CleanResult, Rule, RuleScanResult, ScanItem

#: 进度回调：``progress(名称, 已完成, 总数)``，从工作线程调用
ProgressCallback = Callable[[str, int, int], None]
#: 取消回调：返回 ``True`` 时尽快停止
CancelCallback = Callable[[], bool]

#: 回收站删除失败时是否允许降级为永久删除（默认关闭，建议由 GUI 设置项显式开启）
ALLOW_PERMANENT_FALLBACK: bool = False

#: 单次 Shell 回收站操作批量提交的路径数
_RECYCLE_BATCH_SIZE = 64
#: 每处理这么多条目检查一次取消
_CANCEL_CHECK_INTERVAL = 8


def set_permanent_fallback(enabled: bool) -> None:
    """设置回收站失败时是否降级为永久删除（GUI 设置项调用）。"""
    global ALLOW_PERMANENT_FALLBACK
    ALLOW_PERMANENT_FALLBACK = bool(enabled)


def _should_cancel(cancel: CancelCallback | None) -> bool:
    if cancel is None:
        return False
    try:
        return bool(cancel())
    except Exception:  # noqa: BLE001 - 回调异常不能影响清理
        return False


def _notify(progress: ProgressCallback | None, name: str, done: int, total: int) -> None:
    if progress is None:
        return
    try:
        progress(name, done, total)
    except Exception:  # noqa: BLE001 - UI 回调异常不能中断清理
        return


# ---------------------------------------------------------------------------
# 范围校验
# ---------------------------------------------------------------------------


def _item_in_rule_scope(rule: Rule, path: str) -> bool:
    """条目是否位于规则声明的路径范围内（支持单个 ``*`` 段通配）。"""
    target = ntpath.normcase(safety.expand(path))
    if not target:
        return False
    for raw_path in rule.paths:
        expanded = safety.expand(raw_path)
        if not expanded:
            continue
        if any(ch in expanded for ch in "*?["):
            pattern = ntpath.normcase(expanded)
            # 通配符目录之下的任意后代
            if fnmatch.fnmatchcase(target, pattern + "\\*"):
                return True
            if fnmatch.fnmatchcase(target, pattern):
                return True
        elif safety.within_root(path, expanded):
            return True
    return False


# ---------------------------------------------------------------------------
# 删除原语
# ---------------------------------------------------------------------------


def _still_exists(path: str) -> bool:
    try:
        return os.path.lexists(path)
    except OSError:
        return False


def _remove_file(path: str) -> tuple[bool, str]:
    """删除单个文件（只读属性时先改权限重试一次）。"""
    try:
        os.remove(path)
        return True, ""
    except FileNotFoundError:
        return True, ""  # 已经不存在，视为成功
    except PermissionError:
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            os.remove(path)
            return True, ""
        except OSError as exc:
            return False, f"删除失败（只读/被占用）: {exc}"
    except OSError as exc:
        return False, f"删除失败: {exc}"


def _rmtree(path: str) -> tuple[bool, str]:
    """递归删除目录树（遇到只读文件时自动改权限重试）。"""

    def _onexc(function, target, excinfo):  # pragma: no cover - 依赖具体错误场景
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            function(target)
        except OSError:
            raise excinfo[1] if isinstance(excinfo, tuple) else excinfo

    try:
        shutil.rmtree(path, onexc=_onexc)
        return True, ""
    except FileNotFoundError:
        return True, ""
    except OSError as exc:
        return False, f"删除失败: {exc}"


def _permanent_delete(path: str) -> tuple[bool, str]:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            return _rmtree(path)
        return _remove_file(path)
    except OSError as exc:  # pragma: no cover - 兜底
        return False, f"删除失败: {exc}"


def _account_success(out: CleanResult, rule: Rule, item: ScanItem) -> None:
    freed = max(0, int(item.size_bytes))
    out.freed_bytes += freed
    out.per_rule[rule.id] = out.per_rule.get(rule.id, 0) + freed
    if item.is_dir:
        out.deleted_dirs += 1
        out.deleted_files += max(0, int(item.file_count))
    else:
        out.deleted_files += max(1, int(item.file_count))


# ---------------------------------------------------------------------------
# 条目筛选
# ---------------------------------------------------------------------------


def _collect_deletable(
    out: CleanResult,
    rule: Rule,
    result: RuleScanResult,
    *,
    done_ref: list[int],
    total: int,
    cancel: CancelCallback | None,
    progress: ProgressCallback | None,
) -> tuple[list[ScanItem], bool]:
    """逐条做安全校验，返回可删除条目列表与是否被取消。"""
    candidates: list[ScanItem] = []
    now = time.time()
    cutoff = now - rule.min_age_days * 86400.0 if rule.min_age_days > 0 else None

    for item in result.items:
        done_ref[0] += 1
        if done_ref[0] % _CANCEL_CHECK_INTERVAL == 0:
            if _should_cancel(cancel):
                _notify(progress, rule.name, done_ref[0], total)
                return candidates, True

        path = item.path
        # 1) 硬安全底线
        try:
            safety.assert_deletable(path)
        except safety.UnsafePathError as exc:
            out.failed.append((path, str(exc)))
            continue
        # 2) 必须落在规则声明的范围内
        if not _item_in_rule_scope(rule, path):
            out.failed.append((path, "路径不在规则声明范围内，已拒绝删除"))
            continue
        # 3) 存在性检查（扫描后可能已被其他程序删除）
        try:
            st = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            out.failed.append((path, f"无法访问: {exc}"))
            continue
        # 4) min_age_days 二次校验（扫描到清理之间文件可能被更新）
        if cutoff is not None and not item.is_dir and st.st_mtime >= cutoff:
            continue

        candidates.append(item)
    return candidates, False


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def clean(
    results: list[RuleScanResult],
    *,
    mode: Literal["recycle", "delete"] = "recycle",
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
) -> CleanResult:
    """执行清理，返回 :class:`CleanResult`。

    :param results: 扫描结果（``action=="report"`` 的规则会被忽略）
    :param mode: ``"recycle"`` 进回收站（默认，可撤销）/ ``"delete"`` 永久删除
    :param progress: ``progress(rule_name, done, total)``，从工作线程调用
    :param cancel: 返回 ``True`` 时尽快停止后续删除
    """
    if mode not in ("recycle", "delete"):
        raise ValueError(f"mode 只能是 'recycle' 或 'delete'，收到: {mode!r}")

    global ALLOW_PERMANENT_FALLBACK

    started_at = time.time()
    out = CleanResult(recycled=(mode == "recycle"))
    total_items = sum(len(result.items) for result in results)
    done_ref = [0]
    cancelled = False
    used_permanent_fallback = False

    for result in results:
        if cancelled or _should_cancel(cancel):
            cancelled = True
            break
        rule = result.rule
        _notify(progress, rule.name, done_ref[0], total_items)

        if rule.action == "report":
            continue
        if rule.action == "empty_recycle_bin":
            try:
                ok = winapi.empty_recycle_bin()
            except Exception:  # noqa: BLE001
                ok = False
            if not ok:
                out.failed.append(("<回收站>", "清空回收站失败：可能已为空、被占用或权限不足"))
            done_ref[0] += len(result.items)
            _notify(progress, rule.name, done_ref[0], total_items)
            continue

        # requires_admin 规则在非管理员下只扫描不删除（§0.4）
        if rule.requires_admin and not winapi.is_admin():
            done_ref[0] += len(result.items)
            out.skipped_admin.append(rule.name)
            out.failed.append((rule.name, "需要管理员权限，已跳过清理（请以管理员身份运行）"))
            _notify(progress, rule.name, done_ref[0], total_items)
            continue

        candidates, cancelled = _collect_deletable(
            out,
            rule,
            result,
            done_ref=done_ref,
            total=total_items,
            cancel=cancel,
            progress=progress,
        )
        if cancelled:
            break

        if mode == "recycle":
            for start in range(0, len(candidates), _RECYCLE_BATCH_SIZE):
                if _should_cancel(cancel):
                    cancelled = True
                    break
                chunk = candidates[start : start + _RECYCLE_BATCH_SIZE]
                failure_map: dict[str, str] = {}
                try:
                    failure_map = dict(winapi.recycle_paths([item.path for item in chunk]))
                except Exception as exc:  # noqa: BLE001 - Shell 调用异常不中断整体清理
                    failure_map = {item.path: f"回收站删除失败: {exc}" for item in chunk}

                for item in chunk:
                    error = failure_map.get(item.path)
                    if error is None and _still_exists(item.path):
                        error = "删除后路径仍存在（可能被占用）"
                    if error is None:
                        _account_success(out, rule, item)
                        continue
                    # 回收站失败：默认不降级，除非显式开启
                    if ALLOW_PERMANENT_FALLBACK:
                        ok, perm_error = _permanent_delete(item.path)
                        if ok:
                            used_permanent_fallback = True
                            _account_success(out, rule, item)
                            continue
                        error = f"{error}；永久删除也失败: {perm_error}"
                    out.failed.append((item.path, error))
                _notify(progress, rule.name, done_ref[0], total_items)
        else:
            for item in candidates:
                if _should_cancel(cancel):
                    cancelled = True
                    break
                ok, error = _permanent_delete(item.path)
                if ok:
                    _account_success(out, rule, item)
                else:
                    out.failed.append((item.path, error))
                _notify(progress, rule.name, done_ref[0], total_items)
            if cancelled:
                break

    if used_permanent_fallback:
        out.recycled = False
    out.duration = time.time() - started_at
    return out


def execute_rule_action(
    rule: Rule,
    result: RuleScanResult,
    *,
    mode: str = "recycle",
) -> CleanResult:
    """对单条规则的扫描结果执行动作（供 GUI「单条清理」按钮使用）。"""
    if mode not in ("recycle", "delete"):
        raise ValueError(f"mode 只能是 'recycle' 或 'delete'，收到: {mode!r}")
    target = result
    if result.rule is not rule:
        target = RuleScanResult(
            rule=rule,
            items=result.items,
            error=result.error,
            skipped_reason=result.skipped_reason,
            requires_elevation=result.requires_elevation,
        )
    return clean([target], mode=mode)  # type: ignore[arg-type]


__all__ = [
    "ALLOW_PERMANENT_FALLBACK",
    "CancelCallback",
    "ProgressCallback",
    "clean",
    "execute_rule_action",
    "set_permanent_fallback",
]
