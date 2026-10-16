"""doc_write 工具：结构化文档 -> 产物文件集。

产物同时含人读的 ``<name>.md``（由契约自带渲染）与机读的 ``<name>.json``
（结构化数据，供下游角色 / 评测消费）。
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from ymir.common.contracts import dump_structured
from ymir.common.errors import InvalidParamError


def build_document_files(document: BaseModel, *, name: str) -> dict[str, str]:
    """渲染文档产物文件集（``<name>.md`` + ``<name>.json``）。"""
    to_markdown = getattr(document, "to_markdown", None)
    if not callable(to_markdown):
        raise InvalidParamError(f"文档契约不支持 Markdown 渲染: {type(document).__name__}")
    payload = json.dumps(dump_structured(document), ensure_ascii=False, indent=2) + "\n"
    return {
        f"{name}.md": str(to_markdown()),
        f"{name}.json": payload,
    }
