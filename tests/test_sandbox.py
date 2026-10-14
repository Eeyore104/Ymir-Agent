"""沙箱测试（T03）：Docker（mock 全接口）/ Local（真实轻量）/ 工厂 / 会话服务 / API。

单测严禁真实 Docker：FakeDockerClient 模拟 docker-py 关键协议
（containers.run / exec_run / put_archive / get_archive / stop / remove）。
"""

from __future__ import annotations

import io
import posixpath
import sys
import tarfile

import docker
import pytest

from ymir.common.errors import InvalidParamError, NotFoundError, SandboxError
from ymir.sandbox import (
    DockerSandbox,
    LocalSandbox,
    NetworkPolicy,
    SandboxService,
)
from ymir.sandbox.base import ExecResult, Sandbox, SandboxSession
from ymir.sandbox.factory import build_sandbox
from ymir.storage.repositories import ProjectRepository, RunRepository

# --------------------------------------------------------------------- Fake docker


class FakeContainer:
    """模拟 docker-py 容器：内存文件系统（fs: 绝对路径 -> bytes）。"""

    def __init__(self, client: FakeDockerClient, name: str) -> None:
        self._client = client
        self.id = name
        self.fs: dict[str, bytes] = {}
        self.stopped = False
        self.removed = False
        self.exec_log: list[dict] = []

    def exec_run(self, cmd, workdir=None, demux=True):  # noqa: ANN001
        self.exec_log.append({"cmd": list(cmd), "workdir": workdir, "demux": demux})
        if self._client.exec_raises is not None:
            raise self._client.exec_raises
        if self._client.exec_queue:
            return self._client.exec_queue.pop(0)
        return (0, (b"", b""))

    def put_archive(self, path: str, data: bytes) -> None:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for member in tar.getmembers():
                fileobj = tar.extractfile(member)
                if fileobj is not None:
                    self.fs[posixpath.join(path, member.name)] = fileobj.read()

    def get_archive(self, path: str):
        prefix = path.rstrip("/") + "/"
        if path in self.fs:
            entries = {posixpath.basename(path): self.fs[path]}
        else:
            entries = {p[len(prefix) :]: c for p, c in self.fs.items() if p.startswith(prefix)}
            if not entries:
                raise docker.errors.NotFound(f"no such path: {path}")
        root = posixpath.basename(path)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for rel, data in entries.items():
                info = tarfile.TarInfo(name=f"{root}/{rel}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return iter([buffer.getvalue()]), {}

    def stop(self, timeout=None) -> None:  # noqa: ANN001
        if self.removed:
            raise docker.errors.NotFound("removed")
        self.stopped = True

    def remove(self, force=False) -> None:  # noqa: ANN001
        if self.removed:
            raise docker.errors.NotFound("already removed")
        self.removed = True


class FakeContainers:
    def __init__(self, client: FakeDockerClient) -> None:
        self._client = client
        self._counter = 0

    def run(self, image, command, **kwargs):  # noqa: ANN001
        self._counter += 1
        self._client.run_log.append({"image": image, "command": command, **kwargs})
        return FakeContainer(self._client, f"ctr-{self._counter}")


class FakeDockerClient:
    def __init__(self, *, ping_ok: bool = True) -> None:
        self.ping_ok = ping_ok
        self.exec_queue: list = []
        self.exec_raises: Exception | None = None
        self.run_log: list[dict] = []
        self.containers = FakeContainers(self)

    def ping(self) -> bool:
        if not self.ping_ok:
            raise docker.errors.DockerException("daemon down")
        return True


# --------------------------------------------------------------------- DockerSandbox


class TestDockerSandbox:
    async def test_start_passes_limits_and_network(self) -> None:
        client = FakeDockerClient()
        sbx = DockerSandbox(client=client, image="ymir-sandbox:latest")
        session = await sbx.start()
        assert session.sandbox_id == "ctr-1"
        kwargs = client.run_log[0]
        assert kwargs["image"] == "ymir-sandbox:latest"
        assert kwargs["network_mode"] == "none"
        assert kwargs["mem_limit"] == "4g"
        assert kwargs["memswap_limit"] == "4g"  # 硬上限：无额外 swap 池（T03 验证实验）
        assert kwargs["pids_limit"] == 512
        assert kwargs["cpu_quota"] == 200_000
        assert kwargs["working_dir"] == "/workspace"
        assert kwargs["command"] == ["sleep", "infinity"]

    async def test_open_network_uses_bridge(self) -> None:
        client = FakeDockerClient()
        sbx = DockerSandbox(client=client)
        await sbx.start(network=NetworkPolicy.OPEN)
        assert client.run_log[0]["network_mode"] == "bridge"

    async def test_exec_wraps_timeout_and_demux(self) -> None:
        client = FakeDockerClient()
        client.exec_queue.append((0, (b"hello\n", b"warn")))
        sbx = DockerSandbox(client=client)
        session = await sbx.start()
        result = await session.exec("python main.py", timeout=30)
        assert result.stdout == "hello\n"
        assert result.stderr == "warn"
        assert result.exit_code == 0
        assert result.ok
        cmd = session.container.exec_log[0]["cmd"]
        assert cmd[:2] == ["timeout", "30s"]
        assert cmd[2:] == ["sh", "-c", "python main.py"]

    async def test_exec_timeout_flagged(self) -> None:
        client = FakeDockerClient()
        client.exec_queue.append((124, (b"", b"")))
        sbx = DockerSandbox(client=client)
        session = await sbx.start()
        result = await session.exec("sleep 99", timeout=1)
        assert result.timed_out
        assert not result.ok

    async def test_exec_error_wrapped_as_sandbox_error(self) -> None:
        client = FakeDockerClient()
        client.exec_raises = docker.errors.APIError("boom")
        sbx = DockerSandbox(client=client)
        session = await sbx.start()
        with pytest.raises(SandboxError):
            await session.exec("x")

    async def test_write_read_file_roundtrip(self) -> None:
        client = FakeDockerClient()
        sbx = DockerSandbox(client=client)
        session = await sbx.start()
        await session.write_file("pkg/main.py", "print(1)\n")
        assert await session.read_file("pkg/main.py") == "print(1)\n"
        await session.write_file("/workspace/abs.txt", "A")
        assert await session.read_file("/workspace/abs.txt") == "A"

    async def test_read_missing_file(self) -> None:
        client = FakeDockerClient()
        sbx = DockerSandbox(client=client)
        session = await sbx.start()
        with pytest.raises(NotFoundError):
            await session.read_file("nope.py")

    async def test_list_files(self) -> None:
        client = FakeDockerClient()
        client.exec_queue.append((0, (b"./a.py\n./sub/b.md\n", b"")))
        sbx = DockerSandbox(client=client)
        session = await sbx.start()
        assert await session.list_files(".") == ["a.py", "sub/b.md"]

    async def test_set_network_policy_recreates_and_migrates(self) -> None:
        client = FakeDockerClient()
        sbx = DockerSandbox(client=client)
        session = await sbx.start()
        await session.write_file("main.py", "print(1)")
        old = session.container

        await session.set_network_policy(NetworkPolicy.OPEN)
        assert old.removed
        assert session.network_policy == NetworkPolicy.OPEN
        assert client.run_log[-1]["network_mode"] == "bridge"
        # 工作区随重建迁移
        assert await session.read_file("main.py") == "print(1)"

        # 同策略切换 no-op
        run_count = len(client.run_log)
        await session.set_network_policy(NetworkPolicy.OPEN)
        assert len(client.run_log) == run_count

    async def test_stop_and_destroy_idempotent(self) -> None:
        client = FakeDockerClient()
        sbx = DockerSandbox(client=client)
        session = await sbx.start()
        await sbx.stop(session)
        assert session.container.stopped
        await sbx.destroy(session)
        assert session.container.removed
        await sbx.destroy(session)  # 已销毁：幂等不抛

    def test_probe(self) -> None:
        assert DockerSandbox(client=FakeDockerClient()).probe() is True
        assert DockerSandbox(client=FakeDockerClient(ping_ok=False)).probe() is False


# --------------------------------------------------------------------- LocalSandbox


class TestLocalSandbox:
    async def test_exec_and_files(self, tmp_path) -> None:
        sbx = LocalSandbox(base_dir=tmp_path / "sbx")
        session = await sbx.start()
        result = await session.exec("echo hello")
        assert result.ok
        assert "hello" in result.stdout
        await session.write_file("pkg/a.py", "print(1)")
        assert await session.read_file("pkg/a.py") == "print(1)"
        assert await session.list_files() == ["pkg/a.py"]
        await sbx.destroy(session)
        assert not session.workdir.exists()

    async def test_exec_timeout(self, tmp_path) -> None:
        sbx = LocalSandbox(base_dir=tmp_path / "sbx")
        session = await sbx.start()
        cmd = f'"{sys.executable}" -c "import time; time.sleep(5)"'
        result = await session.exec(cmd, timeout=1.0)
        assert result.timed_out

    async def test_path_escape_rejected(self, tmp_path) -> None:
        sbx = LocalSandbox(base_dir=tmp_path / "sbx")
        session = await sbx.start()
        with pytest.raises(InvalidParamError):
            await session.write_file("../evil.txt", "x")
        with pytest.raises(InvalidParamError):
            await session.read_file("../../etc/passwd")

    async def test_read_missing(self, tmp_path) -> None:
        sbx = LocalSandbox(base_dir=tmp_path / "sbx")
        session = await sbx.start()
        with pytest.raises(NotFoundError):
            await session.read_file("none.py")

    async def test_set_network_policy_recorded(self, tmp_path) -> None:
        sbx = LocalSandbox(base_dir=tmp_path / "sbx")
        session = await sbx.start()
        assert session.network_policy == NetworkPolicy.NONE
        await session.set_network_policy(NetworkPolicy.OPEN)
        assert session.network_policy == NetworkPolicy.OPEN


# --------------------------------------------------------------------- 工厂


class TestFactory:
    def test_forced_backends(self, temp_settings) -> None:
        local_settings = temp_settings.model_copy(update={"sandbox_backend": "local"})
        assert build_sandbox(local_settings).backend == "local"
        docker_settings = temp_settings.model_copy(update={"sandbox_backend": "docker"})
        assert build_sandbox(docker_settings).backend == "docker"

    def test_auto_docker_available(self, temp_settings, monkeypatch) -> None:
        monkeypatch.setattr(docker, "from_env", lambda: FakeDockerClient())
        settings = temp_settings.model_copy(update={"sandbox_backend": "auto"})
        assert build_sandbox(settings).backend == "docker"

    def test_auto_fallback_to_local(self, temp_settings, monkeypatch) -> None:
        def _boom():
            raise docker.errors.DockerException("no docker here")

        monkeypatch.setattr(docker, "from_env", _boom)
        settings = temp_settings.model_copy(update={"sandbox_backend": "auto"})
        assert build_sandbox(settings).backend == "local"


# --------------------------------------------------------------------- 会话服务


class FakeRuntimeSession(SandboxSession):
    def __init__(self, sandbox_id: str | None = "fake-1") -> None:
        self._sandbox_id = sandbox_id

    @property
    def sandbox_id(self) -> str | None:
        return self._sandbox_id

    @property
    def network_policy(self) -> NetworkPolicy:
        return NetworkPolicy.NONE

    async def exec(self, cmd: str, *, timeout: float = 60.0, workdir=None) -> ExecResult:
        return ExecResult(stdout="", stderr="", exit_code=0, duration_ms=0)

    async def read_file(self, path: str) -> str:
        return ""

    async def write_file(self, path: str, content: str) -> None:
        return None

    async def list_files(self, path: str = ".") -> list[str]:
        return []

    async def set_network_policy(self, policy: NetworkPolicy) -> None:
        return None


class FakeSandboxBackend(Sandbox):
    backend = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.destroyed: list = []

    @property
    def image(self) -> str:
        return "fake-image"

    async def start(self, *, network: NetworkPolicy = NetworkPolicy.NONE) -> SandboxSession:
        if self.fail:
            raise SandboxError("fake start failure")
        return FakeRuntimeSession()

    async def stop(self, session: SandboxSession) -> None:
        return None

    async def destroy(self, session: SandboxSession) -> None:
        self.destroyed.append(session)


async def _make_project_and_run(session_factory) -> tuple[str, str]:
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="沙箱服务测试")
        run = await RunRepository(session).create(project_id=project.id, requirement="x")
        await session.commit()
        return project.id, run.id


