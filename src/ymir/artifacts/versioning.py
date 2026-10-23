"""产物版本与内容完整性工具（架构设计 §3.6）。

- 路径安全：产物内路径一律 POSIX 风格相对路径（**实现在** :mod:`ymir.common.paths`，
  本模块只做向后兼容的 re-export —— 既有调用方 ``store.py`` / ``diff.py`` /
  测试与外部脚本都按 ``ymir.artifacts.versioning.normalize_rel_path`` 引用，
  v1.2 r4 下沉到 ``common``(rank 5) 后 import 路径不变、行为不变）；
- 内容哈希：sha256(路径 + ``\\0`` + 内容 + ``\\0``) 按路径排序累积 —— 同一文件集
  哈希稳定，可据此判断两次版本内容是否一致。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from ymir.common.errors import InvalidParamError
from ymir.common.paths import normalize_rel_path as normalize_rel_path

__all__ = [
    "compute_content_hash",
    "normalize_files",
    "normalize_rel_path",
]


def normalize_files(files: Mapping[str, str]) -> dict[str, str]:
    """归一化文件集路径并做冲突检测（防静默覆盖 / 防写盘期原生异常）。

    - 同路径不同拼写冲突（归一化后重复）报错；
    - 前缀冲突（``a`` 与 ``a/b`` 不能共存：文件与目录同名）报错——
      否则写盘时 ``mkdir`` 会撞在文件上抛原生 ``OSError``。
    """
    normalized: dict[str, str] = {}
    for path, content in files.items():
        clean = normalize_rel_path(path)
        if clean in normalized:
            raise InvalidParamError(f"产物文件路径冲突: {path}")
        normalized[clean] = content

    # 前缀冲突：任一文件的祖先路径不能同时是文件（文件与目录同名）
    for path in normalized:
        parts = path.split("/")
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            if ancestor in normalized:
                raise InvalidParamError(
                    f"产物路径前缀冲突（文件与目录同名）: {ancestor} 与 {path}"
                )
    return normalized


def compute_content_hash(files: Mapping[str, str]) -> str:
    """文件集内容哈希（路径排序后逐文件喂入，稳定且与顺序无关）。"""
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(files[path].encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()
