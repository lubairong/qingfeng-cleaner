# 贡献指南 · Contributing

欢迎参与 **清风清理 QingFeng Cleaner**！这是一个纯 Python 3.12 标准库（+ tkinter + ctypes）实现的
Windows C 盘清理工具，**零第三方依赖**。在提交代码前，请先花 5 分钟读一遍本文。

---

## 1. 行为准则

- 友善、就事论事，不人身攻击。
- 涉及「删除」的功能改动，必须把**安全**放在第一位：宁可少删，不可误删。
- 不提交任何采集用户数据、上传文件列表、联网上报的代码。

## 2. 开发环境

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 11（Windows 10 21H2+ 亦可） |
| Python | 3.12（必须带 tkinter） |
| 第三方依赖 | **无**，不要引入 `requirements.txt` |
| Git | 提交 PR 时需要；本仓库开发者若本机无 git，可先打包 zip 交给维护者 |

```bat
:: 1. 获取代码（或用 zip 解压）
git clone <your-fork-url> qingfeng-cleaner
cd qingfeng-cleaner

:: 2. 直接运行（开发模式，自动使用项目内解释器）
packaging\run_dev.bat

:: 3. 只扫描模式（安全，不删除任何文件）
packaging\run_dev.bat --scan-only

:: 4. 单元测试
python -m unittest discover -s tests -v
```

> 单元测试**只用标准库 unittest**，不要引入 pytest（CI 不安装第三方包）。

## 3. 目录与写入边界

```
main.py                    程序入口（CLI 参数）
qingfeng/core/             引擎：models / rules / safety / winapi / scanner / cleaner
qingfeng/data/rules/       规则集 JSON，一个分类一个文件
qingfeng/ui/               界面：theme / main_window / widgets / dialogs / tray
assets/                    图标等静态资源
tests/                     单元测试 test_*.py
packaging/                 打包脚本（build.ps1 / launcher.py / run_dev.bat）
tools/                     验证与文档工具（verify.ps1 / capture_screenshots.py / check_links.py）
qa/                        维护者用：规则集与删除路径的独立安全审计（lead_audit.py / smoke_clean.py），CI 可选跑
docs/                      文档（ARCHITECTURE.md 为接口契约，改动需先与维护者确认）
```

> `qa/` 里的脚本是**独立于产品代码的安全审计**：`lead_audit.py` 逐条核对规则集与保护路径边界，
> `smoke_clean.py` 在受控临时目录上验证清理链路；它们会在仓库根目录临时创建 `.qa_*` 目录，
> 跑完自删（残留已被 `.gitignore` 覆盖）。改动 `safety.py` / 规则集后建议维护者跑一遍。
接口契约见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。**改动公开接口（函数签名、数据结构）前必须先提
Issue 讨论**，不要直接改。

### 代码风格

- 遵循 PEP 8，4 空格缩进，行宽约 100。
- 全部 `from __future__ import annotations` + 类型注解。
- 注释与用户可见文案用中文；标识符用英文。
- 不新增第三方依赖，不引入网络请求。
- tkinter 只能在主线程操作；后台线程用 `queue.Queue` + `root.after` 回传。

## 4. 安全红线（PR 会被直接拒绝的情况）

1. 绕过 `qingfeng/core/safety.py` 的 `assert_deletable()` 直接调用删除 API。
2. 把系统目录（`C:\Windows`、`C:\Program Files`、`C:\Users\<user>` 等）加入可删除路径。
3. 默认永久删除（未走回收站 `FOF_ALLOWUNDO`），或把「永久删除」默认打开。
4. 递归删除前不校验 `within_root()`（存在路径穿越 / 符号链接风险）。
5. 未经二次确认就执行清理。
6. 采集、上传任何用户数据，或加入遥测。

新增规则时：`risk` 保守标注（不确定就写 `caution`/`advanced`），`advanced` 必须 `default_enabled: false`；
涉及浏览器/隐私数据的规则**只清缓存**，不得包含密码、书签、Cookie 数据库。

## 5. 新增清理规则

在 `qingfeng/data/rules/<category>.json` 中追加对象，字段定义见 `docs/ARCHITECTURE.md` §4：

```json
{
  "id": "my-app-cache",
  "name": "某应用缓存",
  "category": "应用",
  "description": "某应用下载缓存目录",
  "action": "delete",
  "paths": ["%LOCALAPPDATA%\\MyApp\\cache"],
  "globs": ["*"],
  "risk": "caution",
  "requires_admin": false,
  "default_enabled": true,
  "min_age_days": 7,
  "source": "winapp2.ini"
}
```

要求：

- `id` 全局唯一、kebab-case。
- `paths` 里用环境变量，**不要写死盘符或用户名**。
- 能加 `min_age_days` 就加，避免删掉正在使用的文件。
- 规则里写 `source` 注明参考来源。
- 附一条单元测试，验证规则能被 `validate_rule()` 加载且路径在保护名单之外。

## 6. 提交与 PR

1. Fork → 新建分支：`feat/xxx`、`fix/xxx`、`docs/xxx`、`rules/xxx`。
2. 提交信息建议：`feat(scanner): 支持 min_age_days 过滤` / `fix(safety): 拦截 System32 子目录`。
3. 本地自检（必须全绿）：

   ```bat
   python -m unittest discover -s tests -v
   python main.py --scan-only
   packaging\build.ps1      :: 可选，验证打包
   ```

4. PR 描述写清：改了什么、为什么、如何验证、风险点。
5. CI（`.github/workflows/ci.yml`）会跑 unittest + `--scan-only` 冒烟，必须通过。

## 7. Issue 规范

- Bug 请用项目自带的 Issue 模板，填写 Windows 版本、复现步骤、截图。
- 新规则/新功能请先开 Issue 讨论，避免白做。

## 8. 许可证

提交代码即表示你同意以 [MIT License](LICENSE) 授权你的贡献。
