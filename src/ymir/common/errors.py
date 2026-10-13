"""业务错误码与异常基类（架构设计 §6.3、§10.3-1）。"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class BizCode(IntEnum):
    """业务错误码。

    ``0`` 表示成功；``1001``-``1006`` 为架构设计 §6.3 约定的业务码。
    """

    OK = 0
    INVALID_PARAM = 1001
    NOT_FOUND = 1002
    STATE_CONFLICT = 1003
    BUDGET_EXCEEDED = 1004
    SANDBOX_ERROR = 1005
    MODEL_CALL_FAILED = 1006
    # 扩展：未预期的服务端错误（对齐 RuoYi 约定，超出 §6.3 六码表）
    INTERNAL_ERROR = 500


class YmirError(Exception):
    """ymir 业务异常基类。

    子类通过类属性声明业务码与 HTTP 状态码；全局异常处理器据此统一转换响应。
    """

    biz_code: BizCode = BizCode.INVALID_PARAM
    http_status: int = 400

    def __init__(self, message: str = "", *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message or self.__class__.__name__
        self.details = details


class InvalidParamError(YmirError):
    """参数错误（1001）。"""

    biz_code = BizCode.INVALID_PARAM
    http_status = 400


class NotFoundError(YmirError):
    """资源不存在（1002）。"""

    biz_code = BizCode.NOT_FOUND
    http_status = 404


class StateConflictError(YmirError):
    """状态冲突，如重复审批（1003）。"""

    biz_code = BizCode.STATE_CONFLICT
    http_status = 409


class BudgetExceededError(YmirError):
    """预算超限（1004）。"""

    biz_code = BizCode.BUDGET_EXCEEDED
    http_status = 429


class SandboxError(YmirError):
    """沙箱错误（1005）。"""

    biz_code = BizCode.SANDBOX_ERROR
    http_status = 500


class ModelCallError(YmirError):
    """模型调用失败（1006）。"""

    biz_code = BizCode.MODEL_CALL_FAILED
    http_status = 502
