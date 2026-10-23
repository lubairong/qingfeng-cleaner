# 清风清理 v2 契约：空间透视 + 决策助手

> 本文件是 v2 的增量契约，与 `docs/ARCHITECTURE.md` 并列生效。
> 已有接口一律不改（v1 的 core/ui 全部保留可用），v2 只做**新增**。

## 0. 为什么有 v2

v1 只能清「已知缓存路径」（本机约 700 MB），用户真正的问题是：

1. **不知道 121 GB 都去哪了**（Windows 35.4 / 用户 36.8 / 程序 12.5 / 休眠 6.8 / 还原点与受保护约 27 GB 不可见）；
2. **≥200 MB 大文件 19.5 GB / 61 个**，工具既不展示也不解释，用户只能自己去翻；
3. 有些大文件（升级残留安装包、旧版程序、备份 .7z）**大概率可删但用户不敢下手**；有些（Ollama 模型库、微信数据）**不该删**，工具要能说清。

v2 目标：**先给全景透视，再给分类决策，最后给一键动作（回收站 / 迁移到 D、E 盘）**。

本机实况（2026-10-22 侦察，`qa/recon.json`）：
- C: 200 GB，已用 122.8 GB / 可用 77.2 GB；D: 可用 247.4 GB，E: 可用 262.6 GB
- 可访问部分 88.8 GB；hiberfil 6.76 GB + swapfile 0.25 GB；其余约 27 GB 为 $Recycle.Bin / System Volume Information / 无权限目录 → **必须显式展示为「受保护/需管理员」**，不能假装不存在

## 1. 新增模块与写域

```
qingfeng/core/space.py       # [core-engine] 磁盘索引（树）+ 系统占用探测
qingfeng/core/decide.py      # [core-engine] 大文件分类/建议 + 迁移执行
tests/test_space.py          # [core-engine]
tests/test_decide.py         # [core-engine]
qingfeng/data/heuristics.json# [core-engine] 分类词典（扩展名/路径/年龄阈值）
qingfeng/ui/space_page.py    # [gui] 空间透视页
qingfeng/ui/treemap.py       # [gui] 自绘 Squarified Treemap 画布
qingfeng/ui/actions.py       # [gui] 打开位置 / 回收站 / 迁移 的 UI 封装
docs/research/space-analysis-survey.md   # [researcher]
docs/research/space-heuristics.json      # [researcher]
tools/verify_space.ps1                   # [packaging-qa]
```

**写域不变**：core-engine 只写 `qingfeng/core`、`qingfeng/data`、`tests/`；gui 只写 `qingfeng/ui`、`assets/`；
打包只写 `packaging/`、`tools/`、README/docs(usage、faq、images)、`.github/`。Lead 负责 v1 文件的集成改动（`main.py`、`docs/ARCHITECTURE*.md`）。

## 2. 数据模型 `qingfeng/core/space.py`

```python
@dataclass
class SpaceNode:
    path: str                 # 绝对路径
    name: str                 # 显示名（根节点为 "C:"）
    is_dir: bool
    size: int                 # 子树字节数
    file_count: int           # 子树文件数
    mtime: float              # 子树内最新修改时间
    children: list["SpaceNode"] = field(default_factory=list)  # 已按 size 降序
    truncated: bool = False   # 子节点被折叠（小于 min_child_size 的已聚合进 `other`）
    other_size: int = 0       # 被折叠子项合计
    error: str | None = None

@dataclass
class SpaceIndex:
    root: str                        # "C:\"
    total_bytes: int                 # 磁盘已用（来自 winapi.drive_usage）
    accessible_bytes: int            # 成功统计到的字节
    unaccounted_bytes: int           # total - accessible - 已知系统文件，>=0
    system_files: dict[str, int]     # {"hiberfil.sys": 6756286464, ...}
    protected_hint: dict[str, str]   # {"System Volume Information": "还原点，需管理员", ...}
    duration: float
    built_at: float
    cancelled: bool = False
    truncated: bool = False
    root_node: SpaceNode
    def find(self, path: str) -> SpaceNode | None   # 按路径查节点
    def to_json(self) -> dict
    @classmethod
    def from_json(cls, data: dict) -> "SpaceIndex"   # 缓存用

def build_index(root: str = "C:\\", *,
                min_child_size: int = 32 * 1024 * 1024,
                max_nodes: int = 150_000,
                progress: Callable[[str, int], None] | None = None,   # (当前路径, 已处理节点数)
                cancel: Callable[[], bool] | None = None) -> SpaceIndex

def probe_system_space() -> dict
# 返回可直接展示的 dict：
#   {"recycle_bin": {"count": int, "bytes": int},
#    "hiberfil": {"exists": bool, "bytes": int},
#    "pagefile": {...}, "swapfile": {...},
#    "winsxs": {"bytes": int},                 # 扫描 C:\Windows\WinSxS
#    "windows_old": {"exists": bool, "bytes": int},
#    "restore_points": {"available": bool, "bytes": int|None, "note": str},  # 非管理员返回 available=False
#    "windows": {"bytes": int}, "installer_cache": {"bytes": int}}
# 全部只读；任何异常都降级为 {"available": False, ...}，绝不抛。

# 缓存：qingfeng/core/space.py 负责写出/读取
def cache_path() -> Path                      # %LOCALAPPDATA%\QingFengCleaner\space_index.json
def save_index(idx: SpaceIndex) -> None
def load_cached_index(max_age_hours: float = 24) -> SpaceIndex | None
```

