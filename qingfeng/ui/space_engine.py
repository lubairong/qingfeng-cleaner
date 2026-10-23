"""清风清理 v2 — 空间透视的引擎适配层。

优先使用 core-engine 的 `qingfeng.core.space` / `qingfeng.core.decide`；
未就绪（导入失败/接口不全/调用异常）时自动退化为 `space_mock`，保证界面可独立运行。
"""

from __future__ import annotations

import inspect
import logging
import traceback
from typing import Any

from . import mock_engine, space_mock

log = logging.getLogger(__name__)


class SpaceEngine:
    """空间索引 + 决策 + 迁移的统一门面。"""

    def __init__(self, engine=None) -> None:
        self.engine = engine or mock_engine  # 提供 drive_usage/human_size 的 v1 适配层
        self.mode = "mock"
        self.reason = ""
        self._space = None
        self._decide = None
        self._detect()

    # -- 检测 ----------------------------------------------------------
    def _detect(self) -> None:
        self.mode = "mock"
        self.reason = ""
        self._space = None
        self._decide = None
        try:
            from qingfeng.core import decide as decide_mod
            from qingfeng.core import space as space_mod
        except Exception as exc:
            self.reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            log.info("空间透视使用演示数据（core.space/decide 未就绪：%s）", self.reason)
            return
        missing = [
            name for name, mod in (
                ("build_index", space_mod), ("probe_system_space", space_mod),
                ("collect_review_items", decide_mod), ("group_review_items", decide_mod),
                ("suggest_targets", decide_mod), ("move_many", decide_mod),
            )
            if not callable(getattr(mod, name, None))
        ]
        if missing:
            self.reason = "接口不完整：" + ", ".join(missing)
            log.info("空间透视使用演示数据（%s）", self.reason)
            return
        self._space = space_mod
        self._decide = decide_mod
        self.mode = "native"

    def refresh(self) -> None:
        """重新探测（core 可能在运行期间就绪）。"""
        self._detect()

    @property
    def is_native(self) -> bool:
        return self.mode == "native"

    @property
    def status_text(self) -> str:
        return "空间索引：系统引擎" if self.is_native else "空间索引：演示数据"

    @property
    def status_detail(self) -> str:
        if self.is_native:
            return "已连接 qingfeng.core.space / decide"
        return f"core.space/decide 未就绪，使用演示数据（{self.reason or '未检测到'}）"

    def _degrade(self, exc: Exception) -> None:
        self.mode = "mock"
        self.reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        log.warning("空间引擎调用失败，退化到演示数据：%s", exc)
        traceback.print_exc()

    # -- 系统信息 ------------------------------------------------------
    def human_size(self, n: float) -> str:
        try:
            return self.engine.human_size(int(n))
        except Exception:
            return space_mock._human_size(n) if hasattr(space_mock, "_human_size") else f"{int(n)} B"

    def drive_usage(self, drive: str = "C:") -> tuple[int, int]:
        try:
            return self.engine.drive_usage(drive)
        except Exception:
            return space_mock._drive_usage(drive)

    def is_admin(self) -> bool:
        try:
            return bool(self.engine.is_admin())
        except Exception:
            return False

    # -- 索引 ----------------------------------------------------------
    def load_cached_index(self, max_age_hours: float = 24):
        if self.is_native and self._space is not None:
            try:
                return self._space.load_cached_index(max_age_hours)
            except Exception as exc:
                self._degrade(exc)
        try:
            return space_mock.load_cached_index(max_age_hours)
        except Exception:
            return None

    def save_index(self, index) -> None:
        try:
            if self.is_native and self._space is not None:
                self._space.save_index(index)
            else:
                space_mock.save_index(index)
        except Exception:
            log.info("索引缓存写入失败（忽略）", exc_info=True)

    def cache_path(self):
        try:
            if self.is_native and self._space is not None:
                return self._space.cache_path()
        except Exception:
            pass
        return space_mock.cache_path()

    def build_index(self, root: str = "C:\\", *, progress=None, cancel=None):
        if self.is_native and self._space is not None:
            try:
                return self._space.build_index(root, progress=progress, cancel=cancel)
            except Exception as exc:
                self._degrade(exc)
        return space_mock.build_index(root, progress=progress, cancel=cancel)

    def probe_system_space(self, index=None) -> dict:
        if self.is_native and self._space is not None:
            try:
                fn = self._space.probe_system_space
                if index is not None:
                    try:
                        params = inspect.signature(fn).parameters
                    except (TypeError, ValueError):
                        params = {}
                    if "index" in params:
                        return fn(index=index)
                return fn()
            except Exception as exc:
                self._degrade(exc)
        return space_mock.probe_system_space()

    # -- 决策 ----------------------------------------------------------
    def collect_review_items(self, index, *, min_size=None, limit=800):
        if self.is_native and self._decide is not None:
            try:
                kwargs = {}
                if min_size is not None:
                    kwargs["min_size"] = min_size
                return self._decide.collect_review_items(index, limit=limit, **kwargs)
            except Exception as exc:
                self._degrade(exc)
        return space_mock.collect_review_items(index, min_size=min_size or 100 * 1024 * 1024,
                                               limit=limit)

    def group_review_items(self, items):
        if self.is_native and self._decide is not None:
            try:
                return self._decide.group_review_items(items)
            except Exception as exc:
                self._degrade(exc)
        return space_mock.group_review_items(items)

    def heuristics(self) -> dict:
        if self.is_native and self._decide is not None:
            try:
                return self._decide.heuristics()
            except Exception:
                pass
        return space_mock.heuristics()

    def action_policy(self, item):
        """动作闸门：直接读 core.decide.action_policy；未就绪时用保守策略。"""
        if self.is_native and self._decide is not None:
            fn = getattr(self._decide, "action_policy", None)
            if callable(fn):
                try:
                    policy = fn(item)
                    if hasattr(policy, "can_delete"):
                        return policy
                    if isinstance(policy, dict):
                        return space_mock.ActionPolicy(
                            can_delete=bool(policy.get("can_delete", False)),
                            can_move=bool(policy.get("can_move", False)),
                            deny_reason=str(policy.get("deny_reason") or ""),
                        )
                    return space_mock.ActionPolicy(
                        can_delete=False, can_move=False,
                        deny_reason="action_policy 返回了未知结构，暂不可操作",
                    )
                except Exception:
                    log.warning("action_policy 调用失败，使用保守策略",
                                exc_info=True)
        return space_mock.action_policy(item)

    # -- 迁移 ----------------------------------------------------------
    @staticmethod
    def _target_to_dict(target) -> dict:
        """统一 core 的 TargetOption dataclass / dict 为界面 dict。"""
        if isinstance(target, dict):
            data = dict(target)
        else:
            data = {
                "drive": getattr(target, "drive", ""),
                "path": getattr(target, "path", ""),
                "free": getattr(target, "free_bytes",
                                getattr(target, "free", 0)),
                "total": getattr(target, "total_bytes",
                                 getattr(target, "total", 0)),
                "writable": getattr(target, "writable", False),
                "note": getattr(target, "note", ""),
            }
        data["drive"] = str(data.get("drive") or "")
        data["path"] = str(data.get("path") or "")
        data["free"] = int(data.get("free") or data.get("free_bytes") or 0)
        data["total"] = int(data.get("total") or data.get("total_bytes") or 0)
        data["writable"] = bool(data.get("writable"))
        data["note"] = str(data.get("note") or "")
        return data

    def suggest_targets(self) -> list[dict]:
        if self.is_native and self._decide is not None:
            try:
                targets = self._decide.suggest_targets()
                if targets:
                    return [self._target_to_dict(t) for t in targets]
            except Exception as exc:
                self._degrade(exc)
        return [self._target_to_dict(t) for t in space_mock.suggest_targets()]

    def move_many(self, paths, dest_root, *, progress=None, cancel=None):
        if self.is_native and self._decide is not None:
            try:
                return self._decide.move_many(
                    paths, dest_root, progress=progress, cancel=cancel
                )
            except Exception as exc:
                self._degrade(exc)
        return space_mock.move_many(paths, dest_root, progress=progress, cancel=cancel)
