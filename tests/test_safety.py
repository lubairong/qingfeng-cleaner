"""路径安全守卫单元测试（tests/test_safety.py）。

重点覆盖 docs/ARCHITECTURE.md §5 的保护路径底线、大小写不敏感、
``..`` 路径穿越、符号链接绕过、以及「误报不安全」的兜底行为。
"""

from __future__ import annotations

import ntpath
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import temp_dir  # noqa: E402

from qingfeng.core import safety  # noqa: E402
from qingfeng.core.safety import (  # noqa: E402
    UnsafePathError,
    assert_deletable,
    expand,
    is_protected,
    within_root,
)


def _env(name: str) -> str:
    return os.environ.get(name, "")


class TestExpand(unittest.TestCase):
    def test_expand_environment_variable(self) -> None:
        temp = _env("TEMP")
        if not temp:
            self.skipTest("TEMP 未设置")
        self.assertEqual(ntpath.normcase(expand("%TEMP%")), ntpath.normcase(temp))

    def test_expand_normalizes_dotdot(self) -> None:
        self.assertEqual(
            ntpath.normcase(expand(r"C:\Windows\Temp\..\System32")),
            ntpath.normcase(r"C:\Windows\System32"),
        )

    def test_expand_trailing_separator_and_empty(self) -> None:
        self.assertEqual(expand(""), "")
        self.assertEqual(ntpath.normcase(expand("C:\\Windows\\")), ntpath.normcase("C:\\Windows"))

    def test_expand_tilde(self) -> None:
        expanded = expand("~")
        self.assertTrue(os.path.isabs(expanded))
        self.assertNotIn("~", expanded)


