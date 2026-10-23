"""清风清理 QingFeng Cleaner — 程序入口。

用法：
    python main.py                 # 启动图形界面（含托盘）
    python main.py --scan-only     # 只扫描并打印报告，不删除任何文件
    python main.py --no-tray       # 不启用托盘
    python main.py --hidden        # 启动时隐藏到托盘
    python main.py --rules-dir DIR # 使用自定义规则目录
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qingfeng import APP_NAME, APP_NAME_EN, __version__  # noqa: E402


def _fmt(n: int) -> str:
    try:
        from qingfeng.core.winapi import human_size
    except Exception:  # pragma: no cover - 引擎缺失时的兜底
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(n)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{n} B"
    return human_size(n)


def run_scan_only(rules_dir: Path | None) -> int:
    """只扫描、只打印，绝不删除任何文件。"""
    from qingfeng.core import rules as rules_mod
    from qingfeng.core.scanner import scan

    rules = rules_mod.load_rules(rules_dir)
    print(f"{APP_NAME} v{__version__} — 只扫描模式（不会删除任何文件）")
    print(f"已加载 {len(rules)} 条规则\n")

    def progress(name: str, done: int, total: int) -> None:
        if total and (done == total or done % 5 == 0):
            print(f"\r  扫描中 [{done}/{total}] {name[:40]:<40}", end="", flush=True)

    report = scan(rules, progress=progress)
    print("\r" + " " * 78 + "\r", end="")

    total = 0
    advanced = 0
    need_admin = 0
    for res in report.results:
        if res.total_bytes <= 0 and not res.items and not getattr(res, "requires_elevation", False):
            continue
        flag = ""
        if getattr(res, "requires_elevation", False):
            need_admin += 1
            flag = "  [需管理员：以管理员身份运行后可清理]"
        elif res.skipped_reason:
            flag = f"  [跳过: {res.skipped_reason}]"
        elif res.error:
            flag = f"  [错误: {res.error}]"
        elif res.rule.risk != "safe":
            flag += f"  [{res.rule.risk}]"
        print(f"{res.rule.category:<6} {res.rule.name:<28} {_fmt(res.total_bytes):>10}  "
              f"{res.file_count:>7} 项{flag}")
        if res.rule.action == "delete" and not getattr(res, "requires_elevation", False):
            if res.rule.default_enabled:
                total += res.total_bytes
            else:
                advanced += res.total_bytes

    print("-" * 78)
    print(f"可安全清理合计（默认勾选）: {_fmt(total)}")
    if advanced:
        print(f"进阶项（默认不勾选，需手动开启）: {_fmt(advanced)}")
    if need_admin:
        print(f"另有 {need_admin} 条系统规则需要管理员权限（以管理员身份运行后可扫描/清理）")
    print(f"C 盘可用: {_fmt(report.drive_free)} / {_fmt(report.drive_total)}")
    print(f"扫描耗时: {report.duration:.2f}s")
    if report.cancelled:
        print("（扫描被取消）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qingfeng",
        description=f"{APP_NAME} {APP_NAME_EN} — Windows C 盘清理工具",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--scan-only", action="store_true", help="只扫描并打印报告")
    parser.add_argument("--no-tray", action="store_true", help="不启用系统托盘")
    parser.add_argument("--hidden", action="store_true", help="启动时隐藏到托盘")
    parser.add_argument("--rules-dir", type=Path, default=None, help="自定义规则目录")
    parser.add_argument(
        "--page",
        choices=("clean", "space"),
        default="clean",
        help="启动时显示的页面：clean=清理（默认），space=空间透视",
    )
    args = parser.parse_args(argv)

    if args.rules_dir is not None:
        import qingfeng

        qingfeng.RULES_DIR = args.rules_dir.resolve()

    if args.scan_only:
        return run_scan_only(args.rules_dir)

    from qingfeng.ui.main_window import launch

    return launch(with_tray=not args.no_tray, start_hidden=args.hidden, page=args.page)


if __name__ == "__main__":
    sys.exit(main())