索引硬性要求：

- **多线程**（`ThreadPoolExecutor`，8 线程按子树分发）或至少高效的 `os.scandir`；本机 122 GB / 40 万文件目标 **≤ 60 秒**，有进度回调、可取消。
- **硬链接去重（必须）**：按 `(st_dev, st_ino)` 去重累计，WinSxS / System32 大量硬链接重复计数会让
  统计虚高（本机 Windows 目录 35.4 GB 才是真实值）。同一 inode 只计一次大小、文件数也只计一次。
  注意：文件数可以按"逻辑条目"计，但**字节数必须按 inode 去重**。
- 跳过 reparse point（junction/symlink）避免死循环；无权限目录记录到 `error` / `protected_hint` 后继续。
- 节点数超过 `max_nodes` 时截断（`truncated=True`）而不是吃爆内存；`min_child_size` 以下的子项聚合进 `other_size`。
- 索引必须**只读**，绝不改写被扫目录的 mtime。
- 缓存 json 体积 < 15 MB；`load_cached_index` 失效时返回 None。

## 3. 决策模型 `qingfeng/core/decide.py`

```python
@dataclass
class ReviewItem:
    path: str
    name: str
    is_dir: bool
    size: int
    mtime: float
    age_days: int
    ext: str                       # 小写，无点；目录为空串
    category: str                  # 见下表
    advice: str                    # "safe_delete" | "review" | "keep" | "move"
    reason: str                    # 一句话中文理由，直接显示给用户
    risk: str                      # "safe" | "caution" | "danger"
    group: str                     # 分组 key，用于界面聚合

@dataclass
class ReviewGroup:
    key: str
    label: str                     # 中文分组名
    items: list[ReviewItem]
    total_bytes: int
    advice: str                    # 该组的总体建议

def collect_review_items(index: SpaceIndex, *,
                         min_size: int = 100 * 1024 * 1024,
                         limit: int = 800) -> list[ReviewItem]
def group_review_items(items: list[ReviewItem]) -> list[ReviewGroup]
def heuristics() -> dict                       # 读 qingfeng/data/heuristics.json

@dataclass
class MoveResult:
    moved: int = 0
    moved_bytes: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (path, reason)
    failed: list[tuple[str, str]] = field(default_factory=list)
    dest_root: str = ""
    duration: float = 0.0

def move_many(paths: list[str], dest_root: str, *,
              progress: Callable[[str, int, int], None] | None = None,
              cancel: Callable[[], bool] | None = None) -> MoveResult
# 迁移规则：
#   1) 逐个路径过 safety.assert_deletable（受保护路径一律拒绝，进 skipped）
#   2) 目标盘必须是固定磁盘（GetDriveTypeW == 3）且与源不同盘；空间不足要提前判定
#   3) 同盘/目标在源内部 → 拒绝（防递归）
#   4) 目标保留原目录结构：<dest_root>\<盘符>_mirror\<原相对路径>，防重名覆盖
#   5) 复制成功后才删源（跨盘 shutil.move 本地等价），失败保留源并记录
#   6) 不跟随 reparse point
```

### 3.1 分类词典 `qingfeng/data/heuristics.json`

