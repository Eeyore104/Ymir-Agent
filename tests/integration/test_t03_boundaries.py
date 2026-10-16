"""T03 独立 QA 边界测试（角色运行时 + 沙箱 + 产物版本 + artifacts API）。

由独立 QA 全新编写，不复用 ``tests/test_roles.py`` / ``tests/test_sandbox.py`` /
``tests/test_artifacts.py`` / ``tests/integration/test_t03_pipeline.py`` 内部的
辅助类与 fixture（假网关 / 假 docker 客户端均在本文件独立构造），仅复用
``tests/conftest.py`` 暴露的通用 fixture（client / session_factory / tmp_path /
temp_settings）。

纪律：0 真实 LLM 调用（全部假网关）；沙箱用 LocalSandbox + 自建假 docker 客户端
（不依赖本机 Docker 是否启动）。

覆盖点：
- 验收①：PM 单跑（截断）-> prd v1 落库（prd.md + prd.json）+ 消息（to_role=architect）
  + PRDDocument 校验（坏 JSON 触发带纠错的重试；重试耗尽 -> RoleOutputError -> run failed）
- 验收②：全链跑到 coding -> code 产物含 smoke_report.json 且版本随重跑递增
- 验收③（替代）：假 docker 限额参数透传 / exec 超时 / destroy 幂等 / 网络策略
  同策略 no-op 与异策略重建迁移文件；LocalSandbox 真实 exec 冒烟与超时；工厂降级
- 验收④：diff API 新增/修改/删除 + stats；同版本空 diff；不存在 1002；version<=0
- 反例：版本不可变冲突无残留；路径穿越多种拼写；预算超限 -> paused + 事件有序
"""

from __future__ import annotations

import asyncio
import io
import json
import posixpath
import sys
import tarfile
import time
from types import SimpleNamespace
from typing import Any

import docker
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.store import ArtifactStore
from ymir.common.errors import BizCode, InvalidParamError, StateConflictError
from ymir.config import PROJECT_ROOT
from ymir.events.bus import EventPublisher
from ymir.gateway.budget import BudgetGuard
from ymir.gateway.gateway import ChatResult, ChatUsage, ModelGateway
from ymir.gateway.providers import (
    BudgetSpec,
    ModelsConfig,
    ModelSpec,
    ProviderRegistry,
    ProviderSpec,
)
from ymir.gateway.routing import ModelRouter
from ymir.gateway.usage import UsageRecorder
from ymir.messages.bus import MessagePublisher
from ymir.models.entities import Event, Message, StageExecution
from ymir.orchestrator.engine import OrchestrationEngine
from ymir.orchestrator.role_executor import RoleStageExecutor
from ymir.roles.prompts import PromptRenderer
from ymir.roles.registry import RoleRegistry
from ymir.roles.runtime import RoleRuntime
from ymir.roles.schemas import PRDDocument
from ymir.sandbox import DockerSandbox, LocalSandbox, NetworkPolicy, SandboxService
from ymir.sandbox.factory import build_sandbox
from ymir.sop.loader import load_pipeline, truncate_pipeline
from ymir.sop.models import PipelineDefinition
from ymir.storage.repositories import ProjectRepository, RunRepository

# --------------------------------------------------------------- 假响应（0 真实调用）

PRD_JSON = json.dumps(
    {
        "title": "待办工具 PRD",
        "background": "需求背景",
        "goals": ["可增删改查"],
        "user_stories": [
            {
                "id": "US-1",
                "title": "管理待办",
                "as_a": "用户",
                "i_want": "记录待办",
                "so_that": "不遗忘",
                "acceptance_criteria": ["可以新增"],
            }
        ],
        "requirements": [
            {
                "id": "R-1",
                "title": "新增待办",
                "description": "支持创建",
                "priority": "P0",
                "acceptance_criteria": ["创建返回 id"],
            }
        ],
        "out_of_scope": [],
    },
    ensure_ascii=False,
)

DESIGN_JSON = json.dumps(
    {
        "title": "待办工具设计",
        "overview": "单文件 CLI",
        "modules": [{"name": "cli", "responsibility": "命令解析", "interfaces": ["main()"]}],
        "tech_stack": {"语言": "Python"},
        "directory_structure": "main.py",
        "api_drafts": [],
        "data_models": [],
    },
    ensure_ascii=False,
)

