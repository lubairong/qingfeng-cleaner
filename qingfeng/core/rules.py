"""清风清理 —— 规则加载与校验。

规则文件位于 ``qingfeng/data/rules/*.json``，一个分类一个文件，格式见
``docs/ARCHITECTURE.md`` §4。本模块保证：

- 任何非法规则（缺字段、id 重复、分类越界、路径指向受保护目录……）都抛
  :class:`RuleError`，绝不静默加载半成品规则；
- ``risk == "advanced"`` 的规则强制 ``default_enabled = False``（§0.4 默认禁用）；
- ``action == "delete"`` 的规则路径必须是绝对路径且不得指向受保护目录（安全兜底，
  与 ``safety.py`` 运行时校验双保险）。
"""

from __future__ import annotations

import glob as _glob
import json
import ntpath
import os
import re
from pathlib import Path
from typing import Any

from . import safety
from .models import ACTION_VALUES, CATEGORIES, RISK_VALUES, Rule

#: kebab-case 规则 id
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
#: 路径中出现的通配符字符（§4.1 只允许单个 ``*`` 段）
_WILDCARD_CHARS = "*?["


class RuleError(ValueError):
    """规则不合法（JSON 结构错误、字段缺失、取值越界等）。"""


def _expand_path(raw_path: str) -> str:
    return safety.expand(raw_path)


def _require_text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuleError(f"字段 {key!r} 必须是非空字符串（规则: {raw.get('id', '<未知>')}）")
    return value.strip()


def _optional_text(raw: dict[str, Any], key: str, default: str = "") -> str:
    value = raw.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise RuleError(f"字段 {key!r} 必须是字符串（规则: {raw.get('id', '<未知>')}）")
    return value.strip()


def _bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise RuleError(f"字段 {key!r} 必须是布尔值（规则: {raw.get('id', '<未知>')}）")
    return value


def _int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuleError(f"字段 {key!r} 必须是整数（规则: {raw.get('id', '<未知>')}）")
    if value < 0:
        raise RuleError(f"字段 {key!r} 不能为负数（规则: {raw.get('id', '<未知>')}）")
    return value


def _str_list(raw: dict[str, Any], key: str) -> list[str]:
    value = raw.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise RuleError(f"字段 {key!r} 必须是字符串数组（规则: {raw.get('id', '<未知>')}）")
    result: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise RuleError(f"字段 {key!r} 含非字符串/空字符串（规则: {raw.get('id', '<未知>')}）")
        result.append(entry.strip())
    return result


def _val(raw: str, allowed: tuple[str, ...], field_name: str, rule_id: str) -> str:
    if raw not in allowed:
        raise RuleError(f"字段 {field_name!r} 取值非法: {raw!r}，允许: {list(allowed)}（规则: {rule_id}）")
    return raw


# ---------------------------------------------------------------------------
# 路径展开（§4.1：允许单个 ``*`` 段，表示展开该层下的每一个子目录）
# ---------------------------------------------------------------------------


def _split_path_segments(expanded: str) -> tuple[str, list[str]]:
    """把绝对路径拆成 ``(盘符, 路径段列表)``。"""
    drive, tail = ntpath.splitdrive(expanded)
    segments = [segment for segment in tail.split("\\") if segment]
    return drive, segments


def _has_wildcard(text: str) -> bool:
    return any(ch in text for ch in _WILDCARD_CHARS)


def _validate_wildcards(rule_id: str, expanded: str) -> None:
    """校验通配符约束：只允许单个 ``*`` 段，不允许 ``**``，不允许出现在盘符/根段。"""
    if "**" in expanded:
        raise RuleError(f"规则 {rule_id} 的路径不支持 ** 递归通配: {expanded!r}")
    _, segments = _split_path_segments(expanded)
    wildcard_indexes = [
        index for index, segment in enumerate(segments) if _has_wildcard(segment)
    ]
    if not wildcard_indexes:
        return
    if len(wildcard_indexes) > 1:
        raise RuleError(f"规则 {rule_id} 的路径只允许一个 * 段: {expanded!r}")
    index = wildcard_indexes[0]
    segment = segments[index]
    if segment != "*":
        # 允许 * 独占一段，例如 User Data\*\Cache；不允许 *Cache*、*.log 这类写法
        raise RuleError(f"规则 {rule_id} 的路径通配符必须独占一个路径段: {expanded!r}")
    if index == 0:
        raise RuleError(f"规则 {rule_id} 的通配符不允许出现在盘符/根段: {expanded!r}")


