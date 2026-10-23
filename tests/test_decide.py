"""v2 决策助手与迁移单元测试（tests/test_decide.py）。

迁移测试全部在工作区临时目录内进行；"异盘"通过 mock 盘符/磁盘类型/剩余空间模拟，
不依赖机器上是否存在 D:/E: 可写目录。
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import temp_dir  # noqa: E402

from qingfeng.core import decide, safety, space  # noqa: E402


def _write_file(path: Path, size: int, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"y" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _review_item_for(path: str, *, is_dir: bool = False) -> decide.ReviewItem:
    """按真实分类构造一个 ReviewItem（供 action_policy 测试使用）。"""
    now = time.time()
    category, advice, risk, reason, group = decide.classify(
        path, size=0, mtime=now, is_dir=is_dir, age_days=0
    )
    return decide.ReviewItem(
        path=path,
        name=path.rsplit("\\", 1)[-1],
        is_dir=is_dir,
        size=0,
        mtime=now,
        age_days=0,
        ext="" if is_dir else os.path.splitext(path)[1].lower().lstrip("."),
        category=category,
        advice=advice,
        reason=reason,
        risk=risk,
        group=group,
    )


def _fake_cross_drive(dest_root: str):
    """把 dest_root 伪装成 D: 盘、其它路径保持真实盘符。"""
    dest_norm = os.path.normcase(str(dest_root))
    real = decide._drive_of

    def fake(path: str) -> str:
        if os.path.normcase(str(path)).startswith(dest_norm):
            return "D:"
        return real(path)

    return fake


class _MovePatches:
    """move_many 测试用的公共 patch 组合（模拟异盘固定磁盘 + 充足空间）。"""

    def __init__(self, dest_root: str, free_bytes: int = 10 * 1024 ** 3):
        self.dest_root = dest_root
        self.free_bytes = free_bytes

    def __enter__(self):
        self._patches = [
            mock.patch.object(decide, "_drive_of", side_effect=_fake_cross_drive(self.dest_root)),
            mock.patch.object(decide, "_is_fixed_drive", return_value=True),
            mock.patch.object(decide, "_dest_free_bytes", return_value=self.free_bytes),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc_info):
        for patch in reversed(self._patches):
            patch.stop()


class TestHeuristicsAndClassify(unittest.TestCase):
    def test_heuristics_structure(self) -> None:
        data = decide.heuristics()
        self.assertEqual(data.get("version"), 1)
        categories = data.get("categories") or []
        self.assertGreaterEqual(len(categories), 14)
        by_key = {str(category.get("key")): category for category in categories}
        self.assertEqual(by_key["installer"].get("match"), "all")
        self.assertIn(".exe", [str(item).lower() for item in by_key["installer"]["exts"]])
        self.assertGreaterEqual(len(data.get("age_rules") or []), 3)
        self.assertGreaterEqual(len(data.get("never_touch") or []), 18)

    def test_installer_requires_all_conditions(self) -> None:
        # Downloads 里的 .exe：扩展名 + 路径上下文都命中 → 安装包、可安全清理
        category, advice, risk, reason, group = decide.classify(
            r"C:\Users\me\Downloads\setup.exe",
            size=600 * 1024 ** 2,
            mtime=time.time() - 100 * 86400,
            is_dir=False,
            age_days=100,
        )
        self.assertEqual(category, "installer")
        self.assertEqual(advice, "safe_delete")
        self.assertEqual(group, "installer")
        self.assertIn("100", reason)  # {age} 占位符已渲染
        # 随机目录里的 .exe 不能因为扩展名就被判"可删"
        category2, advice2, _risk2, _reason2, _group2 = decide.classify(
            r"E:\tools\some-app.exe",
            size=600 * 1024 ** 2,
            mtime=time.time(),
            is_dir=False,
            age_days=0,
        )
        self.assertNotEqual(category2, "installer")
        self.assertNotEqual(advice2, "safe_delete")

    def test_media_and_keep_categories(self) -> None:
        video = decide.classify(r"E:\movies\a.mp4", size=500 * 1024 ** 2, mtime=time.time(), is_dir=False, age_days=1)
        self.assertEqual(video[0], "video")
        self.assertEqual(video[1], "review")
        model = decide.classify(r"E:\models\x.gguf", size=4 * 1024 ** 3, mtime=time.time(), is_dir=False, age_days=1)
        self.assertEqual(model[1], "keep")
        self.assertEqual(model[4], "keep")
        database = decide.classify(r"E:\misc\data.sqlite", size=300 * 1024 ** 2, mtime=time.time(), is_dir=False, age_days=3)
        self.assertEqual(database[1], "keep")

    def test_never_touch_beats_categories(self) -> None:
        category, advice, risk, reason, group = decide.classify(
            r"C:\Windows\Media\theme.mp4", size=9 * 1024 ** 3, mtime=time.time(), is_dir=False, age_days=5
        )
        self.assertEqual(advice, "keep")
        self.assertEqual(risk, "danger")
        self.assertEqual(group, "keep")
        self.assertIn("请勿手动删除", reason)
        self.assertEqual(category, "keep")

    def test_age_rule_fallback(self) -> None:
        category, advice, _risk, reason, group = decide.classify(
            r"E:\misc\unknown.xyz",
            size=2 * 1024 ** 3,
            mtime=time.time() - 400 * 86400,
            is_dir=False,
            age_days=400,
        )
        self.assertEqual(category, "large-misc")
        self.assertEqual(advice, "review")
        self.assertEqual(group, "large-misc")
        self.assertTrue(reason)

    def test_large_misc_fallback(self) -> None:
        category, advice, _risk, _reason, group = decide.classify(
            r"E:\misc\nothing.bin", size=100 * 1024 ** 2, mtime=time.time(), is_dir=False, age_days=1
        )
        self.assertEqual(category, "program")  # .bin 命中已安装程序扩展名
        fresh = decide.classify(
            r"E:\misc\blob.zzz", size=100 * 1024 ** 2, mtime=time.time(), is_dir=False, age_days=1
        )
        self.assertEqual(fresh[0], "large-misc")
        self.assertEqual(fresh[4], "large-misc")


class TestReviewCollection(unittest.TestCase):
    def test_collect_and_group(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            _write_file(root / "movie.mp4", 300 * 1024 * 2, mtime=time.time() - 10 * 86400)
            _write_file(root / "node_modules" / "pkg" / "a.js", 300 * 1024 * 2)
            _write_file(root / "tiny.txt", 10)
            index = space.build_index(str(root), min_child_size=64 * 1024, max_nodes=5000)
            items = decide.collect_review_items(index, min_size=64 * 1024)
            paths = {item.path for item in items}
            self.assertTrue(any(path.endswith("movie.mp4") for path in paths))
            self.assertTrue(any(path.endswith("node_modules") for path in paths))
            self.assertFalse(any(path.endswith("tiny.txt") for path in paths))
            groups = decide.group_review_items(items)
            keys = [group.key for group in groups]
            self.assertIn("video", keys)
            self.assertIn("devcache", keys)
            for group in groups:
                self.assertEqual(group.total_bytes, sum(item.size for item in group.items))
                self.assertTrue(group.label)
            # 分组顺序符合契约
            ordered = [key for key in decide.GROUP_ORDER if key in keys]
            self.assertEqual(keys, ordered)

    def test_collect_limit_sorted(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            for index in range(6):
                _write_file(root / f"v{index}.mp4", (index + 1) * 1024 * 128)
            index = space.build_index(str(root), min_child_size=64 * 1024, max_nodes=5000)
            items = decide.collect_review_items(index, min_size=64 * 1024, limit=3)
            self.assertEqual(len(items), 3)
            sizes = [item.size for item in items]
            self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_root_level_system_files_filtered(self) -> None:
        """hiberfil/pagefile/swapfile/DumpStack.log* 不应混入大文件决策表。"""

        def file_node(path: str, size: int) -> space.SpaceNode:
            return space.SpaceNode(
                path=path,
                name=path.rsplit("\\", 1)[-1],
                is_dir=False,
                size=size,
                file_count=1,
                mtime=time.time(),
                children=[],
            )

        big = 300 * 1024 * 1024
        root = space.SpaceNode(
            path="C:\\",
            name="C:",
            is_dir=True,
            size=big * 5,
            file_count=5,
            mtime=time.time(),
            children=[
                file_node("C:\\hiberfil.sys", 6 * 1024 ** 3),
                file_node("C:\\pagefile.sys", 2 * 1024 ** 3),
                file_node("C:\\swapfile.sys", big),
                file_node("C:\\DumpStack.log.tmp", big),
                file_node("C:\\DumpStack.log", big),
                file_node("C:\\Users\\me\\movie.mp4", big),
            ],
        )
        index = space.SpaceIndex(
            root="C:\\",
            total_bytes=big * 10,
            accessible_bytes=big * 10,
            unaccounted_bytes=0,
            system_files={"hiberfil.sys": 6 * 1024 ** 3},
            protected_hint={},
            duration=0.0,
            built_at=time.time(),
            root_node=root,
        )
        items = decide.collect_review_items(index, min_size=100 * 1024 ** 2)
        names = {os.path.basename(item.path).lower() for item in items}
        for blocked in ("hiberfil.sys", "pagefile.sys", "swapfile.sys", "dumpstack.log.tmp", "dumpstack.log"):
            self.assertNotIn(blocked, names)
        self.assertIn("movie.mp4", names)

    def test_review_group_dataclass(self) -> None:
        now = time.time()
        items = [
            decide.ReviewItem(
                path=r"E:\a\setup.exe",
                name="setup.exe",
                is_dir=False,
                size=1000,
                mtime=now,
                age_days=5,
                ext="exe",
                category="installer",
                advice="safe_delete",
                reason="安装包",
                risk="caution",
                group="installer",
            ),
            decide.ReviewItem(
                path=r"E:\b\m.mp4",
                name="m.mp4",
                is_dir=False,
                size=2000,
                mtime=now,
                age_days=6,
                ext="mp4",
                category="video",
                advice="review",
                reason="视频",
                risk="caution",
                group="video",
            ),
        ]
        groups = decide.group_review_items(items)
        self.assertEqual([group.key for group in groups], ["installer", "video"])
        installer = groups[0]
        self.assertEqual(installer.total_bytes, 1000)
        self.assertIn("放心清理", installer.advice)


class TestMoveMany(unittest.TestCase):
    def setUp(self) -> None:
        self._src_tmp = temp_dir()
        self._dst_tmp = temp_dir()
        self.src = Path(self._src_tmp.name)
        self.dst = Path(self._dst_tmp.name)

    def tearDown(self) -> None:
        self._src_tmp.cleanup()
        self._dst_tmp.cleanup()

    def test_move_file_success(self) -> None:
        source = self.src / "big.bin"
        _write_file(source, 256 * 1024)
        with _MovePatches(str(self.dst)):
            result = decide.move_many([str(source)], str(self.dst))
        self.assertEqual(result.failed, [])
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.moved, 1)
        self.assertEqual(result.moved_bytes, 256 * 1024)
        self.assertFalse(source.exists())
        expected = decide._dest_path_for(str(source), str(self.dst))
        target = Path(expected)
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"y" * (256 * 1024))
        self.assertEqual(result.dest_root, str(self.dst))

    def test_move_directory_success(self) -> None:
        source = self.src / "cache-dir"
        _write_file(source / "a.bin", 1024)
        _write_file(source / "nested" / "b.bin", 2048)
        with _MovePatches(str(self.dst)):
            result = decide.move_many([str(source)], str(self.dst))
        self.assertEqual(result.moved, 1)
        self.assertFalse(source.exists())
        target = Path(decide._dest_path_for(str(source), str(self.dst)))
        self.assertTrue(target.is_dir())
        self.assertTrue((target / "nested" / "b.bin").exists())

    def test_conflict_does_not_overwrite(self) -> None:
        source = self.src / "same.bin"
        _write_file(source, 4096)
        expected = Path(decide._dest_path_for(str(source), str(self.dst)))
        _write_file(expected, 8)
        with _MovePatches(str(self.dst)):
            result = decide.move_many([str(source)], str(self.dst))
        self.assertEqual(result.moved, 1)
        self.assertFalse(source.exists())
        self.assertEqual(expected.read_bytes(), b"y" * 8)  # 原文件未被覆盖
        alternates = [
            path
            for path in expected.parent.iterdir()
            if path.name.startswith(expected.stem) and path.name != expected.name
        ]
        self.assertEqual(len(alternates), 1)
        self.assertEqual(alternates[0].stat().st_size, 4096)

    def test_insufficient_space_keeps_source(self) -> None:
        source = self.src / "big.bin"
        _write_file(source, 128 * 1024)
        with _MovePatches(str(self.dst), free_bytes=1024):
            result = decide.move_many([str(source)], str(self.dst))
        self.assertEqual(result.moved, 0)
        self.assertTrue(any("空间不足" in reason for _path, reason in result.failed))
        self.assertTrue(source.exists())

    def test_protected_source_rejected(self) -> None:
        protected = Path(os.environ.get("SystemDrive", "C:") + "\\Windows\\System32\\kernel32.dll")
        if not protected.exists():
            self.skipTest("kernel32.dll 不存在")
        with _MovePatches(str(self.dst)):
            result = decide.move_many([str(protected)], str(self.dst))
        self.assertEqual(result.moved, 0)
        self.assertTrue(
            any(
                ("程序安装" in reason) or ("系统目录" in reason) or ("受保护" in reason)
                for _path, reason in result.skipped
            )
        )
        self.assertTrue(protected.exists())

    def test_same_drive_rejected(self) -> None:
        source = self.src / "a.bin"
        _write_file(source, 1024)
        # 不 mock：源与目标都在 E: → 同盘拒绝
        result = decide.move_many([str(source)], str(self.dst))
        self.assertEqual(result.moved, 0)
        self.assertTrue(any("同盘" in reason for _path, reason in result.skipped))
        self.assertTrue(source.exists())

    def test_dest_inside_source_rejected(self) -> None:
        source = self.src / "outer"
        _write_file(source / "a.bin", 1024)
        inner = source / "inner"
        inner.mkdir()
        with _MovePatches(str(inner)):
            result = decide.move_many([str(source)], str(inner))
        self.assertEqual(result.moved, 0)
        self.assertTrue(any("内部" in reason for _path, reason in result.skipped))
        self.assertTrue(source.exists())

    def test_cancel_before_move(self) -> None:
        source = self.src / "a.bin"
        _write_file(source, 1024)
        with _MovePatches(str(self.dst)):
            result = decide.move_many([str(source)], str(self.dst), cancel=lambda: True)
        self.assertEqual(result.moved, 0)
        self.assertTrue(source.exists())
        self.assertTrue(any("已取消" in reason for _path, reason in result.skipped))

    def test_cancel_after_first_item(self) -> None:
        first = self.src / "one.bin"
        second = self.src / "two.bin"
        _write_file(first, 1024)
        _write_file(second, 1024)
        state = {"cancel": False}

        def progress(path: str, done: int, total: int) -> None:
            if done >= 1:
                state["cancel"] = True

        with _MovePatches(str(self.dst)):
            result = decide.move_many(
                [str(first), str(second)], str(self.dst), progress=progress, cancel=lambda: state["cancel"]
            )
        self.assertEqual(result.moved, 1)
        self.assertEqual(sum(1 for _ in result.skipped), 1)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())

    def test_dest_root_missing_or_invalid(self) -> None:
        source = self.src / "a.bin"
        _write_file(source, 1024)
        result = decide.move_many([str(source)], str(self.dst / "no-such-dir"))
        self.assertEqual(result.moved, 0)
        self.assertTrue(result.failed)
        self.assertTrue(source.exists())
        result_empty = decide.move_many([str(source)], "")
        self.assertEqual(result_empty.moved, 0)
        self.assertTrue(result_empty.failed)

    def test_dest_not_writable(self) -> None:
        source = self.src / "a.bin"
        _write_file(source, 1024)
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        if not program_files.exists():
            self.skipTest("Program Files 不存在")
        if decide._is_writable_dir(str(program_files)):
            self.skipTest("当前进程可写 Program Files，无法验证不可写分支")
        with mock.patch.object(decide, "_drive_of", side_effect=_fake_cross_drive(str(program_files))), mock.patch.object(
            decide, "_is_fixed_drive", return_value=True
        ), mock.patch.object(decide, "_dest_free_bytes", return_value=10 ** 12):
            result = decide.move_many([str(source)], str(program_files))
        self.assertEqual(result.moved, 0)
        self.assertTrue(any("不可写" in reason for _path, reason in result.failed))
        self.assertTrue(source.exists())

    def test_move_result_duration(self) -> None:
        result = decide.move_many([], str(self.dst))
        self.assertGreaterEqual(result.duration, 0.0)
        self.assertEqual(result.moved, 0)


class TestSuggestTargets(unittest.TestCase):
    def test_root_writable_and_fallback(self) -> None:
        def fake_write(path: str) -> bool:
            normalized = os.path.normcase(path)
            return normalized in (
                os.path.normcase(r"E:\QingFengMoved"),
                os.path.normcase(r"D:\Download\QingFengMoved"),
            )

        with mock.patch.object(decide, "_enumerate_fixed_drives", return_value=["D:", "E:"]), mock.patch.object(
            decide.winapi, "drive_usage", side_effect=lambda drive: (100 * 1024 ** 3, 500 * 1024 ** 3) if drive == "D:" else (200 * 1024 ** 3, 500 * 1024 ** 3)
        ), mock.patch.object(decide, "_list_target_subdirs", return_value=[r"D:\Download"]), mock.patch.object(
            decide, "_try_write_dir", side_effect=fake_write
        ):
            options = decide.suggest_targets("C:")
        self.assertEqual([option.drive for option in options], ["E:", "D:"])
        self.assertTrue(options[0].writable)
        self.assertEqual(options[0].path, r"E:\QingFengMoved")
        self.assertEqual(options[0].note, "")
        self.assertTrue(options[1].writable)
        self.assertEqual(options[1].path, r"D:\Download\QingFengMoved")
        self.assertIn("根目录", options[1].note)

    def test_not_writable(self) -> None:
        with mock.patch.object(decide, "_enumerate_fixed_drives", return_value=["D:"]), mock.patch.object(
            decide.winapi, "drive_usage", return_value=(0, 0)
        ), mock.patch.object(decide, "_list_target_subdirs", return_value=[]), mock.patch.object(
            decide, "_try_write_dir", return_value=False
        ):
            options = decide.suggest_targets("C:")
        self.assertEqual(len(options), 1)
        self.assertFalse(options[0].writable)
        self.assertIn("管理员", options[0].note)

    def test_exclude_drive(self) -> None:
        with mock.patch.object(decide, "_enumerate_fixed_drives", return_value=["C:", "D:", "E:"]), mock.patch.object(
            decide.winapi, "drive_usage", return_value=(1024, 2048)
        ), mock.patch.object(decide, "_list_target_subdirs", return_value=[]), mock.patch.object(
            decide, "_try_write_dir", return_value=True
        ):
            options = decide.suggest_targets("E:")
        self.assertEqual([option.drive for option in options], ["C:", "D:"])


class TestActionPolicy(unittest.TestCase):
    """§3.4 授权矩阵。"""

    def _policy(self, path: str, *, is_dir: bool = False):
        item = _review_item_for(path, is_dir=is_dir)
        return decide.action_policy(item)

    def test_program_files_denied(self) -> None:
        edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        policy = self._policy(edge)
        self.assertFalse(policy.can_delete)
        self.assertFalse(policy.can_move)
        self.assertIn("程序安装目录", policy.deny_reason)
        self.assertIn("卸载", policy.deny_reason)

    def test_windows_denied(self) -> None:
        policy = self._policy(r"C:\Windows\System32\kernel32.dll")
        self.assertFalse(policy.can_delete)
        self.assertFalse(policy.can_move)
        self.assertIn("系统目录", policy.deny_reason)

    def test_programdata_installer_downloads_allowed(self) -> None:
        policy = self._policy(r"C:\ProgramData\Intel\DSA\Downloads\gfx_win_101.7092.exe")
        self.assertTrue(policy.can_delete)
        self.assertFalse(policy.can_move)
        # safety 第二道闸也要放行（v2 授权矩阵的安全例外）
        self.assertFalse(safety.is_protected(r"C:\ProgramData\Intel\DSA\Downloads\gfx_win_101.7092.exe"))

    def test_programdata_other_denied(self) -> None:
        policy = self._policy(r"C:\ProgramData\Microsoft\Windows\WER\ReportQueue\a.dmp")
        self.assertFalse(policy.can_delete)
        self.assertFalse(policy.can_move)

    def test_user_dirs_allowed(self) -> None:
        for path in (
            r"C:\Users\me\AppData\Local\Temp\big.tmp",
            r"C:\Users\me\Videos\movie.mp4",
        ):
            with self.subTest(path=path):
                policy = self._policy(path)
                self.assertTrue(policy.can_delete)
                self.assertTrue(policy.can_move)

    def test_appdata_packages_denied(self) -> None:
        policy = self._policy(r"C:\Users\me\AppData\Local\Packages\SomeApp\LocalCache\big.dat")
        self.assertFalse(policy.can_delete)
        self.assertFalse(policy.can_move)
        self.assertIn("Packages", policy.deny_reason)

    def test_special_dirs_denied(self) -> None:
        for path in (
            r"C:\$Recycle.Bin\S-1-5-21\file.bin",
            r"C:\System Volume Information\{guid}\x",
            r"C:\Recovery\WindowsRE\x.wim",
            r"C:\EFI\Microsoft\bootmgfw.efi",
            r"C:\Boot\BCD",
            r"C:\hiberfil.sys",
        ):
            with self.subTest(path=path):
                policy = self._policy(path)
                self.assertFalse(policy.can_delete)
                self.assertFalse(policy.can_move)

    def test_non_system_drive_allowed(self) -> None:
        policy = self._policy(r"D:\media\movie.mkv")
        self.assertTrue(policy.can_delete)
        self.assertTrue(policy.can_move)


class TestReviewPruningAndDowngrade(unittest.TestCase):
    def _build_index(self) -> space.SpaceIndex:
        def node(path: str, size: int, *, is_dir: bool = False, children=None) -> space.SpaceNode:
            return space.SpaceNode(
                path=path,
                name=path.rsplit("\\", 1)[-1],
                is_dir=is_dir,
                size=size,
                file_count=1 if not is_dir else len(children or []),
                mtime=time.time(),
                children=list(children or []),
            )

        edge_exe = node(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", 1400)
        edge_app = node(
            r"C:\Program Files (x86)\Microsoft\Edge\Application",
            1400,
            is_dir=True,
            children=[edge_exe],
        )
        edge_root = node(
            r"C:\Program Files (x86)\Microsoft\Edge",
            1400,
            is_dir=True,
            children=[edge_app],
        )
        pf = node(r"C:\Program Files (x86)", 1400, is_dir=True, children=[edge_root])

        pkg_file = node(r"C:\Users\me\projects\node_modules\pkg\a.js", 600)
        modules = node(
            r"C:\Users\me\projects\node_modules",
            600,
            is_dir=True,
            children=[pkg_file],
        )
        projects = node(r"C:\Users\me\projects", 600, is_dir=True, children=[modules])
        me = node(r"C:\Users\me", 600, is_dir=True, children=[projects])
        users = node(r"C:\Users", 600, is_dir=True, children=[me])

        root = node("C:\\", 2000, is_dir=True, children=[pf, users])
        return space.SpaceIndex(
            root="C:\\",
            total_bytes=2000,
            accessible_bytes=2000,
            unaccounted_bytes=0,
            system_files={},
            protected_hint={},
            duration=0.1,
            built_at=time.time(),
            root_node=root,
        )

    def test_prune_and_downgrade(self) -> None:
        index = self._build_index()
        items = decide.collect_review_items(index, min_size=100)
        paths = [item.path for item in items]
        self.assertIn(r"C:\Program Files (x86)\Microsoft\Edge\Application", paths)
        self.assertIn(r"C:\Users\me\projects\node_modules", paths)
        # 父目录纳入后，其子项必须被剪掉（不重复计数、不冲突）
        self.assertNotIn(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", paths)
        self.assertNotIn(r"C:\Users\me\projects\node_modules\pkg\a.js", paths)
        # Program Files 被授权矩阵降级为 keep/danger
        edge = next(item for item in items if item.path.endswith("Edge\\Application"))
        self.assertEqual(edge.advice, "keep")
        self.assertEqual(edge.risk, "danger")
        self.assertIn("程序安装目录", edge.reason)
        self.assertEqual(edge.group, "keep")
        # 用户目录里的缓存目录保持可清理
        modules = next(item for item in items if item.path.endswith("node_modules"))
        self.assertEqual(modules.advice, "review")
        self.assertEqual(modules.group, "devcache")
        # 分组合计 = 条目合计（无父子重复计数）
        groups = decide.group_review_items(items)
        self.assertEqual(sum(group.total_bytes for group in groups), sum(item.size for item in items))
        # 降级项必须仍在 keep 组里（界面要能看到"为什么不能删"）
        keep_group = next(group for group in groups if group.key == "keep")
        keep_paths = {item.path for item in keep_group.items}
        self.assertIn(r"C:\Program Files (x86)\Microsoft\Edge\Application", keep_paths)
        self.assertEqual(keep_group.advice, "不要删")


class TestDeleteMany(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reject_program_files(self) -> None:
        blocked = r"C:\Program Files\SomeApp\app.exe"
        result = decide.delete_many([blocked])
        self.assertEqual(result.freed_bytes, 0)
        self.assertTrue(result.failed)
        self.assertTrue(any("程序安装目录" in reason for _path, reason in result.failed))

    def test_reject_windows(self) -> None:
        result = decide.delete_many([r"C:\Windows\System32\kernel32.dll"])
        self.assertEqual(result.freed_bytes, 0)
        self.assertTrue(result.failed)

    def test_delete_user_file_to_recycle_bin(self) -> None:
        target = self.root / "big-user-file.bin"
        _write_file(target, 4096)
        result = decide.delete_many([str(target)])
        if result.failed:
            self.skipTest(f"当前环境回收站不可用: {result.failed}")
        self.assertFalse(target.exists())
        self.assertGreaterEqual(result.freed_bytes, 4096)
        self.assertTrue(result.recycled)

    def test_cancel_keeps_files(self) -> None:
        target = self.root / "keep-me.bin"
        _write_file(target, 1024)
        result = decide.delete_many([str(target)], cancel=lambda: True)
        self.assertTrue(target.exists())
        self.assertEqual(result.freed_bytes, 0)

    def test_programdata_downloads_policy_but_nonexistent(self) -> None:
        # 授权矩阵允许（首闸通过），但路径不存在 → 记入 failed 且不删除
        result = decide.delete_many([r"C:\ProgramData\Intel\DSA\Downloads\no-such-file.exe"])
        self.assertTrue(result.failed)
        self.assertEqual(result.freed_bytes, 0)


if __name__ == "__main__":
    unittest.main()
