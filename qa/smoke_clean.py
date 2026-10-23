"""Lead 端到端清理烟雾测试（真实删除路径，但只在受控临时目录内）。

验证：
  1) 扫描 → 回收站删除（mode="recycle"）→ 文件确实消失
  2) 扫描 → 永久删除（mode="delete"）→ 文件确实消失
  3) 保护路径请求被 cleaner 拒绝并记入 failed，绝不删除
  4) winapi.recycle_bin_size 在真实删除后可返回统计（不强制非零）

安全：所有被测文件都在工作区内的 .qa_smoke_* 目录，绝不触碰真实系统目录。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qingfeng.core import winapi
from qingfeng.core.cleaner import clean
from qingfeng.core.models import Rule
from qingfeng.core.scanner import scan

ok = 0
total = 0


def check(cond: bool, name: str, detail: str = "") -> None:
    global ok, total
    total += 1
    if cond:
        ok += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  <- {detail}" if detail else ""))


def make_tree(base: Path, n: int = 4, size: int = 2048) -> None:
    (base / "sub").mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (base / f"f{i}.tmp").write_bytes(b"x" * size)
    (base / "sub" / "nested.tmp").write_bytes(b"y" * size)


def rule_for(path: Path, rid: str) -> Rule:
    return Rule(
        id=rid, name="烟雾测试", category="系统", description="",
        action="delete", paths=[str(path)], globs=["*"], risk="safe",
    )


def main() -> int:
    base = ROOT / f".qa_smoke_{os.getpid()}"
    try:
        # ---- 1. 回收站删除 ----
        d1 = base / "recycle_case"
        make_tree(d1)
        before_files = [p for p in d1.rglob("*") if p.is_file()]
        rep = scan([rule_for(d1, "smoke-recycle")])
        check(rep.total_bytes == 5 * 2048, "回收站用例：扫描统计", f"{rep.total_bytes}")
        res = clean(rep.results, mode="recycle")
        left = [p for p in d1.rglob("*") if p.is_file()]
        check(not left, "回收站删除：文件已消失", f"剩余 {len(left)}")
        check(len(res.failed) == 0, "回收站删除：无失败项", str(res.failed[:3]))
        check(res.recycled is True, "回收站删除：recycled=True")
        items, size = winapi.recycle_bin_size("E:")
        print(f"    (info) E: 回收站现有 {items} 项 / {size} 字节")

        # ---- 2. 永久删除 ----
        d2 = base / "delete_case"
        make_tree(d2, n=3)
        rep2 = scan([rule_for(d2, "smoke-delete")])
        res2 = clean(rep2.results, mode="delete")
        left2 = [p for p in d2.rglob("*") if p.is_file()]
        check(not left2, "永久删除：文件已消失", f"剩余 {len(left2)}")
        check(res2.recycled is False, "永久删除：recycled=False")

        # ---- 3. 保护路径拒绝（即使规则范围被伪造）----
        d3 = base / "guard_case"
        make_tree(d3, n=1)
        rep3 = scan([rule_for(d3, "smoke-guard")])
        # 伪造一个指向 C:\Windows\System32 的条目
        bad = rep3.results[0].items[0]
        bad.path = r"C:\Windows\System32\notepad.exe"
        res3 = clean(rep3.results, mode="delete")
        check(any("System32" in p for p, _ in res3.failed), "保护路径：被拒绝并记录", str(res3.failed[:2]))
        check(Path(r"C:\Windows\System32\notepad.exe").exists(), "保护路径：notepad.exe 完好")

        # ---- 4. admin 规则整条跳过 ----
        d4 = base / "admin_case"
        make_tree(d4, n=2)
        r4 = rule_for(d4, "smoke-admin")
        r4.requires_admin = True
        rep4 = scan([r4])
        res4 = clean(rep4.results, mode="delete")
        left4 = [p for p in d4.rglob("*") if p.is_file()]
        if not winapi.is_admin():
            check(bool(left4) and res4.freed_bytes == 0, "管理员规则：非管理员下整条跳过", f"剩余 {len(left4)}")
        else:
            check(not left4, "管理员规则：管理员下正常删除")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print(f"\n烟雾测试：{ok}/{total} PASS")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
