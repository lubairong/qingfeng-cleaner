"""Lead v2.1 独立审计：提权重启命令 + 非管理员降级 + 单文件/目录版交付检查。

用法：
    python qa/audit_v21.py

检查项：
  A. relaunch_command：frozen / source / extra_args / 空 argv 四种形态
  B. restart_as_admin：模拟 ShellExecuteW，断言命令行里可执行文件只出现一次
  C. scanner：非管理员时 requires_admin 规则不扫、requires_elevation=True、无 error
  D. cleaner：非管理员时 skipped_admin 收集、freed_bytes=0
  E. CLI：--scan-only 输出含「需管理员」提示、不含 `[错误:` 行
  F. 交付：dist 只保留目录版（若已重新打包）；单文件不应在 dist 根
"""

from __future__ import annotations

import subprocess
import sys
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


def main() -> int:  # noqa: C901
    from qingfeng.core import cleaner, scanner, winapi
    from qingfeng.core.models import Rule, RuleScanResult

    # ---------- A. relaunch_command ----------
    section("A. relaunch_command（提权重启命令构造）")
    saved = (getattr(sys, "frozen", False), sys.executable, list(sys.argv))
    try:
        # frozen 形态
        sys.frozen = True  # type: ignore[attr-defined]
        sys.executable = r"C:\Tools\QingFengCleaner\QingFengCleaner.exe"
        sys.argv = [sys.executable, "--page", "space"]
        exe, args = winapi.relaunch_command()
        check(exe == sys.executable, "frozen: 可执行文件正确", str(exe))
        check(args == ["--page", "space"], "frozen: 参数不含 exe 自身", str(args))
        check(all("QingFengCleaner.exe" not in a for a in args), "frozen: args 里无自身路径", str(args))

        # frozen + extra_args
        exe2, args2 = winapi.relaunch_command(["--page", "clean"])
        check(args2 == ["--page", "clean"], "frozen: extra_args 覆盖", str(args2))

        # source 形态
        del sys.frozen  # type: ignore[attr-defined]
        sys.executable = r"C:\Python312\python.exe"
        sys.argv = [r"E:\proj\main.py", "--page", "space"]
        exe3, args3 = winapi.relaunch_command()
        check(exe3 == sys.executable, "source: 可执行文件=pythonexe", str(exe3))
        check(args3 == [r"E:\proj\main.py", "--page", "space"], "source: 参数含脚本路径", str(args3))

        # 空 argv 边界
        sys.argv = [r"E:\proj\main.py"]
        _, args4 = winapi.relaunch_command([])
        check(args4 == [r"E:\proj\main.py"], "source: 空 extra_args 回退到脚本", str(args4))
    finally:
        sys.frozen, sys.executable, sys.argv = saved  # type: ignore[attr-defined]

    # ---------- B. restart_as_admin 模拟 ----------
    section("B. restart_as_admin（模拟 ShellExecuteW）")
    captured: dict[str, str] = {}

    class FakeShell:
        def ShellExecuteW(self, hwnd, verb, file, params, cwd, show):  # noqa: N802, ANN001
            captured.update(verb=verb, file=file, params=params)
            return 42  # >32 表示成功

    saved_shell = getattr(winapi, "_shell32", None)
    saved_ready = getattr(winapi, "_api_ready", None)
    saved_b = (getattr(sys, "frozen", False), sys.executable, list(sys.argv))
    try:
        winapi._shell32 = FakeShell()  # type: ignore[attr-defined]
        winapi._api_ready = True  # type: ignore[attr-defined]
        sys.frozen = True  # type: ignore[attr-defined]
        sys.executable = r"C:\Tools\QingFengCleaner\QingFengCleaner.exe"
        sys.argv = [sys.executable, "--page", "space"]
        ok = winapi.restart_as_admin()
        check(ok is True, "frozen: restart_as_admin 返回 True")
        check(captured.get("verb") == "runas", "frozen: verb=runas", str(captured.get("verb")))
        params = captured.get("params", "")
        check(params.count("QingFengCleaner.exe") == 0, "frozen: params 里不重复 exe 路径", params)
        check("--page space" in params, "frozen: params 保留 --page", params)
        check(winapi.restart_as_admin(["--page", "clean"]) is True and "--page clean" in captured.get("params", ""),
              "frozen: extra_args 生效", captured.get("params", ""))
    finally:
        winapi._shell32 = saved_shell  # type: ignore[attr-defined]
        winapi._api_ready = saved_ready  # type: ignore[attr-defined]
        sys.frozen, sys.executable, sys.argv = saved_b  # type: ignore[attr-defined]

    # ---------- C. scanner 非管理员降级 ----------
    section("C. scanner：非管理员时 requires_admin 规则降级")
    rule = Rule(id="audit-admin", name="审计系统项", category="系统", description="",
                action="delete", paths=[r"C:\Windows\Temp"], globs=["*"], risk="safe",
                requires_admin=True)
    saved_admin = winapi.is_admin
    try:
        winapi.is_admin = lambda: False  # type: ignore[assignment]
        rep = scanner.scan([rule])
        res = rep.results[0]
        check(res.requires_elevation is True, "非管理员: requires_elevation=True")
        check(res.error is None, "非管理员: 无 error", str(res.error))
        check(bool(res.skipped_reason) and "管理员" in (res.skipped_reason or ""),
              "非管理员: skipped_reason 提示管理员", str(res.skipped_reason))
        check(res.items == [], "非管理员: 未扫描出条目")

        winapi.is_admin = lambda: True  # type: ignore[assignment]
        rep2 = scanner.scan([rule])
        res2 = rep2.results[0]
        check(res2.requires_elevation is False, "管理员: requires_elevation=False")
        check(res2.error is None or "拒绝" not in (res2.error or ""), "管理员: 不再报权限错误")
    finally:
        winapi.is_admin = saved_admin  # type: ignore[assignment]

    # ---------- D. cleaner 非管理员跳过 ----------
    section("D. cleaner：非管理员 skipped_admin")
    fake = RuleScanResult(rule=rule)
    try:
        winapi.is_admin = lambda: False  # type: ignore[assignment]
        out = cleaner.clean([fake])
        check(bool(getattr(out, "skipped_admin", None)), "非管理员: skipped_admin 记录规则名",
              str(getattr(out, "skipped_admin", None)))
        check(out.freed_bytes == 0, "非管理员: freed_bytes=0")
    finally:
        winapi.is_admin = saved_admin  # type: ignore[assignment]

    # ---------- E. CLI 输出 ----------
    section("E. CLI --scan-only 输出")
    proc = subprocess.run([sys.executable, str(ROOT / "main.py"), "--scan-only"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          cwd=str(ROOT), timeout=180)
    out = (proc.stdout or "") + (proc.stderr or "")
    check(proc.returncode == 0, "--scan-only 退出码 0", str(proc.returncode))
    check("需管理员" in out, "输出含「需管理员」提示")
    check("[错误:" not in out, "输出不含 [错误: 行", out[out.find("[错误:") : out.find("[错误:") + 80] if "[错误:" in out else "")
    check("可安全清理合计" in out, "输出含清理合计")

    # ---------- F. dist 交付结构 ----------
    section("F. dist 交付结构")
    dist = ROOT / "dist"
    if dist.exists():
        names = {p.name for p in dist.iterdir()}
        check("QingFengCleaner-onedir" in names, "dist 含目录版")
        check("QingFengCleaner.exe" not in names, "dist 根不含单文件版（已移除）", str(sorted(names)))
        bat = [n for n in names if n.endswith(".bat")]
        check(bool(bat), "dist 含启动 bat", str(sorted(names)))
    else:
        check(False, "dist 存在", "dist 目录不存在（需重新打包）")

    return finish()


def finish() -> int:
    total = len(results)
    passed = sum(1 for ok, _, _ in results if ok)
    print("\n" + "=" * 64)
    print(f"v2.1 审计结果：{passed}/{total} PASS" + ("  全部通过 ✅" if passed == total else "  存在失败 ❌"))
    if passed != total:
        for ok, name, detail in results:
            if not ok:
                print(f"  - {name}  {detail}")
    print("=" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
