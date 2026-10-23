"""清风清理 —— 路径安全守卫。

本模块是删除操作的**最后一道防线**，对应 ``docs/ARCHITECTURE.md`` §5。

实现原则（宁可误报不安全，不可漏删）：

1. 一切比较都基于「规范化后的 Windows 全路径」：
   - 先展开环境变量/``~``，再 ``ntpath.normpath``；
   - 用 ``os.path.realpath`` 解析 ``..``、符号链接、junction，防止绕过；
   - 用 ``ntpath.normcase`` 大小写不敏感比较，并统一 ``/`` 为 ``\\``。
2. 保护分四层：
   - **精确保护**：盘符根、系统目录本身、用户目录（桌面/文档/下载/图片/视频/音乐/OneDrive 等）；
   - **整树保护**：C:\\Program Files、C:\\Recovery、C:\\$Recycle.Bin、用户文档类目录等；
   - **白名单**：``C:\\Windows`` 下只有少数可清理目录（Temp/Logs/Prefetch/...）放行；
     ``C:\\ProgramData`` 下只有 WER 崩溃报告目录放行；
   - **祖先保护**：若待删路径是某个受保护路径的祖先（删它会连带删掉受保护内容），同样拒绝。
3. 任何无法判定的输入（空路径、非绝对路径）一律视为不安全。
4. **唯一例外（Lead 批准，2026-10-22）**：微信经典版缓存目录
   ``%USERPROFILE%\\Documents\\WeChat Files\\<账号>\\FileStorage\\Cache``
   （及其内部文件）允许清理。该例外以显式的 :data:`_DEEP_ALLOW_PATTERNS`
   实现，只允许这一种路径形态；**新增任何穿透保护目录的例外都必须先经 Lead 批准**，
   不得在规则或调用方临时放宽。
"""

from __future__ import annotations

import ntpath
import os
import re

#: Windows 目录（环境变量缺失时的兜底）
_WINDIR_FALLBACK = r"C:\Windows"
#: 系统盘（环境变量缺失时的兜底）
_SYSTEM_DRIVE_FALLBACK = "C:"

#: ``C:\Windows`` 下允许清理的顶层目录白名单（其余全部保护）
_WINDOWS_ALLOWED_TOPS: frozenset[str] = frozenset(
    {
        "temp",
        "logs",
        "prefetch",
        "minidump",
        "livekernelreports",
        "softwaredistribution",
        "systemtemp",
        "inetcache",
        "pchealth",
    }
)

#: 用户目录下「整树保护」的标准文件夹名（即使当前用户没有也要登记）
_USER_PROTECTED_DIR_NAMES: tuple[str, ...] = (
    "Documents",
    "Desktop",
    "Downloads",
    "Pictures",
    "Videos",
    "Music",
    "OneDrive",
    "Saved Games",
    "Favorites",
    "Links",
    "Searches",
    "Contacts",
    "3D Objects",
    "My Documents",
    "OneDrive - Personal",
    ".ssh",
    ".aws",
    ".azure",
    ".kube",
    ".gnupg",
    ".docker",
    ".config",
    ".gitconfig",
    "NTUSER.DAT",
    "ntuser.dat",
)

#: 系统盘根目录下的关键文件（禁止按文件删除）
_ROOT_PROTECTED_FILES: tuple[str, ...] = (
    "pagefile.sys",
    "hiberfil.sys",
    "swapfile.sys",
    "bootmgr",
    "BOOTNXT",
    "DumpStack.log.tmp",
)

#: 系统盘根目录下的整树保护目录（文件夹名）
_ROOT_PROTECTED_DIRS: tuple[str, ...] = (
    "Recovery",
    "$Recycle.Bin",
    "System Volume Information",
    "Boot",
    "EFI",
    "Windows.old",
    "$Windows.~BT",
    "$Windows.~WS",
    "$SysReset",
    "$WinREAgent",
    "ESD",
)

# ---------------------------------------------------------------------------
# 缓存（首次调用时构建，避免每次删除都枚举 C:\Users）
# ---------------------------------------------------------------------------

_BUILT = False
_PROTECTED_KEYS: set[str] = set()  # 规范化 key（normcase）
_PROTECTED_TREES: set[str] = set()  # 规范化 key，整棵子树保护
_ALL_PROTECTED: set[str] = set()  # keys | trees，用于祖先判定
_DRIVE_ROOTS: set[str] = set()
_WINDIR_KEY = ""
_PROGRAMDATA_KEY = ""
_PROGRAMDATA_ALLOWED: list[str] = []
#: ProgramData 下 ``\Downloads\`` 子树的残留安装包例外（v2 授权矩阵，Lead 批准）
_PROGRAMDATA_DOWNLOAD_PATTERNS: list[re.Pattern[str]] = []
_PROFILE_ROOT_KEYS: set[str] = set()

