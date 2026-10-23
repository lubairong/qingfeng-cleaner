# QingFeng Cleaner（清风清理）— 架构契约 v1

> 所有成员必须严格遵守本文件中的路径、类名、函数签名与数据结构，禁止擅自改动接口。
> 需要变更接口 → 先告知 Lead，由 Lead 更新本文件。

## 0. 目标

Windows 11 的 C 盘清理工具。**带界面 + 系统托盘**。零第三方依赖（仅 Python 3.12 标准库 + tkinter），
用 ctypes 调 Win32。设计原则来自 GitHub 开源方案调研（BleachBit / Dism++ / winapp2.ini 等），
但代码为自研。

核心安全原则：

1. **默认只扫描，不删除**；删除前必须显式勾选 + 二次确认。
2. 删除默认走**回收站**（`SHFileOperationW` + `FOF_ALLOWUNDO`），永久删除需单独开关。
3. 一切删除必须过 `safety.py` 守卫：命中保护路径立即拒绝，绝不递归删除系统目录。
4. 规则声明 `risk`，`advanced` 默认禁用；`requires_admin` 的规则在非管理员下只扫描不删除。
5. 危险操作（清空回收站、休眠文件、Windows 更新缓存）走"报告 + 建议命令"或需管理员。

## 1. 目录结构（写入者标注）

```
main.py                        # [Lead] 入口
qingfeng/__init__.py           # [Lead] APP_NAME / __version__
qingfeng/core/                 # [core-engine]
    __init__.py
    models.py                  # 数据结构
    rules.py                   # 规则加载/校验
    safety.py                  # 路径安全守卫
    winapi.py                  # ctypes Win32 封装
    scanner.py                 # 扫描
    cleaner.py                 # 清理
qingfeng/data/rules/*.json     # [core-engine] 规则集，一个分类一个文件
qingfeng/ui/                   # [gui]
    __init__.py
    theme.py
    main_window.py
    widgets.py
    dialogs.py
    tray.py
assets/*                       # [gui] 图标等
tests/test_*.py                # [core-engine] 单元测试
tools/verify_*.ps1             # [packaging-qa] 验证脚本
packaging/                     # [packaging-qa] 打包
docs/                          # [Lead]/[researcher]/[packaging-qa]
README.md, LICENSE, .gitignore # [packaging-qa]
```

**写入域互不重叠。** 任何人不要修改别人域内的文件。

## 2. 数据模型 `qingfeng/core/models.py`

全部为 `dataclass`，纯标准库，不依赖 tkinter。

```python
Risk = Literal["safe", "caution", "advanced"]
Action = Literal["delete", "empty_recycle_bin", "report"]   # report = 只展示建议命令

@dataclass
class Rule:
    id: str                     # 唯一，kebab-case，如 "user-temp"
    name: str                   # 中文显示名，如 "用户临时文件"
    category: str               # 见 §3 分类表
    description: str            # 一句话说明
    action: Action
    paths: list[str] = field(default_factory=list)   # 支持 %TEMP% %LOCALAPPDATA% %WINDIR% 等
    globs: list[str] = field(default_factory=list)   # 相对 paths 的匹配式，如 "*.log"；空=整个 path
    exclude_globs: list[str] = field(default_factory=list)
    risk: Risk = "safe"
    requires_admin: bool = False
    default_enabled: bool = True
    min_age_days: int = 0       # 只处理 mtime 早于 N 天前的文件
    follow_dirs: bool = True    # 是否递归子目录
    protected_globs: list[str] = field(default_factory=list)  # 命中即跳过（规则级）
    report_note: str = ""       # action=report 时展示的命令/说明
    source: str = ""            # 参考来源，如 "bleachbit", "winapp2.ini"

@dataclass
class ScanItem:
    rule_id: str
    path: str
    is_dir: bool
    size_bytes: int
    file_count: int
    mtime: float
    risk: Risk = "safe"

@dataclass
class RuleScanResult:
    rule: Rule
    items: list[ScanItem] = field(default_factory=list)
    error: str | None = None
    skipped_reason: str | None = None    # 如 "需要管理员权限"
    @property
    def total_bytes(self) -> int
    @property
    def file_count(self) -> int

@dataclass
class ScanReport:
    results: list[RuleScanResult]
    started_at: float
    duration: float
    drive_free: int
    drive_total: int
    cancelled: bool = False
    @property
    def total_bytes(self) -> int
    @property
    def cleanable_bytes(self) -> int   # 排除 action=="report"

@dataclass
class CleanResult:
    deleted_files: int = 0
    deleted_dirs: int = 0
    freed_bytes: int = 0
    recycled: bool = True
    failed: list[tuple[str, str]] = field(default_factory=list)   # (path, error)
    per_rule: dict[str, int] = field(default_factory=dict)        # rule_id -> freed_bytes
    duration: float = 0.0
```

## 3. 分类表（`category` 只能取这些值，GUI 按此排序展示）