CODE_JSON = json.dumps(
    {
        "files": [
            {
                "path": "main.py",
                "content": (
                    "def main():\n    print('ok')\n\n\n"
                    "if __name__ == '__main__':\n    main()"
                ),
            }
        ],
        "run_commands": ['python -c "print(\'smoke-ok\')"'],
        "notes": "最小示例",
    },
    ensure_ascii=False,
)


class _FakeGateway:
    """脚本化假网关：按调用顺序返回响应并记录调用参数（0 真实调用）。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages,
        *,
        tier,
        run_id=None,
        stage_id=None,
        temperature=None,
        max_tokens=None,
        response_format=None,
    ) -> ChatResult:
        self.calls.append(
            {
                "messages": [dict(m) for m in messages],
                "tier": str(tier),
                "stage_id": stage_id,
            }
        )
        content = self._responses.pop(0) if self._responses else "{}"
        return ChatResult(
            content=content,
            provider="fake",
            model="fake-model",
            tier=str(tier),
            usage=ChatUsage(prompt_tokens=10, completion_tokens=20),
            cost_cny=0.0001,
            latency_ms=2,
        )


# --------------------------------------------------------------- 装配与等待工具


def _build_engine(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
    gateway: Any,
    *,
    truncated_to: str | None = None,
) -> tuple[OrchestrationEngine, ArtifactStore]:
    events = EventPublisher(session_factory)
    messages = MessagePublisher(session_factory, events)
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    sandbox_service = SandboxService(
        session_factory=session_factory,
        sandbox=LocalSandbox(base_dir=tmp_path / "sandboxes"),
    )
    roles_dir = PROJECT_ROOT / "config" / "roles"
    runtime = RoleRuntime(
        gateway=gateway,
        registry=RoleRegistry(roles_dir),
        renderer=PromptRenderer(roles_dir / "prompts"),
    )
    executor = RoleStageExecutor(
        session_factory=session_factory,
        store=store,
        runtime=runtime,
        sandbox_service=sandbox_service,
        events=events,
        exec_timeout=30.0,
    )
    loader = None
    if truncated_to is not None:
        truncated = truncate_pipeline(load_pipeline("software-dev"), truncated_to)

        def _loader(_pid: str) -> PipelineDefinition:
            return truncated

        loader = _loader
    engine = OrchestrationEngine(
        session_factory=session_factory,
        events=events,
        messages=messages,
        executor=executor,
        definition_loader=loader,
    )
    return engine, store


async def _new_project_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    requirement: str = "做一个待办工具",
    project_id: str | None = None,
) -> tuple[str, str]:
    async with session_factory() as session:
        if project_id is None:
            project = await ProjectRepository(session).create(name="t03-qa")
            project_id = project.id
        run = await RunRepository(session).create(project_id=project_id, requirement=requirement)
        await session.commit()
        return project_id, run.id


async def _new_run_in_project(
    session_factory: async_sessionmaker[AsyncSession], project_id: str, requirement: str = "重跑"
) -> str:
    async with session_factory() as session:
        run = await RunRepository(session).create(project_id=project_id, requirement=requirement)
        await session.commit()
        return run.id


async def _project(session_factory: async_sessionmaker[AsyncSession]) -> str:
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="t03-qa-store")
        await session.commit()
        return project.id


def _scalars(result: Any) -> list[Any]:
    return list(result.scalars().all())


async def _wait_status(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    statuses: set[str],
    timeout: float = 30.0,
):
    deadline = time.monotonic() + timeout
    while True:
        async with session_factory() as session:
            run = await RunRepository(session).get(run_id)
        if run is not None and run.status in statuses:
            return run
        if time.monotonic() > deadline:
            current = run.status if run is not None else "missing"
            raise AssertionError(f"run 未在 {timeout}s 内到达 {statuses}（当前 {current}）")
        await asyncio.sleep(0.05)


async def _wait_events(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    types_needed: set[str],
    timeout: float = 10.0,
):
    deadline = time.monotonic() + timeout
    while True:
        async with session_factory() as session:
            stmt = select(Event).where(Event.run_id == run_id).order_by(Event.seq)
            rows = _scalars(await session.execute(stmt))
        if types_needed <= {row.type for row in rows}:
            return rows
        if time.monotonic() > deadline:
            raise AssertionError(f"事件未齐全：需要 {types_needed}")
        await asyncio.sleep(0.05)


# =============================================================== 验收① PM 单跑


async def test_pm_truncated_to_architecture_prd_v1_artifact_and_message_roles(
    session_factory, tmp_path
) -> None:
    """截断到 architecture：prd v1 落库（prd.md+prd.json）且过 PRDDocument 校验，
    消息 to_role 正确（prd -> architect；末阶段 architect 广播）。"""
    gateway = _FakeGateway([PRD_JSON, DESIGN_JSON])
    engine, store = _build_engine(session_factory, tmp_path, gateway, truncated_to="architecture")
    project_id, run_id = await _new_project_run(session_factory)

    await engine.launch(run_id)
    run = await _wait_status(session_factory, run_id, {"completed", "failed"})
    assert run.status == "completed", run.error

    arts = {a.name: a for a in await store.list_artifacts(project_id)}
    assert set(arts) == {"prd", "design"}
    assert arts["prd"].current_version == 1
    assert arts["prd"].type == "prd"

    prd = await store.latest_by_name(project_id, "prd")
    assert prd is not None and prd.version == 1
    assert set(prd.files) == {"prd.md", "prd.json"}
    doc = PRDDocument.model_validate(json.loads(prd.files["prd.json"]))  # 过 schema 校验
    assert doc.title == "待办工具 PRD"
    assert doc.to_markdown() == prd.files["prd.md"]

    async with session_factory() as session:
        msgs = _scalars(
            await session.execute(
                select(Message).where(Message.run_id == run_id).order_by(Message.seq)
            )
        )
    assert [m.from_role for m in msgs] == ["pm", "architect"]
    assert msgs[0].to_role == "architect"
    assert msgs[0].content_refs[0]["name"] == "prd"
    assert msgs[0].content_refs[0]["version"] == 1
    assert msgs[1].to_role is None  # 末阶段：广播
    assert [call["tier"] for call in gateway.calls] == ["planner", "planner"]

    await engine.shutdown()


async def test_pm_truncated_to_prd_runs_single_stage_only(session_factory, tmp_path) -> None:
    gateway = _FakeGateway([PRD_JSON])
    engine, store = _build_engine(session_factory, tmp_path, gateway, truncated_to="prd")
    project_id, run_id = await _new_project_run(session_factory)

    await engine.launch(run_id)
    run = await _wait_status(session_factory, run_id, {"completed", "failed"})
    assert run.status == "completed", run.error
    assert [a.name for a in await store.list_artifacts(project_id)] == ["prd"]
    assert len(gateway.calls) == 1
    await engine.shutdown()


async def test_prd_bad_json_retries_with_correction_then_succeeds(
    session_factory, tmp_path
) -> None:
    """坏 JSON -> 带纠错消息重试 -> 成功产 prd v1（retry.max_attempts=2）。"""
    gateway = _FakeGateway(["这不是 JSON 输出", PRD_JSON])
    engine, store = _build_engine(session_factory, tmp_path, gateway, truncated_to="prd")
    project_id, run_id = await _new_project_run(session_factory)

    await engine.launch(run_id)
    run = await _wait_status(session_factory, run_id, {"completed", "failed"})
    assert run.status == "completed", run.error

    assert len(gateway.calls) == 2
    second = gateway.calls[1]["messages"]
    # 第二次调用：system, user, assistant(坏输出), user(纠错)
    assert len(second) == 4
    assert any(m["role"] == "user" and "未通过校验" in m["content"] for m in second)
    loaded = await store.latest_by_name(project_id, "prd")
    assert loaded is not None and loaded.version == 1
    await engine.shutdown()


async def test_prd_schema_invalid_retries_then_succeeds(session_factory, tmp_path) -> None:
    """JSON 合法但 schema 非法（缺 requirements）-> 同样触发重试并最终成功。"""
    invalid = json.dumps(
        {"title": "x", "background": "b", "goals": [], "user_stories": [], "requirements": []},
        ensure_ascii=False,
    )
    gateway = _FakeGateway([invalid, PRD_JSON])
    engine, store = _build_engine(session_factory, tmp_path, gateway, truncated_to="prd")
    project_id, run_id = await _new_project_run(session_factory)

    await engine.launch(run_id)
    run = await _wait_status(session_factory, run_id, {"completed", "failed"})
    assert run.status == "completed", run.error
    assert len(gateway.calls) == 2
    loaded = await store.latest_by_name(project_id, "prd")
    assert loaded is not None and loaded.version == 1
    await engine.shutdown()


async def test_invalid_output_exhausts_retry_run_failed_no_silent_swallow(
    session_factory, tmp_path
) -> None:
    """重试耗尽 -> RoleOutputError 冒泡 -> engine 标 run failed，且无半成品产物落库。"""
    gateway = _FakeGateway(["bad-1", "bad-2"])
    engine, store = _build_engine(session_factory, tmp_path, gateway, truncated_to="prd")
    project_id, run_id = await _new_project_run(session_factory)

    await engine.launch(run_id)
    run = await _wait_status(session_factory, run_id, {"completed", "failed"})
    assert run.status == "failed"
    assert run.error and "prd" in run.error
    assert run.error_code == 1006  # PM-10：RoleOutputError -> MODEL_CALL_FAILED
    assert len(gateway.calls) == 2  # 恰好 max_attempts 次

    assert await store.list_artifacts(project_id) == []  # 不静默产半成品
    async with session_factory() as session:
        stages = _scalars(
            await session.execute(
                select(StageExecution).where(StageExecution.run_id == run_id)
            )
        )
    assert [s.status for s in stages] == ["failed"]

    events = await _wait_events(session_factory, run_id, {"run.failed"})
    types = [e.type for e in events]
    assert "stage.failed" in types and "run.failed" in types
    failed_event = next(e for e in events if e.type == "run.failed")
    assert failed_event.payload.get("error_code") == 1006  # PM-10：事件透传错误码
    await engine.shutdown()


# =============================================================== 验收② 全链代码冒烟


async def test_full_chain_to_coding_smoke_report_and_version_increment(
    session_factory, tmp_path
) -> None:
    """截断到 coding：code 产物含 smoke_report.json（LocalSandbox 真实执行），
    同名重跑（新 run）版本递增为 v2。"""
    gateway = _FakeGateway([PRD_JSON, DESIGN_JSON, CODE_JSON])
    engine, store = _build_engine(session_factory, tmp_path, gateway, truncated_to="coding")
    project_id, run_id = await _new_project_run(session_factory)

    await engine.launch(run_id)
    run = await _wait_status(session_factory, run_id, {"completed", "failed"})
    assert run.status == "completed", run.error

    code = await store.latest_by_name(project_id, "code")
    assert code is not None and code.version == 1
    assert {"main.py", "code.json", "smoke_report.json"} <= set(code.files)
    smoke = json.loads(code.files["smoke_report.json"])
    assert smoke["all_ok"] is True
    assert smoke["backend"] == "local"
    assert len(smoke["commands"]) == 1
    cmd = smoke["commands"][0]
    assert cmd["exit_code"] == 0 and cmd["timed_out"] is False
    assert "smoke-ok" in cmd["stdout"]

    # 同名重跑（新 run，同项目）-> 版本递增
    gateway2 = _FakeGateway([PRD_JSON, DESIGN_JSON, CODE_JSON])
    engine2, _ = _build_engine(session_factory, tmp_path, gateway2, truncated_to="coding")
    run2_id = await _new_run_in_project(session_factory, project_id)
    await engine2.launch(run2_id)
    run2 = await _wait_status(session_factory, run2_id, {"completed", "failed"})
    assert run2.status == "completed", run2.error
    code_v2 = await store.latest_by_name(project_id, "code")
    assert code_v2 is not None and code_v2.version == 2

    await engine.shutdown()
    await engine2.shutdown()


# =============================================================== 验收③ 沙箱（替代验证）


class _FakeContainer:
    """最小假容器：内存文件系统（绝对路径 -> bytes）。"""

    def __init__(self, client: _FakeDockerClient, name: str) -> None:
        self._client = client
        self.id = name
        self.fs: dict[str, bytes] = {}
        self.stopped = False
        self.removed = False
        self.exec_log: list[dict[str, Any]] = []

    def exec_run(self, cmd, workdir=None, demux=True):  # noqa: ANN001
        self.exec_log.append({"cmd": list(cmd), "workdir": workdir})
        if self._client.exec_queue:
            return self._client.exec_queue.pop(0)
        return (0, (b"", b""))

    def put_archive(self, path: str, data: bytes) -> None:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for member in tar.getmembers():
                handle = tar.extractfile(member)
                if handle is not None:
                    self.fs[posixpath.join(path, member.name)] = handle.read()

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
        self.stopped = True

    def remove(self, force=False) -> None:  # noqa: ANN001
        if self.removed:
            raise docker.errors.NotFound("already removed")
        self.removed = True


class _FakeContainers:
    def __init__(self, client: _FakeDockerClient) -> None:
        self._client = client
        self._counter = 0

    def run(self, image, command, **kwargs):  # noqa: ANN001
        self._counter += 1
        self._client.run_log.append({"image": image, "command": command, **kwargs})
        return _FakeContainer(self._client, f"ctr-{self._counter}")


class _FakeDockerClient:
    def __init__(self, *, ping_ok: bool = True) -> None:
        self.ping_ok = ping_ok
        self.exec_queue: list = []
        self.run_log: list[dict[str, Any]] = []
        self.containers = _FakeContainers(self)

    def ping(self) -> bool:
        if not self.ping_ok:
            raise docker.errors.DockerException("daemon down")
        return True


async def test_docker_limits_and_network_mode_passed() -> None:
    client = _FakeDockerClient()
    sbx = DockerSandbox(
        client=client, image="ymir-sandbox:test", cpus=4, memory="2g", pids_limit=128
    )
    session = await sbx.start(network=NetworkPolicy.NONE)
    assert session.sandbox_id == "ctr-1"
    kwargs = client.run_log[0]
    assert kwargs["image"] == "ymir-sandbox:test"
    assert kwargs["network_mode"] == "none"
    assert kwargs["mem_limit"] == "2g"
    assert kwargs["pids_limit"] == 128
    assert kwargs["cpu_quota"] == 400_000
    assert kwargs["working_dir"] == "/workspace"


async def test_docker_restricted_and_open_use_bridge() -> None:
    client = _FakeDockerClient()
    sbx = DockerSandbox(client=client)
    await sbx.start(network=NetworkPolicy.RESTRICTED)
    await sbx.start(network=NetworkPolicy.OPEN)
    assert [entry["network_mode"] for entry in client.run_log] == ["bridge", "bridge"]


async def test_docker_exec_wraps_timeout_and_flags_timeout() -> None:
    client = _FakeDockerClient()
    client.exec_queue.append((124, (b"", b"")))
    sbx = DockerSandbox(client=client)
    session = await sbx.start()
    result = await session.exec("sleep 9", timeout=2)
    assert result.timed_out is True
    assert result.ok is False
    assert session.container.exec_log[0]["cmd"][:3] == ["timeout", "2s", "sh"]


async def test_docker_destroy_idempotent() -> None:
    client = _FakeDockerClient()
    sbx = DockerSandbox(client=client)
    session = await sbx.start()
    await sbx.destroy(session)
    assert session.container.removed
    await sbx.destroy(session)  # 二次 destroy：幂等不抛


async def test_docker_network_policy_same_noop_diff_rebuild_migrates_files() -> None:
    client = _FakeDockerClient()
    sbx = DockerSandbox(client=client)
    session = await sbx.start()  # network=none
    await session.write_file("pkg/main.py", "print(1)\n")

    runs_before = len(client.run_log)
    await session.set_network_policy(NetworkPolicy.NONE)  # 同策略：no-op
    assert len(client.run_log) == runs_before

    await session.set_network_policy(NetworkPolicy.OPEN)  # 异策略：重建容器
    assert len(client.run_log) == runs_before + 1
    assert session.network_policy == NetworkPolicy.OPEN
    assert await session.read_file("pkg/main.py") == "print(1)\n"  # 工作区随重建迁移


async def test_local_sandbox_real_exec_files_and_destroy(tmp_path) -> None:
    sbx = LocalSandbox(base_dir=tmp_path / "sbx")
    session = await sbx.start()
    result = await session.exec("echo smoke-ok")
    assert result.ok and "smoke-ok" in result.stdout
    await session.write_file("pkg/a.txt", "x")
    assert await session.read_file("pkg/a.txt") == "x"
    assert await session.list_files() == ["pkg/a.txt"]
    await sbx.destroy(session)
    assert not session.workdir.exists()
    await sbx.destroy(session)  # 二次 destroy：幂等不抛


async def test_local_sandbox_exec_timeout(tmp_path) -> None:
    sbx = LocalSandbox(base_dir=tmp_path / "sbx")
    session = await sbx.start()
    result = await session.exec(f'"{sys.executable}" -c "import time; time.sleep(5)"', timeout=1.0)
    assert result.timed_out is True


def test_factory_auto_and_forced_backends(temp_settings, monkeypatch) -> None:
    monkeypatch.setattr(docker, "from_env", lambda: _FakeDockerClient())
    auto = temp_settings.model_copy(update={"sandbox_backend": "auto"})
    assert build_sandbox(auto).backend == "docker"

    def _boom():
        raise docker.errors.DockerException("no docker here")

    monkeypatch.setattr(docker, "from_env", _boom)
    assert build_sandbox(auto).backend == "local"  # 降级
    forced_local = temp_settings.model_copy(update={"sandbox_backend": "local"})
    forced_docker = temp_settings.model_copy(update={"sandbox_backend": "docker"})
    assert build_sandbox(forced_local).backend == "local"
    assert build_sandbox(forced_docker).backend == "docker"


# =============================================================== 验收④ diff API


async def _api_project(client) -> str:
    resp = await client.post("/api/v1/projects", json={"name": "t03-qa-diff"})
    return resp.json()["data"]["id"]


async def test_diff_api_added_modified_removed_and_stats(client) -> None:
    store = client._transport.app.state.artifacts
    project_id = await _api_project(client)
    await store.save(
        project_id=project_id,
        name="prd",
        artifact_type="prd",
        files={"keep.md": "same\n", "mod.md": "old\n", "gone.md": "bye\n"},
    )
    saved = await store.save(
        project_id=project_id,
        name="prd",
        artifact_type="prd",
        files={"keep.md": "same\n", "mod.md": "new\n", "fresh.py": "print(1)\n"},
    )

    resp = await client.get(
        f"/api/v1/artifacts/{saved.artifact_id}/diff", params={"v1": 1, "v2": 2}
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["stats"] == {"files_changed": 3, "additions": 2, "deletions": 2}
    by_path = {item["path"]: item for item in data["files"]}
    assert set(by_path) == {"mod.md", "gone.md", "fresh.py"}  # keep.md 未变：不出现
    assert by_path["mod.md"]["status"] == "modified"
    assert by_path["gone.md"]["status"] == "removed"
    assert by_path["fresh.py"]["status"] == "added"
    assert by_path["mod.md"]["diff"].startswith("--- a/mod.md")
    assert "+++ b/mod.md" in by_path["mod.md"]["diff"]
    assert "+new" in by_path["mod.md"]["diff"] and "-old" in by_path["mod.md"]["diff"]


async def test_diff_api_same_version_returns_empty(client) -> None:
    store = client._transport.app.state.artifacts
    project_id = await _api_project(client)
    saved = await store.save(
        project_id=project_id, name="prd", artifact_type="prd", files={"a.md": "x\n"}
    )
    resp = await client.get(
        f"/api/v1/artifacts/{saved.artifact_id}/diff", params={"v1": 1, "v2": 1}
    )
    data = resp.json()["data"]
    assert data["files"] == []
    assert data["stats"] == {"files_changed": 0, "additions": 0, "deletions": 0}


async def test_diff_api_unknown_artifact_returns_1002(client) -> None:
    resp = await client.get("/api/v1/artifacts/no-such-id/diff", params={"v1": 1, "v2": 2})
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002


@pytest.mark.parametrize("version", [0, -1])
async def test_version_content_non_positive_returns_1002(client, version: int) -> None:
    store = client._transport.app.state.artifacts
    project_id = await _api_project(client)
    saved = await store.save(
        project_id=project_id, name="prd", artifact_type="prd", files={"a.md": "x"}
    )
    resp = await client.get(
        f"/api/v1/artifacts/{saved.artifact_id}/versions/{version}/content"
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002


async def test_diff_api_non_positive_version_returns_1002(client) -> None:
    store = client._transport.app.state.artifacts
    project_id = await _api_project(client)
    saved = await store.save(
        project_id=project_id, name="prd", artifact_type="prd", files={"a.md": "x"}
    )
    resp = await client.get(
        f"/api/v1/artifacts/{saved.artifact_id}/diff", params={"v1": 0, "v2": 1}
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002


# =============================================================== 反例：产物库


async def test_version_conflict_raises_1003_and_leaves_no_new_row(
    session_factory, tmp_path
) -> None:
    project_id = await _project(session_factory)
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    v1 = await store.save(
        project_id=project_id, name="prd", artifact_type="prd", files={"prd.md": "v1"}
    )
    # 预置 v2 目录（外部占用）——版本不可变语义下不得覆盖
    (store.root / project_id / v1.artifact_id / "v2").mkdir(parents=True)

    with pytest.raises(StateConflictError) as excinfo:
        await store.save(
            project_id=project_id, name="prd", artifact_type="prd", files={"prd.md": "v2"}
        )
    assert excinfo.value.biz_code == BizCode.STATE_CONFLICT

    artifact, versions = await store.get_artifact_detail(v1.artifact_id)
    assert [row.version for row in versions] == [1]  # 无 v2 行
    assert artifact.current_version == 1
    assert v1.storage_path  # v1 元数据仍在
    v1_file = store.root / project_id / v1.artifact_id / "v1" / "prd.md"
    assert v1_file.read_text(encoding="utf-8") == "v1"


async def test_failed_write_leaves_no_files_and_no_artifact_row(session_factory, tmp_path) -> None:
    """写入中途失败（路径前缀冲突）应回滚：无数据库行、无残留文件。"""
    project_id = await _project(session_factory)
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    with pytest.raises((InvalidParamError, OSError)):
        await store.save(
            project_id=project_id,
            name="code",
            artifact_type="code",
            files={"a": "x", "a/b": "y"},
        )
    assert await store.list_artifacts(project_id) == []
    if store.root.exists():
        assert [p for p in store.root.rglob("*") if p.is_file()] == []


@pytest.mark.parametrize(
    "bad", ["../x.py", "/abs.py", "C:\\x.py", "c:/x.py", "a/../../b.py", "/etc/passwd"]
)
async def test_path_traversal_variants_rejected_and_not_written(
    session_factory, tmp_path, bad: str
) -> None:
    project_id = await _project(session_factory)
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    with pytest.raises(InvalidParamError):
        await store.save(
            project_id=project_id, name="code", artifact_type="code", files={bad: "boom"}
        )
    assert await store.list_artifacts(project_id) == []
    assert not store.root.exists() or not any(store.root.rglob("*.py"))


# =============================================================== 反例：预算链路


def _tiny_budget_gateway(session_factory: async_sessionmaker[AsyncSession]) -> ModelGateway:
    """真实网关 + 假客户端 + 极小预算（必然超限，且客户端永不被触达）。"""
    config = ModelsConfig(
        version="t03-qa-budget",
        providers={
            "alpha": ProviderSpec(
                base_url="http://alpha.local",
                api_key_env="ALPHA_KEY",
                models={
                    "alpha-plan": ModelSpec(tier="planner", price_in=1.0, price_out=2.0),
                    "alpha-exec": ModelSpec(tier="executor", price_in=1.0, price_out=2.0),
                },
            )
        },
        routing={"planner": ["alpha-plan"], "executor": ["alpha-exec"]},
        budget=BudgetSpec(per_run_tokens=1, per_day_cost_cny=50.0),
    )
    registry = ProviderRegistry(config)
    recorder = UsageRecorder(session_factory)

    def _client_factory(spec):  # noqa: ANN001
        def _create(**params: Any):  # noqa: ANN202
            raise AssertionError("预算超限：不应触达客户端调用")

        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
        )

    return ModelGateway(
        registry=registry,
        router=ModelRouter(registry, config.routing),
        guard=BudgetGuard(recorder, config.budget),
        recorder=recorder,
        client_factory=_client_factory,
    )


async def test_budget_exceeded_pauses_run_with_ordered_events(
    session_factory, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ALPHA_KEY", "test-key")
    gateway = _tiny_budget_gateway(session_factory)
    engine, _store = _build_engine(session_factory, tmp_path, gateway)
    _project_id, run_id = await _new_project_run(session_factory)

    await engine.launch(run_id)
    run = await _wait_status(session_factory, run_id, {"paused", "failed"})
    assert run.status == "paused", run.error

    events = await _wait_events(session_factory, run_id, {"budget.exceeded", "run.paused"})
    types = [event.type for event in events]
    assert "stage.failed" in types
    assert "budget.exceeded" in types
    # 事件序号按 run 单调无空洞
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    # 顺序：stage.failed -> budget.exceeded -> run.paused
    assert (
        types.index("stage.failed")
        < types.index("budget.exceeded")
        < types.index("run.paused")
    )
    await engine.shutdown()
