"""中立共享契约（跨 ``tools`` / ``roles`` / ``evaluation`` 复用）。

本模块是「包级循环依赖」的收敛点（ARCH-01 / ARCH-13）：把原本定义在
``roles`` 子包、却被 ``tools`` 反向依赖的共享契约下沉到中立层，使依赖方向
保持单向（``roles -> tools``、``tools -> common``、``common`` 不依赖任何业务包）。

下沉内容：
- :class:`UpstreamArtifact`：上游产物（名称 + 版本 + 文件集），由
  ``tools/read_artifact`` 生产、``roles`` 消费；
- :class:`CodeFile` / :class:`CodeBundle`：代码产物契约，``tools/code_writer``
  与 ``roles`` 输出校验共用；
- :func:`dump_structured`：结构化序列化（落 ``<out>.json``），``tools/doc_writer``
  与 ``roles`` 共用；
- :class:`RoleOutputError`：角色输出解析 / 校验失败错误（``roles.runtime`` 与
  :mod:`ymir.common.jsonx` 共用）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from ymir.common.errors import BizCode, YmirError


class RoleOutputError(YmirError):
    """角色输出不满足契约（JSON 解析失败 / schema 校验失败，重试后仍不过）。"""

    biz_code = BizCode.MODEL_CALL_FAILED
    http_status = 502


@dataclass
class UpstreamArtifact:
    """上游产物（按名字引用的最新版本）。"""

    name: str
    version: int
    files: dict[str, str]


class CodeFile(BaseModel):
    """一个代码文件。"""

    path: str = Field(description="相对路径，如 app/main.py")
    content: str


class CodeBundle(BaseModel):
    """代码产物（engineer 输出契约）。"""

    files: list[CodeFile] = Field(min_length=1)
    run_commands: list[str] = Field(
        default_factory=list, description="冒烟/自测命令（在沙箱内执行）"
    )
    notes: str = Field(default="", description="实现说明与未决事项")


def dump_structured(model: BaseModel) -> dict[str, Any]:
    """结构化数据序列化（落 ``<out>.json``）。"""
    return model.model_dump(mode="json")