| 顺序 | category | 说明 |
|---|---|---|
| 1 | `系统` | Windows 临时文件、更新缓存、日志、转储、缩略图缓存 |
| 2 | `浏览器` | Edge/Chrome/Firefox 缓存（只清缓存，绝不动密码/书签/Cookie，除非规则显式 advanced） |
| 3 | `应用` | 常见软件残留缓存（微信/QQ/钉钉/Office/Teams/Steam 等） |
| 4 | `隐私痕迹` | 最近文件记录、跳转列表、剪贴板历史等（advanced） |
| 5 | `大文件` | 扫描型：C 盘 top N 大文件/大目录，action=report，只报告 |
| 6 | `系统维护` | 休眠文件、WinSxS、还原点、页面文件 —— action=report 或需管理员 |

## 4. 引擎 API（`qingfeng/core/`）

```python
# rules.py
def load_rules(rules_dir: Path | None = None) -> list[Rule]   # 读 qingfeng/data/rules/*.json，非法规则抛 RuleError
def validate_rule(raw: dict) -> Rule                          # 校验 + 填默认值
class RuleError(ValueError): ...

# safety.py
class UnsafePathError(ValueError): ...
def expand(path: str) -> str                                  # 展开环境变量 + 归一化
def is_protected(path: str) -> bool                           # 见 §5
def assert_deletable(path: str) -> None                       # 不安全则抛 UnsafePathError
def within_root(child: str, root: str) -> bool                # 防路径穿越

# scanner.py
def scan(rules: list[Rule], *,
         progress: Callable[[str, int, int], None] | None = None,
         cancel: Callable[[], bool] | None = None,
         max_depth: int = 24) -> ScanReport
#  progress(category_or_rule_name, done_count, total_count) 会从工作线程调用

# cleaner.py
def clean(results: list[RuleScanResult], *,
          mode: Literal["recycle", "delete"] = "recycle",
          progress: Callable[[str, int, int], None] | None = None,
          cancel: Callable[[], bool] | None = None) -> CleanResult
def execute_rule_action(rule: Rule, result: RuleScanResult, *, mode: str = "recycle") -> CleanResult

# winapi.py  (全部 ctypes，无第三方依赖)
def is_admin() -> bool
def drive_usage(drive: str = "C:") -> tuple[int, int]         # (free, total)
def recycle_paths(paths: list[str]) -> list[tuple[str, str]]  # 回收站删除，返回失败列表
def empty_recycle_bin() -> bool
def restart_as_admin(argv: list[str] | None = None) -> bool   # ShellExecuteW "runas"
def human_size(n: int) -> str                                 # "1.2 GB"
def open_in_explorer(path: str) -> None
```

规则 JSON 格式（`qingfeng/data/rules/system.json`）：

```json
{
  "version": 1,
  "category": "系统",
  "rules": [
    {
      "id": "user-temp",
      "name": "用户临时文件",
      "category": "系统",
      "description": "当前用户 %TEMP% 下的临时文件",
      "action": "delete",
      "paths": ["%TEMP%"],
      "globs": ["*"],
      "exclude_globs": ["*.sys"],
      "risk": "safe",
      "requires_admin": false,
      "default_enabled": true,
      "min_age_days": 0,
      "follow_dirs": true,
      "source": "bleachbit"
    }
  ]
}
```

## 4.1 契约修订（v1.1，Lead 批准，2026-10-22）

来自 GitHub 调研的 3 条硬性约束，core-engine 必须实现：

1. **`paths` 允许出现单个 `*` 段**（如 `%LOCALAPPDATA%\Microsoft\Edge\User Data\*`、`...\Firefox\Profiles\*`），
   表示"展开该层下的每一个子目录"。只允许单层 `*`，不允许 `**`、不允许出现在盘符/根段。
   展开由 `rules.expand_paths()` 完成，`scanner` 拿到的已是具体路径。无法展开时用 `skipped_reason` 说明。
2. **递归必须跳过 junction / reparse point**（`os.scandir` 条目 `entry.is_symlink()` 或
   `stat().st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`），否则
   `%LOCALAPPDATA%\Application Data` 这类循环链接会导致死循环。跨卷的 reparse 也直接跳过。
3. **`action == "report"` 的规则 `paths` 可以为空**，此时只展示 `report_note`（建议命令/说明），
   `scanner` 返回空的 `RuleScanResult` 且 `total_bytes == 0`，GUI 不得把它算进"可清理"。

## 5. 保护路径（`safety.py`，硬编码底线，规则不可覆盖）

