"""Docker 沙箱实现（架构设计 §3.5）。

- 默认镜像 ``ymir-sandbox:latest``（``docker/sandbox.Dockerfile`` 构建，**预置 pytest + ruff**，
  基准跑分预检的必需项）；基础镜像 ``python:3.12-slim`` **不含这些工具，禁止作为默认值回退**；
- 每 run 一个会话；限额 ``--cpus=2 --memory=4g --pids-limit=512``；
- 默认 ``network=none``（断网执行）；``restricted`` / ``open`` 走 bridge；
- docker-py 为同步客户端：全部调用经 ``asyncio.to_thread`` 转线程，不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import posixpath
import shlex
import tarfile
import time
from typing import Any

import docker
from docker.errors import DockerException
from docker.errors import NotFound as DockerNotFound

from ymir.common.errors import NotFoundError, SandboxError
from ymir.sandbox.base import ExecResult, NetworkPolicy, Sandbox, SandboxSession

logger = logging.getLogger(__name__)

#: 网络策略 -> Docker network_mode（restricted 的白名单精细化留 v1.x，见 base.NetworkPolicy）
_NETWORK_MODE: dict[NetworkPolicy, str] = {
    NetworkPolicy.NONE: "none",
    NetworkPolicy.RESTRICTED: "bridge",
    NetworkPolicy.OPEN: "bridge",
}


def _decode(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", errors="replace")


class DockerSandbox(Sandbox):
    """本机 Docker 沙箱（MVP 主实现）。"""

    backend = "docker"

    def __init__(
        self,
        *,
        image: str = "ymir-sandbox:latest",
        cpus: int = 2,
        memory: str = "4g",
        pids_limit: int = 512,
        workdir: str = "/workspace",
        client: Any = None,
        client_factory: Any = None,
    ) -> None:
        self._image = image
        self._cpus = cpus
        self._memory = memory
        self._pids_limit = pids_limit
        self._workdir = workdir
        self._client = client
        self._client_factory = client_factory

    # ------------------------------------------------------------------ 属性 / 探测

    @property
    def image(self) -> str:
        return self._image

    @property
    def workdir(self) -> str:
        return self._workdir

    def _get_client(self) -> Any:
        if self._client is None:
            factory = self._client_factory or docker.from_env
            self._client = factory()
        return self._client

    def probe(self) -> bool:
        """同步探测 Docker 可用性（工厂降级决策用，一次性）。"""
        try:
            return bool(self._get_client().ping())
        except Exception as exc:  # noqa: BLE001 - 探测任何异常都视为不可用
            logger.debug("Docker 探测失败: %s", exc)
            return False

    # ------------------------------------------------------------------ 生命周期

    async def start(self, *, network: NetworkPolicy = NetworkPolicy.NONE) -> DockerSession:
        try:
            container = await asyncio.to_thread(self._run_container, network)
        except SandboxError:
            raise
        except Exception as exc:  # noqa: BLE001 - docker 异常统一转业务错误
            raise SandboxError(f"Docker 沙箱启动失败: {exc}") from exc
        return DockerSession(self, container, network)

    async def stop(self, session: SandboxSession) -> None:
        docker_session = _as_docker_session(session)
        try:
            await asyncio.to_thread(docker_session.container.stop, timeout=5)
        except DockerNotFound:
            return  # 已不存在：幂等
        except Exception as exc:  # noqa: BLE001
            raise SandboxError(f"沙箱停止失败: {exc}") from exc

    async def destroy(self, session: SandboxSession) -> None:
        docker_session = _as_docker_session(session)
        try:
            await asyncio.to_thread(docker_session.container.remove, force=True)
        except DockerNotFound:
            return  # 已销毁：幂等
        except Exception as exc:  # noqa: BLE001
            raise SandboxError(f"沙箱销毁失败: {exc}") from exc

    # ------------------------------------------------------------------ 网络策略重建

    async def recreate_session(self, session: DockerSession, policy: NetworkPolicy) -> None:
        """以新网络策略重建容器并迁移工作区（Docker 不支持运行中改网络）。"""
        workspace_tar = await asyncio.to_thread(self._export_workspace, session.container)
        with contextlib.suppress(DockerNotFound):
            await asyncio.to_thread(session.container.remove, force=True)
        new_container = await asyncio.to_thread(self._run_container, policy)
        if workspace_tar:
            await asyncio.to_thread(self._import_workspace, new_container, workspace_tar)
        session._replace_container(new_container, policy)  # noqa: SLF001 - 会话内部状态

    # ------------------------------------------------------------------ 底层操作

    def _run_container(self, network: NetworkPolicy) -> Any:
        client = self._get_client()
        return client.containers.run(
            self._image,
            ["sleep", "infinity"],
            detach=True,
            working_dir=self._workdir,
            network_mode=_NETWORK_MODE[policy_key(network)],
            mem_limit=self._memory,
            # 硬内存上限：禁用额外 swap（默认 Docker 会给出 2×memory 的 mem+swap 总池，
            # 违反「4G 硬限额」语义；见 T03 验证实验）
            memswap_limit=self._memory,
            pids_limit=self._pids_limit,
            cpu_quota=int(self._cpus * 100_000),
            auto_remove=False,
            labels={"ymir.sandbox": "1"},
        )

    def _export_workspace(self, container: Any) -> bytes | None:
        try:
            stream, _stat = container.get_archive(self._workdir)
        except DockerNotFound:
            return None
        buffer = io.BytesIO()
        for chunk in stream:
            buffer.write(chunk)
        return buffer.getvalue() or None

    def _import_workspace(self, container: Any, tar_bytes: bytes) -> None:
        # get_archive(workdir) 的成员带 workdir 目录名前缀，解到其父目录恢复原路径
        parent = posixpath.dirname(self._workdir.rstrip("/")) or "/"
        container.put_archive(parent, tar_bytes)


def policy_key(policy: NetworkPolicy | str) -> NetworkPolicy:
    """把策略（枚举或字符串）归一化为枚举键。"""
    return policy if isinstance(policy, NetworkPolicy) else NetworkPolicy(policy)


def _as_docker_session(session: SandboxSession) -> DockerSession:
    if not isinstance(session, DockerSession):
        raise SandboxError("会话类型不匹配（期望 DockerSession）")
    return session


class DockerSession(SandboxSession):
    """Docker 会话：在容器内执行命令 / 读写工作区文件。"""

    def __init__(self, sandbox: DockerSandbox, container: Any, network: NetworkPolicy) -> None:
        self._sandbox = sandbox
        self._container = container
        self._network = policy_key(network)

    @property
    def container(self) -> Any:
        return self._container

    @property
    def sandbox_id(self) -> str | None:
        return getattr(self._container, "id", None)

    @property
    def network_policy(self) -> NetworkPolicy:
        return self._network

    def _replace_container(self, container: Any, network: NetworkPolicy) -> None:
        self._container = container
        self._network = policy_key(network)

    def _abs(self, path: str) -> str:
        if path.startswith("/"):
            return path
        return posixpath.join(self._sandbox.workdir, path)

    # ------------------------------------------------------------------ 执行

    async def exec(
        self, cmd: str, *, timeout: float = 60.0, workdir: str | None = None
    ) -> ExecResult:
        started = time.perf_counter()

        def _run() -> tuple[int, bytes | None, bytes | None]:
            wrapped = ["timeout", f"{max(1, int(timeout))}s", "sh", "-c", cmd]
            exit_code, output = self._container.exec_run(
                wrapped,
                workdir=workdir or self._sandbox.workdir,
                demux=True,
            )
            if isinstance(output, tuple):
                stdout, stderr = output
            else:  # 个别版本 demux 后仍返回单流
                stdout, stderr = output, None
            return exit_code or 0, stdout, stderr

        try:
            exit_code, stdout, stderr = await asyncio.to_thread(_run)
        except SandboxError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SandboxError(f"沙箱命令执行失败: {exc}") from exc

        duration_ms = int((time.perf_counter() - started) * 1000)
        return ExecResult(
            stdout=_decode(stdout),
            stderr=_decode(stderr),
            exit_code=exit_code,
            duration_ms=duration_ms,
            # coreutils timeout 触发时退出码 124；被 KILL 时 137
            timed_out=exit_code in (124, 137),
        )

    # ------------------------------------------------------------------ 文件

    async def read_file(self, path: str) -> str:
        try:
            data = await asyncio.to_thread(self._read_bytes, path)
        except DockerNotFound as exc:
            raise NotFoundError(f"沙箱文件不存在: {path}") from exc
        except DockerException as exc:
            raise SandboxError(f"沙箱读文件失败: {exc}") from exc
        return data.decode("utf-8", errors="replace")

    async def write_file(self, path: str, content: str) -> None:
        try:
            await asyncio.to_thread(self._write_bytes, path, content.encode("utf-8"))
        except DockerException as exc:
            raise SandboxError(f"沙箱写文件失败: {exc}") from exc

    async def list_files(self, path: str = ".") -> list[str]:
        result = await self.exec(f"find {shlex.quote(path)} -type f | sort", timeout=30)
        if not result.ok:
            return []
        base = path.rstrip("/") or "."
        files: list[str] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                files.append(posixpath.relpath(line, base))
        return files

    def _read_bytes(self, path: str) -> bytes:
        stream, _stat = self._container.get_archive(self._abs(path))
        buffer = io.BytesIO()
        for chunk in stream:
            buffer.write(chunk)
        buffer.seek(0)
        with tarfile.open(fileobj=buffer) as tar:
            member = tar.next()
            if member is None:
                return b""
            fileobj = tar.extractfile(member)
            return fileobj.read() if fileobj is not None else b""

    def _write_bytes(self, path: str, data: bytes) -> None:
        abs_path = self._abs(path)
        parent = posixpath.dirname(abs_path) or "/"
        name = posixpath.basename(abs_path) or "file"
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
        self._container.exec_run(["mkdir", "-p", parent])
        self._container.put_archive(parent, buffer.getvalue())

    # ------------------------------------------------------------------ 网络

    async def set_network_policy(self, policy: NetworkPolicy) -> None:
        target = policy_key(policy)
        if target == self._network:
            return
        await self._sandbox.recreate_session(self, target)
