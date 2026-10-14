"""code_write 工具：CodeBundle -> 代码产物文件集。

展开每个代码文件（路径经安全归一化），并落 ``<name>.json`` 元数据
（文件清单 / 自测命令 / 实现说明）——下游与 Web 端消费用。
"""

from __future__ import annotations

import json

from ymir.artifacts.versioning import normalize_rel_path
from ymir.common.errors import InvalidParamError
from ymir.roles.schemas import CodeBundle


def build_code_files(bundle: CodeBundle, *, name: str = "code") -> dict[str, str]:
    """展开代码文件集（含统一换行的文件内容与元数据 JSON）。"""
    files: dict[str, str] = {}
    for item in bundle.files:
        path = normalize_rel_path(item.path)
        if path in files:
            raise InvalidParamError(f"代码文件路径冲突: {item.path}")
        content = item.content if item.content.endswith("\n") else item.content + "\n"
        files[path] = content
    meta = {
        "files": [item.path for item in bundle.files],
        "run_commands": list(bundle.run_commands),
        "notes": bundle.notes,
    }
    files[f"{name}.json"] = json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
    return files