根级永不删除：`C:\`、`C:\Windows`、`C:\Program Files`、`C:\Program Files (x86)`、
`C:\ProgramData`、`C:\Users`、`C:\Users\<user>`、`C:\Users\<user>\AppData`、
`C:\Windows\System32`、`C:\Windows\SysWOW64`、`C:\Windows\WinSxS`、`C:\Recovery`、
`C:\$Recycle.Bin`、`C:\System Volume Information`、`C:\Boot`、`C:\EFI`、
所有盘符根、`C:\Users\<user>\Documents/Desktop/Pictures/Videos/Music/OneDrive`。

另：路径长度 > 3 且父目录是上述之一 且目标本身是受保护目录内部的**系统关键子目录**（如
`C:\Windows\System32\config`）→ 保护。实现上用规范化后的 `ntpath` 全路径比较，大小写不敏感，
解析 `..` 与符号链接（`os.path.realpath`）。**宁可误报不安全，不可漏删。**

## 6. GUI API（`qingfeng/ui/`）

```python
# main_window.py
class MainWindow:            # 内部持有 tk.Tk
    def __init__(self, root, engine=None) -> None
    def run(self) -> None                  # mainloop
    def start_scan(self, auto: bool = False) -> None
    def show(self) -> None                 # 托盘"显示主窗口"
def launch(*, with_tray: bool = True, start_hidden: bool = False) -> int   # 供 main.py 调用

# tray.py
class TrayIcon:
    def __init__(self, on_show: Callable[[], None], on_scan: Callable[[], None],
                 on_quit: Callable[[], None], icon_path: Path | None = None)
    def start(self) -> None      # 后台线程跑消息循环
    def stop(self) -> None
```

要求：

- 界面中文，Windows 11 风格（ttk + 自绘配色），支持高 DPI（`ctypes.windll.shcore.SetProcessDpiAwareness(1)`）。
- 布局：顶部磁盘占用条（C 盘可用/总量 + 可清理量）；中部按 category 分组的可勾选列表
  （Tk ttk.Treeview，列：勾选、名称、大小、文件数、风险）；底部：扫描/清理/设置按钮 + 进度条 + 状态栏。
- 扫描在后台线程，UI 通过 `queue.Queue` + `root.after(80, ...)` 消费，禁止在子线程碰 tkinter。
- 清理前弹二次确认（`dialogs.confirm_clean`），显示将释放多少、是否回收站、advanced 规则数量。
- 引擎未就绪时（`ImportError`）用内置 mock 数据渲染，保证界面可独立开发运行。
- 托盘：ctypes `Shell_NotifyIconW` + 隐藏窗口收 `WM_APP` 消息；菜单含 显示主窗口 / 快速扫描 / 退出；
  关闭窗口默认最小化到托盘（可设置里关闭该行为）。无图标时用 `assets/app.ico`，缺失则程序内生成。

## 7. 运行与验证

- 开发运行：`python main.py`（用内置 python：`C:\Users\HUAWEI\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`）
- 命令行参数：`--scan-only`（扫完打印报告退出）、`--no-tray`、`--hidden`、`--rules-dir <path>`、`--version`
- 单元测试：`python -m unittest discover -s tests -v`（不要用 pytest，未安装）
- GUI 冒烟：启动后用 PowerShell + System.Drawing 截屏，人工看图确认不白屏不崩。

## 8. 验收标准

1. `python -m unittest discover -s tests` 全绿。
2. `python main.py --scan-only` 在真机跑通，输出各类别可清理字节数，且不删除任何文件。
3. GUI 能启动、扫描、显示结果、勾选、清理到回收站（在受控测试目录上验证）。
4. 托盘图标存在，菜单可用。
5. 危险路径测试：对 `C:\Windows\System32` 构造的删除请求必须抛 `UnsafePathError`。

---

## 9. 交付记录（2026-10-22 定稿）

| 项 | 状态 |
|---|---|
| 规则集 | **59 条 / 6 分类**（系统 19 · 应用 14 · 浏览器 6 · 隐私痕迹 7 · 大文件 5 · 系统维护 8） |
| 知识来源 | GitHub 调研 + 官方文档：winapp2.ini、BleachBit、Dism++、Optimizer、WinDirStat/WizTree、Microsoft Learn（详见 docs/research/integration-map.md） |
| 安全审计 | qa/lead_audit.py **29/29 PASS**；红队审查 3 项必须改 + 12 项建议全部处置（docs/research/redteam-review.md） |
| 单元测试 | 118 个用例 OK（2 个符号链接用例在无开发者模式环境下 skip） |
| 真机扫描 | C: 200 GB / 可用 ~78 GB；默认勾选可清理约 663–700 MB，全量扫描 0.2 s，零删除 |
| 端到端验证 | tools/verify.ps1：**7 PASS / 0 FAIL / 1 SKIP**（SKIP 为本机沙箱自解压限制，目录版产物已真实验证） |
| 打包产物 | dist/QingFengCleaner-onedir/（推荐，已验证）+ dist/QingFengCleaner.exe（单文件便携版） |
| 界面 | 175% DPI 真机适配：窗口按 SPI_GETWORKAREA 计算（2264×1532 @ 2520×1596 工作区），五列表格与底部操作区完整可见 |
| 开源交付物 | README/LICENSE(MIT)/CONTRIBUTING/.gitignore/CI/Issue 模板/使用文档/FAQ，相对链接检查通过（35 条） |
