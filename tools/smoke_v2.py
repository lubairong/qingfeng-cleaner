"""v2 冒烟脚本：空间索引 / 决策链路（供 tools/verify.ps1 与 CI 复用，零第三方依赖）。

两个步骤分开跑，便于 verify.ps1 各自计时、也避免重复 60 秒索引：

    # 1) 只读扫描 C: 建索引，断言可访问字节 > 0、耗时可接受，并把索引json存到工作目录
    python tools/smoke_v2.py --step index --save .tmp/space_index.json --max-seconds 120

    # 2) 用上一步的索引跑决策链路：大文件收集 / 分组 / 授权矩阵 / 迁移目标探测
    python tools/smoke_v2.py --step decide --load .tmp/space_index.json

退出码：0 = PASS；1 = FAIL（打印原因）。
输出为单行 JSON（便于脚本/CI 解析），失败时额外把 traceback 打到 stderr。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def step_index(args: argparse.Namespace) -> int:
    from qingfeng.core import space

    t0 = time.monotonic()
    index = space.build_index(args.drive)
    duration = time.monotonic() - t0
    result = {
        "step": "index",
        "drive": args.drive,
        "accessible_bytes": int(index.accessible_bytes),
        "total_bytes": int(index.total_bytes),
        "unaccounted_bytes": int(index.unaccounted_bytes),
        "duration": round(duration, 2),
        "cancelled": bool(index.cancelled),
        "truncated": bool(index.truncated),
    }
    ok = (
        not index.cancelled
        and int(index.accessible_bytes) > 0
        and int(index.total_bytes) > 0
        and duration <= args.max_seconds
    )
    cache_ok = False
    try:
        space.save_index(index)
        loaded = space.load_cached_index()
        cache_ok = loaded is not None and int(loaded.accessible_bytes) == int(index.accessible_bytes)
    except Exception as exc:  # 缓存目录不可写时降级，不影响索引本身
        result["cache_error"] = f"{type(exc).__name__}: {exc}"
    result["cache_ok"] = cache_ok
    if args.save:
        try:
            target = Path(args.save)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(index.to_json(), ensure_ascii=False), encoding="utf-8")
            result["saved"] = str(target)
        except Exception as exc:  # 保存失败时 decide 步骤会自己重建
            result["save_error"] = f"{type(exc).__name__}: {exc}"
    result["ok"] = ok
    _emit(result)
    return 0 if ok else 1


def step_decide(args: argparse.Namespace) -> int:
    from qingfeng.core import decide, space

    if args.load and Path(args.load).exists():
        index = space.SpaceIndex.from_json(json.loads(Path(args.load).read_text(encoding="utf-8")))
        source = "cache-json"
    else:
        index = space.build_index("C:\\")
        source = "rebuilt"

    items = decide.collect_review_items(index, limit=args.limit)
    groups = decide.group_review_items(items)
    can_delete = sum(1 for i in items if decide.action_policy(i).can_delete)
    can_move = sum(1 for i in items if decide.action_policy(i).can_move)
    targets = decide.suggest_targets()
    group_total = sum(int(g.total_bytes) for g in groups)
    items_total = sum(int(i.size) for i in items)
    result = {
        "step": "decide",
        "source": source,
        "items": len(items),
        "items_bytes": items_total,
        "groups": len(groups),
        "group_keys": [g.key for g in groups],
        "group_bytes_sum": group_total,
        "bytes_consistent": group_total == items_total,
        "can_delete": can_delete,
        "can_move": can_move,
        "targets": [{"drive": t.drive, "free_bytes": int(t.free_bytes), "writable": bool(t.writable)}
                    for t in targets],
    }
    ok = (
        len(items) > 0
        and len(groups) > 0
        and result["bytes_consistent"]
        and can_delete + can_move > 0
    )
    result["ok"] = ok
    _emit(result)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v2 空间索引 / 决策冒烟")
    parser.add_argument("--step", choices=("index", "decide"), required=True)
    parser.add_argument("--drive", default="C:\\")
    parser.add_argument("--max-seconds", type=float, default=120.0)
    parser.add_argument("--limit", type=int, default=800)
    parser.add_argument("--save", default="")
    parser.add_argument("--load", default="")
    args = parser.parse_args(argv)
    try:
        return step_index(args) if args.step == "index" else step_decide(args)
    except Exception:
        traceback.print_exc()
        _emit({"step": args.step, "ok": False, "error": "exception"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
