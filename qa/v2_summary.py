"""v2 最终交付数据汇总（Lead 用，只读）。"""
from __future__ import annotations

from qingfeng.core import decide, space

GB = 2 ** 30
idx = space.load_cached_index()
items = decide.collect_review_items(idx)
probe = space.probe_system_space(index=idx)
ok = [i for i in items if decide.action_policy(i).can_delete]
mv = [i for i in items if decide.action_policy(i).can_move]
print("索引: 已用 %.1f GB / 可访问 %.1f GB / 受保护 %.1f GB" % (
    idx.total_bytes / GB, idx.accessible_bytes / GB, idx.unaccounted_bytes / GB))
print("大文件: %d 项 / %.1f GB；可清理 %d 项 / %.1f GB；可迁移 %d 项 / %.1f GB" % (
    len(items), sum(i.size for i in items) / GB,
    len(ok), sum(i.size for i in ok) / GB,
    len(mv), sum(i.size for i in mv) / GB))
for g in decide.group_review_items(items):
    print("  %-20s %3d 项 %6.2f GB  %s" % (g.label, len(g.items), g.total_bytes / GB, g.advice))
print("休眠 %.1f GB；WinSxS %.1f GB（资源管理器口径 %.1f GB）；Installer %.2f GB" % (
    probe["hiberfil"]["bytes"] / GB, probe["winsxs"]["bytes"] / GB,
    probe["winsxs"].get("bytes_raw", 0) / GB, probe["installer_cache"]["bytes"] / GB))
for t in decide.suggest_targets():
    print("迁移目标: %s %s %.1f GB 可用 writable=%s %s" % (
        t.drive, t.path, t.free_bytes / GB, t.writable, t.note))
