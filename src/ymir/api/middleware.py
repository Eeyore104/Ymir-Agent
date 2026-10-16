"""TenantContext 中间件（§2.4 扩展点）。

MVP 为 no-op 直通：仅将请求落地到默认租户；v2.x 从 token 解析租户。

实现为**纯 ASGI 中间件**（ARCH-10）：``BaseHTTPMiddleware`` 会为每个请求
包一层 anyio task 并改写 ``StreamingResponse`` 的取消/透传行为，对 SSE 长连接
有额外开销与边缘行为；纯 ASGI 直接透传 ``scope/receive/send``，规避该问题。
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send


class TenantContextMiddleware:
    """为每个 HTTP 请求注入租户上下文（MVP 恒为默认租户）。"""

    def __init__(self, app: ASGIApp, default_tenant_id: str = "default") -> None:
        self.app = app
        self.default_tenant_id = default_tenant_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            # Starlette 的 Request.state 读取 scope["state"]；写入即为注入。
            state = scope.setdefault("state", {})
            state["tenant_id"] = self.default_tenant_id
        await self.app(scope, receive, send)
