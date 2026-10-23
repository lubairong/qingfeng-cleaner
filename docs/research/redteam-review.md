# 规则集红队审查与修复复核：59 条规则（原审查 58 条）

> 审查对象：`qingfeng/data/rules/*.json` 最终 **59 条**规则（`version: 1`，6 个文件；原审查时 58 条，修复轮新增 `icon-cache`）。
> 审查立场：安全审核者/攻击者——假设用户会"全选 + 一键清理"，假设程序以**非管理员**默认配置运行，假设最坏情况下进入**永久删除**模式。
> 审查方式：**只读**——读规则 JSON、跑 Python 统计、走读 `qingfeng/core/{rules,safety,scanner,cleaner,winapi}.py`、在本机 Windows 11 上做 ACL/路径探测。**未修改 `qingfeng/` 下任何文件**；所有改法以"file + rule id + 字段"形式交 Lead 裁决，最终由 core-engine 实施。
>
> 审查时间：2026-10-22（初版）· 复核时间：2026-10-22（修复轮后重新统计，本文即复核版）
> 相关文档：[integration-map.md](integration-map.md) / [github-survey.md](github-survey.md)

---

## 1. 复核结论摘要（含修复状态）

| 档位 | 问题数 | 涉及规则 | 修复状态 | 说明 |
|---|---|---|---|---|
| **必须改** | **3 项** | **5 条** | **✅ 5/5 已修复** | Teams 应用状态（M1）、隐私规则 0 命中（M2）、Prefetch 默认开启（M3）全部处置完毕 |
| **建议提示** | 12 项 | 22 条 | **✅ 10 项已修复 / 🟡 2 项部分修复** | S1–S7、S9、S11、S12 已落地；S8 部分（Steam 日志路径已删，3 条待装机核实）；S10 部分（GUI 结果弹窗已改，回收站占用预览未做） |
| **可接受** | — | 32 条 | **不改（风险可接受）** | 含 S3 拆分新增的 `icon-cache`（默认关闭） |

**修复统计**：已修复 **13 项**（3 必须改 + 10 建议提示）、部分修复 **2 项**（S8 / S10）、明确不改 **0 项**；可接受类 32 条规则维持原判。
**规则层面**：必须改的 5 条规则**全部已修复**；建议提示的 22 条规则中 19 条已修复、3 条（`dingtalk-cache`/`opera-cache`/`zoom-logs`）路径仍需装机核实（功能项，非安全项）。

**修复后独立安全结论（详见 §6）**：在"非管理员 + 默认勾选 + 回收站模式"下，**未发现"用户点一下就会永久丢失文档/照片/聊天记录"的路径**；初版审查指出的 3 类问题已全部消除，残余风险为"GUI 语义提示不完整 + 3 条路径待核实"两类低风险项。逐条覆盖见附录 A。

---

## 2. 必须改（3 项 / 5 条规则 —— 全部 ✅ 已修复）

### M1 · Teams 两条缓存规则会删到应用状态，不是"只清缓存" —— ✅ 已修复

- **原位置**：`qingfeng/data/rules/apps.json` · `teams-new-cache`（原 `paths` 含 `IndexedDB` / `Local Storage` / `blob_storage`）、`teams-classic-cache`（同上）。
- **原风险**：这三类目录承载 Teams 登录令牌与本地应用状态；`blob_storage` 可能含正在上传/发送的附件。清理后强制重新登录、极端场景丢未发送内容，与 `risk: safe` + "只清缓存"声明不符。
- **修复后（实测读回）**：
  - `teams-new-cache`：`paths` **8 → 5**，仅保留 `Cache`、`Code Cache`、`GPUCache`、`Service Worker`、`logs`；
  - `teams-classic-cache`：`paths` **9 → 6**，仅保留 `Cache`、`Code Cache`、`GPUCache`、`Service Worker`、`logs`、`tmp`；
  - 两条仍为 `risk: safe` + `default_enabled: true`，声明与行为一致。
- **复核**：`IndexedDB`/`Local Storage`/`blob_storage` 在全部 6 个规则文件中已无匹配（脚本可核对）。

### M2 · 两条隐私规则因"通配段 + 文件名"永久 0 命中（假清理）—— ✅ 已修复（引擎修复）

