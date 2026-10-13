"""TenantContext 中间件（§2.4 扩展点）。

MVP 为 no-op 直通：仅将请求落地到默认租户；v2.x 从 token 解析租户。
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class TenantContextMiddleware(BaseHTTPMiddleware):
    """为每个请求注入租户上下文（MVP 恒为默认租户）。"""

    def __init__(self, app, default_tenant_id: str = "default") -> None:  # noqa: ANN001
        super().__init__(app)
        self.default_tenant_id = default_tenant_id

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request.state.tenant_id = self.default_tenant_id
        return await call_next(request)
