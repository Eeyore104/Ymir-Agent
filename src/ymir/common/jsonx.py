"""LLM 输出 JSON 容错解析（中立层，供 ``roles`` 与 ``evaluation`` 复用）。

原实现位于 ``roles.runtime``，因 ``evaluation`` 反向依赖 ``roles`` 形成
``evaluation -> roles`` 依赖边（ARCH-13）；下沉到中立层后，``roles`` 与
``evaluation`` 均从本模块导入，依赖方向保持单向。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ymir.common.contracts import RoleOutputError

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """从 LLM 输出中提取 JSON 对象（容错：纯 JSON / 代码围栏 / 前后夹杂说明）。"""
    stripped = text.strip()
    candidates: list[str] = [stripped]
    fence = _FENCE_RE.search(stripped)
    if fence:
        candidates.append(fence.group(1).strip())
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise RoleOutputError(f"无法从模型输出提取 JSON 对象（前 200 字符）：{stripped[:200]}")