- **原位置**：`privacy.json` · `chromium-browsing-history`（10 条 `...\User Data\*\History` 形态）、`firefox-form-history`（`Profiles\*\formhistory.sqlite`）；根因在 `core/rules.py` · `_expand_one_path()` 只保留 `isdir()` 匹配项。
- **修复后（实测读回）**：`core/rules.py` 新增 `_wildcard_is_last_segment()`（第 129-133 行）并在 `_expand_one_path()` 里按 `dirs_only` 分流（第 154-163 行）：
  - `*` 在**末段** → 仍只展开该层子目录（保持 `Profiles\*` 等语义）；
  - `*` 在**中间段** → 文件与目录都收（修复 `User Data\*\History` 类路径）。
- **复核证据（本机）**：`rules.expand_paths('chromium-browsing-history')` **0 → 4**（Edge 已安装，4 个目标文件存在）；`firefox-form-history` = 0 属预期（本机未装 Firefox，路径形态正确，装机后即可命中）。
- **附带效果**：`activity-timeline` 仍为 1（`*` 在末段语义未变），`edge-cache` 15、`wechat-cache` 0（本机未装微信），无回归。

### M3 · `windows-prefetch` 默认开启，违背调研结论 —— ✅ 已修复

- **原位置**：`system.json` · `windows-prefetch`，原 `default_enabled: true`、`requires_admin: false`、`min_age_days: 0`。
- **修复后（实测读回）**：`default_enabled: false`、`requires_admin: true`、`min_age_days: 30`；`globs: ["*.pf"]`、`exclude_globs: ["Layout.ini","ReadyBoot"]` 保持不变。
- **复核**：默认配置下不再清理 Prefetch；即使用户手动开启，也只删 30 天以上的 `*.pf`，与调研结论（[github-survey.md](github-survey.md) §5 第 3 条）一致。

---

## 3. 建议提示（12 项 / 22 条规则 —— 10 项已修复、2 项部分修复）

### S1 · 4 条系统规则的 `requires_admin` 标反 —— ✅ 已修复

- **修复后（实测读回，全部 `system.json`）**：`windows-logs`、`windows-prefetch`、`live-kernel-reports`、`windows-systemtemp` 的 `requires_admin` **均为 `true`**。
- **附加项已做**：`windows-logs` 增加 `min_age_days: 7`，`exclude_globs: ["CBS", "WindowsUpdate"]`。
- **复核**：非管理员下这 4 条会走"整条规则跳过 + 需要管理员提示"（`cleaner.py:270-275`），不再产生逐项"拒绝访问"噪音。本机实测 `%WINDIR%\Logs` 的 `BUILTIN\Users` 仅 `ReadAndExecute`、`%WINDIR%\Prefetch` 列目录被拒，与修复理由一致。

### S2 · `windows-update-download` 默认开启可能打断更新 —— ✅ 已修复

- **修复后**：`default_enabled: false`（仍 `caution` + `requires_admin: true`）。`maintenance.json` 的 `windows-update-cleanup`（report）继续承担"停服务→删→启服务"的引导。

### S3 · `thumbnails-cache` 占用失败 + 白图标 —— ✅ 已修复（拆分 + GUI）

- **修复后**：`thumbnails-cache.globs` 收窄为 `["thumbcache_*.db"]`；`iconcache_*.db` 拆成**新规则 `icon-cache`**（`caution` + `default_enabled: false`，归入"系统"类），即默认清理不会碰图标缓存。
- **GUI**：清理完成后提示"重启资源管理器以重建图标缓存"并提供重启入口（core-engine/GUI 侧确认）。
- **复核**：59 条中 `icon-cache` 是唯一新增规则，默认关闭；`thumbnails-cache` 仍默认开（只清缩略图，可重建）。

### S4 · `font-cache` 路径写错（死规则）—— ✅ 已修复

- **修复后**：`paths: ["%LOCALAPPDATA%\\Microsoft\\Windows\\Caches"]`、`globs: ["*.db"]`、`default_enabled: false`。
- **复核**：本机 `%LOCALAPPDATA%\FontCache` 不存在（原路径必 0 命中）；新路径 `%LOCALAPPDATA%\Microsoft\Windows\Caches` 存在。默认关闭降低误伤面。

### S5 · `npm-cache` / `pip-cache` 默认开启影响离线/构建速度 —— ✅ 已修复

- **修复后**：两条 `min_age_days: 7`（默认仍开，但只清 7 天未用的缓存）。

