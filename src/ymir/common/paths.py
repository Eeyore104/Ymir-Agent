"""路径安全（rank 5，**唯一实现**）。

产物内/补丁内/骨架内/推送 ``path_prefix`` 等**一切外部输入的相对路径**都必须过
:func:`normalize_rel_path`（docs/09 §13「路径安全」：禁止手写路径拼接校验）。

放在 ``common`` 而不是 ``artifacts`` 的原因（v1.2 r4 架构收口）：``templates``(rank 2)
与 ``artifacts``(rank 3) 都要用它，而 ``templates -> artifacts`` 是**逆层**（CI 拦）。
下沉到 rank 5 后两条边都变成 ``2 -> 5`` / ``3 -> 5``，合法且无循环。

★ 本模块是路径校验的**唯一实现**，禁止在其它包复制第二份正则/拆分逻辑。
"""

from __future__ import annotations

import re

from ymir.common.errors import InvalidParamError

#: Windows 盘符前缀（``C:``）；POSIX 下同样要拒绝，避免跨平台行为分叉。
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")

__all__ = ["normalize_rel_path", "normalize_scope_path"]

#: 目录前缀的尾部分隔符（``layout.allowed_prefixes`` / ``protected.paths`` 共用）。
PREFIX_SEP = "/"


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


def looks_like_file_path(path: str) -> bool:
    """末段带扩展名（含 ``.``）即视为**文件**；否则视为**目录**。

    这是 ``normalize_scope_path`` 的判定依据，也是它的已知边界：**无扩展名的文件**
    （``Makefile`` / ``Dockerfile``）会被判成目录 —— 这类文件请改用显式目录前缀
    表达作用域，或加扩展名。
    """
    cleaned = str(path).strip().replace("\\", "/").rstrip("/")
    if not cleaned:
        return False
    return "." in cleaned.split("/")[-1]


def normalize_scope_path(path: str) -> str:
    """归一化「作用域路径」：文件精确路径 **或** 目录前缀（v1.3，QA F4）。

    规则（与 ``layout.allowed_prefixes`` / ``protected.paths`` 同一约定）：

    - 显式以 ``/`` 结尾 ⇒ 目录前缀，归一化后**补回** ``/``；
    - 末段**不带扩展名**（如 ``tests`` / ``tests/fixtures``）⇒ 视为目录，**自动补** ``/``；
    - 末段带扩展名（如 ``tests/conftest.py``）⇒ 文件精确路径，原样返回。

    ★ 为什么要自动补（QA F4）：模板作者写 ``paths: [tests]`` 本意是「整个 tests 目录」，
    若按精确文件匹配处理，**目录保护会静默失效且无任何报错**。自动补 ``/`` 让意图与
    实现一致；判定依据见 :func:`looks_like_file_path`（无扩展名文件的已知边界）。

    Raises:
        InvalidParamError: 路径非法（空 / 绝对路径 / ``..`` 逃逸）。
    """
    raw = str(path).strip().replace("\\", "/")
    is_prefix = raw.endswith(PREFIX_SEP)
    normalized = normalize_rel_path(raw.rstrip(PREFIX_SEP))
    if is_prefix or not looks_like_file_path(normalized):
        return f"{normalized}{PREFIX_SEP}"
    return normalized