class TestSandboxService:
    async def test_start_and_destroy_lifecycle_records(self, session_factory) -> None:
        _, run_id = await _make_project_and_run(session_factory)
        service = SandboxService(session_factory=session_factory, sandbox=FakeSandboxBackend())

        managed = await service.start(run_id)
        records = await service.list_sessions(run_id=run_id)
        assert len(records) == 1
        assert records[0].status == "running"
        assert records[0].container_id == "fake-1"
        assert records[0].image == "fake-image"

        await service.destroy(managed)
        record = await service.get(managed.record_id)
        assert record.status == "destroyed"
        assert record.destroyed_at is not None

    async def test_start_failure_marks_record_stopped(self, session_factory) -> None:
        _, run_id = await _make_project_and_run(session_factory)
        service = SandboxService(
            session_factory=session_factory, sandbox=FakeSandboxBackend(fail=True)
        )
        with pytest.raises(SandboxError):
            await service.start(run_id)
        records = await service.list_sessions(run_id=run_id)
        assert [r.status for r in records] == ["stopped"]

    async def test_get_missing(self, session_factory) -> None:
        service = SandboxService(session_factory=session_factory, sandbox=FakeSandboxBackend())
        with pytest.raises(NotFoundError):
            await service.get("no-such-session")

    async def test_lazy_loader_used(self, session_factory) -> None:
        backend = FakeSandboxBackend()
        service = SandboxService(session_factory=session_factory, sandbox_loader=lambda: backend)
        assert service.sandbox is backend
        assert service.sandbox is backend  # 缓存


# --------------------------------------------------------------------- API


async def test_sandboxes_api_flow(client) -> None:
    project_resp = await client.post("/api/v1/projects", json={"name": "沙箱 API"})
    project_id = project_resp.json()["data"]["id"]
    run_resp = await client.post(
        f"/api/v1/projects/{project_id}/runs", json={"requirement": "做一个示例"}
    )
    run_id = run_resp.json()["data"]["run_id"]

    service = client._transport.app.state.sandbox_service
    managed = await service.start(run_id)
    await service.destroy(managed)

    resp = await client.get("/api/v1/sandboxes", params={"run_id": run_id})
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["status"] == "destroyed"

    resp = await client.get(f"/api/v1/sandboxes/{managed.record_id}")
    assert resp.json()["data"]["id"] == managed.record_id

    resp = await client.get("/api/v1/sandboxes/no-such")
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002