### S6 · `adobe-media-cache` 缺少"关闭应用后再清"的约束 —— ✅ 已修复

- **修复后**：`min_age_days: 1`（`risk: caution` 不变）。

### S7 · 5 条大文件 `report` 规则文案与实现不符 —— ✅ 已修复

- **修复后（`bigfiles.json`）**：5 条 `description` 统一为"仅提供命令建议，本工具不执行扫描/删除；…"；`big-files-top50`、`big-folders-top20` 的 `requires_admin` 改为 `true`，与 `report_note` 中"（管理员）"一致。
- **复核**：`big-drive-top`（advanced，默认关）与 `big-files-top50` 功能仍重叠，属产品取舍，不影响安全。

### S8 · 未在真机核实的路径 —— 🟡 部分修复

- **已做**：`steam-htmlcache` 的 `paths` 2 → **1**，删除本机确认不存在的 `%LOCALAPPDATA%\Steam\logs`（仅保留 `htmlcache`）。
- **仍待办**：`dingtalk-cache`、`opera-cache`、`zoom-logs` 路径本机无法验证（软件未安装），打包前用 `tools/verify_*.ps1` 逐条 `Test-Path` 核实；规则本身风险低（0 命中≠误删）。

### S9 · `activity-timeline` 删除范围偏大 —— ✅ 已修复

- **修复后**：`paths: ["%LOCALAPPDATA%\\ConnectedDevicesPlatform"]`、`globs: ["ActivitiesCache.db","ActivitiesCache.db-wal","ActivitiesCache.db-shm"]`、`follow_dirs: true`（不再整目录 `*`）。
- **复核**：本机 `expand_paths` = 1；规则仍 advanced + 默认关闭，只删活动时间线数据库文件。

### S10 · 回收站结果语义 / 回收站占用预览 —— 🟡 部分修复

- **已做（GUI 层）**：结果弹窗区分"已移入回收站（可撤销）"与"已释放空间"；避免用户误以为磁盘已释放。
- **未做**：`winapi.py` 仍未实现 `SHQueryRecycleBinW`，`recycle-bin` 规则扫描时无法显示"将清空多少 MB"；`report_note` 仍是一句提示。
- **影响**：默认关闭 + 二次确认已足够防止误操作；缺少的是"知情度"。建议后续补 `SHQueryRecycleBinW`（约 30 行 ctypes）。

### S11 · 浏览器运行中清理 —— ✅ 已修复（GUI 层，规则字段不变）

- **修复后**：GUI 增加了运行中程序检测（`msedge.exe`/`chrome.exe`/`firefox.exe`），清理前提示先退出浏览器；§4 契约未加字段，规则本身不变（`edge-cache`/`chrome-cache`/`firefox-cache` 仍默认开）。
- **复核**：检测是"提示"而非"强制拦截"——用户坚持清理时仍会有占用失败（只失败不误删），可接受。

### S12 · `live-kernel-reports` 默认开启 —— ✅ 已修复

- **修复后**：`default_enabled: false`（并同步 `requires_admin: true`，见 S1）。未解决内核问题的诊断转储默认不再被清。

---

## 4. 可接受（32 条，不改）

| 分组 | 规则 | 为什么可接受 |
|---|---|---|
| 用户/系统临时文件 | `user-temp`、`windows-temp`、`windows-systemtemp`（权限已按 S1 修） | 缓存目录；运行中占用只会失败不会误删；回收站可撤销 |
| 诊断/报告数据 | `wer-user`、`wer-system`、`windows-minidump`、`crash-dumps-user` | 崩溃报告，删了不影响系统；批量清理属用户预期行为 |
| 日志 | `cbs-log`（7 天保留）、`windows-logs`（权限/保留期已按 S1 修） | 日志可再生；CBS 保留 7 天、Windows 日志仅留 7 天内非更新日志 |
| 更新/传递优化 | `delivery-optimization-cache` | 默认关闭 + 需管理员 + 会重建 |
| 回收站 | `recycle-bin` | 默认关闭 + GUI 二次确认；语义提示见 S10（部分修复） |
| 旧系统/维护报告 | `windows-old`、`hibernation-file`、`pagefile`、`winsxs-component-store`、`windows-installer-cache`、`restore-points`、`cleanmgr-sagerun`、`reserved-storage`、`windows-update-cleanup` | 全部 `report`，只展示命令，**永不删除**；命令均来自 Microsoft Learn 且带警告 |
| 浏览器缓存 | `chromium-crashpad`、`edge-webview-cache` | 崩溃转储/WebView 缓存，可再生；`protected_globs` 已挡住 Cookie/密码类文件 |
| 应用缓存 | `discord-cache`、`vscode-cache`、`office-filecache`、`onedrive-logs`、`java-webstart-cache`、`wechat-cache` | 缓存/日志可再生；`wechat-cache` 默认关闭 + 窄白名单（仅 `FileStorage\Cache`）+ `min_age_days: 1`；`office-filecache` 有 1 天年龄门槛 |
| 隐私痕迹（默认关） | `recent-files`、`jump-lists`、`clipboard-history`、`notification-db` | 全是"记录"不是文档；`advanced` 强制默认关闭；`recent-files` 描述已提示会重置快速访问 |
| 大文件（report） | `big-downloads`、`leftover-installers`、`big-drive-top`、`big-files-top50`、`big-folders-top20` | 只报告不执行（文案已按 S7 统一） |
| 修复轮新增 | `icon-cache` | Explorer 图标缓存，可再生；`caution` + 默认关闭（S3 拆分产物） |