class TestProtectedPaths(unittest.TestCase):
    """§5 列出的根级路径必须全部被保护。"""

    def _assert_protected(self, path: str) -> None:
        with self.subTest(path=path):
            self.assertTrue(is_protected(path), f"{path} 应受保护")
            with self.assertRaises(UnsafePathError):
                assert_deletable(path)

    def test_shipped_protection_table(self) -> None:
        cases = [
            "C:\\",
            r"%WINDIR%",
            r"%ProgramFiles%",
            r"%ProgramFiles(x86)%",
            r"%ProgramData%",
            r"%SYSTEMDRIVE%\Users",
            r"%USERPROFILE%",
            r"%USERPROFILE%\AppData",
            r"%WINDIR%\System32",
            r"%WINDIR%\SysWOW64",
            r"%WINDIR%\WinSxS",
            r"%SYSTEMDRIVE%\Recovery",
            r"%SYSTEMDRIVE%\$Recycle.Bin",
            r"%SYSTEMDRIVE%\System Volume Information",
            r"%SYSTEMDRIVE%\Boot",
            r"%SYSTEMDRIVE%\EFI",
            r"%USERPROFILE%\Documents",
            r"%USERPROFILE%\Desktop",
            r"%USERPROFILE%\Downloads",
            r"%USERPROFILE%\Pictures",
            r"%USERPROFILE%\Videos",
            r"%USERPROFILE%\Music",
            r"%USERPROFILE%\OneDrive",
        ]
        for case in cases:
            self._assert_protected(case)

    def test_system_critical_children(self) -> None:
        for case in (
            r"%WINDIR%\System32\config",
            r"%WINDIR%\System32\drivers",
            r"%WINDIR%\Fonts",
            r"%WINDIR%\Microsoft.NET",
            r"%WINDIR%\CSC",
            r"%ProgramData%\SomeApplication",
            r"%USERPROFILE%\Documents\report.docx",
            r"%USERPROFILE%\Desktop\a.txt",
            r"%USERPROFILE%\Downloads\setup.exe",
        ):
            self._assert_protected(case)

    def test_root_files_protected(self) -> None:
        drive = _env("SystemDrive") or "C:"
        for name in ("pagefile.sys", "hiberfil.sys", "swapfile.sys", "bootmgr"):
            self._assert_protected(ntpath.join(f"{drive}\\", name))

    def test_case_insensitive(self) -> None:
        self.assertTrue(is_protected(r"c:\windows\system32"))
        self.assertTrue(is_protected(r"C:\WINDOWS\SysWOW64"))
        self._assert_protected(r"c:\\WINDOWS\\system32")

    def test_trailing_separator(self) -> None:
        self.assertTrue(is_protected("C:\\Windows\\System32\\"))
        self.assertTrue(is_protected(os.environ["USERPROFILE"] + "\\"))

    def test_empty_or_relative_paths_are_protected(self) -> None:
        for value in ("", "   ", None):
            with self.subTest(value=value):
                self.assertTrue(is_protected(value))  # type: ignore[arg-type]
        self.assertTrue(is_protected("Windows\\System32"))
        with self.assertRaises(UnsafePathError):
            assert_deletable("")
        with self.assertRaises(UnsafePathError):
            assert_deletable("relative\\path")

    def test_dotdot_traversal_blocked(self) -> None:
        with self.assertRaises(UnsafePathError):
            assert_deletable(r"C:\Windows\Temp\..\System32")
        with self.assertRaises(UnsafePathError):
            assert_deletable(r"C:\Windows\Temp\Temp\..\..\System32")
        self.assertTrue(is_protected(r"c:\windows\temp\..\syswow64"))

    def test_other_drive_roots_protected(self) -> None:
        for letter in "CDEFG":
            root = f"{letter}:\\"
            if os.path.exists(root):
                self._assert_protected(root)

    def test_allowed_cache_paths(self) -> None:
        allowed = [
            r"%TEMP%\some-file.tmp",
            r"%WINDIR%\Temp\dummy.tmp",
            r"%WINDIR%\Logs\CBS\dummy.log",
            r"%WINDIR%\Prefetch\dummy.pf",
            r"%WINDIR%\SoftwareDistribution\Download\dummy.cab",
            r"%WINDIR%\SoftwareDistribution\DeliveryOptimization\dummy",
            r"%LOCALAPPDATA%\Microsoft\Windows\WER\ReportQueue\dummy",
            r"%PROGRAMDATA%\Microsoft\Windows\WER\ReportQueue\dummy",
            r"%PROGRAMDATA%\Intel\DSA\Downloads\gfx_win_101.7092.exe",
            r"%PROGRAMDATA%\SomeVendor\Update\Downloads\setup.msi",
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Cache\dummy",
            r"%APPDATA%\Microsoft\Windows\Recent\dummy.lnk",
            r"%LOCALAPPDATA%\Packages\SomeApp_8wekyb3d8bbwe\LocalCache\Cache\dummy",
        ]
        for path in allowed:
            with self.subTest(path=path):
                self.assertFalse(is_protected(path), f"{path} 应允许清理")
                assert_deletable(path)  # 不应抛异常

    @unittest.skipUnless(os.name == "nt", "仅 Windows 检查符号链接/junction 绕过")
    def test_symlink_bypass_blocked(self) -> None:
        with temp_dir() as tmp:
            link = os.path.join(tmp, "fake-temp")
            try:
                os.symlink(os.environ["WINDIR"], link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("当前环境无法创建符号链接（需要开发者模式/管理员）")
            try:
                self.assertTrue(is_protected(link))
                with self.assertRaises(UnsafePathError):
                    assert_deletable(link)
            finally:
                try:
                    os.unlink(link)
                except OSError:
                    pass

    def test_empty_path_in_protection_table(self) -> None:
        self.assertTrue(safety.is_protected(None))  # type: ignore[arg-type]


class TestWeChatDeepAllow(unittest.TestCase):
    """唯一深目录例外（Lead 批准）：仅放行微信经典版缓存目录。

    规格：``%USERPROFILE%\\Documents\\WeChat Files\\<账号>\\FileStorage\\Cache``
    及其内部文件；Documents 下其余一切路径仍必须受保护。
    """

    def setUp(self) -> None:
        profile = os.environ.get("USERPROFILE", "")
        if not profile:
            self.skipTest("USERPROFILE 未设置")
        self.base = ntpath.join(profile, "Documents", "WeChat Files")
        self.account = self.base + "\\wxid_demo_123456"
        self.cache = self.account + "\\FileStorage\\Cache"

    def test_cache_directory_and_contents_allowed(self) -> None:
        allowed = [
            self.cache,
            self.cache + "\\some-file.dat",
            self.cache + "\\sub\\nested.tmp",
        ]
        for path in allowed:
            with self.subTest(path=path):
                self.assertFalse(is_protected(path), f"{path} 应允许清理")
                assert_deletable(path)  # 不应抛异常

    def test_allow_is_case_insensitive(self) -> None:
        self.assertFalse(is_protected(self.cache.upper()))
        self.assertFalse(is_protected(self.cache.title()))

    def test_other_wechat_subdirs_rejected(self) -> None:
        rejected = [
            self.base,  # WeChat Files 本身
            self.account,  # 账号目录本身
            self.account + "\\FileStorage",  # 缓存父目录
            self.account + "\\FileStorage\\MsgAttach",
            self.account + "\\Msg",
            self.account + "\\FileStorage\\Cache2",
            self.account + "\\FileStorage\\Cachex\\a.dat",
        ]
        for path in rejected:
            with self.subTest(path=path):
                self.assertTrue(is_protected(path), f"{path} 应继续受保护")
                with self.assertRaises(UnsafePathError):
                    assert_deletable(path)

    def test_documents_root_still_rejected(self) -> None:
        profile = os.environ["USERPROFILE"]
        self._assert_rejected(ntpath.join(profile, "Documents"))
        self._assert_rejected(ntpath.join(profile, "Documents", "report.docx"))

    def test_dotdot_escape_from_cache_rejected(self) -> None:
        # Cache\..\..\..\Desktop → Documents\WeChat Files\Desktop（仍在文档树内）
        self._assert_rejected(self.cache + "\\..\\..\\..\\Desktop")
        # 更深一层穿越回用户桌面
        self._assert_rejected(self.cache + "\\..\\..\\..\\..\\Desktop")
        # 回退到 FileStorage / Msg 也不允许
        self._assert_rejected(self.cache + "\\..\\FileStorage")
        self._assert_rejected(self.cache + "\\..\\..\\Msg")

    def _assert_rejected(self, path: str) -> None:
        self.assertTrue(is_protected(path), f"{path} 应受保护")
        with self.assertRaises(UnsafePathError):
            assert_deletable(path)


class TestWithinRoot(unittest.TestCase):
    def test_equal_and_inside(self) -> None:
        self.assertTrue(within_root(r"C:\Users\a\Temp", r"C:\Users\a\Temp"))
        self.assertTrue(within_root(r"C:\Users\a\Temp\x\y", r"C:\Users\a\Temp"))
        self.assertTrue(within_root(r"c:\users\A\temp\x", r"C:\Users\a\Temp"))

    def test_outside(self) -> None:
        self.assertFalse(within_root(r"C:\Users\a\TempX", r"C:\Users\a\Temp"))
        self.assertFalse(within_root(r"C:\Users\b\Temp", r"C:\Users\a\Temp"))
        self.assertFalse(within_root(r"D:\Temp", r"C:\Temp"))

    def test_traversal_escape(self) -> None:
        self.assertFalse(within_root(r"C:\Users\a\Temp\..\..\Windows", r"C:\Users\a\Temp"))
        self.assertFalse(within_root(r"C:\Users\a\Temp\..\Other", r"C:\Users\a\Temp"))

    def test_empty_inputs(self) -> None:
        self.assertFalse(within_root("", r"C:\Temp"))
        self.assertFalse(within_root(r"C:\Temp", ""))


class TestProtectedTableContents(unittest.TestCase):
    def test_protection_table_not_empty(self) -> None:
        table = safety.protected_paths()
        self.assertGreater(len(table), 20)
        joined = "\n".join(sorted(table))
        for needle in ("windows", "program files", "programdata", "users"):
            self.assertIn(needle, joined.lower())


if __name__ == "__main__":
    unittest.main()