def _wildcard_is_last_segment(expanded: str) -> bool:
    """``*`` 是否位于路径最后一段（决定展开时是否只收目录）。"""
    _, segments = _split_path_segments(expanded)
    indexes = [index for index, segment in enumerate(segments) if _has_wildcard(segment)]
    return bool(indexes) and indexes[0] == len(segments) - 1


def _expand_one_path(raw_path: str) -> list[str]:
    """展开单条路径：环境变量 + 单个 ``*`` 段，返回存在的具体路径。

    ``*`` 段的语义（§4.1 + 红队 M2）：

    - ``*`` 在**最后一段**（如 ``Profiles\\*``）：表示"该层下每一个子目录"，只收目录；
    - ``*`` 在**中间**（如 ``User Data\\*\\History``、``Profiles\\*\\formhistory.sqlite``）：
      表示"该层下每个子目录里的目标项"，文件与目录都收（否则隐私规则永远 0 命中）。
    """
    expanded = safety.expand(raw_path)
    if not expanded:
        return []
    if not _has_wildcard(expanded):
        return [expanded]
    try:
        matches = sorted(_glob.glob(expanded))
    except (OSError, ValueError):
        return []
    dirs_only = _wildcard_is_last_segment(expanded)
    resolved: list[str] = []
    for match in matches:
        if dirs_only:
            try:
                if not os.path.isdir(match):
                    continue
            except OSError:
                continue
        resolved.append(safety.expand(match))
    return resolved


def _wildcard_specimen(expanded: str, placeholder: str = "__qingfeng_wildcard__") -> str:
    """把 ``*`` 段替换成占位目录名，得到一个"替身样本路径"。

    用于通配路径展开不到任何真实目录时的安全检查：对替身样本调用
    ``safety.is_protected`` 即可判断"若真存在该层子目录，其路径是否受保护"。
    """
    drive, segments = _split_path_segments(expanded)
    replaced = [placeholder if _has_wildcard(segment) else segment for segment in segments]
    base = f"{drive}\\" if drive else ""
    return ntpath.join(base, *replaced) if replaced else base


def expand_paths(rule: Rule) -> list[str]:
    """展开规则的全部路径（环境变量 + 单个 ``*`` 段），返回**已存在的具体路径**。

    - 无通配符的路径原样返回（存在性由 scanner 再判断）；
    - ``*`` 在最后一段时只展开该层的**子目录**（§4.1：表示"该层下每一个子目录"）；
    - ``*`` 在中间段时，展开该层每个子目录下的目标项（文件或目录都收），
      例如 ``User Data\\*\\History``；
    - 展开不到任何路径时返回 ``[]``，由 scanner 写入 ``skipped_reason``。
    """
    result: list[str] = []
    seen: set[str] = set()
    for raw_path in rule.paths:
        for expanded in _expand_one_path(raw_path):
            key = ntpath.normcase(expanded)
            if key in seen:
                continue
            seen.add(key)
            result.append(expanded)
    return result


def _validate_rule_paths(rule: Rule) -> None:
    """校验规则路径：绝对路径 + 通配符规则；删除类规则还要过 ``safety`` 兜底。"""
    for raw_path in rule.paths:
        expanded = _expand_path(raw_path)
        if not expanded or not os.path.isabs(expanded):
            raise RuleError(f"规则 {rule.id} 的路径无法展开为绝对路径: {raw_path!r}")
        _validate_wildcards(rule.id, expanded)
        if rule.action != "delete":
            continue
        if _has_wildcard(expanded):
            matches = _expand_one_path(expanded)
            if matches:
                for match in matches:
                    if safety.is_protected(match):
                        raise RuleError(
                            f"规则 {rule.id} 的通配路径匹配到受保护目录，禁止加载: {match}"
                        )
            else:
                # 展开不到真实目录时，用替身样本路径判定（占位段不参与真实删除）
                specimen = _wildcard_specimen(expanded)
                if specimen and safety.is_protected(specimen):
                    raise RuleError(
                        f"规则 {rule.id} 的通配路径样本受保护，禁止加载: {raw_path!r}"
                    )
            continue
        if safety.is_protected(expanded):
            raise RuleError(f"规则 {rule.id} 指向受保护路径，禁止加载: {raw_path!r}")