> 备注：必须改/建议提示涉及的 27 条规则已在 §2/§3 列档，不在此表重复。可接受类 32 条 + 必须改 5 条 + 建议提示 22 条 = **59**。

---

## 5. 关键防线核对（修复后复核）

| # | 防线 | 核对结果 | 证据 |
|---|---|---|---|
| 1 | §5 保护路径硬编码 | 通过：盘符根/Windows/Program Files/ProgramData/用户标准目录/`WinSxS`/`$Recycle.Bin`/`System Volume Information` 等全部登记；含 realpath + 祖先判定 | `safety.py:36-103`、`312-443` |
| 2 | 用户文档目录不被触碰 | 通过：**120 条路径**中 `%USERPROFILE%` 仅 1 条（`wechat-cache` 的 `Documents\WeChat Files\<账号>\FileStorage\Cache`），其余 `LOCALAPPDATA` 80 / `APPDATA` 27 / `WINDIR` 10 / `TEMP` 1 / `PROGRAMDATA` 1；无 Desktop/Downloads/OneDrive/Documents 直删路径 | 脚本穷举（见 §7） |
| 3 | 微信例外足够窄 | 通过：正则只匹配 `<userprofile>\documents\wechat files\<账号>\filestorage\cache` 及其内部；规则默认关闭 | `safety.py:124-127`、`446-467` |
| 4 | 浏览器用户数据不删 | 通过：规则路径只到 `Cache/Code Cache/GPUCache/ShaderCache/Service Worker` 等；`protected_globs` 显式挡 `Cookies`/`Login Data`/`Bookmarks`/`Local State`/`History`；穷举 120 条路径，无一条指向 `Cookies`/`Login Data`/`Bookmarks`/`Local State`/`Web Data` | `browser.json`、`privacy.json`、脚本检查 |
| 5 | 默认回收站、失败不降级 | 通过：`mode="recycle"` 为默认；`ALLOW_PERMANENT_FALLBACK=False`，回收站失败不自动永久删除 | `cleaner.py:37-38`、`289-317` |
| 6 | 管理员门禁 | 通过：**20 条** `requires_admin` 规则在非管理员下清理阶段整条跳过 | `cleaner.py:270-275` |
| 7 | advanced 默认关闭 | 通过：`validate_rule` 强制 `risk=advanced → default_enabled=False`，15 条 advanced 全部默认关；另有 10 条 caution 主动关闭，共 25 条默认关 | `rules.py:250-253` |
| 8 | 不跟随 junction/符号链接 | 通过：扫描器对 symlink/reparse point 双检跳过；安全守卫构建保护表时也跳过旧兼容链接 | `scanner.py:255-265`、`safety.py:256-269` |
| 9 | report 规则永不删除 | 通过：14 条 report 在扫描阶段短路、清理阶段 `continue`；`empty_recycle_bin` 单独 action 且默认关闭 | `scanner.py:395-400`、`cleaner.py:257-268` |
| 10 | 规则加载期兜底 | 通过：`validate_rule` 对 `delete` 规则要求 paths 非空、绝对路径、非受保护；通配路径用替身样本判定保护 | `rules.py:183-209`、`254-262` |
| 11 | 通配段不再"假成功" | 通过：`*` 在中间段时文件+目录都收；`chromium-browsing-history` 本机 0 → 4，隐私清理不再空转 | `rules.py:129-164`（红队 M2 修复） |

