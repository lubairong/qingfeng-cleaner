# v2 调研：空间透视 + 决策助手（开源方案对比 & 分类词典设计）

> 对应契约：[docs/ARCHITECTURE-V2.md](../ARCHITECTURE-V2.md)（§2 SpaceIndex、§3 ReviewItem/heuristics、§4 GUI）。
> 配套交付：**`docs/research/space-heuristics.json`**（14 个分类 + 3 条年龄规则 + 18 条 never_touch，core-engine 直接消费）。
> 调研时间：2026-10-22 · 写域：`docs/research/`（本次未改动其他文件）。
> 本机侦察数据来自 `qa/recon.json`（Lead 提供）+ 我自己跑的两轮只读扫描（见文末复现命令）。

---

## 0. 一页结论（给 Lead / core-engine / gui）

1. **v2 的价值不在"多清 700MB"，而在"把 122.8GB 讲清楚并给出可执行建议"**。竞品（WizTree/WinDirStat/TreeSize）都只做到"展示+手动删"，没有一个把"该不该删"用中文理由讲清楚——这正是我们的差异化点。
2. **索引实现不建议抄 WizTree 的 MFT 直读**（闭源、需要管理员卷句柄、Python 标准库做不了）；用 `os.scandir` + 多线程即可满足"122GB / 40 万文件 ≤60 秒"。**本机实测：扫描 49.6 万个目录项、筛出 71 个 ≥100MB 大文件只用了 3.2 秒**（不含 `C:\Windows` 受保护目录），60 秒目标非常宽松。
3. **大文件决策的关键是"上下文"，不是"扩展名"**：本机 71 个 ≥100MB 文件中，单看 `.exe` 会把 Claude CLI、微信插件、Ollama 运行时都误判成"安装包"。因此 `space-heuristics.json` 里 `installer` 类用 **`match: "all"`（扩展名 ∧ 路径上下文）**，其余类用 `any`——这是本词典最重要的设计决定（§5.2）。
4. **迁移功能的最大坑不是拷贝，而是目标盘可写性**：本机实测 `D:\` 根对普通用户只给 `RX`（不能建文件夹），`E:\` 根可写；必须"探测可写 → 失败回退到用户已有可写目录 → 再失败提示需要管理员"，不能假设 `<盘符>:\QingFengMoved` 可用（§6）。
5. **系统占用要"显式展示"，不能假装不存在**：WinSxS 14.4GB、hiberfil 6.76GB、约 27GB 的 `$Recycle.Bin`/`System Volume Information`/无权限目录，都要给出"需管理员/给命令"的卡片；工具只提供命令、绝不代执行（§7）。

**分类词典实测覆盖率**（本机 71 个 ≥100MB 文件，合计 18.22GB）：

| 分类 | 文件数 | 大小 | 建议 | 说明 |
|---|---|---|---|---|
| `program` 已安装程序 | 21 | 4.52 GB | keep | Ollama 运行时、微信/飞书/Codex 等程序文件 |
| `installer` 安装包/升级残留 | 15 | **4.29 GB** | **safe_delete** | Intel 显卡驱动 exe、各 `*-updater\installer.exe`、Quark 更新包 |
| `model-db` 模型/数据库 | 8 | 3.17 GB | keep | Ollama CUDA DLL、`workbuddy.db-wal` |
| `app-version-dup` 程序旧版本 | 11 | **3.07 GB** | review | EdgeCore/EdgeWebView/Copilot 多版本副本 |
| `devcache` 开发缓存 | 6 | 1.16 GB | review | npm/node_modules、ms-playwright |
| `backup` 备份/旧副本 | 3 | 1.03 GB | review | GreenCore7z 的 chrome.7z、Huawei `boot.bak` |
| `never_touch` | 4 | 0.54 GB | keep(danger) | Windows 目录内文件 |
| `personal` 个人文件 | 1 | 0.23 GB | keep | 桌面 PDF |
| `app-cache` / `archive` | 1 / 1 | 0.10 / 0.10 GB | review | — |
| **合计** | **71** | **18.22 GB** | — | **100% 命中分类，0 个落入 large-misc** |

按建议聚合：**keep 8.46GB / review 5.47GB / safe_delete 4.29GB**——即"一键安全可清 4.29GB + 需你确认 5.47GB"，比 v1 的 700MB 高一个数量级，且每项都有中文理由。

---

## 1. 竞品对比（空间分析工具）

| 工具 | 性质/许可 | 索引原理 | 空间归类方式 | "建议清理 vs 你来判断" | Treemap/可视化 | 重复文件 | 迁移到其它盘 |
|---|---|---|---|---|---|---|---|
| **WizTree**（[diskanalyzer.com](https://diskanalyzer.com/)） | 闭源免费（个人）；商业需 Supporter/企业授权 | **直读 NTFS MFT**（官网原文："reads the hard drive's Master File Table (MFT) directly from the disk (like Everything Search does)"），非 NTFS 走普通扫描；**正确处理硬链接、不重复计数**；自称比 WinDirStat 快 46× | 目录树 + 按大小排序；按扩展名统计 | ❌ 只有"找大文件/重复文件"，**没有删除建议**，用户自己判断 | 有 treemap（可下钻、点击定位） | ✅（内置重复文件查找） | ❌ 无迁移功能 |
| **WinDirStat**（[windirstat/windirstat](https://github.com/windirstat/windirstat)） | **GPL-3.0**（应用部分 GPLv3+，部分源码更宽松；logo CC BY 3.0）；C++/MFC，4.4k stars | 目录遍历；**管理员运行时启用"加速的本地 NTFS 扫描"**（2.x 新特性） | 目录树 + **文件类型统计** + treemap/sunburst/flame graph 多视图 | ❌ 无建议，只有工具（可调 Windows 清理工具/压缩/硬链接去重） | treemap + 旭日图 + 火焰图，交互成熟：点击定位、右键操作 | ✅ 可配置 hash、云端文件保护、稀疏哈希标记"可能重复"（Probable duplicates） | ❌（可"move"文件但无目标盘管理） |
| **TreeSize Free**（[jam-software.com/treesize](https://www.jam-software.com/treesize)） | 闭源，个人免费（商用需 Personal/Professional） | 目录遍历（多线程） | 目录树 + 按大小/年龄/类型排序、**扩展名分组视图**、饼图/柱图/treemap | ❌ 无建议；Free 版连"Top 大文件过滤/重复文件"都要 Personal | treemap + 饼图 + 柱图，Explorer 式界面；PDF 报告 | Personal 起才有 | ❌ 无迁移 |
| **SpaceSniffer**（[uderzo.it](http://www.uderzo.it/main_products/space_sniffer/)） | 闭源免费ware | 目录遍历（单线程，慢） | 纯 treemap（面积即大小） | ❌ 无建议 | **最纯粹的 treemap 交互**：无限下钻、双击进入、按文件类型/时间过滤、色彩自定义——我们 v2 的 treemap 交互主要参考它 | ❌ | ❌ |
| **Czkawka**（[qarmin/czkawka](https://github.com/qarmin/czkawka)） | Rust；GitHub API 显示 `NOASSERTION`（仓库 LICENSE 为 MIT，需人工确认）；34.4k stars | 按需扫描（不做常驻索引） | 工具式：重复文件/相似图片/大文件/空文件夹/临时文件 | ⚠️ 只给"相似度/大小"排序，无中文建议 | 无 treemap（列表/网格） | ✅ **业界参考级**：大小分组→部分哈希→全量哈希；相似图片用感知哈希 | ❌ |
| **dupeGuru**（[arsenetar/dupeguru](https://github.com/arsenetar/dupeguru)） | Python；**GPL-3.0**；7.9k stars | 目录遍历 + 内容哈希 | 只做重复文件（标准/音乐标签/图片） | ⚠️ 分组展示"保留哪个"，无空间建议 | 列表 + 分组高亮 | ✅ 成熟：多种匹配引擎（文件名/标签/内容哈希），可忽略列表 | ❌ |
| **Everything**（[voidtools.com](https://www.voidtools.com/)） | 闭源免费ware | **文件名索引**（NTFS MFT/USN Journal 增量更新），毫秒级搜索 | 搜索而非分析；按大小/日期排序 | ❌ 纯搜索 | ❌ | ✅ 有 Find Duplicates（按文件名/大小） | ❌ |
| **Windows 存储感知 / 存储设置**（[MS Support](https://support.microsoft.com/en-us/windows/experience/storage-filemanagement/manage-drive-space-with-storage-sense)） | 系统内置 | 不建索引，按需统计 | **官方口径**：Apps & features / Temporary files / Documents / Pictures / Other / System & reserved | ⚠️ 只做"自动清理"（临时文件、回收站按天、下载文件夹按天、云文件脱水），**不解释单个大文件**；Storage Sense **只作用于系统盘 C:** | ❌ 无 treemap（只有磁盘占用条） | ❌ | ❌（但有"保存新内容到其它盘"设置） |

**我们 v2 的定位差异**：WizTree/TreeSize 解决"看见"，Czkawka/dupeGuru 解决"重复"，Storage Sense 解决"自动清缓存"；**没有一个把"这个 4.3GB 该不该删"翻译成人话**。v2 = 看见（treemap+树）+ 判断（分组/建议/理由）+ 动作（回收站/迁移），这正好补上 v1 只清 700MB 的短板。

**许可证结论**：WinDirStat(GPL-3.0)、dupeGuru(GPL-3.0)、DriverStore Explorer(GPL-2.0) 的**代码不可拷贝**；WizTree/TreeSize/SpaceSniffer/Everything 闭源，只能参考思路与交互；我们的索引/决策代码与 `space-heuristics.json` 数据全部自写（路径与扩展名属事实信息）。Czkawka 许可待人工确认，不复用其代码。

---

## 2. Treemap / 旭日图交互（v2 §4.1 落地参考）

**算法**：用 **Squarified Treemap**（Bruls/Huizing/van Wijk, 2000）。要点：
- 输入：当前目录下按 `size` 降序的子项；输出：矩形集合，尽量接近正方形（长宽比 ≤ ~2.5），避免细长条。
- 实现自绘 `tk.Canvas.create_rectangle`；**只画当前层**（默认 200 块内），子目录块带下钻；双击/右键菜单"打开位置"。
- 性能：200 个 `create_rectangle` 在 Tk 上远低于 200ms；避免每次重绘创建数千 item，用 `canvas.delete("all")` + 批量创建；颜色按 `.get(key)` 缓存。
- 交互参考 SpaceSniffer：单击选中高亮、双击下钻、面包屑返回、悬停 tooltip（路径/大小/修改时间）、右键"回收站/迁移/打开位置"。
- 色彩编码（与 §4.1 一致）：目录=蓝灰系（按深度变浅）、`installer`=橙、`video`=紫、`vm/archive`=深蓝、`keep/program/system`=灰、`safe_delete` 高亮描边。
- 旭日图（sunburst）参考 WinDirStat：v2 不做（Tk Canvas 上实现成本高、收益低），用 treemap + 目录树即可；如需，用同心圆环 + 最多 3 层限制。

**目录树联动**：Treeview 与 treemap 双向同步选中；树节点显示 `size / file_count / 最新 mtime`；`truncated=True` 时显示 "… 其余 {other_size} 已折叠"。

---

## 3. "空间归类"与"建议表达"的设计（对比 Storage Sense 口径）

### 3.1 Windows 官方口径（Storage Sense）

- 官方"存储"页把空间拆为：**Apps & features / Temporary files / Documents / Pictures / Other / System & reserved**（Win11 实际文案随版本略变），并单独列"清理建议"（临时文件、回收站、下载、以前的 Windows 安装）。
- Storage Sense 自动清理的只有：临时文件、回收站按天数、下载文件夹按天数（默认不动下载！）、云文件脱水（Win11 22H2+ 默认 30 天）。**只作用于系统盘**，且 FAQ 明确"Downloads 和云内容除非显式设置否则不处理"。
- 对我们的启示：① 用"Apps/临时文件/你的文件/系统保留"这套**用户已熟悉的词汇**做顶部构成条（§4.1 已这么设计）；② Storage Sense 是"自动清"，我们是"解释 + 决策"，两者互补——在空间页放一个"用 Windows 自带清理（cleanmgr / 存储感知）"的引导链接，不做重复实现。

### 3.2 我们的四级表达（建议 core-engine/gui 按此实现）

| 级别 | `advice` | UI 文案 | 默认勾选 | 动作 |
|---|---|---|---|---|
| 放心清理 | `safe_delete` | "升级残留，装完通常可删" | ✅ 勾选 | 回收站（二次确认显示合计） |
| 你来判断 | `review` | 展示 `reason`（含大小/年龄） | ❌ 不勾选 | 手动勾选 → 回收站/迁移 |
| 建议迁移 | `review` + group∈{video,archive,vm} | "建议迁移到 D/E 盘" | ❌ | 默认动作按钮是"迁移"而非"删除" |
| 不要删 | `keep` | 展示 `reason`（"删除会导致程序无法运行/可能丢数据"） | ❌ 且列表灰显 | 仅"打开位置"；勾选需额外警告 |

`risk` 三档用于颜色/图标：`safe` 绿、`caution` 橙、`danger` 红；`never_touch` 命中强制 `keep + danger` 并显示"系统/程序目录，请勿手动删除"。

**分组 chips 顺序**按契约 §3.2；我们的 14 个分类到 7 个 group 的映射：

| 契约 group | 我们的 category key | 说明 |
|---|---|---|
| `installer` | `installer`、`driver-package` | 驱动包归入安装包组（文案"驱动安装包可删，DriverStore 另议"） |
| `app-version-dup` | `app-version-dup` | Edge/Copilot 旧版本 |
| `backup` | `backup` | |
| `devcache` | `devcache`、`app-cache` | 应用缓存归到"可清缓存"组（v1 清理页可清） |
| `video` / `archive` / `vm` | `video`、`archive`、`vm` | 三组并列，默认动作"迁移" |
| `large-misc` | `large-misc` | 兜底（本机实测 0 命中） |
| `keep` | `model-db`、`personal`、`program`、`system-media`、`never_touch` | 细分 keep 子标签，界面可按 `label` 再分节 |

> 建议 core-engine 在 `heuristics.json` 之外另加一张 `key→group` 映射（或直接在代码里映射），不要改契约 §3.2 的 7 个 group key。

---

## 4. 重复文件判定（v2 先不做，v2.1 再做）

参考 Czkawka / dupeGuru / WinDirStat 的成熟做法：

1. **按大小分组**（不同大小必然不重复）→ 2. **部分哈希**（头/中/尾各 4–8KB，快速剔除）→ 3. **全量哈希**（BLAKE2b/xxhash 分块，避免读整个大文件到内存）→ 4. 分组结果按"保留路径优先级"（用户目录 > Program Files > 缓存）给出建议。
- 优化：先按文件名相同过滤（Czkawka 的 "name+size" 快速模式）；非管理员读不了的文件跳过不报错。
- **v2 不做重复文件的理由**：① 61–71 个大文件里重复项很少（本机扫描未发现同名同大小）；② 全量哈希 18GB 需要几分钟且 IO 冲击大；③ "迁移"已能解决大部分空间问题。建议 v2.1 作为独立页签，且**只标记、不自动删**。
- 若要低成本做个"疑似重复"：同大小+同文件名+同扩展名 → 列入 `review`，不入 `safe_delete`。

---

## 5. `space-heuristics.json` 设计说明（交付物）

### 5.1 结构

```json
{ "version": 1,
  "categories": [ { "key","label","match","exts","path_globs","advice","risk","reason" } × 14 ],
  "age_rules": [ { "min_size","min_age_days","advice","reason" } × 3 ],
  "never_touch": [ "glob" × 18 ] }
