"""pip 包装器：绕过 DSH Windows 沙箱的一个约束。

现象：沙箱下由 os.mkdir(path, 0o700)（tempfile.mkdtemp 内部就是这么干的）创建的目录，
其显式 DACL 不含沙箱令牌可用的 ACE，导致之后往目录里写文件报 PermissionError [Errno 13]，
pip 的临时解包目录全走 tempfile.mkdtemp，于是 pip install / download 必然失败。

对策：把 os.mkdir 的 mode 统一改成 0o777（Windows 上不落显式 DACL，走继承），
再以本模块为入口调用 pip。

用法：
    python tools/pip_sandbox.py install --target ... pyyaml
"""

from __future__ import annotations

import os
import sys


def _patch() -> None:
    real_mkdir = os.mkdir

    def mkdir(path, mode=0o777, *, dir_fd=None):
        # 忽略 0o700 这类显式 mode，避免生成沙箱令牌无法写入的目录
        if dir_fd is None:
            return real_mkdir(path, 0o777)
        return real_mkdir(path, 0o777, dir_fd=dir_fd)

    os.mkdir = mkdir


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    _patch()
    from pip._internal.cli.main import main as pip_main

    return int(pip_main(args))


if __name__ == "__main__":
    sys.exit(main())
