# GitHub 开源清理方案调研（Windows 磁盘/缓存清理）

> 面向 **清风清理 QingFeng Cleaner**（Windows 11 · Python 3.12 标准库 + tkinter + ctypes · MIT）。
> 产出目标：给 `core-engine` 一份"能落地"的规则与实现参考，而不是论文。
> 配套规则集：`docs/research/rules-recommendation.json`（38 条调研建议，字段严格按 `docs/ARCHITECTURE.md` §4；经 core-engine 合并与红队修复后，最终规则集为 `qingfeng/data/rules/*.json` 的 **59 条**，映射见 [integration-map.md](integration-map.md)）。

**数据采集方式与时间**（2026-10-22，本机时间）：

- 本机无法访问 `github.com` HTML 页面与 `raw.githubusercontent.com`（多次 30s 超时），因此星标/许可证/语言全部取自 **GitHub REST API**（`https://api.github.com/repos/<owner>/<repo>`，HTTP 200 可直连），规则文件正文取自 **jsDelivr 镜像**（`https://cdn.jsdelivr.net/gh/<owner>/<repo>@<branch>/<path>`）与 **Microsoft Learn**。
- 星标为采集时刻快照，量级参考即可；许可证以 API 返回的 SPDX 为准，并注明仓库性质。

---

## 1. 项目速览

