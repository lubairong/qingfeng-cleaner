# 开源融合映射：从 38 条调研建议到最终 59 条规则

> 本文回答一个问题：**最终 `qingfeng/data/rules/*.json` 里的 59 条规则，哪一条是从哪个开源项目/官方文档来的，我们把它改成了什么样？**
>
> 所有数字均由脚本从规则文件实际统计，可复现（见 §9）。调研原始建议见 [rules-recommendation.json](rules-recommendation.json)，项目/许可证分析见 [github-survey.md](github-survey.md)，红队审查与修复状态见 [redteam-review.md](redteam-review.md)。
>
> 统计时间：2026-10-22（**红队修复轮之后重新统计**）· 规则集版本：`version: 1`，6 个分类文件，共 **59 条**（原 58 条，修复轮新增 1 条 `icon-cache`）。

---

## 1. 数字总览（可被代码验证）

| 维度 | 统计结果 |
|---|---|
| 规则总数 | **59**（红队修复前 58 → 修复后 59） |
| 规则文件 | 6 个（`system.json` 19 / `apps.json` 14 / `maintenance.json` 8 / `privacy.json` 7 / `browser.json` 6 / `bigfiles.json` 5） |
| 分类分布（§3 六类） | 系统 **19**、浏览器 **6**、应用 **14**、隐私痕迹 **7**、大文件 **5**、系统维护 **8** |
| action 分布 | `delete` **44**、`report` **14**、`empty_recycle_bin` **1** |
| risk 分布 | `safe` **27**、`caution` **17**、`advanced` **15** |
| 需管理员 | **20** 条（`requires_admin: true`；修复轮把 6 条标反/漏标的系统规则改正） |
| 默认关闭 | **25** 条（15 条 advanced 强制关闭 + 10 条 caution 主动关闭：`wechat-cache`、`windows-prefetch`、`live-kernel-reports`、`font-cache`、`windows-update-download`、`delivery-optimization-cache`、`icon-cache`、`recycle-bin`、`cleanmgr-sagerun`、`windows-update-cleanup`） |
| 路径总数 | **120** 条（`LOCALAPPDATA` 80 / `APPDATA` 27 / `WINDIR` 10 / `USERPROFILE` 1 / `TEMP` 1 / `PROGRAMDATA` 1） |
| 含通配段 `*` 的规则 | **6** 条（`wechat-cache`、`edge-cache`、`chrome-cache`、`firefox-cache`、`chromium-browsing-history`、`firefox-form-history`；`activity-timeline` 修复后已收窄、不再含通配） |
| 无 paths 的规则 | **15** 条（14 条 `report` + 1 条 `empty_recycle_bin`；44 条 delete 规则全部有 paths） |
| `source` 字段不同取值 | **16** 种（见 §2） |

**融合比例**：调研建议的 **38 条全部被采纳（38/38）**，其中 29 条字段原样进入最终规则集、9 条被改造（`thumbnails-cache` 在修复轮又二次改造，见 §10）；core-engine 在合并阶段**新增 20 条**，红队修复轮又**新增 1 条**（`icon-cache`，由 `thumbnails-cache` 拆分而来）→ 最终 **59 条**。逐条明细见 §4、§5、§10。

---

## 2. `source` 字段 → 开源项目/文档 映射

`source` 是我们自己约定的审计锚点（§4 `Rule.source`），共 16 种取值。按"提及即归属"聚合时会有 5 条规则带复合来源，故下表按"提及次数"统计，合计 64，减去 5 条复合来源 = 59。