#: ``C:\Windows`` 根目录白名单的前缀；在 build 时填充
_WINDIR = ""
#: ``C:\Windows`` 下额外允许的具体缓存子树（白名单顶层目录之外的例外）
_WINDOWS_ALLOWED_SUBTREES: list[str] = []

#: 允许穿透「整树保护」的**极窄**例外（新增必须经 Lead 批准）。
#: 每项是作用于「realpath + normcase 后的规范化 key」的正则。
#: 当前两条：
#:   1) ``<userprofile>\documents\wechat files\<账号>\filestorage\cache`` 及其内部文件；
#:   2) ``<programdata>\**\downloads\**`` 下的残留安装包（v2 空间透视授权矩阵；
#:      具体类别由 decide.action_policy 再按 installer 收窄）。
_DEEP_ALLOW_PATTERNS: list[re.Pattern[str]] = []


class UnsafePathError(ValueError):
    """路径不安全（受保护 / 越界 / 无法判定），拒绝删除。"""


# ---------------------------------------------------------------------------
# 基础路径工具
# ---------------------------------------------------------------------------


def _strip_trailing(path: str) -> str:
    """去掉多余结尾分隔符；盘符根（``C:\\``）保持原样。"""
    if not path:
        return ""
    drive, tail = ntpath.splitdrive(path)
    if tail in ("", "\\", "/"):
        return f"{drive}\\" if drive else tail
    return path.rstrip("\\/")


def expand(path: str) -> str:
    """展开环境变量与 ``~``，并归一化路径。

    只做字符串层面的展开与归一化，不解析符号链接（需要时由 ``_real`` 完成）。
    空输入返回空字符串。
    """
    if path is None:
        return ""
    text = str(path).strip()
    if not text:
        return ""
    text = os.path.expandvars(text)
    text = os.path.expanduser(text)
    return _strip_trailing(ntpath.normpath(text))


def _real(path: str) -> str:
    """展开 + 绝对化 + realpath（解析 ``..``、符号链接、junction）。"""
    p = expand(path)
    if not p:
        return ""
    if not ntpath.isabs(p):
        p = ntpath.abspath(p)
    try:
        p = os.path.realpath(p)
    except OSError:
        p = ntpath.abspath(p)
    return _strip_trailing(p)


def _key(path: str) -> str:
    """比较用的规范化 key：realpath + 小写 + 统一分隔符。"""
    real = _real(path)
    return ntpath.normcase(real) if real else ""


def _is_under(key: str, root_key: str) -> bool:
    """key 是否位于 root_key 之下（不含相等）。两者都必须是规范化 key。"""
    if not key or not root_key:
        return False
    if key == root_key:
        return False
    prefix = root_key if root_key.endswith("\\") else root_key + "\\"
    return key.startswith(prefix)


def within_root(child: str, root: str) -> bool:
    """判断 ``child`` 是否等于 ``root`` 或位于 ``root`` 之下（防路径穿越）。

    大小写不敏感，并先解析 ``..`` / 符号链接。``C:\\foo`` 与 ``C:\\foobar``
    不会被误判为同根。
    """
    c = _key(child)
    r = _key(root)
    if not c or not r:
        return False
    if c == r:
        return True
    return _is_under(c, r)


# ---------------------------------------------------------------------------
# 受保护路径构建
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "")
    return value.strip() if value.strip() else default


def _add_key(path: str) -> None:
    key = _key(path)
    if key:
        _PROTECTED_KEYS.add(key)


def _add_tree(path: str) -> None:
    """登记整树保护：路径本身与其下所有内容都禁止删除。"""
    key = _key(path)
    if key:
        _PROTECTED_TREES.add(key)
        # 整树保护的根自身也必须精确保护（_is_under 不包含相等）
        _PROTECTED_KEYS.add(key)


