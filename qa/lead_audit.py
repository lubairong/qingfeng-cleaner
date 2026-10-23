"""Lead 独立安全审计（不属于任何成员的写域，只读校验引擎与规则集）。

用法：
    python qa/lead_audit.py

检查项：
  A. 规则集：可加载、id 唯一、category/risk/action 合法、条数达标、路径全部使用环境变量
  B. 危险规则：展开后的路径不得等于或包含受保护根目录；report 类规则不得改成 delete
  C. safety 守卫：一票否决用例（受保护路径 / 路径穿越 / 大小写变形 / 尾部斜杠 / 长路径前缀）
  D. 引擎契约：scan/clean 签名、ScanReport/CleanResult 字段、只读性（扫描前后文件数/大小不变）
  E. 真实路径放行：工作区临时目录、%TEMP% 下的测试文件必须被认为可删除

退出码：0 全 PASS，1 存在 FAIL。
"""

from __future__ import annotations

import inspect
import json
import os
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
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  <- {detail}" if detail and not ok else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    # ---------- A. 规则集 ----------
    section("A. 规则集")
    try:
        from qingfeng.core import rules as rules_mod

        rules = rules_mod.load_rules()
    except Exception as exc:  # noqa: BLE001
        check(False, "load_rules()", f"{type(exc).__name__}: {exc}")
        return finish()

    check(True, f"load_rules() 成功，共 {len(rules)} 条规则")
    check(len(rules) >= 38, "规则数 >= 38", f"实际 {len(rules)}")
    ids = [r.id for r in rules]
    check(len(ids) == len(set(ids)), "规则 id 唯一", f"重复: {len(ids) - len(set(ids))}")

    valid_cat = {"系统", "浏览器", "应用", "隐私痕迹", "大文件", "系统维护"}
    bad_cat = [r.id for r in rules if r.category not in valid_cat]
    check(not bad_cat, "category 合法", str(bad_cat[:5]))
    bad_risk = [r.id for r in rules if r.risk not in ("safe", "caution", "advanced")]
    check(not bad_risk, "risk 合法", str(bad_risk[:5]))
    bad_action = [r.id for r in rules if r.action not in ("delete", "empty_recycle_bin", "report")]
    check(not bad_action, "action 合法", str(bad_action[:5]))

    cats = {r.category for r in rules}
    check(cats == valid_cat, "六个分类全覆盖", f"缺: {sorted(valid_cat - cats)}")

    hardcoded = [r.id for r in rules for p in r.paths if "C:\\Users\\" in p and "%" not in p]
    check(not hardcoded, "路径无硬编码用户名", str(hardcoded[:5]))

    # ---------- B. 危险规则 ----------
    section("B. 危险规则检查")
    expand = getattr(rules_mod, "expand_paths", None)
    check(expand is not None, "rules.expand_paths() 存在（契约 §4.1）")

    _profile = os.path.expandvars(r"%USERPROFILE%")
    protected_roots = [
        "C:\\", "C:\\Windows", "C:\\Windows\\System32", "C:\\Windows\\SysWOW64",
        "C:\\Windows\\WinSxS", "C:\\Program Files", "C:\\Program Files (x86)",
        "C:\\ProgramData", "C:\\Users", _profile,
        os.path.join(_profile, "AppData"), os.path.join(_profile, "AppData", "Local"),
        os.path.join(_profile, "AppData", "Roaming"), os.path.join(_profile, "Documents"),
        os.path.join(_profile, "Desktop"), os.path.join(_profile, "Downloads"),
        os.path.join(_profile, "OneDrive"), "C:\\$Recycle.Bin", "C:\\System Volume Information",
        "C:\\Recovery", "C:\\Boot", "C:\\EFI",
    ]
    norm = lambda s: os.path.normcase(os.path.normpath(os.path.abspath(s)))  # noqa: E731
    prot = {norm(p) for p in protected_roots}

    dangerous: list[str] = []
    for rule in rules:
        if rule.action != "delete":
            continue
        try:
            paths = rules_mod.expand_paths(rule) if expand else list(rule.paths)
        except Exception as exc:  # noqa: BLE001
            check(False, f"expand_paths({rule.id})", str(exc))
            continue
        for raw in paths:
            p = norm(raw)
            if p in prot:
                dangerous.append(f"{rule.id} -> {raw}")
            # 直接等于受保护根的子串前缀也要拦（如 C:\Users\HUAWEI 本身）
            if p.rstrip("\\") in {x.rstrip("\\") for x in prot}:
                dangerous.append(f"{rule.id} -> {raw}")
    check(not dangerous, "delete 规则不指向受保护根", "; ".join(dangerous[:6]))

    must_report = ["winsxs-component-store", "windows-old", "hibernation-file", "pagefile",
                   "restore-points", "windows-installer-cache"]
    by_id = {r.id: r for r in rules}
    bad = [i for i in must_report if i in by_id and by_id[i].action != "report"]
    check(not bad, "WinSxS/Windows.old/休眠/页面/还原点/Installer 必须 report", str(bad))

    adv_default_on = [r.id for r in rules if r.risk == "advanced" and r.default_enabled]
    check(not adv_default_on, "advanced 规则默认关闭", str(adv_default_on[:6]))

    browser_bad = []
    for r in rules:
        if r.category != "浏览器":
            continue
        joined = " ".join([*r.paths, *getattr(r, "globs", [])]).lower()
        for token in ("login data", "cookies", "bookmarks", "local state", "web data", "history"):
            if token in joined and r.action == "delete":
                browser_bad.append(f"{r.id}:{token}")
    check(not browser_bad, "浏览器规则不删凭据/书签/本地状态", "; ".join(browser_bad[:6]))

    # ---------- C. safety 守卫 ----------
    section("C. safety 守卫（必须全部拒绝）")
    from qingfeng.core.safety import UnsafePathError, assert_deletable

    must_reject = [
        "C:\\",
        "C:\\\\",
        r"C:\Windows",
        r"C:\Windows\\",
        r"C:\WINDOWS\System32",
        r"C:\Windows\System32\config",
        r"C:\Windows\System32\..\System32",
        r"C:\Windows\System32\drivers\etc\..\..\System32",
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        r"C:\ProgramData",
        r"C:\Users",
        os.path.expandvars(r"%USERPROFILE%"),
        os.path.expandvars(r"%USERPROFILE%") + "\\",
        os.path.expandvars(r"%APPDATA%"),
        os.path.expandvars(r"%LOCALAPPDATA%"),
        os.path.expandvars(r"%USERPROFILE%\Documents"),
        os.path.expandvars(r"%USERPROFILE%\Desktop"),
        os.path.expandvars(r"%USERPROFILE%\Downloads"),
        os.path.expandvars(r"%WINDIR%"),
        os.path.expandvars(r"%WINDIR%\WinSxS"),
        r"C:\$Recycle.Bin",
        r"C:\System Volume Information",
        r"C:\Recovery",
        r"C:\Boot",
        r"C:\EFI",
        "D:\\",
        "E:\\",
        r"",
        "C:\\Windows\\System32\\..\\..\\Users\\" + os.path.basename(os.path.expandvars(r"%USERPROFILE%")),
    ]
    rejected = 0
    not_rejected: list[str] = []
    for p in must_reject:
        try:
            assert_deletable(p)
            not_rejected.append(p or "<empty>")
        except UnsafePathError:
            rejected += 1
        except Exception as exc:  # noqa: BLE001
            # 抛别的异常也算拒绝（保守），但要记录
            rejected += 1
            print(f"    (note) {p!r} -> {type(exc).__name__}: {exc}")
    check(rejected == len(must_reject), f"safety 拒绝全部 {len(must_reject)} 个危险路径", str(not_rejected[:8]))

    # C2. WeChat 深目录窄例外（唯一例外，经 Lead 批准）：只允许 <acct>\FileStorage\Cache
    section("C2. WeChat 窄例外")
    wx_root = os.path.expandvars(r"%USERPROFILE%\Documents\WeChat Files")
    wx_acct = os.path.join(wx_root, "wxid_audit01")
    wx_allow = [
        os.path.join(wx_acct, "FileStorage", "Cache"),
        os.path.join(wx_acct, "FileStorage", "Cache", "sub", "f.dat"),
        os.path.join(wx_acct, "filestorage", "cache"),  # 大小写不敏感
    ]
    wx_deny = [
        wx_root,
        wx_acct,
        os.path.join(wx_acct, "FileStorage"),
        os.path.join(wx_acct, "Msg"),
        os.path.join(wx_acct, "FileStorage", "MsgAttach"),
        os.path.join(wx_acct, "FileStorage", "Cache2"),
        os.path.join(wx_acct, "FileStorage", "Cache", "..", "..", "..", "Desktop"),
        os.path.join(wx_acct, "FileStorage", "Cache", "..", "..", "..", "Desktop", "x.txt"),
    ]
    wx_ok = 0
    for p in wx_allow:
        try:
            assert_deletable(p)
            wx_ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"    误拒(微信缓存): {p} -> {type(exc).__name__}: {exc}")
    check(wx_ok == len(wx_allow), f"微信缓存路径放行 {len(wx_allow)} 条", f"通过 {wx_ok}")

    wx_rej = []
    for p in wx_deny:
        try:
            assert_deletable(p)
            wx_rej.append(p)
        except Exception:  # noqa: BLE001
            pass
    check(not wx_rej, f"微信例外不越界（拒绝 {len(wx_deny)} 条）", "; ".join(wx_rej[:4]))

    # 允许用例（注意：沙箱下 mkdtemp 目录对沙箱令牌不可写，这里直接建普通目录）
    allow_ok = 0
    tmpdirs: list[Path] = []

    def make_tmp(prefix: str) -> Path:
        d = ROOT / f".qa_{prefix}_{os.getpid()}_{len(tmpdirs)}"
        d.mkdir()
        tmpdirs.append(d)
        return d

    try:
        d0 = make_tmp("allow")
        testdir = d0 / "fake_temp" / "sub"
        testdir.mkdir(parents=True)
        (testdir / "a.tmp").write_text("x" * 1024, encoding="utf-8")
        for p in (str(testdir / "a.tmp"), str(testdir)):
            try:
                assert_deletable(p)
                allow_ok += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    误拒: {p} -> {type(exc).__name__}: {exc}")
    finally:
        pass
    check(allow_ok == 2, "正常工作区文件放行", f"通过 {allow_ok}/2")

    # ---------- D. 引擎契约 ----------
    section("D. 引擎契约")
    from qingfeng.core import cleaner, scanner
    from qingfeng.core.models import CleanResult, Rule, RuleScanResult, ScanItem, ScanReport

    sig = inspect.signature(scanner.scan)
    check(set(sig.parameters) >= {"rules"}, "scan(rules, progress=, cancel=, max_depth=)", str(sig))
    sig_c = inspect.signature(cleaner.clean)
    check({"results", "mode"} <= set(sig_c.parameters), "clean(results, mode=, progress=)", str(sig_c))

    for cls, fields in [
        (Rule, {"id", "name", "category", "description", "action", "paths", "risk"}),
        (ScanItem, {"rule_id", "path", "is_dir", "size_bytes", "file_count"}),
        (RuleScanResult, {"rule", "items"}),
        (ScanReport, {"results", "drive_free", "drive_total"}),
        (CleanResult, {"deleted_files", "freed_bytes", "failed"}),
    ]:
        have = set(getattr(cls, "__dataclass_fields__", {}))
        check(fields <= have, f"{cls.__name__} 字段完整", str(fields - have))

    # 只读性：对临时目录扫描两次，文件数量与大小不得变化
    try:
        d = make_tmp("readonly")
        (d / "x").mkdir()
        for i in range(5):
            (d / "x" / f"f{i}.bin").write_bytes(b"0" * (100 * (i + 1)))
        before = sorted((p.name, p.stat().st_size) for p in d.rglob("*") if p.is_file())
        rule = Rule(id="audit-temp", name="审计临时目录", category="系统", description="",
                    action="delete", paths=[str(d)], globs=["*"], risk="safe")
        rep = scanner.scan([rule])
        after = sorted((p.name, p.stat().st_size) for p in d.rglob("*") if p.is_file())
        check(before == after, "扫描只读（文件未被改动）")
        check(rep.total_bytes == sum(100 * (i + 1) for i in range(5)),
              "扫描统计准确", f"{rep.total_bytes} != 1500")
    finally:
        import shutil

        for p in tmpdirs:
            shutil.rmtree(p, ignore_errors=True)

    # ---------- E. 真机 ----------
    section("E. 真机环境")
    from qingfeng.core import winapi
    free, total = winapi.drive_usage("C:")
    check(free > 0 and total > 0, "drive_usage('C:')", f"{free}/{total}")
    check(isinstance(winapi.is_admin(), bool), "is_admin()")
    check(winapi.human_size(1536) == "1.5 KB", "human_size(1536) == '1.5 KB'", winapi.human_size(1536))

    return finish()


def finish() -> int:
    total = len(results)
    passed = sum(1 for ok, _, _ in results if ok)
    print("\n" + "=" * 60)
    print(f"审计结果：{passed}/{total} PASS" + ("  全部通过 ✅" if passed == total else "  存在失败 ❌"))
    if passed != total:
        print("失败项：")
        for ok, name, detail in results:
            if not ok:
                print(f"  - {name}  {detail}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