```

- 与契约 §3.1 完全一致的字段：`key/label/exts/path_globs/advice/risk` + `version/age_rules/never_touch`；
- **新增两个字段**（契约未禁止，core-engine 需感知）：
  - `match`：`"all" | "any"`。`"all"` 表示 **`exts` 命中 且 某个 `path_globs` 命中** 才算该类；`"any"` 表示二者任一命中即可。仅 `installer` 用 `"all"`（见 5.2）。
  - `reason`：一句话中文理由模板，支持 `{size}`（human_size）与 `{age}`（天）两个占位符，直接填进 `ReviewItem.reason`。
- `never_touch` 优先级最高（命中即 `keep` + `danger`），**先于 categories 判定**；`age_rules` 只对 `large-misc`/`review` 做 reason 补充与"久未修改"提级，**不产生 `safe_delete`**（避免"年龄大"被误当成"可删"）。

### 5.2 为什么 `installer` 必须是 `match: "all"`（最重要的坑）

- 如果按 OR 语义，`installer.exts` 里的 `.exe` 会把**所有 exe** 判成"安装包/放心清理"——本机实测会把 `claude.exe`、`WeChatAppEx.exe`、Ollama 运行时等 6.79GB 误标为可删。
- 改为 AND 后：`.exe` 只有在 `*\Downloads\*.exe`、`*\@*-updater\*`、`*ProgramData\Intel\DSA\Downloads\*`、`*Package Cache*`、`*更新包*.exe`、`*Temp\*.exe` 等上下文中才算安装包；其余 exe 落到 `program`（keep）或 `devcache`（review）。
- 这是"发现与判断"的核心：**判断依赖上下文而不是扩展名**。
- 若 core-engine 决定不实现 `match` 字段，**必须在代码里硬编码这一条**：`installer := ext ∈ exts AND path_globs 命中`；否则请勿上线该词典。

### 5.3 覆盖的真实样本（Lead 指定 + 本机实测）

| 样本 | 命中分类 | 建议 |
|---|---|---|
| `*\@*-updater\installer.exe` | installer | safe_delete |
| `*\@*-updater\pending\*` | installer | safe_delete |
| `*ProgramData\Intel\DSA\Downloads\*` | installer | safe_delete |
| `*GreenCore7z\*\*.7z` | backup | review |
| `*Quark\User Data\updates\*` | installer | safe_delete |
| `*Huawei\Driver\OneKey Driver\*.bak` | backup | review |
| `*\Ollama\*`（CUDA/ROCm dll） | model-db | keep |
| `*.db-wal`（workbuddy） | model-db | keep |
| 桌面大 PDF | personal | keep |
| `npm\node_modules\...\claude.exe` | devcache | review（不误判为安装包） |

> 注：`*\*-updater\*` 是比 `@*-updater` 更宽的兜底（本机还有 `obsidian-updater`、`xyz.chatboxapp.app-updater` 等），建议保留两条。

### 5.4 age_rules 的语义

| 阈值 | advice | 用途 |
|---|---|---|
| ≥1GB 且 ≥365 天 | review | "超过 1 年未修改"，给 review 项补一句理由 |
| ≥512MB 且 ≥180 天 | review | "半年未修改，大概率冷数据" |
| ≥100MB 且 ≥730 天 | review | "两年未使用" |

不建议加入"大 + 老 → safe_delete"的年龄规则：年龄不能证明可删（本机 `boot.bak` 2024 年、`Intel Setup.exe` 2023 年都是该保留判断的）。

---

## 6. 迁移到其它盘（v2 §3 `move_many` / §4.1 迁移栏）

### 6.1 本机 ACL 实测（Lead 提供，**这是最现实的坑**）

| 位置 | ACL | 普通用户可写？ | 结论 |
|---|---|---|---|
| `D:\` 根 | `BUILTIN\Users:(OI)(CI)(RX)` + Administrators(F) | ❌ 实测 mkdir 被拒 | **不能**默认用 `D:\QingFengMoved` |
| `D:\Download`、`D:\tmp`、`D:\Doc` 等用户自建目录 | Authenticated Users: Modify | ✅ | 可作为回退目标 |
| `E:\` 根 | Authenticated Users: Modify | ✅ | 可直接作为目标根 |

**因此迁移目标探测算法建议**：

```
候选目标 = [用户选择的目录] 或 [<盘>:\QingFengMoved] 或 [<盘>:\Downloads] 或 [<盘>:\<用户自建可写目录>]
for cand in 候选目标:
    1. 若不存在 → 尝试 makedirs(cand)（捕获 PermissionError）
    2. 写探测：创建 .qf_write_test_<pid> 并删除（真正的可写性判定，ACL 的 RX 也会在此失败）
    3. 检查 free_space >= 待迁移总大小 * 1.05（跨盘复制需要空间）
    4. 通过 → 采用；否则下一个