| `source` 取值（原文） | 条数 | 对应项目 / 文档 | 性质 |
|---|---|---|---|
| `winapp2.ini` | 23 | [MoscaDotTo/Winapp2](https://github.com/MoscaDotTo/Winapp2)（CC-BY-SA-4.0） | 社区规则库，只取路径事实 |
| `bleachbit` | 10 | [bleachbit/bleachbit](https://github.com/bleachbit/bleachbit)（GPL-3.0） | CleanerML 规则思路 |
| `ms-learn` | 6 | [Microsoft Learn](https://learn.microsoft.com/)（DISM/WinSxS/cleanmgr 官方文档） | 官方命令与警告 |
| `researcher` | 4 | 本项目调研自研（[github-survey.md](github-survey.md) 的"可借鉴要点"） | 无第三方来源 |
| `windirstat/wiztree` | 2 | [windirstat/windirstat](https://github.com/windirstat/windirstat)（GPL-3.0）+ WizTree（闭源） | 只借"大文件 Top-N"思路 |
| `bleachbit:chromium + winapp2.ini` | 2 | BleachBit `cleaners/chromium.xml` + Winapp2.ini | 浏览器缓存路径交叉核实 |
| `bleachbit:chromium` | 2 | 同上 | 崩溃报告/浏览记录 |
| `bleachbit:windows_explorer` | 2 | BleachBit `cleaners/windows_explorer.xml` | 最近文件/跳转列表 |
| `ms-learn:teams-vdi + winapp2.ini` | 1 | [MS Learn：Teams VDI 磁盘占用](https://learn.microsoft.com/microsoftteams/teams-client-vdi-requirements-deploy) | 新版 Teams MSIX 路径 |
| `winapp2.ini + researcher` | 1 | Winapp2.ini + 我们的安全收敛 | 微信缓存（safety 例外） |
| `bleachbit:discord` | 1 | BleachBit `cleaners/discord.xml` | 应用缓存 |
| `bleachbit:vscode` | 1 | BleachBit `cleaners/vscode.xml` | 应用缓存 |
| `bleachbit+researcher` | 1 | BleachBit + 自研 PowerShell 建议 | 残留安装包（report） |
| `bleachbit:firefox` | 1 | BleachBit `cleaners/firefox.xml` | Firefox 缓存 |
| `dism++` | 1 | [Chuyu-Team/Dism-Multi-language](https://github.com/Chuyu-Team/Dism-Multi-language)（MIT，仅多语言仓库） | 更新缓存清理思路 |
| `win32-SHEmptyRecycleBin` | 1 | Win32 API 文档（`SHEmptyRecycleBinW`），非开源项目 | 回收站动作 |

**按项目聚合（去重后）**：

| 项目/来源 | 被采纳规则数（含复合） | 我们用到的部分 |
|---|---|---|
| Winapp2.ini | **27** | 应用/系统缓存的"路径在哪"，尤其浏览器与应用目录清单 |
| BleachBit | **20** | CleanerML 的分层结构、Windows 缓存路径、Explorer/浏览器清理顺序（`icon-cache` 为修复轮按 BleachBit 缩略图/图标缓存思路拆出） |
| Microsoft Learn | **7** | DISM 组件清理、WinSxS 警告、cleanmgr、Teams VDI 路径 |
| 本项目自研（researcher） | **6** | 大文件 Top-N、保留存储、更新缓存深度清理、微信安全收敛 |
| WinDirStat / WizTree | **2** | 大文件扫描的产品形态（代码未使用） |
| Dism++ | **1** | "更新缓存应该报告+命令，而不是硬删" |
| Win32 API 文档 | **1** | `SHEmptyRecycleBinW` 静默清空回收站 |

> 说明：`source` 是**多值标注**，同一规则可同时来自两个项目（5 条复合来源）。这不是重复计数错误，而是刻意保留的溯源信息，方便许可证审计（见 §8）。

---

## 3. 我们采用的核心能力 ← 来自哪里 ← 改造成什么

| 我们采用的 | 来自 | 改造 |
|---|---|---|
| 规则/引擎分离、按分类组织清理项 | BleachBit CleanerML（`<cleaner>/<option>/<action>`） | 改为扁平 JSON `Rule` 列表（§4），一个分类一个文件；去掉 XML 表达式；`option` 概念用 `id` 表达 |
| 路径变量（`%LocalAppData%`、`$$profile$$`） | BleachBit `<var>` | 直接用 `%ENV%`（`os.path.expandvars`），并用 safety 展开后校验绝对路径 |
| 浏览器多 profile 的缓存路径 | BleachBit `chromium.xml` + Winapp2.ini `User Data\*` | 引入"单个 `*` 独占一段"的约定（rules.py:108-133）；修复轮后 `*` 在中间段时文件+目录都收（rules.py:129-164，红队 M2） |
| Windows 缩略图/图标/最近文件/跳转列表路径 | BleachBit `windows_explorer.xml`（已抓原文） | 缩略图与图标缓存**拆成两条规则**：`thumbnails-cache`（`thumbcache_*.db`，默认开）与 `icon-cache`（`iconcache_*.db`，默认关）；不自动杀 explorer，改为 GUI 提示重启（红队 S3） |
| 应用缓存清单（Teams/Discord/VS Code/Office 等） | Winapp2.ini 条目 + BleachBit 对应 cleaner | **不整体引入** CC-BY-SA 数据，重写为自研 JSON；只保留明确的缓存子目录，剔除 `\*Cache*` 通配式条目；修复轮把 Teams 的 `IndexedDB/Local Storage/blob_storage` 移除（红队 M1） |
| 回收站删除、静默清空 | Win32 `SHFileOperationW` + `FOF_ALLOWUNDO`；`SHEmptyRecycleBinW` | 默认回收站、失败不降级（cleaner.py:37-38）；`empty_recycle_bin` 独立 action 且默认关闭 |
| WinSxS/更新的"正规清理" | Microsoft Learn + Dism++ | 全部降级为 `action=report` + `report_note` 给命令，**零自动执行** |
| 大文件 Top-N 的产品形态 | WinDirStat / WizTree | 也做成 `report`（引擎只展示建议文本），不实现 MFT 解析 |
| 应用运行检测后再清缓存 | BleachBit `<running type="exe">` | 规则层无此字段（§4 契约），修复轮改在 **GUI 层**实现"运行中程序检测"（红队 S11） |

---

## 4. 调研建议 38 条 → 最终规则的逐条映射

标记说明：**原样** = 16 个字段与建议完全一致；**改造** = 具体差异见"改造说明"列。修复轮的追加变更见 §10。

### 4.1 系统（13 条建议 → 13 条保留）

| 最终 rule id | 来源（survey 对应） | 状态 | 改造说明 |
|---|---|---|---|
| `user-temp` | BleachBit 临时文件清理 | 原样 | — |
| `windows-temp` | BleachBit 临时文件清理 | 原样 | — |
| `inet-cache` | Winapp2.ini INetCache | 原样 | — |
| `wer-user` | Winapp2.ini WER | 原样 | — |
| `wer-system` | Winapp2.ini WER（ProgramData） | 原样 | — |
| `crash-dumps-user` | Winapp2.ini CrashDumps | 原样 | — |
| `windows-minidump` | Winapp2.ini Minidump | 原样 | — |
| `cbs-log` | Winapp2.ini CBS 日志 | 原样 | 保留 `min_age_days: 7` 的排障保留策略 |
| `windows-update-download` | Dism++ 更新缓存清理 | **改造** | 修复轮：`default_enabled: true → false`（红队 S2，避免删正在安装的更新包），其余不变 |
| `delivery-optimization-cache` | Winapp2.ini 传递优化 | 原样 | 仍默认关闭 |
| `thumbnails-cache` | BleachBit windows_explorer 缩略图 | **改造** | 修复轮（红队 S3）：`globs` 收窄为 `["thumbcache_*.db"]`；`iconcache_*.db` 拆成独立规则 `icon-cache`（默认关闭） |
| `recycle-bin` | Win32 `SHEmptyRecycleBinW` | 原样 | 仍默认关闭 + 二次确认 |
| `windows-old` | Microsoft Learn | **改造** | `paths`/`globs` 清空：report 规则由引擎直接输出建议，不再 stat `C:\Windows.old`（该目录被 safety 整树保护，stat 也只会报错） |

### 4.2 浏览器（4 条建议 → 4 条保留）

| 最终 rule id | 来源 | 状态 | 改造说明 |
|---|---|---|---|
| `edge-cache` | BleachBit chromium + Winapp2.ini Edge 条目 | 原样 | 16 条 paths、`*` 段覆盖多 profile、`protected_globs` 兜住 Cookie/密码/书签 |
| `chrome-cache` | 同上（Chrome 路径） | 原样 | 同上 |
| `firefox-cache` | BleachBit `firefox.xml` | 原样 | `Profiles\*` 展开多 profile；排除 `*.sqlite`/`logins.json` |
| `chromium-crashpad` | BleachBit chromium 崩溃报告 | 原样 | — |

### 4.3 应用（7 条建议 → 7 条保留）

| 最终 rule id | 来源 | 状态 | 改造说明 |
|---|---|---|---|
| `teams-new-cache` | MS Learn Teams VDI + Winapp2.ini | **改造** | 修复轮（红队 M1）：`paths` 8 → **5**，移除 `IndexedDB` / `Local Storage` / `blob_storage`，只保留 `Cache`/`Code Cache`/`GPUCache`/`Service Worker`/`logs` |
| `teams-classic-cache` | Winapp2.ini | **改造** | 修复轮（红队 M1）：`paths` 9 → **6**，同样移除三项应用状态目录 |
| `wechat-cache` | Winapp2.ini + 自研安全收敛 | **改造** | ① `default_enabled: true → false`（`Documents` 在 §5 是整树保护，主动降级）；② `source` 加 `+ researcher`；③ 描述改为"仅清 Cache 目录，不动聊天记录/图片/视频/文件"；④ **触发了 safety 的窄例外**（`safety.py:124-127`、`446-467`，Lead 2026-10-22 批准），正则只允许 `<userprofile>\documents\wechat files\<账号>\filestorage\cache` |
| `discord-cache` | BleachBit `discord.xml` | 原样 | — |
| `vscode-cache` | BleachBit `vscode.xml` | 原样 | — |
| `steam-htmlcache` | Winapp2.ini | **改造** | 修复轮（红队 S8）：`paths` 2 → **1**，删除本机确认为不存在的 `%LOCALAPPDATA%\Steam\logs` |
| `office-filecache` | Winapp2.ini | 原样 | 保留 `min_age_days: 1` |

### 4.4 隐私痕迹（5 条建议 → 5 条保留）

| 最终 rule id | 来源 | 状态 | 改造说明 |
|---|---|---|---|
| `recent-files` | BleachBit windows_explorer | 原样 | advanced + 默认关闭 |
| `jump-lists` | BleachBit windows_explorer | 原样 | 同上 |
| `clipboard-history` | Winapp2.ini | 原样 | 同上 |
| `notification-db` | Winapp2.ini | 原样 | 同上 |
| `chromium-browsing-history` | BleachBit chromium | 原样（规则未改） | 红队 M2 的修复在**引擎**：`rules.py:_expand_one_path` 现在"`*` 在中间段时文件+目录都收"，本机实测展开 0 → 4 |

> 注：4.4 标题里的"5 条建议"指调研建议中的 5 条；`activity-timeline`、`firefox-form-history` 是 core-engine 后补、红队修复轮又调整/验证过的规则，统一列在 §5。

### 4.5 大文件（3 条建议 → 3 条保留，另有 2 条新增见 §5）

| 最终 rule id | 来源 | 状态 | 改造说明 |
|---|---|---|---|
| `big-downloads` | WinDirStat/WizTree | **改造** | `paths`/`globs` 清空、`follow_dirs: true → false`，内容改为 `report_note` 里的 PowerShell 建议；修复轮把 `description` 统一为"仅提供命令建议，本工具不执行扫描/删除"（红队 S7） |
| `leftover-installers` | BleachBit + 自研 | **改造** | 同上，改为 PowerShell 建议文本（`-Include` 筛 `.exe/.msi/.iso/.zip/.7z/.rar`） |
| `big-drive-top` | WinDirStat/WizTree | **改造** | 同上（Top-100 + 跳过保护目录的说明写进 `report_note`） |

### 4.6 系统维护（6 条建议 → 6 条保留）

| 最终 rule id | 来源 | 状态 | 改造说明 |
|---|---|---|---|
| `hibernation-file` | Microsoft Learn（powercfg） | **改造** | `paths`/`globs` 清空（report 直出 `powercfg /hibernate off`） |
| `pagefile` | Microsoft Learn（虚拟内存） | **改造** | 同上 |
| `winsxs-component-store` | Microsoft Learn（DISM） | **改造** | 同上；`report_note` 保留 `/AnalyzeComponentStore` 与 `/ResetBase` 的警告 |
| `windows-installer-cache` | Winapp2.ini | **改造** | 同上（只报告，引导 PatchCleaner 类工具） |
| `restore-points` | Microsoft Learn（vssadmin） | 原样 | 本来就是 `paths: []` 的纯建议规则 |
| `cleanmgr-sagerun` | Microsoft Learn（cleanmgr） | 原样 | 同上 |

**小结：29 条原样 + 9 条改造 = 38 条全部落地**；9 条改造里 8 条是"report 规则去掉 paths（避免 stat 受保护路径 / 让引擎直接展示建议）"，1 条是微信的安全降级+窄例外。修复轮又对其中的 `windows-update-download`、`thumbnails-cache`、`teams-*`、`steam-htmlcache`、`big-*` 做了二次调整，见 §10。

---

## 5. core-engine 合并阶段新增的 21 条（不在调研建议内）

这 21 条是在"我们把规则交出去之后"由 core-engine/Lead 按同一批调研数据补充的（20 条 + 红队修复轮新增 1 条 `icon-cache`）。**归属正确性由 `source` 字段保证**；其中 `researcher` 的 4 条是引擎侧按调研文档 §3.3 补的系统维护建议。

| 新增 rule id | 分类 | source | 类型 | 说明 |
|---|---|---|---|---|
| `windows-logs` | 系统 | winapp2.ini | delete | `%WINDIR%\Logs` 全量日志；修复轮加 `min_age_days: 7` + `exclude_globs: ["CBS","WindowsUpdate"]`（红队 S1） |
| `windows-prefetch` | 系统 | bleachbit | delete | Prefetch `*.pf`，保留 `Layout.ini`/`ReadyBoot`；修复轮改为 **默认关 + 需管理员 + `min_age_days: 30`**（红队 M3） |
| `live-kernel-reports` | 系统 | winapp2.ini | delete | LiveKernelReports 内核转储；修复轮改为默认关 + 需管理员（红队 S12/S1） |
| `windows-systemtemp` | 系统 | winapp2.ini | delete | `%WINDIR%\SystemTemp` 服务临时目录；修复轮 `requires_admin: true`（S1） |
| `font-cache` | 系统 | bleachbit | delete | 修复轮改为 `%LOCALAPPDATA%\Microsoft\Windows\Caches` + `*.db` + 默认关（红队 S4，原路径不存在） |
| `icon-cache` | 系统 | bleachbit | delete | **红队 S3 拆出的新规则**：只清 `iconcache_*.db`，`caution` + 默认关（避免白图标） |
| `edge-webview-cache` | 浏览器 | winapp2.ini | delete | WebView2 的 `EBWebView\Default\Cache` |
| `opera-cache` | 浏览器 | bleachbit | delete | Opera `AppData\Roaming` 下的缓存 |
| `onedrive-logs` | 应用 | winapp2.ini | delete | OneDrive 客户端日志（非同步目录） |
| `dingtalk-cache` | 应用 | winapp2.ini | delete | 钉钉 `%APPDATA%\DingTalk` 缓存（路径待装机核实） |
| `adobe-media-cache` | 应用 | winapp2.ini | delete | Adobe Media Cache Files；修复轮 `min_age_days: 1`（红队 S6） |
| `java-webstart-cache` | 应用 | winapp2.ini | delete | Java Deployment cache（Sun/Oracle 两个路径） |
| `npm-cache` | 应用 | bleachbit | delete | `%LOCALAPPDATA%\npm-cache\_cacache`；修复轮 `min_age_days: 7`（红队 S5） |
| `pip-cache` | 应用 | bleachbit | delete | `%LOCALAPPDATA%\pip\Cache`；修复轮 `min_age_days: 7`（红队 S5） |
| `zoom-logs` | 应用 | winapp2.ini | delete | Zoom 运行日志（路径待装机核实） |
| `activity-timeline` | 隐私痕迹 | winapp2.ini | delete | 修复轮收窄：`paths: ["%LOCALAPPDATA%\\ConnectedDevicesPlatform"]` + `globs: ["ActivitiesCache.db*"]`（红队 S9，不再整目录 `*`） |
| `firefox-form-history` | 隐私痕迹 | bleachbit | delete | `Profiles\*\formhistory.sqlite`（advanced；引擎修复后已能命中，见 §4.4） |
| `big-files-top50` | 大文件 | researcher | report | C 盘 Top 50 文件；修复轮 `requires_admin: true` + 文案统一（红队 S7） |
| `big-folders-top20` | 大文件 | researcher | report | C 盘 Top 20 目录（只报告，不执行）；修复轮 `requires_admin: true`（S7） |
| `reserved-storage` | 系统维护 | researcher | report | `DISM /Online /Set-ReservedStorageState /State:Disabled` |
| `windows-update-cleanup` | 系统维护 | researcher | report | 停 wuauserv/bits 后清 SoftwareDistribution 的完整命令链 |

> 与调研建议的对照：新增规则里 **10 条来自 Winapp2.ini 路径清单、7 条来自 BleachBit cleaner、4 条自研**，没有一条来自 GPL 代码或 CC-BY-SA 数据文件本身——与 §8 的许可证路线一致。

---

## 6. 没有照搬的地方及原因（取舍清单）

| # | 开源项目有、我们没有 | 原因 | 替代做法 |
|---|---|---|---|
| 1 | **GPL 代码**（BleachBit/Optimizer/WinDirStat/CompactGUI 一行未抄） | 本项目 MIT；GPL-3.0 传染，拷贝即污染 | 只读源码/规则了解"该清哪、怎么清"，Python 侧全部自研（ctypes 封装、扫描器、守卫） |
| 2 | **Winapp2.ini 数据文件整体引入** | CC-BY-SA-4.0 的 ShareAlike 会把衍生作品拉成 CC-BY-SA，与 MIT 冲突 | 只取"路径事实"（事实不受版权保护），自己写 JSON schema、名称、描述、风险等级；`source` 标注出处 |
| 3 | **注册表级隐私清理**（BleachBit 的 `winreg` action、Winapp2 的 `RegKey`、Optimizer 的 tweak 全家桶） | ① §4 `Rule` 没有注册表字段；② 注册表删除不可回收、误删难恢复；③ 隐私项（UserAssist/Shellbags）清理后系统行为会变 | 0 条注册表规则（120 条路径全为文件系统）；`系统维护` 里的 `report_note` 给出 `vssadmin`/`cleanmgr`/DISM 命令，由用户自己决定 |
| 4 | **手删 WinSxS / 组件存储** | MS 官方原文警告"可能导致系统无法启动、无法更新" | `winsxs-component-store` 只报告，给出 `/AnalyzeComponentStore` → `/StartComponentCleanup [/ResetBase]`，并写明 ResetBase 后更新不可卸载 |
| 5 | **驱动清理 / 服务优化 / Appx 卸载 / Defender tweak**（Dism++、Optimizer、Win11Debloat、Bloatynosy 的能力） | 超出"磁盘清理"定位；改服务/卸载应用不可逆，且容易被安全软件判为风险 | 完全不实现；`README` 的定位就是"只清垃圾不动系统配置" |
| 6 | **shred / 安全擦除**（BleachBit 招牌能力） | 与 §0"默认走回收站、可撤销"原则冲突；SSD 上覆写也无保证 | 不提供；永久删除只作为 GUI 显式开关（`mode="delete"`） |
| 7 | **删 Cookies / 密码 / 书签 / Local State**（BleachBit 有 cookies/passwords/sync 选项） | 这些是用户数据，不是缓存；`Local State` 还含加密密钥元数据 | 浏览器规则只到缓存目录；Advanced 隐私规则最多到 `History/Top Sites/Visited Links/Favicons`；`protected_globs` 显式挡住 `Cookies`/`Login Data`/`Bookmarks`/`Local State` |
| 8 | **Prefetch 默认开启**（我们的原建议：不写；core-engine 曾以 caution + 默认开加入） | 调研结论：清 Prefetch 对 SSD 收益极低、前几次启动变慢，MS 不推荐 | **修复轮已收敛**：`default_enabled: false` + `requires_admin: true` + `min_age_days: 30`（红队 M3，规则仍在但默认不动） |
| 9 | **`*Cache*` 式通配条目**（Winapp2.ini 常见 `\*Cache*\|*\|REMOVESELF`） | 这类通配会扫到大目录，误删风险高 | 规则路径必须显式到缓存目录；通配只允"单个 `*` 独占一段"，且仅中间段可命中文件（rules.py:129-164） |
| 10 | **Storage Sense 注册表写入**（`StoragePolicy` 值） | 值名是未文档化内部实现，随版本变；写错影响系统清理策略 | 只在文档/`report_note` 引导用户去"设置 > 存储"；不写注册表 |
| 11 | **MFT/$MFT 直读加速**（WizTree） | Python 标准库做不到；需要管理员卷句柄；实现风险高 | 大文件分类只报告 + 给 PowerShell 建议；引擎不实现全盘遍历 |
| 12 | **`MoveFileEx(MOVEFILE_DELAY_UNTIL_REBOOT)` 重启后删除** | 不可撤销、用户不可见，违反可撤销原则 | 不实现；删不掉的文件进 `CleanResult.failed` 让用户看到 |
| 13 | **恢复点/卷影副本删除** | 直接删 `System Volume Information` 会破坏 VSS | `restore-points` 只报告 `vssadmin list shadowstorage`，引导系统保护 UI |
| 14 | **应用运行检测写进规则**（BleachBit `<running>`） | §4 契约无此字段，未擅自扩展 | 规则 `description` 提示"建议先退出浏览器/应用"；**修复轮已在 GUI 层实现运行中程序检测**（红队 S11） |

---

## 7. 工程实现层面的借鉴（代码级）

### 7.1 回收站删除：`SHFileOperationW` 而不是 `IFileOperation`

- **选择**：`winapi.py:168-216` 用 `SHFileOperationW` + `FOF_ALLOWUNDO|FOF_NOCONFIRMATION|FOF_SILENT|FOF_NOERRORUI`。理由：ctypes 直调，无需定义 COM 接口；Win11 全版本可用；与 BleachBit/CCleaner 当年的选择一致。
- **代价与补丁**：该 API 批量返回单一错误码，无法定位是哪一项失败 → `_shfileop_delete()` 在返回码非 0 时**逐项重试**定位失败项（`winapi.py:195-216`）；`recycle_paths()` 自行 `CoInitialize/CoUninitialize`，可在任意工作线程调用（`winapi.py:219-255`）。
- **后续升级路径**：若需要"逐文件进度 + 更可靠的回收站语义"，换 `IFileOperation`（Vista+ COM），但会增加 ~100 行 COM 定义；当前收益不足以抵消风险。

### 7.2 不跟随 junction / reparse point

- 扫描器对每个 `DirEntry` 做 `entry.is_symlink()` + `st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT` 双检，命中即跳过（`scanner.py:255-265`）。
- 安全守卫构建保护表时同样跳过旧兼容链接（`safety.py:256-269`），否则 `%LOCALAPPDATA%\Application Data` 这类指向自身的 junction 会把保护表登记成"整个 AppData 被保护"，反过来让缓存规则全部失效。
- 这是从 BleachBit/CCleaner 的"递归走 junction → 无限循环/删到别处"历史问题里学到的（见 github-survey §5 第 8 条）。

### 7.3 回收站失败的降级策略

- `cleaner.py:37-38`：`ALLOW_PERMANENT_FALLBACK = False`；回收站删除失败**默认不降级**为永久删除，只记录失败（`cleaner.py:289-317`）。
- 与 CCleaner/BleachBit 默认直接永久删除不同：我们把"可撤销"当硬约束；GUI 若要开降级必须显式改 `set_permanent_fallback(True)` 并在设置里提示。
- 另：`_remove_file`/`_rmtree` 对只读文件先 `chmod` 再重试一次（`cleaner.py:108-142`），避免"只读属性导致整批失败"。

### 7.4 扫描只读 + 整树聚合

- `scanner.py` 全模块只用 `os.scandir` / `stat` / `glob`，无任何写操作；`report` 规则在扫描阶段直接短路，连路径都不展开（`scanner.py:395-397`）。
- **整树聚合**（`scanner.py:285-291`）：若某顶层子目录"整棵子树都合格"，聚合成一个 `is_dir=True` 的 `ScanItem`，清理时一次 Shell 调用删整个目录。对比 BleachBit 的 `walk.files` 逐文件删除：回收站里只产生少量目录条目，避免"清 10 万个小文件把回收站塞满"。
- 代价：`min_age_days` 对聚合目录无法二次校验（cleaner.py:212 只对文件二次校验），因此聚合条件要求"所有文件都满足 min_age"，逻辑上等价。

### 7.5 规则格式：JSON vs CleanerML vs INI

| 维度 | 我们的 JSON（§4） | BleachBit CleanerML | Winapp2.ini |
|---|---|---|---|
| 解析成本 | `json.load`，零依赖 | XML 解析 | 自写 INI 解析（分号/管道/多值） |
| 表达力 | 中：路径/通配/exclude/风险/年龄 | 强：<var>/<running>/注册表/sqlite 操作 | 强：`DetectFile`/`RegKey`/`ExcludeKey`/`RECURSE` |
| 校验 | `validate_rule` 强校验（分类/action/risk/id/路径保护），非法即抛 `RuleError` | 有自己的 sanity checker | 靠宿主程序容错 |
| 安全审计 | 每条规则 16 字段可 diff；`source` 可溯源 | 需读 XML 树 | 条目散落，难审计 |
| 扩展方式 | 冻结契约，能力扩展走引擎代码 | 加 action command | 加 key 类型 |

结论：以"可审计、可校验、零依赖"为第一优先级选了 JSON；代价是表达力弱（没有变量、注册表、进程检测），这些能力被**显式**推迟到引擎/GUI 层，而不是塞进规则（避免规则变成脚本）。

### 7.6 通配段设计（红队 M2 修复后）

- `rules.py:108-133`：只允许单个 `*` 独占一段、禁 `**`、禁出现在盘符/根段。
- `rules.py:129-164`（修复轮新增）：`_wildcard_is_last_segment()` 判断 `*` 是否在末段——
  - `*` 在**末段**（`Profiles\*`、`ConnectedDevicesPlatform\*`）：只展开该层**子目录**（保持"遍历这一层目录"的语义）；
  - `*` 在**中间段**（`User Data\*\History`、`Profiles\*\formhistory.sqlite`）：文件与目录都收 → 修复了"以文件名结尾的通配路径永远 0 命中"的缺陷（`chromium-browsing-history` 本机展开 0 → 4）。
- 好处：既能表达"所有浏览器 profile / 所有微信账号 / 所有 Firefox profile"，又不会误伤 `*Cache*` 这种危险写法；修复后隐私规则不再"假成功"。

### 7.7 安全守卫"白名单化"而不是"规则自治"

- BleachBit 的安全边界主要在 cleaner XML 自己（规则写错就可能删错）；我们把"能不能删"上收到全局 `safety.py`：
  - §5 全部受保护路径硬编码（`safety.py:36-103`）；
  - `C:\Windows` 下只有 9 个顶层目录放行（`_WINDOWS_ALLOWED_TOPS`），`C:\ProgramData` 下只有 WER 放行；
  - 用户标准目录（Documents/Desktop/Downloads/...）整树保护；
  - **规则只能收窄、不能放宽**，唯一例外是微信 Cache（显式正则 + 文件头写明"新增例外必须 Lead 批准"）。
- 效果：即使规则 JSON 被篡改指向 `C:\Windows\System32`，`rules.validate_rule` 在加载期就会抛 `RuleError`，`cleaner` 运行期还有第二道 `assert_deletable`。
- 这也是我们比 BleachBit/Optimizer "更保守"的地方：它们默认信任规则集/用户选择，我们默认不信任。

---

## 8. 许可证与合规落地（结论）

1. **代码**：0 行 GPL 代码进入仓库；GPL 项目只作为"路径/命令/交互"知识来源。MIT 项目（Dism++ 多语言仓库/Bloatynosy/ThisIsWin11/Win11Debloat）的 功能清单 可参考，实际也几乎未用到。
2. **数据**：Winapp2.ini 条目（CC-BY-SA-4.0）未整体引入；120 条路径是我们自己写的 JSON，`source` 字段仅标注"事实来源"。
3. **文档**：`github-survey.md` 已列出各项目许可证与"可借鉴/不可借鉴"边界。
4. **待办（交 Lead/packaging-qa）**：仓库根加 `THIRD_PARTY.md`/`NOTICE`，内容建议：
   > 规则数据为本项目自行撰写，参考了 BleachBit (GPL-3.0)、Winapp2.ini (CC-BY-SA-4.0) 与 Microsoft Learn 中公开的路径与清理命令；未包含上述项目的代码或数据文件。
5. **审计锚点**：任何规则都能通过 `source` 字段回溯到 [github-survey.md](github-survey.md) 的具体章节；新增规则必须带 `source`（`validate_rule` 允许空，但评审约定要求非空 —— 当前 59 条全部非空，可核对）。

---

## 9. 数字复现命令

```powershell
& 'C:\Users\HUAWEI\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe' -c @"
import json,glob,os,collections
files=sorted(glob.glob(r'E:\deepseekharness在这里的工作区\qingfeng\data\rules\*.json'))
rs=[r for f in files for r in json.load(open(f,encoding='utf-8'))['rules']]
print('total',len(rs))
print('per-file',{os.path.basename(f):len(json.load(open(f,encoding='utf-8'))['rules']) for f in files})
print('cats',dict(collections.Counter(r['category'] for r in rs)))
print('actions',dict(collections.Counter(r['action'] for r in rs)))
print('risk',dict(collections.Counter(r['risk'] for r in rs)))
print('admin',sum(r['requires_admin'] for r in rs),'default_off',sum(not r['default_enabled'] for r in rs))
print('paths',sum(len(r['paths']) for r in rs),'wildcard',[r['id'] for r in rs if any('*' in p for p in r['paths'])])
print('sources',dict(collections.Counter(r['source'] for r in rs)))
"@
```

> 上述命令输出即 §1、§2、§4、§5、§10 的全部数字来源。规则文件是唯一事实来源；本文不凭记忆写数。

---

## 10. 红队修复轮的增量（58 → 59，逐条可核对）

红队审查（[redteam-review.md](redteam-review.md)）的 3 项"必须改"与 12 项"建议提示"落地后，规则集发生如下变化；**本节的字段值全部由当前规则文件读回，与 §1 统计一致**。

| # | 变更 | 文件 / 规则 | 字段（修复后值） | 对应红队项 |
|---|---|---|---|---|
| 1 | Teams 不再删应用状态 | `apps.json` · `teams-new-cache` | `paths` 8 → **5**（移除 `IndexedDB`/`Local Storage`/`blob_storage`） | M1 |
| 2 | 同上（经典版） | `apps.json` · `teams-classic-cache` | `paths` 9 → **6**（同上） | M1 |
| 3 | 通配路径可命中文件 | `core/rules.py` · `_expand_one_path`（非规则文件） | `*` 在中间段时文件+目录都收；本机 `chromium-browsing-history` 展开 **0 → 4** | M2 |
| 4 | Prefetch 默认不动 | `system.json` · `windows-prefetch` | `default_enabled: false`、`requires_admin: true`、`min_age_days: 30` | M3 |
| 5 | 系统日志/内核报告权限修正 | `system.json` · `windows-logs`、`windows-systemtemp`、`live-kernel-reports` | `requires_admin: true`；`windows-logs` 另加 `min_age_days: 7`、`exclude_globs: ["CBS","WindowsUpdate"]`；`live-kernel-reports` 默认关 | S1 / S12 |
| 6 | 更新缓存默认关 | `system.json` · `windows-update-download` | `default_enabled: false` | S2 |
| 7 | 缩略图/图标缓存拆分 | `system.json` · `thumbnails-cache` + **新增** `icon-cache` | `thumbnails-cache.globs: ["thumbcache_*.db"]`；`icon-cache`：`iconcache_*.db`、`caution`、默认关 | S3 |
| 8 | 字体缓存路径修正 | `system.json` · `font-cache` | `paths: ["%LOCALAPPDATA%\\Microsoft\\Windows\\Caches"]`、`globs: ["*.db"]`、`default_enabled: false` | S4 |
| 9 | 构建缓存年龄门槛 | `apps.json` · `npm-cache`、`pip-cache` | `min_age_days: 7` | S5 |
| 10 | Adobe 缓存年龄门槛 | `apps.json` · `adobe-media-cache` | `min_age_days: 1` | S6 |
| 11 | 大文件 report 文案/权限统一 | `bigfiles.json` · 5 条 report | `description` 统一"仅提供命令建议，本工具不执行扫描/删除"；`big-files-top50`/`big-folders-top20` 的 `requires_admin: true` | S7 |
| 12 | 删除不存在的 Steam 日志路径 | `apps.json` · `steam-htmlcache` | `paths` 2 → **1**（仅 `%LOCALAPPDATA%\\Steam\\htmlcache`） | S8 |
| 13 | 活动时间线收窄 | `privacy.json` · `activity-timeline` | `paths: ["%LOCALAPPDATA%\\ConnectedDevicesPlatform"]`、`globs: ["ActivitiesCache.db","ActivitiesCache.db-wal","ActivitiesCache.db-shm"]` | S9 |
| 14 | 回收站/浏览器语义与检测（非规则字段） | GUI / `cleaner` 调用层 | 结果弹窗区分"移入回收站/已释放"、重启资源管理器提示、运行中程序检测 | S10 / S11 |

**修复前后统计对比**：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 规则总数 | 58 | **59**（+`icon-cache`） |
| `delete` 规则 | 43 | **44** |
| `caution` 规则 | 16 | **17** |
| 需管理员 | 14 | **20** |
| 默认关闭 | 20 | **25** |
| 路径总数 | 126 | **120** |
| 含通配段规则 | 7 | **6**（`activity-timeline` 收窄） |

> 结论：修复轮没有新增任何"指向用户文档/凭据"的路径；`icon-cache` 与 `thumbnails-cache` 同源（Explorer 缓存），来源仍为 BleachBit；全部 59 条 `source` 非空，许可证路线（§8）不变。
