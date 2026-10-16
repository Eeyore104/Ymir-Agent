"""真实角色执行器（T03 接入，替换 T02 stub 的同一契约）。

``StageExecutor.execute`` 契约不变（引擎无感切换），职责：
1. 收集输入：``run.context.requirement`` + 上游产物（``stage.inputs`` 中非
   ``requirement`` 的名字 → 项目内最新版本）+ 反馈消息（``type=feedback``）；
2. 调 :class:`~ymir.roles.runtime.RoleRuntime` 执行角色产出；
3. coding 阶段（``use_sandbox=true``）：产物写入沙箱执行 ``run_commands`` 冒烟，
   结果并入 ``smoke_report.json``（冒烟失败不阻断阶段，仅记录）；
4. 产物落 :class:`~ymir.artifacts.store.ArtifactStore` 成新版本 + 发
   ``artifact.created`` 事件；
5. 返回 :class:`~ymir.orchestrator.executor.StageOutcome`（output_refs = 产物引用）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.store import ArtifactStore
from ymir.common.errors import InvalidParamError, SandboxError
from ymir.events.bus import EventPublisher
from ymir.events.log import EventType
from ymir.models.entities import Message, PipelineRun
from ymir.models.enums import ArtifactType, MessageType
from ymir.orchestrator.executor import StageOutcome
from ymir.roles.io import StageInput, StageOutput
from ymir.roles.runtime import RoleRuntime
from ymir.sandbox.base import NetworkPolicy
from ymir.sandbox.session import SandboxService
from ymir.sop.models import ENTRY_INPUT, StageSpec
from ymir.storage.repositories import ProjectRepository
from ymir.tools.read_artifact import read_artifacts

logger = logging.getLogger(__name__)

#: 产物名 -> 产物类型（§3.6）
_ARTIFACT_TYPE_MAP = {
    "prd": ArtifactType.PRD.value,
    "design": ArtifactType.DESIGN.value,
    "code": ArtifactType.CODE.value,
    "evaluation_report": ArtifactType.REPORT.value,
}

#: 冒烟输出截断（单流字符数，控制 smoke_report.json 体积）
_SMOKE_STREAM_LIMIT = 4000

#: 单阶段冒烟命令数上限（防滥用）
_SMOKE_MAX_COMMANDS = 5


class RoleStageExecutor:
    """真实四角色执行器（引擎按阶段调用）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        runtime: RoleRuntime,
        sandbox_service: SandboxService | None = None,
        events: EventPublisher | None = None,
        sandbox_network: NetworkPolicy = NetworkPolicy.NONE,
        exec_timeout: float = 120.0,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._runtime = runtime
        self._sandbox_service = sandbox_service
        self._events = events
        self._sandbox_network = sandbox_network
        self._exec_timeout = exec_timeout

    async def execute(
        self, *, run: PipelineRun, stage: StageSpec, attempt: int
    ) -> StageOutcome:
        stage_input = await self._collect_input(run, stage, attempt)
        output = await self._runtime.execute(
            stage.role, stage_input, run_id=run.id, stage_id=stage.id
        )

        smoke: dict[str, Any] | None = None
        if stage.use_sandbox and self._sandbox_service is not None:
            smoke = await self._run_sandbox_smoke(run, output)
            output.files["smoke_report.json"] = (
                json.dumps(smoke, ensure_ascii=False, indent=2) + "\n"
            )

        artifact_name = stage.outputs[0] if stage.outputs else stage.id
        saved = await self._store.save(
            project_id=run.project_id,
            name=artifact_name,
            artifact_type=_ARTIFACT_TYPE_MAP.get(artifact_name, ArtifactType.DOC.value),
            files=output.files,
            run_id=run.id,
            stage_id=stage.id,
            created_by=stage.role,
            message=(None if attempt <= 1 else f"第 {attempt} 次执行（重跑/修订）"),
        )

        if self._events is not None:
            await self._events.emit(
                run.id,
                EventType.ARTIFACT_CREATED,
                {
                    "artifact_id": saved.artifact_id,
                    "name": saved.artifact_name,
                    "type": saved.artifact_type,
                    "version": saved.version,
                    "file_count": saved.file_count,
                    "stage_id": stage.id,
                },
            )

        payload: dict[str, Any] = {
            "stage_id": stage.id,
            "role_id": stage.role,
            "attempt": attempt,
            "artifact_id": saved.artifact_id,
            "artifact_version": saved.version,
            "model": output.model,
            "provider": output.provider,
            "cost_cny": output.cost_cny,
            "schema_attempts": output.schema_attempts,
        }
        if smoke is not None:
            payload["sandbox"] = {
                "all_ok": smoke.get("all_ok"),
                "commands": len(smoke.get("commands", [])),
                "error": smoke.get("error"),
            }

        return StageOutcome(
            output_refs=[saved.ref()],
            summary=output.summary,
            payload=payload,
        )

    # ------------------------------------------------------------------ 输入收集

    async def _collect_input(
        self, run: PipelineRun, stage: StageSpec, attempt: int
    ) -> StageInput:
        requirement = ""
        if isinstance(run.context, dict):
            requirement = str(run.context.get("requirement", ""))

        project_name = ""
        async with self._session_factory() as session:
            project = await ProjectRepository(session).get(run.project_id)
            if project is not None:
                project_name = project.name

        upstream_names = [name for name in stage.inputs if name != ENTRY_INPUT]
        upstream = await read_artifacts(self._store, run.project_id, upstream_names)
        missing = sorted(set(upstream_names) - {item.name for item in upstream})
        if missing:
            raise InvalidParamError(
                f"阶段 {stage.id} 缺少上游产物 {missing}（请先执行上游阶段）"
            )

        feedback = await self._collect_feedback(run.id, stage.role)
        return StageInput(
            requirement=requirement,
            project_name=project_name,
            upstream=upstream,
            feedback=feedback,
            attempt=attempt,
        )

    async def _collect_feedback(self, run_id: str, role_id: str) -> list[str]:
        """反馈消息（T04 驳回产生；此处已按「目标角色 = 本阶段角色」过滤）。"""
        stmt = (
            select(Message)
            .where(
                Message.run_id == run_id,
                Message.type == MessageType.FEEDBACK.value,
                Message.to_role == role_id,
            )
            .order_by(Message.seq.asc())
        )
        async with self._session_factory() as session:
            rows = list((await session.execute(stmt)).scalars().all())
        return [row.summary or "" for row in rows if row.summary]

    # ------------------------------------------------------------------ 沙箱冒烟

    async def _run_sandbox_smoke(
        self, run: PipelineRun, output: StageOutput
    ) -> dict[str, Any]:
        """在沙箱内执行代码产物的自测命令（尽力而为：失败仅记录，不阻断阶段）。"""
        commands = [
            str(cmd).strip()
            for cmd in output.structured.get("run_commands", [])
            if str(cmd).strip()
        ][:_SMOKE_MAX_COMMANDS]
        report: dict[str, Any] = {
            "backend": getattr(self._sandbox_service.sandbox, "backend", "unknown"),  # type: ignore[union-attr]
            "network": str(self._sandbox_network),
            "executed_at": datetime.now(UTC).isoformat(),
            "commands": [],
            "all_ok": None,
            "error": None,
        }
        if not commands:
            report["error"] = "skipped: 无自测命令"
            return report

        if self._sandbox_service is None:
            # 显式前置条件（QA-08）：assert 在 ``python -O`` 下被剥离，改用明确异常
            raise SandboxError(f"沙箱服务未装配，无法执行冒烟（run {run.id}）")
        try:
            managed = await self._sandbox_service.start(
                run.id, network=self._sandbox_network
            )
        except Exception as exc:  # noqa: BLE001 - 沙箱不可用：记录不阻断
            logger.exception("沙箱冒烟会话启动失败（run %s）", run.id)
            report["error"] = f"沙箱不可用: {exc}"
            return report

        try:
            session = managed.session
            for path, content in output.files.items():
                await session.write_file(path, content)
            for command in commands:
                try:
                    result = await session.exec(command, timeout=self._exec_timeout)
                except Exception as exc:  # noqa: BLE001 - 单命令异常同样记录
                    report["commands"].append(
                        {"command": command, "error": str(exc), "exit_code": None}
                    )
                    continue
                report["commands"].append(
                    {
                        "command": command,
                        "stdout": result.stdout[:_SMOKE_STREAM_LIMIT],
                        "stderr": result.stderr[:_SMOKE_STREAM_LIMIT],
                        "exit_code": result.exit_code,
                        "duration_ms": result.duration_ms,
                        "timed_out": result.timed_out,
                    }
                )
            report["all_ok"] = all(
                item.get("exit_code") == 0 and not item.get("timed_out")
                for item in report["commands"]
            )
        finally:
            try:
                await self._sandbox_service.destroy(managed)
            except Exception:  # noqa: BLE001 - 销毁失败仅告警
                logger.exception("沙箱会话销毁失败（run %s）", run.id)
        return report
