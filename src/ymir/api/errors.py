"""全局异常处理器：统一转换为业务响应信封（§6.3、§10.3-1）。"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ymir.common.errors import BizCode, YmirError

logger = logging.getLogger("ymir.api")


def _error_body(code: int, message: str) -> dict[str, object]:
    return {"code": code, "message": message}


def _format_validation_error(exc: RequestValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        location = ".".join(str(loc) for loc in err.get("loc", ()))
        parts.append(f"{location}: {err.get('msg', 'invalid')}")
    return "; ".join(parts) or "请求参数校验失败"


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器。"""

    @app.exception_handler(YmirError)
    async def _handle_ymir_error(_request: Request, exc: YmirError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=_error_body(int(exc.biz_code), exc.message),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_error_body(int(BizCode.INVALID_PARAM), _format_validation_error(exc)),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            code = int(BizCode.NOT_FOUND)
        elif exc.status_code in (405, 400, 422):
            code = int(BizCode.INVALID_PARAM)
        elif exc.status_code == 409:
            code = int(BizCode.STATE_CONFLICT)
        else:
            code = int(BizCode.INTERNAL_ERROR)
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(code, str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未预期的服务端异常: %s", exc)
        return JSONResponse(
            status_code=500,
            content=_error_body(int(BizCode.INTERNAL_ERROR), "服务器内部错误"),
        )
