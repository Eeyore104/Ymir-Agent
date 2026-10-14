"""产物版本 diff（架构设计 §3.6）：逐文件 unified diff，前端按文件分组渲染。

纯函数实现（无 IO / 无 DB），便于单测与复用；输出结构面向 JSON 序列化。
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: 单文件差异状态
STATUS_ADDED = "added"
STATUS_REMOVED = "removed"
STATUS_MODIFIED = "modified"


@dataclass
class FileDiff:
    """单文件差异（unified diff + 行数统计）。"""

    path: str
    status: str
    diff: str
    additions: int
    deletions: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "diff": self.diff,
            "additions": self.additions,
            "deletions": self.deletions,
        }


@dataclass
class DiffReport:
    """版本间差异汇总。"""

    files: list[FileDiff] = field(default_factory=list)
    files_changed: int = 0
    additions: int = 0
    deletions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [f.to_dict() for f in self.files],
            "stats": {
                "files_changed": self.files_changed,
                "additions": self.additions,
                "deletions": self.deletions,
            },
        }


def _unified_diff(
    base_text: str | None, target_text: str | None, path: str
) -> tuple[str, int, int]:
    """生成单文件 unified diff；返回 (diff 文本, additions, deletions)。

    ``base_text=None`` 表示新增文件（对比空内容），``target_text=None`` 表示删除文件。
    """
    base_lines = base_text.splitlines() if base_text is not None else []
    target_lines = target_text.splitlines() if target_text is not None else []
    diff_lines = list(
        difflib.unified_diff(
            base_lines,
            target_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    additions = 0
    deletions = 0
    # 跳过前两行 header（--- / +++），其余 +/- 行计入统计
    for line in diff_lines[2:]:
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return "\n".join(diff_lines), additions, deletions


def diff_file_sets(
    base: Mapping[str, str],
    target: Mapping[str, str],
) -> DiffReport:
    """对比两个文件集（键为归一化相对路径），返回按路径排序的差异列表。

    内容完全一致的文件不进入结果；新增 / 删除 / 修改均产出 unified diff。
    """
    report = DiffReport()
    for path in sorted(set(base) | set(target)):
        if path not in base:
            status = STATUS_ADDED
        elif path not in target:
            status = STATUS_REMOVED
        elif base[path] == target[path]:
            continue  # 内容一致：无差异
        else:
            status = STATUS_MODIFIED

        diff_text, additions, deletions = _unified_diff(
            base.get(path), target.get(path), path
        )
        report.files.append(
            FileDiff(
                path=path,
                status=status,
                diff=diff_text,
                additions=additions,
                deletions=deletions,
            )
        )
        report.files_changed += 1
        report.additions += additions
        report.deletions += deletions
    return report
