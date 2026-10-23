"""受限 Windows 沙箱下的 Python 执行包装器（开发/验证用，不影响产品运行）。

背景
----
在 DSH Windows 沙箱（Low/受限令牌）里，`os.mkdir(path, 0o700)` 创建的目录会带上
沙箱令牌无权访问的显式 DACL。`tempfile.mkdtemp()` 内部正是用 0o700 建目录，于是
pip / ensurepip / PyInstaller 等一切"先建临时目录再写文件"的工具都会报
`PermissionError [Errno 13]`，与网络、权限设置无关。

对策
----
把 `os.mkdir` 的 mode 统一改成 0o777（Windows 上不落显式 DACL，走继承），
再在同一进程内执行目标模块/脚本。

用法
----
    python tools/sandbox_exec.py -m <module> [args...]     # 等价 python -m <module>
    python tools/sandbox_exec.py <script.py> [args...]     # 等价 python <script.py>

普通环境（非受限沙箱）无需本脚本，直接用 `python -m pip` / `python -m PyInstaller` 即可。
"""

from __future__ import annotations

import os
import runpy
import sys


def _patch_mkdir() -> None:
    real_mkdir = os.mkdir

    def mkdir(path, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            return real_mkdir(path, 0o777)
        return real_mkdir(path, 0o777, dir_fd=dir_fd)

    os.mkdir = mkdir


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__.strip().split("用法")[-1].strip(), file=sys.stderr)
        print("用法: python tools/sandbox_exec.py (-m module | script.py) [args...]", file=sys.stderr)
        return 2

    _patch_mkdir()

    if args[0] == "-m":
        if len(args) < 2:
            print("用法: python tools/sandbox_exec.py -m <module> [args...]", file=sys.stderr)
            return 2
        module, rest = args[1], args[2:]
        sys.argv = [module, *rest]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return 0

    script, rest = args[0], args[1:]
    sys.argv = [script, *rest]
    runpy.run_path(script, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
