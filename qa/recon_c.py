"""C 盘真实占用侦察（Lead 用，只读）。

目标：回答"121 GB 到底在哪"，为 v2「空间分析」功能提供真实数据。
产出 qa/recon.json + 控制台报告。
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "qa" / "recon.json"

SKIP_DIRS = {
    "system volume information", "$recycle.bin", "recovery",
    "config.msi", "msocache",
}
SKIP_PREFIX = ("$",)


def is_reparse(entry: os.DirEntry) -> bool:
    try:
        st = entry.stat(follow_symlinks=False)
        return bool(getattr(st, "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def walk(path: str, depth: int = 0, max_depth: int = 40, errors: list | None = None):
    """返回 (总字节, 文件数, 目录累计 {相对路径: 字节} 只记前 3 层, 大文件列表)。"""
    total = 0
    files = 0
    dirs: dict[str, int] = {}
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except OSError as exc:
        if errors is not None and len(errors) < 40:
            errors.append(f"{path}: {exc.__class__.__name__}")
        return 0, 0, dirs

    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                if is_reparse(entry):
                    continue
                name_l = entry.name.lower()
                if name_l in SKIP_DIRS or name_l.startswith(SKIP_PREFIX):
                    continue
                sub = 0
                if depth < max_depth:
                    sub, fsub, _ = walk(entry.path, depth + 1, max_depth, errors)
                    files += fsub
                total += sub
                if depth < 3 and sub > 0:
                    dirs[entry.path] = sub
            else:
                st = entry.stat(follow_symlinks=False)
                total += st.st_size
                files += 1
                if st.st_size >= 200 * 1024 * 1024:
                    BIG.append({
                        "path": entry.path,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "atime": st.st_atime,
                    })
        except OSError:
            continue
    return total, files, dirs


BIG: list[dict] = []


def probe_system() -> dict:
    info: dict = {}
    for name in ("hiberfil.sys", "pagefile.sys", "swapfile.sys"):
        p = Path("C:/") / name
        try:
            if p.exists():
                info[name] = p.stat().st_size
        except OSError:
            info[name] = -1
    probes = {
        "windows_old": "C:/Windows.old",
        "winsxs": "C:/Windows/WinSxS",
        "installer_cache": "C:/Windows/Installer",
        "downloads": os.path.expandvars(r"%USERPROFILE%\Downloads"),
        "appdata_local": os.path.expandvars(r"%LOCALAPPDATA%"),
        "temp": os.path.expandvars(r"%TEMP%"),
        "wsl": os.path.expandvars(r"%LOCALAPPDATA%\Packages"),
        "docker": os.path.expandvars(r"%LOCALAPPDATA%\Docker"),
        "recycle_e": "E:/$Recycle.Bin",
        "recycle_c": "C:/$Recycle.Bin",
        "evtx": "C:/Windows/System32/winevt/Logs",
        "softwaredistribution": "C:/Windows/SoftwareDistribution",
        "driver_store": "C:/Windows/System32/DriverStore",
        "fonts": "C:/Windows/Fonts",
    }
    for key, p in probes.items():
        try:
            exists = os.path.exists(p)
            info[key] = {"exists": exists, "path": p}
        except OSError:
            info[key] = {"exists": False, "path": p}
    return info


def main() -> int:
    t0 = time.time()
    errors: list[str] = []
    top: dict[str, int] = {}
    grand = 0
    print("扫描 C:\\ 顶层目录（多线程）...", flush=True)
    root_dirs = []
    try:
        with os.scandir("C:/") as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    root_dirs.append(e.path)
    except OSError as exc:
        print(f"无法列出 C:\\ 根目录: {exc}")
        return 1

    def job(p: str):
        return p, walk(p, 0, 40, errors)

    with ThreadPoolExecutor(max_workers=8) as ex:
        for p, (size, files, dirs) in ex.map(job, root_dirs):
            top[p] = size
            grand += size
            print(f"  {p:<42} {size/2**30:8.2f} GB  {files:>7} files", flush=True)

    # 深层目录：取 2 层累计
    print("\n扫描二级目录（找大头）...", flush=True)
    level2: dict[str, int] = {}
    level3: dict[str, int] = {}

    def scan_sub(p: str):
        _, _, dirs = walk(p, 1, 3, errors)
        return dirs

    with ThreadPoolExecutor(max_workers=8) as ex:
        for dirs in ex.map(scan_sub, [p for p, s in top.items() if s > 2 * 2**30]):
            for k, v in dirs.items():
                if v <= 0:
                    continue
                parts = Path(k).parts
                if len(parts) >= 3:
                    level2["/".join(parts[:3])] = level2.get("/".join(parts[:3]), 0) + v
                if len(parts) >= 4:
                    level3["/".join(parts[:4])] = level3.get("/".join(parts[:4]), 0) + v

    level2_top = sorted(level2.items(), key=lambda kv: -kv[1])[:40]
    level3_top = sorted(level3.items(), key=lambda kv: -kv[1])[:40]

    BIG.sort(key=lambda x: -x["size"])
    data = {
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_sec": round(time.time() - t0, 1),
        "grand_total_bytes": grand,
        "top_level": sorted(top.items(), key=lambda kv: -kv[1]),
        "level2_top": level2_top,
        "level3_top": level3_top,
        "big_files_top": BIG[:300],
        "big_files_total_bytes": sum(b["size"] for b in BIG),
        "big_files_count": len(BIG),
        "system_probes": probe_system(),
        "errors": errors[:40],
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print("\n" + "=" * 78)
    print(f"总计: {grand/2**30:.2f} GB   大文件(>=200MB): {len(BIG)} 个 / {data['big_files_total_bytes']/2**30:.2f} GB")
    print(f"耗时: {data['duration_sec']}s   错误: {len(errors)}")
    print("\n--- 二级目录 Top 20 ---")
    for k, v in level2_top[:20]:
        print(f"  {v/2**30:8.2f} GB  {k}")
    print("\n--- 大文件 Top 25 ---")
    for b in BIG[:25]:
        print(f"  {b['size']/2**30:7.2f} GB  {time.strftime('%Y-%m-%d', time.localtime(b['mtime']))}  {b['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