---

## 6. 安全结论（修复后独立判断）

**问：这套工具在"非管理员默认配置"下，是否存在"用户点一下就可能丢数据"的路径？**

**答：没有。** 初版审查指出的 3 类问题（Teams 删应用状态、隐私规则假清理、Prefetch 默认开启）已全部修复；当前 59 条规则中：

- **不会丢用户文档/照片/聊天记录**：所有删除路径都在 `AppData` 缓存、`Windows\Temp/Logs/Prefetch` 或 WER/CrashDumps 等诊断目录；用户文档、桌面、下载、OneDrive、Program Files、`C:\Windows` 非白名单目录全部被 `safety.py` 硬保护；`Documents` 整树保护，唯一例外是微信 `FileStorage\Cache` 且默认关闭。即使规则 JSON 被篡改，加载期 `validate_rule` 也会因受保护路径直接抛 `RuleError`。
- **不会误删浏览器凭据**：没有任何规则删除 `Cookies`/`Login Data`/`Bookmarks`/`Local State`/`Web Data`；Advanced 隐私规则最多删 `History/Top Sites/Visited Links/Favicons`，默认关闭。
- **默认配置可撤销**：删除默认进回收站、`ALLOW_PERMANENT_FALLBACK=False`；20 条需管理员规则非管理员不删；25 条默认关闭（15 advanced + 10 caution）。
- **修复轮带来的新保障**：Teams 状态类目录已移除；隐私规则真正生效（不再假成功）；Prefetch 默认不动；图标缓存默认不碰；系统日志/更新缓存默认不动；GUI 增加了运行中程序检测与"回收站/已释放"结果区分。

**修复后残余风险（均为低风险、非数据丢失）**：

1. **S10 部分修复**：`recycle-bin` 清空前的占用预览（`SHQueryRecycleBinW`）未实现，用户只知道"会清空回收站"、不知道"多少 MB"；默认关闭 + 二次确认可兜底。
2. **S8 部分修复**：`dingtalk-cache`/`opera-cache`/`zoom-logs` 路径待装机核实——最坏情况是"0 命中"，不会误删。
3. **S11 是 GUI 提示而非强制**：用户坚持在浏览器运行时清理，仍会出现占用失败（只失败不误删）。
4. **永久删除模式（用户显式开启）+ 全选 advanced**：此时无回收站兜底，只剩 `safety` + 二次确认 → 建议 GUI 在永久删除模式下对 advanced 规则逐条确认并显示"不可恢复"。
5. **可接受类 32 条**：缓存/诊断/日志数据，回收站可撤销，风险与收益匹配。

**一句话结论：默认配置下没有"一键丢数据"的路径，59 条规则集可以发布；残余风险集中在"知情度/前置提示"（S10/S11）与 3 条待核实路径（S8），建议后续迭代补齐。**

---

## 7. 复现方式（只读）

```powershell
# 1) 规则统计（预期：total 59 / delete 44 / caution 17 / admin 20 / default_off 25 / paths 120）
& 'C:\Users\HUAWEI\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe' -c @"
import json,glob,collections
rs=[r for f in sorted(glob.glob(r'E:\deepseekharness在这里的工作区\qingfeng\data\rules\*.json')) for r in json.load(open(f,encoding='utf-8'))['rules']]
print(len(rs), dict(collections.Counter(r['category'] for r in rs)))
print(dict(collections.Counter(r['action'] for r in rs)), dict(collections.Counter(r['risk'] for r in rs)))
print('admin',sum(r['requires_admin'] for r in rs),'off',sum(not r['default_enabled'] for r in rs),'paths',sum(len(r['paths']) for r in rs))
"@

# 2) M2 修复证据（预期：chromium-browsing-history=4 / firefox-form-history=0（未装 Firefox））
& 'C:\Users\HUAWEI\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe' -c @"
import sys; sys.path.insert(0,r'E:\deepseekharness在这里的工作区')
from qingfeng.core import rules as R
rs={r.id:r for r in R.load_rules()}
for rid in ('chromium-browsing-history','firefox-form-history','activity-timeline','edge-cache'):
    print(rid,len(R.expand_paths(rs[rid])))
"@

# 3) M1 修复证据：Teams 规则不应再含应用状态目录（预期：无匹配）
Select-String -Path 'qingfeng\data\rules\apps.json' -Pattern 'IndexedDB|Local Storage|blob_storage' -SimpleMatch

# 4) S1/S4 的权限与路径证据（只读）
Get-Acl "$env:WINDIR\Logs" | Select-Object -ExpandProperty Access | Where-Object IdentityReference -match 'Users'
try { Get-ChildItem "$env:WINDIR\Prefetch" -ErrorAction Stop | Select-Object -First 1 } catch { "Prefetch denied" }
Test-Path "$env:LOCALAPPDATA\Microsoft\Windows\Caches"   # True → font-cache 新路径有效
```

