"""文档链接检查（标准库实现，供 CI / 本地自检使用）。

扫描仓库内所有 Markdown 文件中的**相对链接与图片引用**，检查目标文件是否存在：

    python tools/check_links.py            # 检查，失败时退出码 1
    python tools/check_links.py --verbose  # 打印所有检查过的链接

规则：
- 跳过 http(s)://、mailto:、纯锚点（#xxx）、以及 <包围的协议链接；
- 跳过 fenced code block（``` 包围）里的内容；
- 去掉锚点后缀（README.md#usage → README.md）后再判断存在性；
- 图片引用 ![alt](path) 也检查；
- 目标为目录时视为有效（GitHub 会显示目录页）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# [text](target) 与 ![alt](target)；忽略 HTML <img src>（本仓库仅用于 assets 图标）
LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
SKIP_PREFIXES = ("http://", "https://", "mailto:", "tel:", "#", "data:")


def iter_markdown_files(root: Path) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        # 忽略虚拟环境 / 构建产物 / 临时目录
        if rel.startswith((".venv/", "build/", "dist/", "tools/_verify_out/", ".tmp/", "qa/")):
            continue
        files.append(path)
    return files


def strip_code_fences(text: str) -> list[str]:
    """把 fenced code block 内的行替换为空行，保留行号不变。"""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return out


def check_file(md: Path, root: Path) -> list[tuple[str, str, bool]]:
    """返回 [(链接, 解析后的相对目标, 是否存在)]。"""
    try:
        text = md.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = md.read_text(encoding="utf-8", errors="replace")
    results: list[tuple[str, str, bool]] = []
    for line in strip_code_fences(text):
        for raw_target in LINK_RE.findall(line):
            target = raw_target.strip()
            if not target or target.startswith(SKIP_PREFIXES):
                continue
            if "://" in target:
                continue
            bare = target.split("#", 1)[0].strip()
            if not bare:
                continue
            if bare.startswith("/"):
                resolved = root / bare.lstrip("/")
            else:
                resolved = (md.parent / bare).resolve()
            results.append((target, bare, resolved.exists()))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 Markdown 相对链接")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    root: Path = args.root.resolve()
    files = iter_markdown_files(root)
    if not files:
        print("[check_links] 未找到 Markdown 文件", file=sys.stderr)
        return 1

    broken: list[tuple[str, str, str]] = []
    total = 0
    for md in files:
        rel_md = md.relative_to(root).as_posix()
        for target, bare, exists in check_file(md, root):
            total += 1
            if args.verbose:
                print(f"  {'OK  ' if exists else 'MISS'} {rel_md}: {target}")
            if not exists:
                broken.append((rel_md, target, bare))

    print(f"[check_links] 检查 {len(files)} 个 Markdown 文件，共 {total} 条相对链接/图片引用")
    if broken:
        print(f"[check_links] 发现 {len(broken)} 条死链：", file=sys.stderr)
        for rel_md, target, bare in broken:
            print(f"  {rel_md} -> {target}", file=sys.stderr)
        return 1
    print("[check_links] PASS：没有死链")
    return 0


if __name__ == "__main__":
    sys.exit(main())
