"""沙箱模块（架构设计 §3.5）：接口 + Docker / Local 实现 + 工厂 + 会话服务。"""

from ymir.sandbox.base import ExecResult, NetworkPolicy, Sandbox, SandboxSession
from ymir.sandbox.docker_sandbox import DockerSandbox
from ymir.sandbox.factory import build_sandbox
from ymir.sandbox.local_sandbox import LocalSandbox
from ymir.sandbox.session import ManagedSession, SandboxService

__all__ = [
    "DockerSandbox",
    "ExecResult",
    "LocalSandbox",
    "ManagedSession",
    "NetworkPolicy",
    "Sandbox",
    "SandboxService",
    "SandboxSession",
    "build_sandbox",
]