def _add_profile(profile: str) -> None:
    """登记一个用户配置目录：根目录精确保护 + 其下标准用户目录整树保护。"""
    profile_key = _key(profile)
    if not profile_key:
        return
    _PROFILE_ROOT_KEYS.add(profile_key)
    _PROTECTED_KEYS.add(profile_key)

    name = ntpath.basename(profile_key.rstrip("\\")).lower()
    if name == "public":
        # 公共目录整树保护，避免误删共享文档
        _PROTECTED_TREES.add(profile_key)

    # 标准用户目录（不管是否存在都登记成整树保护）
    for dir_name in _USER_PROTECTED_DIR_NAMES:
        _add_tree(ntpath.join(profile, dir_name))
    # AppData 各根目录只保护自身（允许清理其下的缓存子目录）
    for sub in ("AppData", r"AppData\Local", r"AppData\Roaming", r"AppData\LocalLow"):
        _add_key(ntpath.join(profile, sub))
    # 真实存在的 profile 直接子目录若不在白名单里，也整树保护（防未知用户目录被删）
    # 注意：必须跳过 reparse point（旧版兼容链接 "Application Data" / "Recent" /
    # "Local Settings" 等实际指向 AppData 子目录，若登记成整树保护会误伤缓存清理）。
    try:
        with os.scandir(profile) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.is_symlink():
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if getattr(st, "st_file_attributes", 0) & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                    continue
                if entry.name.lower() == "appdata":
                    continue
                _add_tree(entry.path)
    except OSError:
        # 枚举失败不影响保护表：上面的标准目录名已经登记
        pass


def _discover_profiles(users_dir: str) -> list[str]:
    profiles: list[str] = []
    for var in ("USERPROFILE", "HOME", "HOMEPATH"):
        raw = _env(var)
        if not raw:
            continue
        if var == "HOMEPATH" and not ntpath.isabs(expand(raw)):
            raw = ntpath.join(_env("HOMEDRIVE", _SYSTEM_DRIVE_FALLBACK), raw)
        profiles.append(raw)
    try:
        with os.scandir(users_dir) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        profiles.append(entry.path)
                except OSError:
                    continue
    except OSError:
        pass

    seen: set[str] = set()
    unique: list[str] = []
    for profile in profiles:
        real = _real(profile)
        if not real:
            continue
        key = ntpath.normcase(real)
        if key in seen:
            continue
        seen.add(key)
        unique.append(real)
    return unique