全部失败 → 返回"目标盘不可写：请先在 D 盘新建一个文件夹，或以管理员身份运行"（GUI 提供"选择其它文件夹"按钮）
```

- 不要用 `os.access(W_OK)` 判定：Windows 上它只看只读属性，**ACL 的 RX 会返回 True 但实际 mkdir 失败**。
- 探测用的临时文件必须用唯一名字并 try/finally 删除；失败也要清理。

### 6.2 复制/移动实现

- **不用 `robocopy /MOVE` 作为主实现**（外部进程、解析日志、权限/路径转义麻烦），用 Python：
  1. `safety.assert_deletable(src)`（受保护路径拒绝，进 `skipped`）；
  2. 目标盘 `GetDriveTypeW == 3`（固定盘）且与源不同卷；目标不在源内部；源不是 reparse point；
  3. 目标路径 = `<dest_root>\<源盘符>_mirror\<源相对路径>`（防重名、可回溯）；
  4. `shutil.copy2`（或分块 `copyfileobj` + 64KB 缓冲）→ 校验（size + mtime + 抽样 BLAKE2b；大文件建议全量 BLAKE2b 但在后台低优先级 IO）；
  5. 校验通过后 `os.remove`/`rmtree` 源；任何一步失败**保留源**并记 `failed`；
  6. 支持 `cancel`：已完成的保留，未完成的源不动；`MoveResult` 记录 `moved/moved_bytes/skipped/failed`。
- **参考 robocopy 语义**（官方文档已核实）：`/MOV` 移动文件、`/MOVE` 移动文件+目录且删除源；`/XJ` 排除 junction；`/LFSM` 低空间暂停；退出码 **≥8 视为失败**（0–7 为成功/有跳过）。我们不需要 robocopy，但可用它做**迁移后的抽样校验工具**或"高级用户复制命令"（GUI 提供"复制 robocopy 命令"按钮）。
- 特殊文件：稀疏文件（VHDX）用 `shutil.copy2` 会实体化、可能撑爆目标盘 → 对 `.vhdx/.vhd` 先检测稀疏属性并提示；OneDrive 云占位文件（`FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS 0x400000`）复制会触发下载 → 跳过并提示"先设为本地可用"；硬链接文件复制后硬链接关系断开（可接受，记录在迁移日志）。

### 6.3 junction 方案（不推荐，仅在文档中说明风险）

- `mklink /J C:\Source D:\Target` 把源目录变成 junction：
  - 我们的 v1 scanner / v2 index **明确跳过 reparse point** → 清理页会"看不见"该目录，索引也会漏算；
  - 软件更新/卸载可能因重定向失败；盘符变化或目标盘离线 → 程序直接坏；杀软/备份工具误判；
  - 不是"迁移"，只是"搬家 + 留链接"，用户很难理解。
- **结论：v2 只做"复制 → 校验 → 删源"的真迁移**，不自动创建 junction；如果用户想保留路径，文档给出手动 `mklink /J` 说明并警告。

### 6.4 非 C 盘也要能"发现与判断"

本机 `D:\` 根堆了大量散落文件（`node.exe`、各种 DLL、pptx/jpg/pdf、`install.exe`、`VC_RED.MSI`、Quark 安装包、`node-v24` 的 msi）——这是典型的"大文件/残留混在非系统盘"。v2 的 `build_index` 已支持任意 `root`，决策词典同样适用；建议 v2.1 增加"扫描其它盘"入口（空间页顶部盘符切换），并注意：**迁移的源与目标不能同盘**，从 D 盘迁移时目标是 E 盘或 C 盘可用目录。

---

## 7. Windows 系统空间去哪了（§4.1 系统占用卡片的数据来源）

本机对账（200GB 盘）：**已用 122.8GB ≈ 可访问 88.8 + hiberfil 6.76 + swapfile 0.25 + 不可见约 27GB**（`$Recycle.Bin` / `System Volume Information` / 无权限目录）。目录构成：Windows 35.4（其中 WinSxS 14.4）、Users 36.8、Program Files 12.5、ProgramData 4.2。

| 项目 | 路径/测量方法 | 非管理员可测？ | 清理/处理命令 | 风险 | 本机 |
|---|---|---|---|---|---|
| **WinSxS 组件库** | `C:\Windows\WinSxS`（目录大小）；官方分析 `Dism.exe /Online /Cleanup-Image /AnalyzeComponentStore`（需管理员） | ❌ 枚举常被拒；索引应记为 `protected_hint` | `Dism.exe /Online /Cleanup-Image /StartComponentCleanup`（立即删旧组件，无 30 天宽限）；`... /StartComponentCleanup /ResetBase`（深度，**之后已装更新不可卸载**）；或计划任务 `schtasks /Run /TN "\Microsoft\Windows\Servicing\StartComponentCleanup"` | 手删 WinSxS 可能导致**无法启动/无法更新**（MS 原文警告）；ResetBase 不可逆 | 14.4GB（其中可回收量需 Analyze 才知道，一般 1–4GB） |
| **hiberfil.sys 休眠** | `C:\hiberfil.sys`（文件大小） | ✅ 可 stat（读元数据） | `powercfg /hibernate off`（管理员）可释放；恢复 `powercfg /hibernate on` | **同时关闭休眠与快速启动**（开机变慢）；笔记本用户可能不想关 | 6.76GB |
| **pagefile.sys 页面文件** | `C:\pagefile.sys`；`Get-CimInstance Win32_PageFileUsage`（WMI 可能需权限） | ⚠️ 部分可读 | 系统属性 → 高级 → 性能 → 虚拟内存，调整大小或移到其它盘（重启生效） | 太小会 OOM/蓝屏；不建议直接删 | 本机未单列（随系统管理） |
| **swapfile.sys** | `C:\swapfile.sys` | ✅ 可 stat | 系统管理，**不要动**（Win11 UWP 交换） | 删除可能导致商店应用异常 | 0.25GB |
| **System Volume Information / 还原点** | 目录不可枚举；`vssadmin list shadowstorage`（管理员） | ❌ `available=False` | `vssadmin resize shadowstorage /for=C: /on=C: /maxsize=10%`；`vssadmin delete shadows /for=C: /oldest`；图形入口：系统保护 → 配置 | 删光还原点 = 失去回滚能力 | 与不可见 27GB 合计 |
| **$Recycle.Bin 回收站** | `SHQueryRecycleBinW`（非管理员可查**自己**的回收站） | ✅ | `SHEmptyRecycleBinW`（我们 v1 已有）；GUI 二次确认 | 清空不可恢复 | 含在不可见 27GB |
| **Windows.old** | `C:\Windows.old` 是否存在 | ✅ Test-Path | 设置 → 存储 → 临时文件 → "以前的 Windows 安装" 或 cleanmgr | 删除后无法回退旧系统 | 本机未列出（可能不存在） |
| **传递优化缓存** | `%WINDIR%\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache`；`Get-DeliveryOptimizationStatus` | ❌ 通常需管理员 | `Delete-DeliveryOptimizationCache -Force`（PowerShell，管理员） | P2P 缓存，删除后重建 | 未单列 |
| **Windows Installer 缓存** | `C:\Windows\Installer` | ❌ 无权限枚举 | **不要手删**；只报告占用，高级用户用 PatchCleaner 查孤立补丁 | 删了导致 MSI 软件无法修复/卸载 | 未单列 |
| **驱动存储 DriverStore** | `C:\Windows\System32\DriverStore\FileRepository`；`pnputil /enum-drivers`（管理员） | ❌ | `pnputil /delete-driver <oemXX.inf> /uninstall /force`（管理员，谨慎）；GUI 可用开源 [DriverStore Explorer](https://github.com/lostindark/DriverStoreExplorer)（**GPL-2.0**，11.9k stars） | 删错驱动可能让设备失效；需保留当前驱动 | 含在 Windows 35.4GB |
| **磁盘清理器项** | `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches` + `cleanmgr /sageset:1`/`/sagerun:1` | ⚠️ 读注册表可、执行需管理员 | 引导用户用 cleanmgr（最安全） | 低 | — |

**设计原则（与契约 §6 一致）**：所有这些只做"展示 + 复制命令 + 以管理员打开 PowerShell"，**工具自身绝不代执行**；非管理员时显式显示 `available=False` + "需要管理员"，不能显示 0 或干脆隐藏——用户需要知道"那 27GB 在哪"。

**索引实现注意（important）**：WinSxS/System32 大量使用**硬链接**，按文件路径累加会重复计数（WizTree 官网专门强调它"correctly handles hard linked files"）。建议 `SpaceIndex` 统计时用 `(st_dev, st_ino)`（Windows 上是 `VolumeSerialNumber + FileIndex`，Python 的 `st_ino` 可用）去重，否则"Windows 35.4GB"会被高估；`accessible_bytes` 与 `unaccounted_bytes` 要用去重后的值对账。

---

## 8. 给 core-engine / gui 的落地建议（contract notes）

1. **`match` 字段必须生效**：`installer` 是 `all`，其余 `any`（§5.2）；这是词典安全的生命线。
2. **`never_touch` 先于 categories**；命中即 `keep+danger`，UI 显示"请勿手动删除"。
3. **匹配顺序 = 数组顺序**（先命中先归类）：`installer → backup → app-version-dup → devcache → app-cache → video → archive → vm → driver-package → model-db → personal → program → system-media → large-misc`；`large-misc` 永远兜底。
4. **`reason` 模板**：`{size}` 用 `winapi.human_size`，`{age}` 用 `int(age_days)`；缺值时不显示占位符。
5. **`age_rules` 只补充理由/提级 review**，不产生 safe_delete。
6. **索引**：`os.scandir` + `ThreadPoolExecutor(8)` 即可；跳过 reparse point；无权限目录记 `error`/`protected_hint`；硬链接按 `(dev, ino)` 去重；`max_nodes` 截断；缓存写 `%LOCALAPPDATA%\QingFengCleaner\space_index.json`（本机实测扫描 49.6 万项 3.2s，60s 目标非常宽松）。
7. **决策收集**：`collect_review_items(min_size=100MB)` 从索引里取所有 ≥100MB 文件（本机 71 个，远小于 `limit=800`）；再加"目录级建议"（如 `AppData\Local\Packages` 整体不拆）时注意不要重复计大小。
8. **迁移目标探测**用"创建+删除临时文件"，**不要用 `os.access`**（§6.1）。
9. **GUI 迁移确认框**必须显示：目标根目录、目标盘可用空间、待迁移合计大小、"复制成功后才删除源文件"、可取消。
10. **treemap**：Squarified，≤200 块，点击下钻/双击打开，颜色按分类，tooltip 含路径/大小/时间（§2）。

---

## 9. 未在线核实 / 已知不确定性

- **SpaceSniffer** 官网本次抓取失败（`fetch failed`），其"无限下钻/过滤"细节来自既有认知，**未在线核实**；不影响我们的实现决策。
- **Czkawka 许可证**：GitHub API 返回 `NOASSERTION`，仓库 LICENSE 需人工确认；无论如何**不复用其代码**。
- **pagefile 大小/删除优化缓存命令**：`Delete-DeliveryOptimizationCache` 各版本可用性未逐条验证；建议 GUI 给命令时附"以管理员身份运行"并允许用户复制后自行执行。
- 应用路径类判断（WeChat/飞书/Codex 等）属于"程序目录→keep"，本机样本已验证；不同软件版本路径可能变化，但 keep/danger 兜底不会造成误删。

---

## 10. 复现命令（只读，本机实测）

```powershell
# 1) 校验 heuristics.json + 用词典给本机 ≥100MB 大文件分类（预期 71 个 / 18.22GB / 100% 命中）
& 'C:\Users\HUAWEI\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe' -c @"
import json,os,fnmatch,collections
d=json.load(open(r'E:\deepseekharness在这里的工作区\docs\research\space-heuristics.json',encoding='utf-8'))
mg=lambda gs,p: any(fnmatch.fnmatchcase(p.lower().replace('/','\\'),g.lower()) for g in gs)
def cls(p):
    if mg(d['never_touch'],p): return 'never_touch','keep'
    e=os.path.splitext(p)[1].lower()
    for c in d['categories']:
        ok=((e in [x.lower() for x in c['exts']]) and mg(c['path_globs'],p)) if c.get('match')=='all' \
           else ((e in [x.lower() for x in c['exts']]) or (bool(c['path_globs']) and mg(c['path_globs'],p)))
        if ok: return c['key'],c['advice']
    return 'large-misc','review'
