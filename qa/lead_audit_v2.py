"""Lead v2 独立审计（空间透视 + 决策助手 + 跨盘迁移）。

用法（在 danger-full-access 会话里跑）：
    python qa/lead_audit_v2.py

检查项：
  A. 索引：可构建 / 计时达标 / 字节自洽 / 硬链接去重有效 / 缓存往返
  B. 决策：collect_review_items 无嵌套重复 / 分组合计自洽 / 分类覆盖
  C. 动作授权：action_policy 矩阵（Windows/Program Files 拒绝、用户目录放行、ProgramData 仅 installer/Downloads 放行）
  D. 删除：delete_many 走回收站、受保护路径拒绝、用户临时文件成功
  E. 迁移：suggest_targets 可写探测、真实跨盘迁移校验 hash、源删除、受保护路径拒绝
  F. 系统探测：probe_system_space(index) 快速路径计时、字段完整、WinSxS raw/dedup 都在

退出码 0 = 全 PASS。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  <- {detail}" if detail and not ok else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    from qingfeng.core import decide, space
    from qingfeng.core import winapi

    # ---------- A. 索引 ----------
    section("A. 磁盘索引")
    t0 = time.time()
    idx = space.build_index("C:\\")
    build_t = time.time() - t0
    check(not idx.cancelled, "build_index 完成未取消")
    check(build_t <= 90, f"索引耗时 {build_t:.1f}s ≤ 90s", f"{build_t:.1f}s")
    total_gb = idx.total_bytes / 2**30
    acc_gb = idx.accessible_bytes / 2**30
    check(idx.total_bytes > 0 and idx.accessible_bytes > 0, "total/accessible 有效",
          f"{total_gb:.1f}/{acc_gb:.1f}")
    check(0 <= idx.unaccounted_bytes <= idx.total_bytes, "unaccounted 合法",
          f"{idx.unaccounted_bytes/2**30:.1f}GB")
    check(idx.accessible_bytes <= idx.total_bytes, "accessible ≤ total")

    root = idx.root_node
    child_sum = sum(c.size for c in root.children) + root.other_size
    # 根节点 size 应等于 accessible（或与之接近）
    check(root.size > 0 and abs(root.size - idx.accessible_bytes) / max(idx.accessible_bytes, 1) < 0.05,
          "根节点大小与 accessible 自洽 ±5%", f"root={root.size} acc={idx.accessible_bytes}")
    check(child_sum <= root.size * 1.01, "子节点合计不超过根", f"{child_sum} vs {root.size}")

    # 硬链接去重：Windows 目录 raw vs dedup（probe 提供）
    probe_fast = space.probe_system_space(index=idx)
    win = probe_fast.get("windows", {})
    check(isinstance(win, dict) and win.get("bytes", 0) > 0, "windows 大小存在", str(win)[:80])
    raw = win.get("bytes_raw")
    if raw:
        check(raw >= win.get("bytes", 0), "windows raw ≥ dedup（硬链接去重生效）",
              f"raw={raw} dedup={win.get('bytes')}")

    # 缓存往返
    t0 = time.time()
    space.save_index(idx)
    save_t = time.time() - t0
    cache = space.cache_path()
    check(cache.exists(), "缓存文件已写出", str(cache))
    if cache.exists():
        check(cache.stat().st_size < 15 * 1024 * 1024, "缓存大小 <15MB",
              f"{cache.stat().st_size/1e6:.2f}MB")
    t0 = time.time()
    cached = space.load_cached_index()
    load_t = time.time() - t0
    check(cached is not None, "缓存可读回")
    check(load_t < 2.0, f"缓存读取 {load_t:.2f}s <2s")
    if cached is not None:
        check(cached.root_node.size == idx.root_node.size, "缓存往返大小一致")
    print(f"    (info) build={build_t:.1f}s save={save_t:.2f}s load={load_t:.2f}s")

    # ---------- B. 决策 ----------
    section("B. 大文件决策")
    items = decide.collect_review_items(idx)
    check(len(items) >= 20, f"大文件条目 {len(items)} ≥ 20", str(len(items)))
    paths = [os.path.normcase(os.path.normpath(i.path)) for i in items]
    nested = 0
    pset = set(paths)
    for p in paths:
        parent = os.path.dirname(p)
        while parent and parent not in ("c:\\", "c:"):
            if parent in pset:
                nested += 1
                break
            parent = os.path.dirname(parent)
    check(nested == 0, "无父子嵌套重复条目", f"{nested} 个嵌套")
    allowed_advice = {"safe_delete", "review", "keep", "move"}
    check(all(i.advice in allowed_advice for i in items), "advice 取值合法",
          str({i.advice for i in items if i.advice not in allowed_advice}))
    check(all(i.category for i in items), "每条都有 category")
    groups = decide.group_review_items(items)
    group_sum = sum(g.total_bytes for g in groups)
    item_sum = sum(i.size for i in items)
    check(group_sum == item_sum, "分组合计 == 条目合计", f"{group_sum} vs {item_sum}")
    check(all(g.items for g in groups), "分组非空")
    cats = {i.category for i in items}
    check(len(cats) >= 3, f"分类覆盖 {len(cats)} 类 ≥3", str(sorted(cats)))
    print(f"    (info) 条目 {len(items)} 个 / {item_sum/2**30:.2f} GB，分组 " +
          ", ".join(f"{g.key}:{len(g.items)}/{g.total_bytes/2**30:.1f}G" for g in groups[:8]))

    # ---------- C. 动作授权矩阵 ----------
    section("C. 动作授权矩阵")
    me = os.path.expandvars(r"%USERPROFILE%")

    def mk(path: str, category: str = "installer", size: int = 300 * 2**20,
           advice: str = "safe_delete", risk: str = "caution") -> decide.ReviewItem:
        return decide.ReviewItem(path=path, name=os.path.basename(path), is_dir=False,
                                 size=size, mtime=time.time() - 86400 * 30, age_days=30,
                                 ext=os.path.splitext(path)[1].lower(), category=category,
                                 advice=advice, reason="audit", risk=risk, group=category)

    cases = [
        # (item, can_delete, can_move, 标签)
        (mk(r"C:\Windows\System32\notepad.exe", "program"), False, False, "Windows 目录拒绝"),
        (mk(r"C:\Program Files\SomeApp\old.exe", "app-version-dup"), False, False, "Program Files 拒绝"),
        (mk(r"C:\Program Files (x86)\Microsoft\Edge\Application\x.exe", "app-version-dup"), False, False,
         "Edge 程序目录拒绝"),
        (mk(r"C:\ProgramData\Microsoft\Windows\WER\x.dmp", "app-cache"), False, False, "ProgramData 非 Downloads 拒绝"),
        (mk(r"C:\ProgramData\Intel\DSA\Downloads\gfx_win.exe", "installer"), True, False,
         "ProgramData\\Intel\\DSA\\Downloads 允许删除"),
        (mk(os.path.join(me, "AppData", "Local", "Temp", "a.tmp"), "installer"), True, True, "用户 Temp 放行"),
        (mk(os.path.join(me, "AppData", "Local", "Packages", "x", "y.dat"), "app-cache"), False, False,
         "AppData\\Packages 拒绝"),
        (mk(r"C:\$Recycle.Bin\S-1-5-21\x", "system-media"), False, False, "回收站目录拒绝"),
        (mk(os.path.join(me, "Videos", "a.mp4"), "video", advice="review", risk="safe"), True, True,
         "用户视频可删可迁"),
    ]
    for item, want_del, want_move, label in cases:
        p = decide.action_policy(item)
        check(p.can_delete == want_del and p.can_move == want_move, label,
              f"got del={p.can_delete} move={p.can_move} reason={p.deny_reason!r}")

    # 真实分类结果里，Program Files 下的条目必须全部 deny
    bad = [i.path for i in items if i.path.lower().startswith(("c:\\program files", "c:\\windows"))
           and decide.action_policy(i).can_delete]
    check(not bad, "真实条目中系统/程序目录全部 deny", str(bad[:3]))

    # ---------- D. 删除 ----------
    section("D. 删除动作（回收站）")
    base = ROOT / f".qa_v2_del_{os.getpid()}"
    base.mkdir()
    try:
        f1 = base / "junk.tmp"
        f1.write_bytes(b"z" * 4096)
        res = decide.delete_many([str(f1)])
        check(not f1.exists(), "delete_many 删除了用户文件", str(res.failed))
        check(res.recycled is True, "默认走回收站")
        # 受保护路径必须被拒
        res2 = decide.delete_many([r"C:\Windows\System32\notepad.exe"])
        check(res2.freed_bytes == 0 and bool(res2.failed or res2.skipped)
              if hasattr(res2, "skipped") else res2.freed_bytes == 0,
              "delete_many 拒绝受保护路径", str(getattr(res2, "failed", None)))
        check(Path(r"C:\Windows\System32\notepad.exe").exists(), "notepad.exe 完好")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    # ---------- E. 迁移 ----------
    section("E. 跨盘迁移")
    targets = [t for t in decide.suggest_targets() if t.writable and t.drive.upper() != "C:"]
    check(bool(targets), f"suggest_targets 找到可写目标 {len(targets)} 个",
          str([(t.drive, t.path, t.writable) for t in decide.suggest_targets()]))
    # 源必须放在 C 盘（真实跨盘场景），用用户 TEMP
    ctemp = tempfile.mkdtemp(prefix="qf_audit_v2_", dir=os.path.expandvars(r"%TEMP%"))
    try:
        if targets:
            tgt = targets[0]
            src = Path(ctemp) / "payload.bin"
            src.write_bytes(os.urandom(3 * 2**20))
            want = sha256(src)
            mv = decide.move_many([str(src)], tgt.path)
            check(mv.moved == 1, "move_many 成功 1 项", str(mv.failed or mv.skipped))
            check(not src.exists(), "源文件已删除")
            found = list(Path(mv.dest_root).rglob(src.name)) if Path(mv.dest_root).exists() else []
            check(bool(found), "目标文件存在", str(mv.dest_root))
            if found:
                check(sha256(found[0]) == want, "迁移后内容 hash 一致")
                found[0].unlink()
            # 受保护路径迁移必须拒绝
            mv2 = decide.move_many([r"C:\Windows\System32\notepad.exe"], tgt.path)
            check(mv2.moved == 0 and bool(mv2.failed or mv2.skipped), "move_many 拒绝受保护路径")
            # 目标不可写时必须拒绝且源不动
            src2 = Path(ctemp) / "payload2.bin"
            src2.write_bytes(b"q" * 4096)
            mv3 = decide.move_many([str(src2)], r"C:\Program Files\QFAuditTarget")
            check(mv3.moved == 0 and src2.exists(), "目标不可写时拒绝且源文件保留",
                  str(mv3.failed or mv3.skipped))
    finally:
        shutil.rmtree(ctemp, ignore_errors=True)
        for leftover in Path(ROOT).glob(".qa_v2_move_*"):
            shutil.rmtree(leftover, ignore_errors=True)

    # ---------- F. 系统探测 ----------
    section("F. 系统占用探测")
    t0 = time.time()
    probe = space.probe_system_space(index=idx)
    probe_t = time.time() - t0
    check(probe_t < 3.0, f"probe(index) 快速路径 {probe_t:.2f}s <3s", f"{probe_t:.2f}s")
    for key in ("recycle_bin", "hiberfil", "windows", "winsxs", "restore_points"):
        check(key in probe, f"probe 含 {key}")
    hb = probe.get("hiberfil", {})
    check(isinstance(hb, dict), "hiberfil 结构正常")
    print(f"    (info) hiberfil={hb.get('bytes', 0)/2**30:.2f}GB "
          f"windows={probe.get('windows', {}).get('bytes', 0)/2**30:.1f}GB "
          f"winsxs={probe.get('winsxs', {}).get('bytes', 0)/2**30:.1f}GB")

    return finish()


def finish() -> int:
    total = len(results)
    passed = sum(1 for ok, _, _ in results if ok)
    print("\n" + "=" * 62)
    print(f"v2 审计结果：{passed}/{total} PASS" + ("  全部通过 ✅" if passed == total else "  存在失败 ❌"))
    if passed != total:
        for ok, name, detail in results:
            if not ok:
                print(f"  - {name}  {detail}")
    print("=" * 62)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
