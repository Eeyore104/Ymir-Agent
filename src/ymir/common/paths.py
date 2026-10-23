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

__all__ = ["normalize_rel_path"]


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
