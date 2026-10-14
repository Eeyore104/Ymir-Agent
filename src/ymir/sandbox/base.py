"""沙箱接口（架构设计 §3.5）：为不可信代码提供隔离执行环境。

接口稳定、实现可换：本机 Docker（MVP）→ K8s / 远程（v1.x 预留）。
会话级能力：``exec / read_file / write_file / list_files / set_network_policy``；
会话生命周期：``start / stop / destroy``（stop 保留现场供排查，destroy 释放资源）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class NetworkPolicy(StrEnum):
    """网络策略。

    - ``none``：完全断网（默认，不可信代码执行）；
    - ``restricted``：受限网络 —— v1.0 语义为「允许出网」，供评测装依赖场景，
      pypi/npm 白名单精细化留 v1.x（沙箱镜像已预装高频测试依赖，多数场景无需联网）；
    - ``open``：开放网络。
    """

    NONE = "none"
    RESTRICTED = "restricted"
    OPEN = "open"


@dataclass
class ExecResult:
    """一次命令执行的结果。"""

    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
        }


class SandboxSession(ABC):
    """一个沙箱会话（工作区 + 执行能力）。"""

    @property
    @abstractmethod
    def sandbox_id(self) -> str | None:
        """底层实例标识（Docker 为容器 ID；Local 为 None）。"""

    @property
    @abstractmethod
    def network_policy(self) -> NetworkPolicy:
        """当前网络策略。"""

    @abstractmethod
    async def exec(
        self, cmd: str, *, timeout: float = 60.0, workdir: str | None = None
    ) -> ExecResult:
        """执行 shell 命令（超时由实现保证，超限时 ``timed_out=True``）。"""

    @abstractmethod
    async def read_file(self, path: str) -> str:
        """读取工作区文件（相对路径按工作目录解析）。"""

    @abstractmethod
    async def write_file(self, path: str, content: str) -> None:
        """写入工作区文件（必要时创建父目录）。"""

    @abstractmethod
    async def list_files(self, path: str = ".") -> list[str]:
        """列出工作区文件（相对路径，按名称排序）。"""

    @abstractmethod
    async def set_network_policy(self, policy: NetworkPolicy) -> None:
        """切换网络策略（Docker：重建容器并迁移工作区；Local：仅记录）。"""


class Sandbox(ABC):
    """沙箱后端：生产会话。"""

    #: 后端标识（docker / local）
    backend: str = "base"

    @abstractmethod
    async def start(self, *, network: NetworkPolicy = NetworkPolicy.NONE) -> SandboxSession:
        """分配隔离环境，返回会话。"""

    @abstractmethod
    async def stop(self, session: SandboxSession) -> None:
        """停止会话（保留现场供排查）。"""

    @abstractmethod
    async def destroy(self, session: SandboxSession) -> None:
        """销毁会话（释放资源）。"""