roots=[r'C:\Users',r'C:\ProgramData',r'C:\Program Files',r'C:\Program Files (x86)',r'C:\Windows\Installer',r'C:\Windows\SoftwareDistribution',r'C:\Windows\Temp']
out=[];t=__import__('time').time()
for root in roots:
    if not os.path.isdir(root): continue
    st=[root]
    while st:
        dd=st.pop()
        try:
            with os.scandir(dd) as it:
                for x in it:
                    try:
                        s=x.stat(follow_symlinks=False)
                        if getattr(s,'st_file_attributes',0)&0x400: continue
                        if x.is_dir(follow_symlinks=False): st.append(x.path); continue
                        if s.st_size>=104857600: out.append((x.path,s.st_size))
                    except OSError: pass
        except OSError: pass
c=collections.Counter(); g=collections.Counter()
for p,s in out: k,_=cls(p); c[k]+=1; g[k]+=s
print('files',len(out),'GB',round(sum(s for _,s in out)/2**30,2),'sec',round(__import__('time').time()-t,1))
for k in sorted(c,key=lambda x:-g[x]): print(f'{k:16s}',c[k],round(g[k]/2**30,2),'GB')
"@

# 2) D/E 盘可写性探测（复现 Lead 的 ACL 结论；只写一个临时文件并删除）
foreach ($d in 'D:\','E:\') {
  $p = Join-Path $d ".qf_write_test_$PID"
  try { New-Item -ItemType File -Path $p -ErrorAction Stop | Out-Null; Remove-Item $p; "$d 可写" }
  catch { "$d 不可写: $($_.Exception.Message)" }
}
```