def _build() -> None:
    """构建保护表（进程内只执行一次）。"""
    global _BUILT, _WINDIR, _WINDIR_KEY, _PROGRAMDATA_KEY, _PROGRAMDATA_ALLOWED
    global _WINDOWS_ALLOWED_SUBTREES, _PROGRAMDATA_DOWNLOAD_PATTERNS

    if _BUILT:
        return

    sysdrive = _env("SystemDrive", _SYSTEM_DRIVE_FALLBACK).rstrip("\\")
    if not sysdrive.endswith(":"):
        sysdrive = (_SYSTEM_DRIVE_FALLBACK).rstrip("\\")
    windir = _env("WINDIR", _WINDIR_FALLBACK)
    program_files = _env("ProgramFiles", f"{sysdrive}\\Program Files")
    program_files_x86 = _env("ProgramFiles(x86)", f"{sysdrive}\\Program Files (x86)")
    program_data = _env("ProgramData", f"{sysdrive}\\ProgramData")
    users_dir = f"{sysdrive}\\Users"

    _WINDIR = windir
    _WINDIR_KEY = _key(windir)
    _PROGRAMDATA_KEY = _key(program_data)

    # 1) 所有存在的盘符根（含可移动盘）
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        try:
            exists = os.path.exists(root)
        except OSError:
            exists = False
        if exists:
            _add_key(root)
            _DRIVE_ROOTS.add(ntpath.normcase(_strip_trailing(_real(root))))
            # 所有盘的回收站 / 卷影信息一律整树保护
            _add_tree(ntpath.join(root, "$Recycle.Bin"))
            _add_tree(ntpath.join(root, "System Volume Information"))

    # 2) 固定系统目录（§5 全部条目）
    _add_key(f"{sysdrive}\\")
    _add_key(windir)  # C:\Windows
    _add_key(program_files)
    _add_key(program_files_x86)
    _add_key(program_data)
    _add_key(users_dir)  # C:\Users
    _add_key(ntpath.join(windir, "System32"))
    _add_key(ntpath.join(windir, "SysWOW64"))
    _add_key(ntpath.join(windir, "WinSxS"))
    _add_key(ntpath.join(sysdrive + "\\", "Recovery"))
    _add_key(ntpath.join(sysdrive + "\\", "$Recycle.Bin"))
    _add_key(ntpath.join(sysdrive + "\\", "System Volume Information"))
    _add_key(ntpath.join(sysdrive + "\\", "Boot"))
    _add_key(ntpath.join(sysdrive + "\\", "EFI"))

    # 3) 整树保护的系统目录
    for dir_name in _ROOT_PROTECTED_DIRS:
        _add_tree(ntpath.join(sysdrive + "\\", dir_name))
    # Windows 内部关键子树（即使名字不在白名单也会被白名单逻辑挡住，
    # 这里显式登记便于祖先判定与可读性）
    for sub in (
        r"System32",
        r"SysWOW64",
        r"WinSxS",
        r"Fonts",
        r"assembly",
        r"servicing",
        r"Microsoft.NET",
        r"CSC",
        r"security",
        r"SystemResources",
        r"Web",
        r"Help",
        r"schemas",
        r"migration",
        r"PolicyDefinitions",
        r"Boot",
        r"inf",
        r"diagnostics",
        r"System32\config",
        r"System32\drivers",
        r"System32\winevt",
        r"System32\spool",
        r"System32\Tasks",
        r"System32\wbem",
        r"System32\LogFiles",
        r"System32\catroot",
        r"System32\catroot2",
        r"SoftwareDistribution\DataStore",
        r"SoftwareDistribution\SLS",
    ):
        key = _key(ntpath.join(windir, sub))
        if key:
            _PROTECTED_TREES.add(key)
    # SoftwareDistribution 根目录本身保护，但其下 Download / DeliveryOptimization 允许清理
    _add_key(ntpath.join(windir, "SoftwareDistribution"))
    # ServiceProfiles 下唯一的已知缓存例外：传递优化（Delivery Optimization）缓存
    _WINDOWS_ALLOWED_SUBTREES = [
        _key(
            ntpath.join(
                windir,
                r"ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization",
            )
        ),
    ]

    # 4) Program Files / Program Files (x86) 整树保护
    for pf in (program_files, program_files_x86):
        key = _key(pf)
        if key:
            _PROTECTED_TREES.add(key)

    # 5) ProgramData：白名单之外的整树保护
    _PROGRAMDATA_ALLOWED = [
        _key(ntpath.join(program_data, r"Microsoft\Windows\WER")),
    ]
    # ProgramData 根自身的精确保护
    _add_key(program_data)
    # v2 授权矩阵：ProgramData 下任意 ``\Downloads\`` 子树的残留安装包可清理
    # （由 decide.action_policy 再按 category=="installer" 收窄）
    _programdata_key = _key(program_data)
    _PROGRAMDATA_DOWNLOAD_PATTERNS = []
    if _programdata_key:
        _PROGRAMDATA_DOWNLOAD_PATTERNS = [
            re.compile(
                r"^" + re.escape(_programdata_key) + r"\\[^\\]+(?:\\[^\\]+)*\\downloads(?:\\|$)"
            )
        ]

    # 6) 系统盘根目录下的关键文件
    for name in _ROOT_PROTECTED_FILES:
        _add_key(ntpath.join(sysdrive + "\\", name))

    # 7) 用户 profile
    for profile in _discover_profiles(users_dir):
        _add_profile(profile)

    # 8) 祖先判定集合
    _ALL_PROTECTED.clear()
    _ALL_PROTECTED.update(_PROTECTED_KEYS)
    _ALL_PROTECTED.update(_PROTECTED_TREES)

    # 9) 唯一的深目录例外（微信经典版缓存），见文件头注释
    _build_deep_allow_patterns()

    _BUILT = True


def _build_deep_allow_patterns() -> None:
    """构建 :data:`_DEEP_ALLOW_PATTERNS`（当前只有微信缓存这一条）。

    只以**当前用户**的 ``%USERPROFILE%\\Documents\\WeChat Files`` 为前缀，
    匹配 ``<账号>\\FileStorage\\Cache`` 及其内部文件；其它账号目录下的
    ``FileStorage`` / ``Msg`` / ``FileStorage\\MsgAttach`` 等一律不匹配。
    """
    global _DEEP_ALLOW_PATTERNS
    patterns: list[re.Pattern[str]] = []
    profile = _real(_env("USERPROFILE"))
    if profile:
        prefix = _key(ntpath.join(profile, "Documents", "WeChat Files"))
        if prefix:
            # 深度恰好 4 段（prefix 之后）：<账号> \ FileStorage \ Cache [+ 内部文件]
            patterns.append(
                re.compile(
                    r"^"
                    + re.escape(prefix)
                    + r"\\[^\\]+\\filestorage\\cache(?:\\|$)"
                )
            )
    _DEEP_ALLOW_PATTERNS = patterns


def _is_deep_allowed(key: str) -> bool:
    """是否命中唯一的深目录例外（入参必须是 realpath+normcase 后的 key）。"""
    return any(pattern.match(key) for pattern in _DEEP_ALLOW_PATTERNS)


