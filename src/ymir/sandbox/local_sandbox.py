"""LocalSandbox：无 Docker 环境的降级实现（架构设计 §3.5 预留）。

⚠️ **隔离性弱于 Docker**：命令直接在本机子进程执行，网络策略无法强制
（仅记录策略状态并告警）。默认仅当 Docker 不可用时由工厂降级选用，
适用于本机单人开发 / CI 冒烟；隔离要求高的场景必须使用 DockerSandbox。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ymir.common.errors import InvalidParamError, NotFoundError
from ymir.sandbox.base import ExecResult, NetworkPolicy, Sandbox, SandboxSession

logger = logging.getLogger(__name__)


class LocalSandbox(Sandbox):
    """本机子进程沙箱（降级实现）。"""

    backend = "local"

    def __init__(self, *, base_dir: Path | str | None = None) -> None:
        # 基目录为可选：不提供时（生产默认）会话工作区直接落系统临时目录，
        # ``destroy`` 即彻底回收，从根上消除「每构造一次泄漏一个基目录」的
        # 问题（QA-03）；提供时视为调用方所有（把工作区聚合到指定目录，如
        # 测试的 tmp_path），本实例不负责清理。
        self._base = Path(base_dir) if base_dir is not None else None
        if self._base is not None:
            self._base.mkdir(parents=True, exist_ok=True)

    @property
    def image(self) -> str:
        return "local"

    async def start(self, *, network: NetworkPolicy = NetworkPolicy.NONE) -> LocalSession:
        policy = network if isinstance(network, NetworkPolicy) else NetworkPolicy(network)
        if policy != NetworkPolicy.NONE:
            logger.warning("LocalSandbox 无法强制网络策略（请求=%s），命令运行于本机网络", policy)
        # dir=None（默认基目录未提供）-> 系统临时目录：会话销毁即回收，无残留
        workdir = Path(tempfile.mkdtemp(prefix="ymir-ws-", dir=self._base))
        return LocalSession(self, workdir, policy)

    async def stop(self, session: SandboxSession) -> None:
        return None  # 子进程模式无「停止但保留现场」语义

    async def destroy(self, session: SandboxSession) -> None:
        local = _as_local_session(session)
        await asyncio.to_thread(shutil.rmtree, local.workdir, ignore_errors=True)


def _as_local_session(session: SandboxSession) -> LocalSession:
    if not isinstance(session, LocalSession):
        raise InvalidParamError("会话类型不匹配（期望 LocalSession）")
    return session


class LocalSession(SandboxSession):
    """本机子进程会话：工作区为临时目录，路径访问限制在目录内。"""

    def __init__(self, sandbox: LocalSandbox, workdir: Path, network: NetworkPolicy) -> None:
        self._sandbox = sandbox
        self._workdir = workdir
        self._network = network

    @property
    def workdir(self) -> Path:
        return self._workdir

    @property
    def sandbox_id(self) -> str | None:
        return None

    @property
    def network_policy(self) -> NetworkPolicy:
        return self._network

    def _resolve(self, path: str) -> Path:
        """把相对路径解析到工作区内；越界（``..`` 逃逸）直接拒绝。"""
        target = (self._workdir / path).resolve()
        base = self._workdir.resolve()
        if target != base and base not in target.parents:
            raise InvalidParamError(f"沙箱路径越界: {path}")
        return target

    async def exec(
        self, cmd: str, *, timeout: float = 60.0, workdir: str | None = None
    ) -> ExecResult:
        cwd = self._resolve(workdir) if workdir else self._workdir
        started = time.perf_counter()

        def _run() -> tuple[int, bytes, bytes, bool]:
            try:
                proc = subprocess.run(  # noqa: S602 - 沙箱语义即执行任意命令（本机降级模式）
                    cmd,
                    shell=True,
                    cwd=cwd,
                    capture_output=True,
                    timeout=timeout,
                )
                return proc.returncode, proc.stdout, proc.stderr, False
            except subprocess.TimeoutExpired as exc:
                # subprocess.run 超时已终止子进程
                return -1, exc.stdout or b"", exc.stderr or b"", True

        exit_code, stdout, stderr, timed_out = await asyncio.to_thread(_run)
        duration_ms = int((time.perf_counter() - started) * 1000)
        return ExecResult(
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

    async def read_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise NotFoundError(f"沙箱文件不存在: {path}")
        with target.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            return handle.read()

    async def write_file(self, path: str, content: str) -> None:
        target = self._resolve(path)

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8", newline="") as handle:
                handle.write(content)

        await asyncio.to_thread(_write)

    async def list_files(self, path: str = ".") -> list[str]:
        base = self._resolve(path)
        if not base.exists():
            return []
        files: list[str] = []
        for item in sorted(base.rglob("*")):
            if item.is_file():
                files.append(item.relative_to(self._workdir).as_posix())
        return files

    async def set_network_policy(self, policy: NetworkPolicy) -> None:
        target = policy if isinstance(policy, NetworkPolicy) else NetworkPolicy(policy)
        if target == self._network:
            return
        logger.warning(
            "LocalSandbox 无法强制网络策略切换（%s -> %s），仅更新记录", self._network, target
        )
        self._network = target
