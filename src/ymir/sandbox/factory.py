"""沙箱工厂（架构设计 §3.5）：按配置选择后端，Docker 不可用时降级 Local。

- ``sandbox_backend = auto``（默认）：探测 Docker，可用则 DockerSandbox，
  否则降级 LocalSandbox（告警日志，隔离性弱化）；
- ``docker`` / ``local``：强制指定（调试 / 演示）。
"""

from __future__ import annotations

import logging

from ymir.config import Settings
from ymir.sandbox.base import Sandbox
from ymir.sandbox.docker_sandbox import DockerSandbox
from ymir.sandbox.local_sandbox import LocalSandbox

logger = logging.getLogger(__name__)


def build_sandbox(settings: Settings) -> Sandbox:
    """按配置构建沙箱后端。"""
    backend = (settings.sandbox_backend or "auto").strip().lower()

    if backend == "local":
        return LocalSandbox()

    docker_sandbox = DockerSandbox(
        image=settings.sandbox_image,
        cpus=settings.sandbox_cpus,
        memory=settings.sandbox_memory,
        pids_limit=settings.sandbox_pids_limit,
    )
    if backend == "docker":
        return docker_sandbox

    # auto：探测降级
    if docker_sandbox.probe():
        logger.info("沙箱后端：Docker（镜像 %s）", settings.sandbox_image)
        return docker_sandbox
    logger.warning("Docker 不可用，沙箱降级为 LocalSandbox（隔离性弱化，仅限本机单人场景）")
    return LocalSandbox()