> **定稿补充（2026-10-22，Lead 批准）**：researcher 已产出 `docs/research/space-heuristics.json`（14 分类 / 3 条
> age_rules / 18 条 never_touch，本机 71 个 ≥100MB 文件 100% 归类）。core-engine 以它为基线合并进
> `qingfeng/data/heuristics.json`。在 §3.1 原结构上新增/明确三条语义：
>
> 1. 每个 category 可含 `"match": "all" | "any"`（缺省 `"all"`）：
>    `all` = 扩展名 **且** 路径模式都命中才算该类；`any` = 命中其一即可。
>    **installer 类必须用 `all`**，否则所有 `.exe`（包括程序运行时）都会被误判"可删"——本机实测会
>    误判 6.79 GB。
> 2. **分类优先级**：`never_touch` 命中 → 直接 keep/danger；其次按 categories 顺序（越靠前优先级越高）；
>    再套 `age_rules`（按 min_size 从大到小匹配第一条）；都没命中 → `large-misc` + `review`。
> 3. `never_touch` 命中即 `advice="keep"`、`risk="danger"`，界面显示"系统/程序目录，请勿手动删除"。

结构（core-engine 负责落地，researcher 给建议值）：

```json
{
  "version": 1,
  "categories": [
    {"key": "installer", "label": "安装包 / 升级残留",
     "exts": [".exe", ".msi", ".msix", ".appx", ".zip", ".7z", ".iso"],
     "path_globs": ["*\\Downloads\\*", "*\\@*-updater\\*", "*\\ProgramData\\*\\Downloads\\*",
                    "*\\AppData\\Local\\Temp\\*"],
     "advice": "safe_delete", "risk": "caution"},
    {"key": "video", "label": "视频 / 录屏", "exts": [".mp4", ".mkv", ...], "advice": "review"},
    {"key": "vm", "label": "虚拟机磁盘", "exts": [".vhd", ".vhdx", ".vmdk", ".qcow2", ".iso"], "advice": "review"},
    {"key": "backup", "label": "备份 / 旧版本副本", "exts": [".bak", ".old", ".backup"], "advice": "review"},
    {"key": "devcache", "label": "开发缓存与构建产物", "exts": [], "path_globs": ["*\\node_modules\\*", "*\\target\\*", "*\\.gradle\\*"], "advice": "review"},
    {"key": "app-version-dup", "label": "程序的旧版本副本", "path_globs": ["*\\EdgeCore\\*", "*\\EdgeWebView\\*"], "advice": "review"},
    {"key": "keep", "label": "建议保留（模型/数据/数据库）", "exts": [".gguf", ".safetensors", ".db", ".sqlite"], "advice": "keep", "risk": "danger"}
  ],
  "age_rules": [
    {"min_size": 1073741824, "min_age_days": 365, "advice": "review", "reason": "超过 1 年未修改"},
    {"min_size": 536870912,  "min_age_days": 180, "advice": "review", "reason": "半年未修改"}
  ],
  "never_touch": ["*\\Windows\\*", "*\\Program Files\\*", "*\\Program Files (x86)\\", "*\\$Recycle.Bin\\*",
                  "*\\System Volume Information\\*", "*\\AppData\\Local\\Packages\\*"]
}
```

`never_touch` 命中即 `advice="keep"` + `risk="danger"`，界面上必须显示"系统/程序目录，请勿手动删除"。

### 3.2 分组展示（界面按此顺序）

| 顺序 | group key | 中文标题 | 默认建议 |
|---|---|---|---|
| 1 | `installer` | 安装包 / 升级残留 | 放心清理（回收站） |
| 2 | `app-version-dup` | 程序的旧版本副本 | 可清理（确认程序未使用旧版） |
| 3 | `backup` | 备份 / 旧副本 | 可清理（确认无回滚需求） |
| 4 | `devcache` | 开发缓存 / 构建产物 | 可清理（下次构建会重建） |
| 5 | `video` / `archive` / `vm` | 视频 / 压缩包 / 镜像 | 你来判断（可迁移到 D/E） |
| 6 | `large-misc` | 其它大文件 | 你来判断 |
| 7 | `keep` | 建议保留 | 不要删 |

## 3.4 动作授权矩阵（安全红线，Lead 定稿）

空间透视页的删除/迁移是**用户驱动的任意路径操作**，比 v1 规则清理危险得多。实测分类结果暴露了三个必须修的问题：
① `app-version-dup` 会把 `C:\Program Files (x86)\Microsoft\Edge\Application` 整个程序目录标成"旧版本副本"（1.33 GB），
一键删除会直接破坏 Edge；② 分类同时返回父目录与其子项（`Copilot` 与 `Copilot\Application`），大小重复计、删除顺序冲突；
③ Intel 驱动下载包在 `C:\ProgramData` 下，若一刀切禁止 ProgramData 会漏掉本该能清的残留。

因此新增：