def validate_rule(raw: dict) -> Rule:
    """校验一条原始规则 dict 并填充默认值，返回 :class:`Rule`。

    非法时抛 :class:`RuleError`。
    """
    if not isinstance(raw, dict):
        raise RuleError("规则必须是 JSON 对象")

    rule_id = _require_text(raw, "id")
    if not _ID_RE.match(rule_id):
        raise RuleError(f"规则 id 必须为 kebab-case（小写字母/数字/连字符）: {rule_id!r}")

    name = _require_text(raw, "name")
    category = _require_text(raw, "category")
    category = _val(category, CATEGORIES, "category", rule_id)
    description = _require_text(raw, "description")
    action = _val(_require_text(raw, "action"), ACTION_VALUES, "action", rule_id)
    risk = _val(_optional_text(raw, "risk", "safe") or "safe", RISK_VALUES, "risk", rule_id)

    rule = Rule(
        id=rule_id,
        name=name,
        category=category,
        description=description,
        action=action,  # type: ignore[arg-type]
        paths=_str_list(raw, "paths"),
        globs=_str_list(raw, "globs"),
        exclude_globs=_str_list(raw, "exclude_globs"),
        risk=risk,  # type: ignore[arg-type]
        requires_admin=_bool(raw, "requires_admin", False),
        default_enabled=_bool(raw, "default_enabled", True),
        min_age_days=_int(raw, "min_age_days", 0),
        follow_dirs=_bool(raw, "follow_dirs", True),
        protected_globs=_str_list(raw, "protected_globs"),
        report_note=_optional_text(raw, "report_note"),
        source=_optional_text(raw, "source"),
    )

    # advanced 规则强制默认禁用（§0.4）
    if rule.risk == "advanced":
        rule.default_enabled = False

    if rule.action == "delete":
        if not rule.paths:
            raise RuleError(f"删除类规则 {rule.id} 必须至少声明一个 paths")
    elif rule.action == "report":
        if not rule.report_note:
            raise RuleError(f"只报告规则 {rule.id} 必须提供 report_note")

    if rule.paths:
        _validate_rule_paths(rule)

    return rule


def _default_rules_dir() -> Path:
    # 延迟导入，避免与 qingfeng/__init__ 循环依赖；main.py 可能修改 qingfeng.RULES_DIR
    import qingfeng

    return Path(qingfeng.RULES_DIR)


def load_rules(rules_dir: Path | None = None) -> list[Rule]:
    """加载规则目录下全部 ``*.json``，返回按分类排序的规则列表。

    非法规则 / JSON 解析失败 / id 重复 → 抛 :class:`RuleError`。
    """
    directory = Path(rules_dir) if rules_dir is not None else _default_rules_dir()
    if not directory.is_dir():
        raise RuleError(f"规则目录不存在: {directory}")

    rules: list[Rule] = []
    seen_ids: set[str] = set()
    seen_files = 0

    for json_file in sorted(directory.glob("*.json")):
        try:
            text = json_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuleError(f"无法读取规则文件 {json_file.name}: {exc}") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuleError(f"规则文件 {json_file.name} JSON 解析失败: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuleError(f"规则文件 {json_file.name} 顶层必须是 JSON 对象")

        version = payload.get("version", 1)
        if version != 1:
            raise RuleError(f"规则文件 {json_file.name} 版本不支持: {version!r}（仅支持 1）")

        file_category = payload.get("category")
        if file_category is not None:
            file_category = _val(str(file_category), CATEGORIES, "category", json_file.name)

        entries = payload.get("rules", [])
        if not isinstance(entries, list):
            raise RuleError(f"规则文件 {json_file.name} 的 rules 字段必须是数组")
        seen_files += 1

        for index, raw in enumerate(entries):
            if not isinstance(raw, dict):
                raise RuleError(f"规则文件 {json_file.name} 第 {index + 1} 条规则必须是 JSON 对象")
            if "category" not in raw and file_category is not None:
                raw = {**raw, "category": file_category}
            rule = validate_rule(raw)
            if file_category is not None and rule.category != file_category:
                raise RuleError(
                    f"规则 {rule.id} 分类 {rule.category!r} 与文件 {json_file.name} 分类 "
                    f"{file_category!r} 不一致"
                )
            if rule.id in seen_ids:
                raise RuleError(f"规则 id 重复: {rule.id}")
            seen_ids.add(rule.id)
            rules.append(rule)

    if seen_files == 0:
        raise RuleError(f"规则目录中没有 *.json 文件: {directory}")

    rules.sort(key=lambda r: CATEGORIES.index(r.category))  # 稳定排序，保留文件内顺序
    return rules


__all__ = ["RuleError", "expand_paths", "load_rules", "validate_rule"]
