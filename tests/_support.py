"""测试辅助：在**工作区内**创建临时目录。

说明：本项目在受限沙箱（workspace-write）下开发：

- 系统 TEMP 子目录不可写；
- ``tempfile.mkdtemp`` 会用 ``mode=0o700`` 建目录，在 Windows 上会破坏继承的 ACL，
  导致沙箱拒绝后续写文件。

因此单元测试统一用本模块的 :func:`temp_dir`：在 ``tests/_tmp/`` 下用 ``os.makedirs``
创建普通目录，测试结束递归清理。生产代码不受影响。
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

#: 测试临时根目录（工作区内，避免依赖系统 TEMP 写权限）
TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


class TestTempDir:
    """简易临时目录对象，兼容 ``tempfile.TemporaryDirectory`` 常用接口。"""

    def __init__(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.name = str(TMP_ROOT / f"qf-{uuid.uuid4().hex[:10]}")
        os.makedirs(self.name, exist_ok=False)

    def cleanup(self) -> None:
        shutil.rmtree(self.name, ignore_errors=True)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()


def temp_dir() -> TestTempDir:
    """创建自动清理的临时目录（位于工作区内）。"""
    return TestTempDir()