```python
# decide.py
@dataclass
class ActionPolicy:
    can_delete: bool
    can_move: bool
    deny_reason: str = ""

def action_policy(item: ReviewItem) -> ActionPolicy
def delete_many(paths: list[str], *, progress=None, cancel=None) -> CleanResult
    # 逐条 action_policy + safety.assert_deletable，默认走回收站；失败的进 failed/skipped
```

授权矩阵：

| 区域 | 可删除 | 可迁移 | 说明 |
|---|---|---|---|
| `C:\Windows\**` | ❌ | ❌ | 只展示、只给管理员命令 |
| `C:\Program Files( (x86))\**` | ❌ | ❌ | 程序安装目录；`app-version-dup` 类一律降级为 `keep/danger` + "请在程序设置或卸载中处理" |
| `C:\ProgramData\**` | ⚠️ 仅 `category=="installer"` 且路径含 `\Downloads\`（如 Intel DSA） | ❌ | 其余一律 keep |
| `C:\Users\<me>\**`（含 AppData） | ✅ 回收站 | ✅ | 主战场；`AppData\Local\Packages\**` 仍拒绝（never_touch） |
| `D:\` / `E:\` 等非系统盘 | ✅ 回收站 | ✅ | 迁移目标不能是源所在盘 |
| `$Recycle.Bin` / `System Volume Information` / `Recovery` / `EFI` / `Boot` | ❌ | ❌ | 永不触碰 |

其它硬性要求：

1. `collect_review_items()` **必须剪枝嵌套**：只返回最上层条目（父目录已纳入时跳过其全部子项），保证大小不重复计算、删除/迁移顺序不冲突。返回项大小之和必须等于分组合计。
2. `action_policy` 与 `safety.assert_deletable` **两道都要过**，缺一不可；GUI 按钮状态直接读 `action_policy`，deny 时禁用按钮并显示 `deny_reason`。
3. `move_many` 同样受矩阵约束（Windows/Program Files/ProgramData 一律不能迁出，否则程序会坏）。
4. 所有删除仍默认进回收站；「永久删除」在空间透视页**不提供**（只有清理页保留该开关）。

## 4. GUI 新增（`qingfeng/ui/`）
顶部主导航从 1 页变 2 页：**「清理」**（v1 原样保留）和 **「空间透视」**（新）。两页共享顶部磁盘卡片。

### 4.1 空间透视页布局

```
┌ 磁盘构成条：已用 122.8 GB 拆成  系统 35.4 | 用户 36.8 | 程序 12.5 | 休眠/交换 7.0 | 还原点等 27.1 | 可清缓存 0.7 ┐
├ 左：目录树（Treeview，大小降序，可展开）         │ 右上：Treemap 画布（当前目录下一层）        │
│   面包屑 C: > Users > HUAWEI > AppData         │  色块=目录/文件，面积=大小，点击下钻，双击打开 │
├ 右中：大文件决策区（分组 chips + 表格）                                             │
│   列：☐ 名称 | 大小 | 修改时间 | 年龄 | 类型 | 建议 | 操作（打开位置 / 回收站 / 迁移）      │
├ 底部：系统占用卡片（WinSxS 14.4 GB / 休眠 6.76 GB / 回收站 / 还原点）  一键复制管理员命令    │
└ 迁移栏：目标盘选择 [D: 247 GB 可用] [E: 262 GB 可用] → 「迁移选中项」                     ┘
```

要求：

- **Treemap**：自绘 `tk.Canvas`，Squarified 算法；颜色按类型（目录=蓝灰系、安装包=橙、视频=紫、镜像=深蓝、保留=灰）；点击选中/下钻，悬停显示 `路径 + 大小 + 修改时间`；返回上级按钮。渲染 200 个块以内必须 < 200 ms。
- **索引构建**：后台线程 + 进度条（"正在统计 C:\Users\HUAWEI\AppData... 已处理 12.3 万项"）+ 取消；优先读缓存（24h 内秒开），提供"重新扫描"按钮。
- **大文件区**：默认按 `size` 降序，支持按 分组/年龄 过滤、多选；勾选后底部显示"已选 N 项 / 合计 X GB" 与三个动作：**移入回收站**、**迁移到 D: / E:**、**打开所在位置**。
- 迁移必须二次确认：显示目标盘、目标根目录、预计耗时提示、"复制成功后才会删除源文件"。迁移中进度条 + 可取消（取消时已完成的保留、未完成的源文件不动）。
- 系统占用卡片：每项显示大小 + 说明 + 建议命令（`DISM /Online /Cleanup-Image /StartComponentCleanup`、`powercfg /h off`、`vssadmin resize shadowstorage` 等），带「复制命令」与「以管理员身份打开 PowerShell」按钮；**工具自身绝不代替用户执行这些命令**。
- 页面顶部一句人话总结，例如：
  「C 盘 122.8 GB 已用：系统 35.4 GB（其中 14.4 GB 是组件库，可 DISM 回收）、你的文件 36.8 GB、程序 12.5 GB、休眠文件 6.8 GB。发现 61 个大文件合计 19.5 GB，其中 8 个是升级残留（建议清理）、23 个视频/镜像（建议迁移到 D 盘）。」
- 禁止在子线程碰 tkinter；所有动作都要有失败提示与撤销提示（回收站可还原）。

## 5. 交付与验收（v2 补充）

1. v1 全部测试仍绿（118 个用例）；新增 space/decide 测试 ≥ 20 个（索引正确性/取消/截断/缓存往返/分类/迁移失败回滚/受保护路径拒绝）。
2. 真机：`build_index` 在 C 盘 ≤ 60 s 且不崩；索引缓存能秒开；大文件列表能列出 ≥ 50 个 ≥100 MB 文件；分类命中本机 61 个大文件中的绝大多数。
3. 迁移实测：在工作区临时目录造 200 MB 文件，迁移到 D 盘目标目录成功且源文件消失、内容校验一致；取消/失败路径不丢源文件。
4. GUI：截图 3 张（空间透视浅色、大文件分组、迁移确认框），175% DPI 下完整可见。
5. `tools/verify.ps1` 增加空间索引冒烟（`python -c "..."` 或脚本入口）与 v2 单测。
6. README/usage/faq 更新 v2 功能说明；CI 增加 space 相关测试步骤。

## 6. Lead 裁决规则

- 任何删除/迁移必须经过 `safety` 守卫 + 用户显式确认；界面上「建议保留」组默认不勾选，且勾选后按钮仍可用但要额外提示。
- 系统级操作（DISM/powercfg/vssadmin）**只提供命令**，工具不代执行。
- 索引结果不允许上传、不允许写注册表；缓存只写 `%LOCALAPPDATA%\QingFengCleaner\`。

## 7. v2.1 权限模型与交付修订（2026-10-23）

### 7.1 问题

非管理员运行时，v1 的一半规则（Windows 临时文件、Prefetch、更新缓存、CBS/系统日志、系统错误报告等）
读不到系统目录，旧实现把它们显示成「扫描出错」；而唯一能解除限制的「以管理员身份重启」在**打包版**里
是坏的：`restart_as_admin()` 把 `sys.argv`（含 argv[0]）整体塞进 `ShellExecuteW` 的 params，frozen 下
命令行变成 `QingFengCleaner.exe "QingFengCleaner.exe"`，argparse 报 unrecognized arguments、exit=2，
UAC 点「是」后新进程立刻退出 —— 用户观感就是「系统层面总是失败」。

### 7.2 权限模型（定稿）

| 场景 | 行为 |
|---|---|
| 非管理员 | `requires_admin` 规则**不扫描**（不再产生 PermissionError 噪音），`RuleScanResult.skipped_reason="需要管理员权限…"`、`requires_elevation=True`；GUI 显示可点击横幅与「管理员模式」chip |
| 提权 | `winapi.relaunch_command(extra_args)`：frozen → `(sys.executable, extra_args or sys.argv[1:])`；source → `(python.exe, [sys.argv[0], *tail])`。`ShellExecuteW("runas")` 后当前实例退出，新实例保留 `--page` |
| 管理员 | 系统规则正常扫描/清理；`CleanResult.skipped_admin` 为空；空间透视的还原点探测变为可用 |
| 清理时被跳过 | `CleanResult.skipped_admin` 收集规则名，GUI 在结果摘要里给「以管理员身份重启并重试」 |
| 永久失败 | 记录到 `failed`，界面如实展示，不静默 |

### 7.3 单文件 exe 不再作为交付物

实测（真实用户令牌）：`dist\QingFengCleaner.exe`（PyInstaller onefile）在工作区**中文路径**下启动时
弹出 `Could not create temporary directory!` 并挂起；同一文件复制到 ASCII 路径后 `--version` 正常；
最小 hello onefile 同样表现。**目录版**（onedir）在中文路径下 `--scan-only` / GUI 均正常。

结论：默认只交付目录版 `dist\QingFengCleaner-onedir\` + 启动脚本 `dist\启动清风清理.bat`；
单文件版仅在显式 `-OneFile` 时构建到 `dist\_experimental\`，安装说明不推荐。

