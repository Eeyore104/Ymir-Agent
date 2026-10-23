"""模板降级可观测性测试（v1.2 · r5）。

r5 定案：模板在 run 执行期被删除 / 改坏时，run 降级为通用模式继续跑，**必须**把
原因写进 ``run.context["template"]["degraded"]`` —— 用户以为有模板约束、实际没有，
比报错更危险。

本模块覆盖 4 条 DoD：

1. **正常路径零写入**：模板加载成功时 ``run.context["template"]`` 的键集合精确等于
   ``{"id","version"}``（无 ``degraded``，§10 兼容契约）；
2. **降级写入**：被删 -> ``template_not_found: <id>``；改坏 -> ``template_invalid: <原因>``；
   且 run 状态仍为 ``running``、阶段照常产出产物；
3. **只写一次**：两次降级机会，``degraded`` 仍是首次的值；
4. **真的落库**：写入后用**新 session** 重新读 ``run.context``，``degraded`` 键存在
   （防「改了内存对象但没 commit」这类假绿）。

测试纪律：fake runtime / fake sandbox / 真 ArtifactStore（tmp 目录）+ 真 DB 会话，
0 模型调用、0 Docker。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ymir.orchestrator.role_executor as role_executor_module
from ymir.artifacts.store import ArtifactStore
from ymir.common.errors import NotFoundError
from ymir.models.enums import RunStatus
from ymir.orchestrator.role_executor import RoleStageExecutor
from ymir.roles.io import StageOutput
from ymir.sop.models import StageSpec
from ymir.storage.repositories import ProjectRepository, RunRepository
from ymir.templates.loader import TemplateDefinitionError
from ymir.templates.models import (
    CheckSpec,
    SkeletonFile,
    SkeletonSpec,
    StackSpec,
    TemplateSpec,
)

#: 假模型输出（一阶段一文件，避免与骨架/合并逻辑纠缠）
CODE_FILES: dict[str, str] = {"app/api/users.py": "def list_users() -> list[str]:\n    return []\n"}


def _template_spec() -> TemplateSpec:
    """最小但形态完整的模板（reserved / seed 各有）。"""
    return TemplateSpec(
        id="tpl-demo",
        version="1.0",
        name="演示模板",
        stack=StackSpec(dependencies=["fastapi"]),
        skeleton=SkeletonSpec(
            files=[
                SkeletonFile(
                    path="pyproject.toml",
                    content='[project]\nname = "app"\n',
                    reserved=True,
                ),
                SkeletonFile(path="app/main.py", content="from fastapi import FastAPI\n"),
            ]
        ),
        checks=[CheckSpec(run="python -m compileall -q app")],
    )


class _FakeRuntime:
    """假角色运行时：返回预置文件集（0 模型调用）。"""

    def __init__(self) -> None:
        self.output = StageOutput(
            files=dict(CODE_FILES),
            summary="fake",
            structured={"files": [], "run_commands": []},
        )

    async def execute(self, role, stage_input, *, run_id=None, stage_id=None):  # noqa: ANN001, ANN201
        return self.output


class _FakeSession:
    """假沙箱会话（本模块 use_sandbox=False，仅作装配占位）。"""

    async def write_file(self, path: str, content: str) -> None:
        return None

    async def exec(self, command: str, timeout: float | None = None):  # noqa: ANN201
        return SimpleNamespace(stdout="ok", stderr="", exit_code=0, duration_ms=1, timed_out=False)


class _FakeSandboxService:
    """假沙箱服务（不依赖 Docker）。"""

    def __init__(self) -> None:
        self.sandbox = SimpleNamespace(backend="fake")
        self.session = _FakeSession()

    async def start(self, run_id: str, *, network=None):  # noqa: ANN001, ANN201
        return SimpleNamespace(session=self.session)

    async def destroy(self, managed) -> None:  # noqa: ANN001
        return None


def _stage() -> StageSpec:
    """coding 阶段：产出 code（不开沙箱，聚焦上下文写入）。"""
    return StageSpec(id="coding", role="engineer", inputs=[], outputs=["code"])


async def _running_run(
    session_factory: async_sessionmaker[AsyncSession],
    context: dict[str, Any],
) -> Any:
    """建 project + run，并把状态推到 running（模拟执行期）。"""
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="degraded-probe")
        await session.commit()
        project_id = project.id
        run = await RunRepository(session).create(
            project_id=project_id, requirement="在现有项目上改", context=context
        )
        await session.commit()
        run_id = run.id
        await RunRepository(session).update_status(run_id, RunStatus.RUNNING)
        await session.commit()
    # 新 session 读出「执行期视角」的 run（无未提交变更，属性不过期）
    async with session_factory() as session:
        loaded = await RunRepository(session).get(run_id)
    assert loaded is not None
    return loaded


def _executor(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> RoleStageExecutor:
    return RoleStageExecutor(
        session_factory=session_factory,
        store=ArtifactStore(session_factory, tmp_path / "artifacts"),
        runtime=_FakeRuntime(),  # type: ignore[arg-type]
        sandbox_service=_FakeSandboxService(),  # type: ignore[arg-type]
    )


async def _read_context(
    session_factory: async_sessionmaker[AsyncSession], run_id: str
) -> tuple[dict[str, Any], str]:
    """新 session 重新读 ``run.context`` 与状态（DoD ④ 的落库校验）。"""
    async with session_factory() as session:
        row = await RunRepository(session).get(run_id)
        assert row is not None
        return dict(row.context or {}), str(row.status)


class TestNormalPathWritesNothing:
    """DoD ①：模板加载成功时零写入。"""

    async def test_valid_template_keeps_reference_shape(
        self, session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(role_executor_module, "load_template", lambda _tid: _template_spec())
        run = await _running_run(
            session_factory, {"template": {"id": "tpl-demo", "version": "1.0"}}
        )
        executor = _executor(session_factory, tmp_path)

        await executor.execute(run=run, stage=_stage(), attempt=1)

        # 键集合精确相等：没有 degraded
        assert set(run.context["template"]) == {"id", "version"}
        stored_context, status = await _read_context(session_factory, run.id)
        assert set(stored_context["template"]) == {"id", "version"}
        assert "degraded" not in json.dumps(stored_context, ensure_ascii=False)
        assert status == RunStatus.RUNNING.value

    async def test_run_without_template_key_is_untouched(
        self, session_factory, tmp_path: Path
    ) -> None:
        """v1.1 老 run（无 template 键）：一个字节都不写。"""
        run = await _running_run(session_factory, {"context_refs": []})
        executor = _executor(session_factory, tmp_path)

        await executor.execute(run=run, stage=_stage(), attempt=1)

        assert "template" not in run.context
        stored_context, _ = await _read_context(session_factory, run.id)
        assert "template" not in stored_context


class TestDegradedIsRecorded:
    """DoD ②：降级写入 + 状态不变 + 阶段正常产出。"""

    async def test_template_missing_records_not_found(
        self, session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(template_id: str) -> None:
            raise NotFoundError(f"模板不存在: {template_id}")

        monkeypatch.setattr(role_executor_module, "load_template", _boom)
        run = await _running_run(session_factory, {"template": {"id": "gone", "version": "1.0"}})
        executor = _executor(session_factory, tmp_path)

        outcome = await executor.execute(run=run, stage=_stage(), attempt=1)

        assert run.context["template"]["degraded"] == "template_not_found: gone"
        # 阶段仍正常推进：产物照常落盘
        assert outcome.output_refs

        stored_context, status = await _read_context(session_factory, run.id)
        assert stored_context["template"]["degraded"] == "template_not_found: gone"
        # 引用快照原样保留（不因降级丢掉 id/version）
        assert stored_context["template"]["id"] == "gone"
        # 降级不等于失败
        assert status == RunStatus.RUNNING.value

    async def test_template_invalid_records_single_line_reason(
        self, session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(template_id: str) -> None:
            raise TemplateDefinitionError("skeleton 文件数 6 超过上限 5\n详情见模板 YAML")

        monkeypatch.setattr(role_executor_module, "load_template", _boom)
        run = await _running_run(session_factory, {"template": {"id": "broken"}})
        executor = _executor(session_factory, tmp_path)

        await executor.execute(run=run, stage=_stage(), attempt=1)

        degraded = run.context["template"]["degraded"]
        assert degraded.startswith("template_invalid: ")
        # 换行被压平（JSON 列不留多行长文）
        assert "\n" not in degraded
        assert "skeleton 文件数 6 超过上限 5" in degraded

        stored_context, _ = await _read_context(session_factory, run.id)
        assert stored_context["template"]["degraded"] == degraded


class TestDegradedWrittenOnce:
    """DoD ③：两次降级机会只保留首次原因。"""

    async def test_second_degradation_does_not_overwrite(
        self, session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await _running_run(session_factory, {"template": {"id": "gone", "version": "1.0"}})
        executor = _executor(session_factory, tmp_path)

        def _not_found(template_id: str) -> None:
            raise NotFoundError(f"模板不存在: {template_id}")

        monkeypatch.setattr(role_executor_module, "load_template", _not_found)
        await executor.execute(run=run, stage=_stage(), attempt=1)
        first = run.context["template"]["degraded"]
        assert first == "template_not_found: gone"

        # 第二次降级机会：原因不同，但不得覆盖首次
        def _invalid(template_id: str) -> None:
            raise TemplateDefinitionError("later 改动导致非法")

        monkeypatch.setattr(role_executor_module, "load_template", _invalid)
        await executor.execute(run=run, stage=_stage(), attempt=1)

        assert run.context["template"]["degraded"] == first
        stored_context, _ = await _read_context(session_factory, run.id)
        assert stored_context["template"]["degraded"] == first


class TestRealLoaderEndToEnd:
    """端到端（**不 mock loader**）：真 YAML 生效 → 执行期被删 → 降级真的落库。

    与上面几个用例的区别：这里不打桩 ``load_template``，而是把
    ``DEFAULT_TEMPLATES_DIR`` 指到临时目录（**配置**，不是逻辑替身），让真实加载器
    读真实 YAML；然后 **删掉那个文件** 再跑一次。这样"模板在 run 执行期被删"是
    真实磁盘事件，而不是"抛一个伪造的 NotFoundError"。

    所有落库断言都用**新 session** 重读（防"改了内存对象但没 commit"的假绿）。
    """

    async def test_template_used_then_deleted_writes_degraded_to_db(
        self, session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shutil

        from ymir.config import PROJECT_ROOT
        from ymir.templates import loader as loader_module

        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        shutil.copy(
            PROJECT_ROOT / "config" / "templates" / "python-cli.yaml",
            templates_dir / "python-cli.yaml",
        )
        monkeypatch.setattr(loader_module, "DEFAULT_TEMPLATES_DIR", templates_dir)

        store = ArtifactStore(session_factory, tmp_path / "artifacts")

        def _fresh_executor() -> RoleStageExecutor:
            """每次 run 一份新 runtime/executor：``_FakeRuntime.output`` 会被上一次
            执行写回（合并后的整树），复用会污染第二次 run。"""
            return RoleStageExecutor(
                session_factory=session_factory,
                store=store,
                runtime=_FakeRuntime(),  # type: ignore[arg-type]
                sandbox_service=_FakeSandboxService(),  # type: ignore[arg-type]
            )

        context = {"template": {"id": "python-cli", "version": "1.0"}}

        # ---- ① 正常路径：模板真的被用上（骨架文件进产物），且引用形态零写入 ----
        run_ok = await _running_run(session_factory, dict(context))
        outcome_ok = await _fresh_executor().execute(
            run=run_ok, stage=_stage(), attempt=1
        )

        stored_ok, status_ok = await _read_context(session_factory, run_ok.id)
        assert set(stored_ok["template"]) == {"id", "version"}  # 精确等于，无 degraded
        assert "degraded" not in json.dumps(stored_ok, ensure_ascii=False)
        assert status_ok == RunStatus.RUNNING.value
        files_ok = (await store.load(outcome_ok.output_refs[0]["artifact_id"])).files
        assert "ruff.toml" in files_ok  # 骨架确实注入 ⇒ 模板真的生效（不是静默跳过）

        # ---- ② 执行期把模板文件删掉（真实磁盘事件）→ 同一 id 的下一个 run 降级 ----
        (templates_dir / "python-cli.yaml").unlink()

        run_gone = await _running_run(session_factory, dict(context))
        outcome_gone = await _fresh_executor().execute(
            run=run_gone, stage=_stage(), attempt=1
        )

        stored_gone, status_gone = await _read_context(session_factory, run_gone.id)
        assert stored_gone["template"]["degraded"] == "template_not_found: python-cli"
        assert stored_gone["template"]["id"] == "python-cli"  # 引用快照保留
        assert status_gone == RunStatus.RUNNING.value  # 降级 ≠ 失败
        files_gone = (await store.load(outcome_gone.output_refs[0]["artifact_id"])).files
        assert "ruff.toml" not in files_gone  # 骨架不再注入（真降级了）
        assert "app/api/users.py" in files_gone  # 但阶段照常产出


class TestLoadTemplateSpecResult:
    """装载结果的三态（供 ``execute`` 判定是否落库）。"""

    def test_reports_reason_for_missing_template(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(template_id: str) -> None:
            raise NotFoundError(f"模板不存在: {template_id}")

        monkeypatch.setattr(role_executor_module, "load_template", _boom)
        result = RoleStageExecutor._load_template_spec(
            SimpleNamespace(id="run-x", context={"template": {"id": "gone"}})
        )
        assert result.spec is None
        assert result.degraded_reason == "template_not_found: gone"

    def test_no_reference_means_no_degradation(self) -> None:
        result = RoleStageExecutor._load_template_spec(
            SimpleNamespace(id="run-x", context={"context_refs": []})
        )
        assert result.spec is None
        assert result.degraded_reason == ""