def reset_cache() -> None:
    """清空保护表缓存（仅供测试/环境变量变化后重新构建使用）。"""
    global _BUILT, _WINDIR, _WINDIR_KEY, _PROGRAMDATA_KEY, _PROGRAMDATA_ALLOWED
    global _WINDOWS_ALLOWED_SUBTREES, _PROGRAMDATA_DOWNLOAD_PATTERNS, _DEEP_ALLOW_PATTERNS
    _BUILT = False
    _WINDIR = ""
    _WINDIR_KEY = ""
    _PROGRAMDATA_KEY = ""
    _PROGRAMDATA_ALLOWED = []
    _PROGRAMDATA_DOWNLOAD_PATTERNS = []
    _WINDOWS_ALLOWED_SUBTREES = []
    _DEEP_ALLOW_PATTERNS = []
    _PROTECTED_KEYS.clear()
    _PROTECTED_TREES.clear()
    _ALL_PROTECTED.clear()
    _DRIVE_ROOTS.clear()
    _PROFILE_ROOT_KEYS.clear()


def _windows_allowed(key: str) -> bool:
    """``C:\\Windows`` 之下的路径是否落在可清理白名单内。"""
    rel = key[len(_WINDIR_KEY) :].lstrip("\\")
    if not rel:
        return False
    top = rel.split("\\", 1)[0]
    if top in _WINDOWS_ALLOWED_TOPS:
        return True
    return any(
        allowed and (key == allowed or _is_under(key, allowed))
        for allowed in _WINDOWS_ALLOWED_SUBTREES
    )


def _programdata_allowed(key: str) -> bool:
    """``C:\\ProgramData`` 之下只有白名单子树可清理。

    白名单 = WER 崩溃报告 + ``\\Downloads\\`` 下的残留安装包（v2 授权矩阵）。
    后者由 ``decide.action_policy`` 再按 installer 类别收窄。
    """
    if any(_is_under(key, allowed) or key == allowed for allowed in _PROGRAMDATA_ALLOWED if allowed):
        return True
    return any(pattern.match(key) for pattern in _PROGRAMDATA_DOWNLOAD_PATTERNS)


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def is_protected(path: str) -> bool:
    """判断路径是否受保护（受保护 => 绝不允许删除）。

    对空路径、相对路径、盘符根、系统目录、用户文档目录、``C:\\Windows``
    非白名单目录、``C:\\ProgramData`` 非白名单目录、以及「受保护路径的祖先」
    一律返回 ``True``。
    """
    if path is None:
        return True
    raw = str(path).strip()
    if not raw:
        return True
    _build()

    # 非绝对路径无法可靠判定，按不安全处理（宁可误报）
    expanded = expand(raw)
    if not expanded or not ntpath.isabs(expanded):
        return True

    p = _real(raw)
    if not p:
        return True
    key = ntpath.normcase(p)

    # 盘符根 / UNC 根
    drive, tail = ntpath.splitdrive(key)
    if not tail or tail in ("\\", "/"):
        return True

    # 唯一的深目录例外（微信经典版缓存）：命中则明确可删，不再走保护判定
    if _is_deep_allowed(key):
        return False

    # 精确保护
    if key in _PROTECTED_KEYS:
        return True

    # 整树保护
    for tree in _PROTECTED_TREES:
        if _is_under(key, tree):
            return True

    # C:\Windows 白名单
    if _WINDIR_KEY and _is_under(key, _WINDIR_KEY):
        return not _windows_allowed(key)

    # C:\ProgramData 白名单
    if _PROGRAMDATA_KEY and _is_under(key, _PROGRAMDATA_KEY):
        return not _programdata_allowed(key)

    # 祖先保护：删它会连带删掉受保护内容
    for protected in _ALL_PROTECTED:
        if _is_under(protected, key):
            return True

    return False


def assert_deletable(path: str) -> None:
    """校验路径可删除；不安全则抛 :class:`UnsafePathError`。"""
    if path is None or not str(path).strip():
        raise UnsafePathError("空路径，拒绝删除")
    expanded = expand(str(path))
    if not expanded:
        raise UnsafePathError(f"路径为空，拒绝删除: {path!r}")
    if not ntpath.isabs(expanded):
        raise UnsafePathError(f"非绝对路径，拒绝删除: {path}")
    if is_protected(expanded):
        raise UnsafePathError(f"受保护路径，拒绝删除: {path}")


def protected_paths() -> frozenset[str]:
    """返回当前保护表（规范化 key，供调试/自检使用）。"""
    _build()
    return frozenset(_ALL_PROTECTED)