| 项目 | Stars（量级） | 语言 | 许可证 | 清理维度 | 规则存储 | 回收站 / 管理员 | 对我们 |
|---|---|---|---|---|---|---|---|
| [bleachbit/bleachbit](https://github.com/bleachbit/bleachbit) | 7.2k | Python | **GPL-3.0** | 系统/浏览器/应用/隐私，Linux+Windows | CleanerML XML（`cleaners/*.xml`），可导入 winapp2.ini | 支持 shred、可选回收站；部分规则需 root/管理员 | 架构参考价值最高，**代码不可抄** |
| [Chuyu-Team/Dism-Multi-language](https://github.com/Chuyu-Team/Dism-Multi-language) | 20.6k | HTML（仓库本身是多语言/issue 仓库） | **MIT**（仅此仓库内容） | WinSxS 组件清理、更新缓存、驱动清理、CompactOS | 不在此仓库（Dism++ 主体源码未开源） | 需管理员；DISM API | 学"该调什么系统命令"，许可无障碍 |
| [MoscaDotTo/Winapp2](https://github.com/MoscaDotTo/Winapp2) | 1.0k | VB.NET（配套 winapp2ool） | **CC-BY-SA-4.0** | 4068 条社区条目：浏览器/应用/系统全覆盖 | INI（`[名称 *]` + `FileKey1=路径\|模式;模式\|RECURSE\|REMOVESELF`） | 由宿主程序决定；本身只是声明式数据 | **路径事实**的宝库，但数据带 ShareAlike |
| [hellzerg/optimizer](https://github.com/hellzerg/optimizer) | 18.2k | C# | **GPL-3.0** | 临时文件、回收站、浏览器缓存、隐私痕迹、注册表调整 | C# 代码内硬编码 + 少量列表 | 回收站/永久删除可选；多数功能需管理员 | 功能清单参考，代码不可抄；**仓库已于 2026-01 归档** |
| [builtbybel/Bloatynosy](https://github.com/builtbybel/Bloatynosy) | 5.7k | C# | **MIT** | 去 bloat、Appx 卸载、部分清理 | C# 代码 + 内嵌清单 | 需管理员 | Win11 场景/Appx 处理思路可复用 |
| [builtbybel/ThisIsWin11](https://github.com/builtbybel/ThisIsWin11) | 5.2k | C# | **MIT** | Win11 精简/隐私开关（已归档，2023-06 后停更） | C# 代码 | 需管理员 | 已过时，仅作 Win11 设置项索引 |
| [Raphire/Win11Debloat](https://github.com/Raphire/Win11Debloat) | 60.4k | PowerShell | **MIT** | 去预装应用、关遥测、隐私/资源管理器开关 | PowerShell 数组 + 注册表键值 | 需管理员 | 注册表项/Appx 名单可直接借鉴（MIT） |
| [windirstat/windirstat](https://github.com/windirstat/windirstat) | 4.4k | C++ | **GPL-3.0** | 磁盘占用可视化（treemap）、大文件、重复文件 | 无规则集，纯扫描器 | 只读/删除走资源管理器 | "大文件"分类的扫描交互参考 |
| [IridiumIO/CompactGUI](https://github.com/IridiumIO/CompactGUI) | 9.1k | VB.NET | **GPL-3.0** | 用 Windows WOF/CompactOS 压缩游戏与程序目录 | 无规则集 | 需管理员（部分） | 与"清理"互补的减容手段，思路参考 |
| [qarmin/czkawka](https://github.com/qarmin/czkawka) | 34.4k | Rust | GitHub API 显示 **NOASSERTION**（仓库 LICENSE 为 MIT，含第三方资源声明，需人工确认） | 重复文件、相似图片、空目录、大文件 | 无规则集，GUI 参数 | 删除可走回收站 | 大文件/重复文件体验参考 |
| WizTree / CCleaner | 闭源 | — | 专有 | WizTree：NTFS MFT 级极速扫描；CCleaner：最大众的清理项集合 | 闭源 | — | **只能看思路与文档，绝不能抄代码** |

> 说明：Dism++ 的发行物由 Chuyu-Team 维护，但 `Dism-Multi-language` 仓库只放多语言文本与问题跟踪，**MIT 只覆盖该仓库的文件**，不代表 Dism++ 可执行程序/内核源码是 MIT。这一点在许可证结论里会再强调。
>
> Winapp2.ini 文件头自述：`Version: 260915`、`# of entries: 4,068`、`Winapp2.ini is fully licensed under the CC-BY-SA-4.0 license agreement`（读自 jsDelivr 抓到的文件头）。

---

## 2. 各项目值得看的地方

### 2.1 BleachBit（GPL-3.0，Python）—— 我们架构上的"近亲"

- **CleanerML 结构**（已抓取 `cleaners/windows_explorer.xml`、`cleaners/chromium.xml` 原文）：
  - `<cleaner id="..." os="...">` → `<option id="...">` → 多个 `<action command="..." search="..." path="..."/>`。
  - `command` 取值：`delete`、`winreg`、`process`、`sqlite.vacuum`、`cookie`、`chrome.history` 等；`search` 取值：`file`（单文件）、`glob`（通配）、`walk.files`（递归文件）、`walk.all`（递归文件+目录）。
  - 支持 `<var name="profile"><value os="windows">%LocalAppData%\...\Default</value></var>` 变量，变量用 `$$profile$$` 引用 —— 我们的 `paths` + `%ENV%` 是同一思路的简化版。
  - 有 `<running type="exe" same_user="true">chrome.exe</running>` 声明：**浏览器在跑就提示先关闭**，这个字段对我们的 `Rule` 值得以后扩展（当前 §4 没有，不要擅自加）。
  - 有 `<warning>` 字段，对应我们 GUI 的"风险提示"。
- **实测可用路径（可直接落到我们规则里）**：
  - 缩略图：`%LOCALAPPDATA%\Microsoft\Windows\Explorer\thumbcache*.db`；清理顺序是 `taskkill /f /IM explorer.exe` → 删 → `%WINDIR%\explorer.exe %WINDIR%\explorer.exe` 重启。
  - 最近文件：`%APPDATA%\Microsoft\Windows\Recent\*.lnk`；Jump List：`...\Recent\AutomaticDestinations\*.automaticDestinations-ms`、`...\CustomDestinations\*customDestinations-ms`。
  - Chromium（Chrome/Edge 同源）：`User Data\<Profile>\` 下的 `Cache`、`Code Cache`、`GPUCache`、`ShaderCache`、`GrShaderCache`、`DawnCache`、`DawnGraphiteCache`、`DawnWebGPUCache`、`Service Worker`，以及 `User Data\` 根下的 `ShaderCache`/`GrShaderCache`/`GPUPersistentCache`/`component_crx_cache`/`extensions_crx_cache`。
  - **明确只对 `Local State` 做 JSON 键清理，不整文件删除**（`Local State` 里有 profile 索引和加密密钥元数据，整删会搞坏登录态）。
- 优点：规则与引擎分离干净、跨平台、有 `--preset` 静默清理、Winapp2 兼容。
- 缺点：GPL 传染；GTK 依赖；Windows GUI 体验老派；规则 XML 对小白不友好。
- **结论：抄结构不抄代码。** 我们的 `Rule` dataclass 已经是 CleanerML 的合理裁剪。

### 2.2 Dism++（仓库 MIT，但只是多语言仓库）

- 清理能力来自 Windows 的 DISM/CBS 组件服务：更新清理、WinSxS 过期组件、驱动包、日志。**它不做"手删系统目录"这种事**，这点最关键：凡是 WinSxS/更新相关，一律交给系统 API。
- 微软官方（已抓取 Learn 原文）给出的可执行命令（**我们只做 `report`，让用户自己执行或提权执行**）：
  - 分析：`Dism.exe /Online /Cleanup-Image /AnalyzeComponentStore`
  - 清理（同计划任务，但立即删除、无 30 天宽限、无 1 小时超时）：`Dism.exe /Online /Cleanup-Image /StartComponentCleanup`
  - 深度清理：`Dism.exe /Online /Cleanup-Image /StartComponentCleanup /ResetBase`
    （警告：执行后**所有已安装更新无法卸载**，但不影响以后卸载新更新）
  - 服务包：`Dism.exe /Online /Cleanup-Image /SPSuperseded`
  - 计划任务入口：`schtasks.exe /Run /TN "\Microsoft\Windows\Servicing\StartComponentCleanup"`
  - MS 原文警告：*"Deleting files from the WinSxS folder or deleting the entire WinSxS folder may severely damage your system so that your PC might not boot and make it impossible to update."*
- 对我们：`系统维护` 分类全部做成 `action=report` + `report_note`；引擎最多提供"以管理员身份执行"的按钮，**默认不自动跑 DISM**。

### 2.3 Winapp2.ini（CC-BY-SA-4.0）

- 格式（行业事实标准，CCleaner/System Ninja/BleachBit 等都能吃）：
  - `[条目名 *]`（星号 = 默认勾选/默认可用），`LangSecRef=`（分类），`Section=`（Applications/Games 等）。
  - `DetectFile=` / `Detect1=HKLM\...`：**存在才显示该条目** —— 这是"按机器裁剪规则"的经典做法，避免列表里一堆灰条。
  - `FileKey1=%LocalAppData%\Foo\*Cache*|*|REMOVESELF`：`路径前缀|匹配模式（分号分隔多模式）|RECURSE（递归）|REMOVESELF（清完删目录）`。
  - `RegKey1=HKCU\...|值名`：注册表项清理（我们 §4 的 `Rule` 没有注册表字段，**不要动契约**；这类条目只在文档里列，当前版本不实现）。
  - `ExcludeKey`/`Warning`：白名单与警告。
  - `\*` 段表示目录通配（如 `User Data\*` 匹配所有 profile），这和我们的规则里用 `*` 段的思路一致。
- 优点：覆盖极全、声明式、可审计、可裁剪（winapp2ool -trim）。
- 缺点：数据量大到拖慢启动（README 自己承认）；条目质量参差；**CC-BY-SA 不能直接搬**。
- 我们的用法：把它的 **路径事实**（"某软件的缓存在哪"）作为线索，用自己的 schema 重写 JSON，条目名/描述全部自写，`source` 标注 `winapp2.ini`。

### 2.4 Optimizer（GPL-3.0，已归档）

- C# WinForms，功能表很全：临时文件、回收站、浏览器缓存、最近文件、剪贴板、失效快捷方式、DNS 缓存、缩略图缓存、Windows 更新缓存等；附带大量注册表 tweak。
- 亮点：**一键"清理 + 优化"分层**，危险项有二次确认；有 portable 模式。
- 缺点：GPL；2026-01 归档停更；部分 tweak 争议大（关遥测/关 Defender 相关）。
- 对我们：只借"功能清单 + 风险分级"；我们不做注册表 tweak（超出"清理"定位）。

### 2.5 Win11Debloat（MIT）/ Bloatynosy（MIT）/ ThisIsWin11（MIT）

- 这三个是 Win11 场景的"去预装 + 隐私开关"脚本/程序，MIT 许可最友好。
- 可借鉴：`Get-AppxPackage`/`Remove-AppxPackage` 名单、隐私相关注册表键（`HKCU\Software\Microsoft\Windows\CurrentVersion\...`）、遥测开关清单。
- 但注意：**"去 bloat" 与"清垃圾"是两件事**。我们的 `系统`/`应用` 分类只清缓存文件，不卸载应用、不改注册表；这些项目的价值主要在于"知道 Win11 哪些目录在长大"（例如 `%LOCALAPPDATA%\Packages` 下的 UWP 数据 —— 但**这是雷区，见 §5 风险清单**）。

### 2.6 大文件可视化：WinDirStat（GPL-3.0）/ WizTree（闭源）/ Czkawka（NOASSERTION）

- WinDirStat：treemap + 扩展名统计 + 删除回调资源管理器；C++/MFC 老架构，代码不可抄。
- WizTree：**闭源免费**。它的"快"来自直接读 NTFS `$MFT`（USN journal / 原始卷），需要管理员卷句柄；这个思路**只能当知识**，我们 v1 不实现（Python 标准库做不到，风险也高）。
- Czkawka：Rust，重复文件/相似图片/大文件，UI 体验是现代清理工具的标杆（分栏、可撤销）。
- 对我们 `大文件` 分类：用 `os.scandir` + `os.stat(follow_symlinks=False)` 做 top-N 足够（C 盘 50 万文件量级，Python 也能在十秒级完成），**不要**为了快去做 MFT 解析。

### 2.7 闭源参照：CCleaner / WizTree

- CCleaner 的项清单（Custom Clean 的 Applications/Windows 分组）是行业事实标准，但闭源 + 曾有捆绑历史，**只当需求清单**。
- 一律不引用其图标、文案、代码。

---

## 3. 我们可借鉴的实现要点（具体到 API / 路径 / 命令）

### 3.1 Win32 API（ctypes，全部标准库可实现）

| 用途 | API | 备注 |
|---|---|---|
| 展开 `%VAR%` | `ExpandEnvironmentStringsW` 或 `os.path.expandvars` | `%LOCALAPPDATA%` 等一定存在；`%TEMP%` 可能指向别的盘，别假设在 C 盘 |
| 已知文件夹 | `SHGetKnownFolderPath(FOLDERID_LocalAppData / RoamingAppData / LocalAppDataLow / Profile / ProgramData / Windows)` | 比拼 `C:\Users\<user>` 稳，支持漫游/重定向；返回值需 `CoTaskMemFree` |
| 回收站删除 | `IFileOperation` + `FOF_ALLOWUNDO`（Vista+，推荐）；旧式 `SHFileOperationW` + `FOF_ALLOWUNDO` | 我们 §4 已定 `SHFileOperationW`，可用；注意 `pFrom` 需要**双 null 结尾**的路径串 |
| 清空回收站 | `SHEmptyRecycleBinW(NULL, NULL, SHERB_NOCONFIRMATION\|SHERB_NOPROGRESSUI\|SHERB_NOSOUND)` | 静默清空；先 `SHQueryRecycleBinW` 拿条数与字节数（`SHQUERYRBINFO`）用于扫描显示 |
| 磁盘容量 | `GetDiskFreeSpaceExW` | 返回 free/total，注意 64 位拆分 |
| 快速遍历 | `FindFirstFileExW(..., FindExInfoBasic, ..., FIND_FIRST_EX_LARGE_FETCH)` | 比 `FindFirstFileW` 快；Python 用 `os.scandir` 已足够，别过早优化 |
| 真实大小 | `GetCompressedFileSizeW` | 压缩/稀疏文件按实际占盘算（WinSxS 大量硬链接，**我们只报告不统计**） |
| 是否管理员 | `OpenProcessToken` + `GetTokenInformation(TokenElevation)`，或 `IsUserAnAdmin`（不推荐） | 非管理员时 `requires_admin` 规则只扫描 |
| 浏览器在跑 | `CreateToolhelp32Snapshot` / `EnumProcesses` 查 `msedge.exe`/`chrome.exe`/`firefox.exe` | 在跑就提示"建议先完全退出浏览器"，删锁定文件只会大面积失败 |
| 占用进程 | Restart Manager：`RmStartSession`/`RmRegisterResources`/`RmGetList` | Dism++/CCleaner 级体验，v2 再做 |

### 3.2 扫描/删除的工程细节（这些都是"坑里学来的"）

- **junction/符号链接**：`%LOCALAPPDATA%\Application Data`、`%APPDATA%\Local Settings` 是**循环 junction**（指向自己）。递归扫描必须 `follow_symlinks=False` 且跳过 reparse point（`FILE_ATTRIBUTE_REPARSE_POINT`），否则会无限循环或跑到别的盘。
- **长路径**：`MAX_PATH=260`。`os.scandir` 在 Python 3.12 上对长路径仍需 `\\?\` 前缀或进程 manifest；删除前 `ntpath` 归一化，路径 > 259 时加 `\\?\`。
- **删除失败是常态**：文件被占用（杀软、浏览器、Office）→ 记进 `CleanResult.failed`，**不要中断整轮**，也不要重试到卡死。
- **回收站配额**：回收站默认约占卷容量 5%，超大文件（>配额）会删除失败。`mode="recycle"` 下失败的文件应汇总提示"部分文件超出回收站容量，可改用永久删除"，绝不静默降级为永久删除。
- **先扫后删、二次确认**：BleachBit 的 `preview` 模式、我们的 `--scan-only` 都是同一件事，保持。
- **大小统计**：目录大小要递归累加；硬链接会重复计数（`*Recycle.Bin`、WinSxS 均大量硬链接），所以我们 **`大文件` 分类只列文件、不列目录**。

### 3.3 Windows 自带"正规清理入口"（最安全的一类）

- **磁盘清理 `cleanmgr`**：清理项注册在 `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches\<Handler>`，每项有 `StateFlags0000..0003` DWORD。
  - 用法：`cleanmgr /sageset:1` 勾选保存 → `cleanmgr /sagerun:1` 静默执行。
  - 我们做成 `report`，`report_note` 直接给出这两条命令（见 `cleanmgr-sagerun` 规则）。
- **存储感知 Storage Sense**：设置 > 系统 > 存储 > 存储感知；一键"临时文件"入口就是 `cleanmgr`/Storage Sense 的聚合。
  - 其注册表位置为 `HKCU\Software\Microsoft\Windows\CurrentVersion\StorageSense\Parameters\StoragePolicy`，但**值名（01/08/32/2048 等）是未文档化的内部实现**，随版本变；**不要写死、不要帮用户改**，只在文档里提"可引导用户去设置页"。
- **Windows.old**：设置 > 系统 > 存储 > 临时文件（"以前的 Windows 安装"）或 `cleanmgr`；删除后无法回退。
- **休眠**：`powercfg /hibernate off`（管理员）；页文件：系统属性 > 高级 > 虚拟内存。
- **还原点**：`vssadmin list shadowstorage` 查占用，系统保护里调上限 / 删旧还原点；`C:\System Volume Information` 不可手删。
- **更新缓存**：`%WINDIR%\SoftwareDistribution\Download`，更稳的做法是先 `net stop wuauserv` + `net stop bits` 再删、再启动服务（我们只做 `delete` + 提示，不自动停服务）。

### 3.4 目录/路径清单（已交叉核实或高置信）

| 内容 | 路径 | 依据 |
|---|---|---|
| 缩略图/图标缓存 | `%LOCALAPPDATA%\Microsoft\Windows\Explorer\thumbcache_*.db`、`iconcache_*.db` | BleachBit `windows_explorer.xml`（已抓原文） |
| 最近文件 | `%APPDATA%\Microsoft\Windows\Recent\*.lnk` | 同上 |
| 跳转列表 | `%APPDATA%\Microsoft\Windows\Recent\{Automatic,Custom}Destinations\*` | 同上 |
| Edge 缓存 | `%LOCALAPPDATA%\Microsoft\Edge\User Data\<profile>\{Cache,Code Cache,GPUCache,...}` | BleachBit chromium + Winapp2 条目（已抓） |
| Chrome 缓存 | `%LOCALAPPDATA%\Google\Chrome\User Data\<profile>\...` | 同上 |
| Firefox 缓存 | `%LOCALAPPDATA%\Mozilla\Firefox\Profiles\<随机>.default-release\cache2` | BleachBit firefox.xml（目录结构常识 + 该 cleaner 存在） |
| 新版 Teams | `%LOCALAPPDATA%\Packages\MSTeams_8wekyb3d8bbwe\`（Microsoft Learn VDI 文档确认此包路径） | MS Learn：Teams VDI 要求 |
| 经典 Teams | `%APPDATA%\Microsoft\Teams\` | Winapp2 / 常识 |
| 微信 | `%USERPROFILE%\Documents\WeChat Files\<wxid>\FileStorage\Cache`（**仅 Cache**） | 常识 + Winapp2；4.x 见 §6 未核实 |
| Office | `%LOCALAPPDATA%\Microsoft\Office\16.0\OfficeFileCache`（15.0 同理） | Winapp2 条目 |
| 更新下载 | `%WINDIR%\SoftwareDistribution\Download` | 常识 + 各清理器 |
| 组件日志 | `%WINDIR%\Logs\CBS` | 常识 |
| 错误报告 | `%LOCALAPPDATA%\Microsoft\Windows\WER`、`%PROGRAMDATA%\Microsoft\Windows\WER` | Winapp2 |
| 崩溃转储 | `%LOCALAPPDATA%\CrashDumps`、`%WINDIR%\Minidump` | Winapp2 |
| WinSxS | `%WINDIR%\WinSxS`（**只报告**） | MS Learn 警告 |

---

## 4. 许可证结论（本项目 MIT，必须严格遵守）

**结论先行：本项目可以"借鉴思路、自己写代码、自己写规则数据"，但不允许拷贝任何 GPL 代码，也不允许把 CC-BY-SA 数据搬进仓库。**

| 来源 | 许可证 | 可以做什么 | 不可以做什么 |
|---|---|---|---|
| BleachBit | GPL-3.0 | 读它的 XML 了解"哪些路径该清"、"删缩略图要重启 explorer"这类**事实与方法**；在文档里引用并注明出处 | 复制 Python 代码、复制 CleanerML 文件内容进仓库、把其 cleaner XML 转换成我们的 JSON 直接分发 |
| Dism++ 多语言仓库 | MIT | 复用该仓库内的语言文本/少量脚本片段（保留版权声明） | 认为 Dism++ 主体源码也是 MIT —— 它并未开源；不要反编译/搬运 | 
| Winapp2.ini | CC-BY-SA-4.0 | 用其条目作为"路径在哪"的**事实线索**；自己重写名称/描述/JSON；标注 `source` | 把 winapp2.ini 或其条目文本/结构整体纳入 MIT 仓库（ShareAlike 会要求衍生作品同样 CC-BY-SA，与 MIT 冲突） |
| Optimizer / WinDirStat / CompactGUI | GPL-3.0 | 功能清单、交互思路、风险分级参考 | 复制代码、复制资源/图标/文案 |
| Bloatynosy / ThisIsWin11 / Win11Debloat | MIT | 代码片段可复用（保留版权与许可声明）；注册表键/Appx 名单可参考 | 直接大段搬运而不带声明；我们并不需要它们的功能 |
| Czkawka | NOASSERTION（待人工确认 LICENSE） | 仅 UI/交互参考 | 任何代码复用（许可不明） |
| CCleaner / WizTree | 专有 | 需求清单/思路 | 一切形式的代码、资源、文案复用 |

**对外合规动作**（交给 packaging-qa / Lead）：
1. 仓库根 `LICENSE` 用标准 MIT 文本；
2. 加 `NOTICE`/`THIRD_PARTY.md`：列出"规则参考来源 = bleachbit / winapp2.ini / Microsoft Learn"，并声明"规则数据为自行撰写，未包含第三方代码或数据文件"；
3. 代码中不出现第三方项目的类名/函数名/注释原文；注释里引用 MS Learn 链接没问题（政府作品/文档引用属合理使用，且我们只引用命令语法）。

---

## 5. 误删风险清单（来自社区踩坑与官方警告）

> 这张表建议 Lead 直接转给 `core-engine` 的 `safety.py` 作者与 GUI 的确认弹窗文案。

| # | 坑 | 后果 | 我们的对策 |
|---|---|---|---|
| 1 | 整个删 `%LOCALAPPDATA%\Packages` | UWP/MSIX 应用（新版 Teams、Store 应用、部分系统组件）设置与数据全丢 | 只在 `Packages\MSTeams_8wekyb3d8bbwe\LocalCache\...` 这类**具体子目录**写规则；`Packages` 根列入保护路径建议 |
| 2 | 删 Edge/Chrome 的 `Local State` / `Login Data` / `Network\Cookies` | 掉登录、密码库损坏、Cookie 全丢 | 浏览器规则只列缓存目录；`exclude_globs` + `protected_globs` 双保险；`History` 只进 advanced 规则 |
| 3 | 清空 `Prefetch` | 争议项：对 SSD 收益极低，且会让前几次启动变慢（系统要重建预读数据） | **不写规则**。若要，只允许 `min_age_days>=30` + `advanced` + 默认关闭 |
| 4 | 手删 `WinSxS` 内容 | 系统可能无法启动、无法更新（MS 明确警告） | 只 `action=report`；safety 硬保护 `%WINDIR%\WinSxS` |
| 5 | 删 `C:\Windows\Installer` | 软件无法修复/卸载/MSI 卸载报错 | 只报告 |
| 6 | 删 `C:\System Volume Information` | 破坏 VSS/还原点 | 保护路径 + 只用 `vssadmin` 引导 |
| 7 | 删微信/QQ 的 `FileStorage\File|Video|Image`、`Documents\Tencent Files` | 用户聊天文件/接收文件丢失（**不可恢复**） | 只清 `FileStorage\Cache`；描述里写明"不动聊天记录/图片/视频" |
| 8 | 递归走进 junction（`%LOCALAPPDATA%\Application Data` 等） | 无限循环 / 删到别处 | `follow_symlinks=False` + 跳过 reparse point + `within_root()` 校验 |
| 9 | 停/重启 explorer 时机不对 | 桌面闪黑、任务栏图标重建（不致命但吓人） | 只在删缩略图规则里做，且先检测、后重启、GUI 明确提示 |
| 10 | 大文件走回收站 | 超出回收站配额直接失败 | 失败汇总提示，**不自动降级永久删除** |
| 11 | OneDrive 占位文件/同步目录 | 触发全量重同步，流量爆炸 | `%USERPROFILE%\OneDrive` 在 §5 已保护；规则不触碰 |
| 12 | `requires_admin` 规则在非管理员下强行删 | 大量 PermissionError、半途而废 | 非管理员：只扫描 + `skipped_reason="需要管理员权限"` |
| 13 | 网页缓存删了导致 PWA/离线数据重建、部分站点重新登录 | 体验受损 | 浏览器缓存规则描述里提示"建议先退出浏览器；清理后首次打开网页会变慢" |
| 14 | 删 `%PROGRAMDATA%` 下全局缓存 | 多用户/服务受影响 | 仅限 WER 这类纯诊断目录，且 `requires_admin` |
| 15 | `Windows.old` 手删失败/权限怪 | 删一半，占用还在 | 只报告，引导 `cleanmgr`/设置页 |

---

## 6. 未在线核实 / 未写入规则集的条目（诚实标注）

网络对 `github.com` HTML、`raw.githubusercontent.com` 不稳定（多次超时），Winapp2.ini 全文约 2 MB 抓取被截断，因此下列内容**未能逐条在线核实**，处理方式如下：

1. **微信 4.x（`xwechat_files`）**：新版微信把数据迁到 `%USERPROFILE%\Documents\xwechat_files\`，其缓存子目录命名与 3.x 不同。**未核实 → 不写入规则集**（`wechat-cache` 只覆盖经典 `WeChat Files\*\FileStorage\Cache`）。落地前请在真机 `dir` 一下再补规则。
2. **QQ（NT 版）**：`Documents\Tencent Files\nt_qq\...` 里同时有聊天文件（用户数据）与缓存，目录语义未核实 → **不写入**。若要做，必须先确认某个子目录"纯缓存"，否则宁可缺一条规则。
3. **钉钉**：缓存位置随版本/企业配置变化，未核实 → **不写入**。
4. **Storage Sense 注册表值名**：属未文档化内部实现，**不写进规则/代码**，只引导用户去设置页。
5. **Firefox 规则**：`cache2` 结构来自 BleachBit firefox cleaner 的常识性结构；本地未抓原文（可后续用 jsDelivr 抓 `cleaners/firefox.xml` 复核）。
6. **应用类路径**（Teams 经典版、微信、Office、Discord、VS Code、Steam）：来自 Winapp2.ini / BleachBit 的通行条目与常识，**未逐条抓取原文核对**；已按"只清明确的缓存子目录"原则收敛风险，且全部为 `min_age_days<=1` 或缓存专用目录。真机联调时应逐条 `Test-Path` 验证（见 §7 自检脚本样例）。

---

## 7. 规则落地约定（给 `core-engine` 的交接说明）

`rules-recommendation.json` 严格按 `ARCHITECTURE.md` §4 的 `Rule` 字段书写（16 个字段全给出，含空值），以下是**用规则的引擎需要知道的语义约定**：

1. **`paths` 允许包含单个 `*` 段**。用于：浏览器多 profile（`User Data\*\Cache`）、Firefox 随机 profile 目录（`Profiles\*\cache2`）、微信 `<wxid>` 目录。实现方式：按 `\` 切段，遇到 `*` 就枚举该层目录（`os.scandir`），拼出真实路径列表。
   - 若引擎暂不支持：这些规则会**0 命中**（安全，但用户会觉得"没扫到东西"），因此建议 core-engine 花约 10 行代码支持它。
2. **`globs` 语义**：对 `paths` 下的条目做匹配。我们的写法只有三类：`["*"]`（目录内全部内容，配合 `follow_dirs` 递归）、扩展名模式（`*.dmp`/`*.exe`/`*.db`）、固定文件名。实现上"遍历 `paths` → 命中 glob 的文件/目录→ 删除"即可，**不需要**复杂 `**` 语法。
3. **`exclude_globs` vs `protected_globs`**：两者都是"命中即跳过"，`exclude_globs` 用于通配文件名黑名单（`*Cookies*`），`protected_globs` 用于固定文件名（`Login Data`、`Bookmarks`）。引擎只需都按"跳过"处理即可。
4. **`report` 规则可能是"无 paths"或"单文件路径"**：
   - `restore-points`、`cleanmgr-sagerun` 的 `paths` 为 `[]`：引擎应**仅输出 `report_note`**（一条建议），不要报错、不要计入可清理字节；
   - `hibernation-file`/`pagefile`/`winsxs-component-store` 的 `paths` 指向单文件或大目录：`stat` 单个路径即可，目录仅取浅层大小，**绝不递归进 WinSxS 统计**。
5. **`empty_recycle_bin` 规则**：`paths` 为 `[]`，大小/条数由 `SHQueryRecycleBinW` 提供；GUI 需二次确认（`dialogs.confirm_clean`）。
6. **`min_age_days`**：按 `mtime` 判定（`now - mtime > N*86400`），`0` 表示不限。
7. **`requires_admin=true` 且当前非管理员**：扫描照做（读不到的子项记 `error`/跳过），但**清理阶段整条规则禁用**并展示 `skipped_reason="需要管理员权限，可重启为管理员后再清理"`（对应 §0 原则 4）。
8. **规则文件拆分建议**：按 `category` 分 6 个文件（`system.json`/`browser.json`/`apps.json`/`privacy.json`/`bigfiles.json`/`maintenance.json`），**`category` 字段才是权威**，文件名不影响加载（§4 的 `load_rules` 读 `*.json`）。
9. **性能**：`edge-cache`/`chrome-cache` 各 16 个 `paths`，`chromium-browsing-history` 10 个，全部为浅层目录，扫描开销可忽略；唯一重的是 `big-drive-top`（全盘），已 `default_enabled=false`。

**真机自检样例**（`tools/verify_rules.ps1` 可参考，packaging-qa 域）：

```powershell
$rules = (Get-Content docs/research/rules-recommendation.json -Raw | ConvertFrom-Json).rules
foreach ($r in $rules) {
  foreach ($p in $r.paths) {
    $e = [Environment]::ExpandEnvironmentVariables($p)
    if ($e -notmatch '\*') {
      "{0,-32} {1,-6} {2}" -f $r.id, (Test-Path $e), $e
    }
  }
}
```

---

## 8. 给 Lead 的三句话总结

1. **规则集**：调研阶段给出 38 条建议（覆盖 6 类：系统 13 / 浏览器 4 / 应用 7 / 隐私痕迹 5 / 大文件 3 / 系统维护 6），全部用 `%ENV%` 变量，无硬编码用户名；危险项一律 `report` / `advanced` / `default_enabled=false`。经 core-engine 合并与红队修复轮后，最终规则集为 **59 条**（系统 19 / 浏览器 6 / 应用 14 / 隐私痕迹 7 / 大文件 5 / 系统维护 8），其中 38 条建议全部落地；最终映射与统计见 [integration-map.md](integration-map.md)，审查与修复状态见 [redteam-review.md](redteam-review.md)。
2. **许可证**：BleachBit、Optimizer、WinDirStat、CompactGUI 是 GPL-3.0（只借思路）；Winapp2.ini 是 CC-BY-SA-4.0（只借路径事实，自己重写数据）；MIT 的是 Bloatynosy/ThisIsWin11/Win11Debloat/Dism++ 语言仓库 —— 我们的规则数据全部自写，合规无风险。
3. **最大三个坑**：① `%LOCALAPPDATA%\Packages` 与浏览器 `Local State`/`Login Data` 绝不能整目录/整文件删；② WinSxS 与 Windows.old 只能走 DISM/cleanmgr，手删必炸；③ 递归必须跳过 junction/reparse point，否则 `%LOCALAPPDATA%\Application Data` 这种循环链接会把扫描带进死循环。
