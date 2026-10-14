"""产物版本与内容完整性工具（架构设计 §3.6）。

- 路径安全：产物内路径一律 POSIX 风格相对路径，拒绝绝对路径 / 盘符 / ``..`` 逃逸；
- 内容哈希：sha256(路径 + ``\\0`` + 内容 + ``\\0``) 按路径排序累积 —— 同一文件集
  哈希稳定，可据此判断两次版本内容是否一致。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from ymir.common.errors import InvalidParamError

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def normalize_rel_path(path: str) -> str:
    """校验并归一化产物内相对路径。

    规则：统一为 POSIX 分隔符；拒绝空路径 / 绝对路径 / 盘符 / ``..`` 逃逸；
    丢弃 ``.`` 与多余空段（``a//b`` -> ``a/b``）。
    """
    raw = path.strip().replace("\\", "/")
    if not raw:
        raise InvalidParamError("产物路径不能为空")
    if raw.startswith("/") or _WINDOWS_DRIVE_RE.match(raw):
        raise InvalidParamError(f"产物路径不允许绝对路径/盘符: {path}")
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise InvalidParamError(f"产物路径不允许 '..' 逃逸: {path}")
        parts.append(part)
    if not parts:
        raise InvalidParamError(f"无效的产物路径: {path}")
    return "/".join(parts)


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