---

## 附录 A：59 条规则逐条定档 + 修复状态

| # | rule id | 分类 | 档位 / 修复状态 | 一句话 |
|---|---|---|---|---|
| 1 | user-temp | 系统 | 可接受 · 不改 | 回收站兜底；运行中占用只会失败 |
| 2 | windows-temp | 系统 | 可接受 · 不改 | 同上，需管理员 |
| 3 | windows-logs | 系统 | 建议提示 S1 · ✅已修复 | `requires_admin: true` + `min_age_days: 7` + 排除 `CBS`/`WindowsUpdate` |
| 4 | cbs-log | 系统 | 可接受 · 不改 | 7 天保留策略合理 |
| 5 | windows-prefetch | 系统 | **必须改 M3 · ✅已修复** | 默认关 + 需管理员 + 30 天门槛 |
| 6 | windows-minidump | 系统 | 可接受 · 不改 | 诊断数据；需管理员 |
| 7 | live-kernel-reports | 系统 | 建议提示 S12/S1 · ✅已修复 | 默认关 + `requires_admin: true` |
| 8 | windows-systemtemp | 系统 | 建议提示 S1 · ✅已修复 | `requires_admin: true` |
| 9 | wer-user | 系统 | 可接受 · 不改 | 崩溃报告可再生 |
| 10 | wer-system | 系统 | 可接受 · 不改 | 同上，需管理员 |
| 11 | crash-dumps-user | 系统 | 可接受 · 不改 | 同上 |
| 12 | font-cache | 系统 | 建议提示 S4 · ✅已修复 | 路径改 `%LOCALAPPDATA%\Microsoft\Windows\Caches` + `*.db` + 默认关 |
| 13 | windows-update-download | 系统 | 建议提示 S2 · ✅已修复 | `default_enabled: false` |
| 14 | delivery-optimization-cache | 系统 | 可接受 · 不改 | 默认关 + 需管理员 |
| 15 | thumbnails-cache | 系统 | 建议提示 S3 · ✅已修复 | `globs` 收窄为 `thumbcache_*.db` |
| 16 | icon-cache | 系统 | 可接受 · 不改（新增） | S3 拆分出的 `iconcache_*.db` 规则，caution + 默认关 |
| 17 | inet-cache | 系统 | 可接受 · 不改 | 旧网页缓存 |
| 18 | recycle-bin | 系统 | 可接受 · 不改（S10 部分） | 默认关 + 二次确认；占用预览未实现 |
| 19 | windows-old | 系统 | 可接受 · 不改 | 只报告 |
| 20 | edge-cache | 浏览器 | 建议提示 S11 · ✅已修复（GUI 检测） | 运行中清理失败；GUI 提示先退出浏览器 |
| 21 | chrome-cache | 浏览器 | 建议提示 S11 · ✅已修复（GUI 检测） | 同上 |
| 22 | firefox-cache | 浏览器 | 建议提示 S11 · ✅已修复（GUI 检测） | 同上 |
| 23 | chromium-crashpad | 浏览器 | 可接受 · 不改 | 崩溃转储可再生 |
| 24 | edge-webview-cache | 浏览器 | 可接受 · 不改 | 缓存可再生；宿主进程运行时失败可见 |
| 25 | opera-cache | 浏览器 | 建议提示 S8 · 🟡部分（待装机核实） | 路径本机无法验证，0 命中无害 |
| 26 | teams-new-cache | 应用 | **必须改 M1 · ✅已修复** | `paths` 8→5，移除 IndexedDB/Local Storage/blob_storage |
| 27 | teams-classic-cache | 应用 | **必须改 M1 · ✅已修复** | `paths` 9→6，同上 |
| 28 | wechat-cache | 应用 | 可接受 · 不改 | 默认关 + 窄白名单 + 仅 Cache 目录 |
| 29 | discord-cache | 应用 | 可接受 · 不改 | blob_storage 为媒体缓存，风险低于 Teams |
| 30 | vscode-cache | 应用 | 可接受 · 不改 | 缓存可再生，不动 settings |
| 31 | steam-htmlcache | 应用 | 建议提示 S8 · ✅已修复 | 删除不存在的 `Steam\logs`，仅保留 `htmlcache` |
| 32 | office-filecache | 应用 | 可接受 · 不改 | 1 天门槛 |
| 33 | onedrive-logs | 应用 | 可接受 · 不改 | 日志，不碰同步目录 |
| 34 | dingtalk-cache | 应用 | 建议提示 S8 · 🟡部分（待装机核实） | 路径本机无法验证 |
| 35 | adobe-media-cache | 应用 | 建议提示 S6 · ✅已修复 | `min_age_days: 1` |
| 36 | java-webstart-cache | 应用 | 可接受 · 不改 | 缓存，重新下载即可 |
| 37 | npm-cache | 应用 | 建议提示 S5 · ✅已修复 | `min_age_days: 7` |
| 38 | pip-cache | 应用 | 建议提示 S5 · ✅已修复 | `min_age_days: 7` |
| 39 | zoom-logs | 应用 | 建议提示 S8 · 🟡部分（待装机核实） | 路径本机无法验证 |
| 40 | recent-files | 隐私痕迹 | 可接受 · 不改 | advanced 默认关；描述已提示副作用 |
| 41 | jump-lists | 隐私痕迹 | 可接受 · 不改 | 同上 |
| 42 | clipboard-history | 隐私痕迹 | 可接受 · 不改 | 同上 |
| 43 | notification-db | 隐私痕迹 | 可接受 · 不改 | 同上 |
| 44 | chromium-browsing-history | 隐私痕迹 | **必须改 M2 · ✅已修复** | 引擎通配修复，本机展开 0→4 |
| 45 | activity-timeline | 隐私痕迹 | 建议提示 S9 · ✅已修复 | 收窄为 `ActivitiesCache.db*` |
| 46 | firefox-form-history | 隐私痕迹 | **必须改 M2 · ✅已修复** | 引擎通配修复；本机未装 Firefox（0 属预期） |
| 47 | big-downloads | 大文件 | 建议提示 S7 · ✅已修复 | 文案统一"仅提供命令建议" |
| 48 | leftover-installers | 大文件 | 建议提示 S7 · ✅已修复 | 同上 |
| 49 | big-drive-top | 大文件 | 建议提示 S7 · ✅已修复 | 同上；与 big-files-top50 仍部分重叠（产品取舍） |
| 50 | big-files-top50 | 大文件 | 建议提示 S7 · ✅已修复 | 文案与 `requires_admin: true` 已一致 |
| 51 | big-folders-top20 | 大文件 | 建议提示 S7 · ✅已修复 | 同上 |
| 52 | hibernation-file | 系统维护 | 可接受 · 不改 | report + powercfg 命令 |
| 53 | pagefile | 系统维护 | 可接受 · 不改 | report |
| 54 | winsxs-component-store | 系统维护 | 可接受 · 不改 | report + DISM 警告 |
| 55 | windows-installer-cache | 系统维护 | 可接受 · 不改 | report（勿手删） |
| 56 | restore-points | 系统维护 | 可接受 · 不改 | report + vssadmin |
| 57 | cleanmgr-sagerun | 系统维护 | 可接受 · 不改 | report，最安全的清理入口 |
| 58 | reserved-storage | 系统维护 | 可接受 · 不改 | report + DISM 命令 |
| 59 | windows-update-cleanup | 系统维护 | 可接受 · 不改 | report；与 `windows-update-download` 功能互补 |

> 统计自检：必须改 5 条 + 建议提示 22 条 + 可接受 32 条 = **59**。
> 修复状态统计（按问题项）：已修复 13 项（M1-M3 + S1-S7、S9、S11、S12）、部分修复 2 项（S8/S10）、明确不改 0 项。
> 本文只做审查与建议，未改动 `qingfeng/` 下任何文件；修复由 core-engine 实施、Lead 裁决。
