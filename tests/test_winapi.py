"""Win32 封装单元测试（tests/test_winapi.py）。

注意：本文件**不会**真的清空回收站、打开资源管理器或触发 UAC——
这类副作用全部通过 mock 验证；回收站删除只对工作区内临时文件执行。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import temp_dir  # noqa: E402

from qingfeng.core import winapi  # noqa: E402


class TestHumanSize(unittest.TestCase):
    def test_values(self) -> None:
        self.assertEqual(winapi.human_size(0), "0 B")
        self.assertEqual(winapi.human_size(512), "512 B")
        self.assertEqual(winapi.human_size(1023), "1023 B")
        self.assertEqual(winapi.human_size(1024), "1.0 KB")
        self.assertEqual(winapi.human_size(1536), "1.5 KB")
        self.assertEqual(winapi.human_size(1024 * 1024), "1.0 MB")
        self.assertEqual(winapi.human_size(1024 ** 3), "1.0 GB")
        self.assertEqual(winapi.human_size(int(2.5 * 1024 ** 3)), "2.5 GB")

    def test_negative_and_invalid(self) -> None:
        self.assertIn("B", winapi.human_size(-1))
        self.assertEqual(winapi.human_size("abc"), "abc")  # type: ignore[arg-type]


@unittest.skipUnless(os.name == "nt", "仅 Windows")
class TestSystemInfo(unittest.TestCase):
    def test_is_admin_returns_bool(self) -> None:
        self.assertIsInstance(winapi.is_admin(), bool)

    def test_drive_usage(self) -> None:
        free, total = winapi.drive_usage("C:")
        self.assertIsInstance(free, int)
        self.assertIsInstance(total, int)
        self.assertGreater(total, 0)
        self.assertGreaterEqual(free, 0)
        self.assertLessEqual(free, total)

    def test_drive_usage_unknown_drive(self) -> None:
        free, total = winapi.drive_usage("Q:")
        self.assertEqual((free, total), (0, 0))

    def test_recycle_bin_size(self) -> None:
        # 默认参数（C:）只断言类型与范围，不依赖机器上的回收站实际内容
        items, size = winapi.recycle_bin_size()
        self.assertIsInstance(items, int)
        self.assertIsInstance(size, int)
        self.assertGreaterEqual(items, 0)
        self.assertGreaterEqual(size, 0)
        # 不存在的盘符稳定返回 (0, 0)，不依赖机器状态
        self.assertEqual(winapi.recycle_bin_size("Q:"), (0, 0))
        # 语义约定：空字符串返回 (0, 0)（不猜测"当前驱动器"）
        self.assertEqual(winapi.recycle_bin_size(""), (0, 0))
        self.assertEqual(winapi.recycle_bin_size(None), (0, 0))  # type: ignore[arg-type]


@unittest.skipUnless(os.name == "nt", "仅 Windows")
class TestRecyclePaths(unittest.TestCase):
    def test_empty_list(self) -> None:
        self.assertEqual(winapi.recycle_paths([]), [])

    def test_recycle_temp_files(self) -> None:
        with temp_dir() as tmp:
            first = Path(tmp) / "recycle-a.txt"
            second = Path(tmp) / "recycle-b.txt"
            first.write_bytes(b"a")
            second.write_bytes(b"b")
            failures = winapi.recycle_paths([str(first), str(second)])
            if failures:
                self.skipTest(f"当前环境回收站不可用: {failures}")
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())

    def test_recycle_nonexistent_path_does_not_raise(self) -> None:
        missing = str(Path(os.environ.get("SystemDrive", "C:") + "\\") / "qingfeng-no-such-file-12345.tmp")
        failures = winapi.recycle_paths([missing])
        self.assertIsInstance(failures, list)


class TestWin32WrappersWithMocks(unittest.TestCase):
    def test_empty_recycle_bin_success_and_failure(self) -> None:
        fake_shell = mock.Mock()
        fake_shell.SHEmptyRecycleBinW.return_value = 0
        with mock.patch.object(winapi, "_api_ready", True), mock.patch.object(winapi, "_shell32", fake_shell):
            self.assertTrue(winapi.empty_recycle_bin())
        fake_shell.SHEmptyRecycleBinW.return_value = 0x80004005
        with mock.patch.object(winapi, "_api_ready", True), mock.patch.object(winapi, "_shell32", fake_shell):
            self.assertFalse(winapi.empty_recycle_bin())

    def test_empty_recycle_bin_handles_exception(self) -> None:
        fake_shell = mock.Mock()
        fake_shell.SHEmptyRecycleBinW.side_effect = OSError("mock")
        with mock.patch.object(winapi, "_api_ready", True), mock.patch.object(winapi, "_shell32", fake_shell):
            self.assertFalse(winapi.empty_recycle_bin())

    def test_restart_as_admin_success(self) -> None:
        fake_shell = mock.Mock()
        fake_shell.ShellExecuteW.return_value = 42
        with mock.patch.object(winapi, "_api_ready", True), mock.patch.object(winapi, "_shell32", fake_shell):
            self.assertTrue(winapi.restart_as_admin(["main.py", "--hidden"]))
        args = fake_shell.ShellExecuteW.call_args[0]
        self.assertEqual(args[1], "runas")
        self.assertIn("--hidden", args[3])

    def test_restart_as_admin_denied(self) -> None:
        fake_shell = mock.Mock()
        fake_shell.ShellExecuteW.return_value = 5  # 小于等于 32 = 失败
        with mock.patch.object(winapi, "_api_ready", True), mock.patch.object(winapi, "_shell32", fake_shell):
            self.assertFalse(winapi.restart_as_admin(["main.py"]))

    def test_open_in_explorer_calls_shell(self) -> None:
        fake_shell = mock.Mock()
        fake_shell.ShellExecuteW.return_value = 42
        with temp_dir() as tmp:
            with mock.patch.object(winapi, "_api_ready", True), mock.patch.object(
                winapi, "_shell32", fake_shell
            ):
                winapi.open_in_explorer(tmp)
                winapi.open_in_explorer(str(Path(tmp) / "some-file.txt"))
        self.assertEqual(fake_shell.ShellExecuteW.call_count, 2)
        self.assertEqual(fake_shell.ShellExecuteW.call_args_list[0][0][2], "explorer.exe")

    def test_shfileop_struct_fields(self) -> None:
        fields = [name for name, _ in winapi.SHFILEOPSTRUCTW._fields_]
        self.assertEqual(
            fields,
            [
                "hwnd",
                "wFunc",
                "pFrom",
                "pTo",
                "fFlags",
                "fAnyOperationsAborted",
                "hNameMappings",
                "lpszProgressTitle",
            ],
        )


class TestRelaunchCommand(unittest.TestCase):
    """提权重启命令：打包版绝不能把 exe 自身路径再塞进参数。"""

    def test_source_mode_includes_script_path(self) -> None:
        with mock.patch.object(sys, "argv", ["main.py", "--hidden"]):
            file, args = winapi.relaunch_command()
        self.assertEqual(file, sys.executable)
        self.assertEqual(args, ["main.py", "--hidden"])

    def test_source_mode_extra_args_override_tail(self) -> None:
        with mock.patch.object(sys, "argv", ["main.py", "--hidden"]):
            _file, args = winapi.relaunch_command(["--page", "space"])
        self.assertEqual(args, ["main.py", "--page", "space"])

    def test_frozen_mode_does_not_repeat_exe(self) -> None:
        exe = r"C:\Program Files\QingFeng\QingFengCleaner.exe"
        with mock.patch.object(winapi, "_is_frozen", return_value=True), mock.patch.object(
            sys, "executable", exe
        ), mock.patch.object(sys, "argv", [exe]):
            file, args = winapi.relaunch_command()
        self.assertEqual(file, exe)
        self.assertEqual(args, [])
        self.assertFalse(any(exe.lower() in arg.lower() for arg in args))

    def test_frozen_mode_extra_args(self) -> None:
        exe = r"C:\Program Files\QingFeng\QingFengCleaner.exe"
        with mock.patch.object(winapi, "_is_frozen", return_value=True), mock.patch.object(
            sys, "executable", exe
        ), mock.patch.object(sys, "argv", [exe]):
            file, args = winapi.relaunch_command(["--page", "space"])
        self.assertEqual(file, exe)
        self.assertEqual(args, ["--page", "space"])
        self.assertNotIn(exe, args)

    def test_empty_argv(self) -> None:
        with mock.patch.object(sys, "argv", []), mock.patch.object(winapi, "_is_frozen", return_value=False):
            _file, args = winapi.relaunch_command()
        self.assertEqual(args, [])
        with mock.patch.object(sys, "argv", []), mock.patch.object(winapi, "_is_frozen", return_value=True):
            _file, args = winapi.relaunch_command(["--page", "space"])
        self.assertEqual(args, ["--page", "space"])

    def test_restart_frozen_commandline_contains_exe_once(self) -> None:
        fake_shell = mock.Mock()
        fake_shell.ShellExecuteW.return_value = 42
        exe = r"C:\Program Files\QingFeng\QingFengCleaner.exe"
        with mock.patch.object(winapi, "_api_ready", True), mock.patch.object(
            winapi, "_shell32", fake_shell
        ), mock.patch.object(winapi, "_is_frozen", return_value=True), mock.patch.object(
            sys, "executable", exe
        ), mock.patch.object(sys, "argv", [exe]):
            self.assertTrue(winapi.restart_as_admin(["--page", "space"]))
        args = fake_shell.ShellExecuteW.call_args[0]
        self.assertEqual(args[1], "runas")
        self.assertEqual(args[2], exe)
        full_command = f"{args[2]} {args[3]}"
        self.assertEqual(full_command.lower().count(exe.lower()), 1)
        self.assertIn("--page", args[3])

    def test_restart_source_commandline_contains_script_once(self) -> None:
        fake_shell = mock.Mock()
        fake_shell.ShellExecuteW.return_value = 42
        with mock.patch.object(winapi, "_api_ready", True), mock.patch.object(
            winapi, "_shell32", fake_shell
        ), mock.patch.object(winapi, "_is_frozen", return_value=False), mock.patch.object(
            sys, "argv", ["main.py", "--hidden"]
        ):
            self.assertTrue(winapi.restart_as_admin())
        args = fake_shell.ShellExecuteW.call_args[0]
        full_command = f"{args[2]} {args[3]}"
        self.assertIn("main.py", args[3])
        self.assertIn("--hidden", args[3])
        self.assertEqual(full_command.lower().count("main.py"), 1)


if __name__ == "__main__":
    unittest.main()
