"""真实角色执行器（T03 接入，替换 T02 stub 的同一契约）。

``StageExecutor.execute`` 契约不变（引擎无感切换），职责：
1. 收集输入：``run.context.requirement`` + 上游产物（``stage.inputs`` 中非
   ``requirement`` 的名字 → 项目内最新版本）+ 反馈消息（``type=feedback``）
   + **定向修复上下文**（v1.1，``payload.source=auto_repair`` 的消息）；
2. 调 :class:`~ymir.roles.runtime.RoleRuntime` 执行角色产出；
3. coding 阶段（``use_sandbox=true``）：产物写入沙箱执行 ``run_commands`` 冒烟，
   结果并入 ``smoke_report.json``（冒烟失败不阻断阶段，仅记录）；
4. 产物落 :class:`~ymir.artifacts.store.ArtifactStore` 成新版本 + 发
   ``artifact.created`` 事件；
5. 返回 :class:`~ymir.orchestrator.executor.StageOutcome`（output_refs = 产物引用）。

v1.1 定向修复（docs/07 §4.4.4 / B-01③）：自动修复消息**不进** ``feedback`` 列表
（避免双份注入），而是解析成 :class:`~ymir.roles.io.RepairContext`，并按
``previous.artifact_name/version`` 加载**上一版自身产物**填 ``previous_output``
（加载失败只告警、``previous_output=None``，绝不让阶段失败）。
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.merge import (
    DEFAULT_MAX_FILE_BYTES,
    PATCH_MANIFEST_NAME,
    MergeResult,
    merge_trees,
)
from ymir.artifacts.store import ArtifactStore
from ymir.common.errors import InvalidParamError, NotFoundError, SandboxError, YmirError
from ymir.events.bus import EventPublisher
from ymir.events.log import EventType
from ymir.models.entities import Message, PipelineRun
from ymir.models.enums import ArtifactType, MessageType
from ymir.orchestrator.executor import StageOutcome
from ymir.roles.io import (
    BaselineContext,
    RepairContext,
    StageInput,
    StageOutput,
    TemplateContext,
    UpstreamArtifact,
)
from ymir.roles.runtime import RoleRuntime
from ymir.sandbox.base import NetworkPolicy
from ymir.sandbox.session import SandboxService
from ymir.sop.models import ENTRY_INPUT, StageSpec
from ymir.storage.repositories import ProjectRepository
from ymir.templates import load_template
from ymir.templates.models import TemplateSpec
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

#: 自动修复消息的来源标识（与 ``RepairController`` 发出的 ``payload.source`` 一致）
_AUTO_REPAIR_SOURCE = "auto_repair"

#: 失败检查 output 摘要的缺省总长上限（与 ``RepairContextSpec`` 缺省一致）
_DEFAULT_DETAIL_CHARS = 1200

#: 模板降级原因前缀（r5 硬要求：必须落进 ``run.context["template"]["degraded"]``）
_TEMPLATE_DEGRADED_NOT_FOUND = "template_not_found"
_TEMPLATE_DEGRADED_INVALID = "template_invalid"

#: 降级原因里的异常摘要长度上限（JSON 列不留长栈，tooltip 够读即可）
_TEMPLATE_DEGRADED_DETAIL_CHARS = 200


@dataclass(frozen=True)
class TemplateLoadResult:
    """模板装载结果（v1.2 · r5）。

    ``spec is None`` 表示「按通用模式继续」，但**两种原因必须区分**：

    - ``degraded_reason == ""``：本次 run 本来就没用模板（v1.1 路径，零写入）；
    - ``degraded_reason != ""``：模板有引用却装载失败 = **发生降级**，调用方必须把
      原因落进 ``run.context["template"]["degraded"]`` —— 用户以为有模板约束、实际
      没有，比报错更危险（r5 定案）。
    """

    spec: TemplateSpec | None = None
    degraded_reason: str = ""


def _template_error_summary(
    exc: BaseException, limit: int = _TEMPLATE_DEGRADED_DETAIL_CHARS
) -> str:
    """把模板装载异常压成单行摘要（去换行 / 截断），避免 JSON 列膨胀。"""
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    return text if len(text) <= limit else text[:limit] + "…"


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
        max_patch_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._runtime = runtime
        self._sandbox_service = sandbox_service
        self._events = events
        self._sandbox_network = sandbox_network
        self._exec_timeout = exec_timeout
        #: 增量模式下单文件上限（超限在 ``merge_trees`` 记 ``too_large`` 并跳过）
        self._max_patch_file_bytes = max_patch_file_bytes

    async def execute(
        self, *, run: PipelineRun, stage: StageSpec, attempt: int
    ) -> StageOutcome:
        template_load = self._load_template_spec(run)
        template_spec = template_load.spec
        if template_load.degraded_reason:
            # r5：降级必须可观测 —— 模板被删/改坏时把原因落进 run.context，只写一次
            await self._record_template_degraded(run, reason=template_load.degraded_reason)
        baseline_ref, base_files = await self._load_baseline(run)
        stage_input = await self._collect_input(
            run, stage, attempt, template_spec=template_spec, baseline_ref=baseline_ref
        )
        output = await self._runtime.execute(
            stage.role, stage_input, run_id=run.id, stage_id=stage.id
        )

        # ---- 阶段一（合法化）+ 阶段二（合入）：骨架注入 / patch 契约 ----------
        # 顺序是硬要求（docs/09 §4.1.3 S1）：runtime.execute 之后、smoke 之前、save 之前。
        # 无模板且无基线时整段跳过 ⇒ v1.1 路径逐字节不变。
        merged = self._apply_patch_contract(
            output,
            template_spec=template_spec,
            base_files=base_files,
            stage_input=stage_input,
        )
        smoke_files = merged.files if merged is not None else output.files

        smoke: dict[str, Any] | None = None
        if stage.use_sandbox and self._sandbox_service is not None:
            smoke = await self._run_sandbox_smoke(run, output, files=smoke_files)
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
        if merged is not None:
            # 审批卡片摘要必须能看到改动面与骨架冲突（docs/09 §13「报告纪律」）
            payload["patch"] = {
                "mode": merged.manifest.mode,
                "counts": merged.manifest.counts.model_dump(),
                "changed_paths": list(merged.manifest.changed_paths),
                "reserved_conflicts": list(merged.manifest.reserved_conflicts),
                "conflicts": [item.model_dump() for item in merged.manifest.conflicts],
            }

        return StageOutcome(
            output_refs=[saved.ref()],
            summary=output.summary,
            payload=payload,
            changed_paths=(
                tuple(merged.manifest.changed_paths) if merged is not None else ()
            ),
        )

    # ------------------------------------------------------------------ patch 契约

    def _apply_patch_contract(
        self,
        output: StageOutput,
        *,
        template_spec: TemplateSpec | None,
        base_files: dict[str, str],
        stage_input: StageInput,
    ) -> MergeResult | None:
        """阶段一/阶段二合并（`docs/09 §4.1.3 S1 + §4.2.2`）。

        - **阶段一（合法化）**：``merge_trees(base=基线正文, patch=模型输出, mode=patch)``
          —— 传基线**正文**（不是 ``{}``）才能逐字节判定 noop；骨架按优先级表注入；
        - **阶段二（合入）**：``out1.files`` 即整棵交付树，冒烟用它、评测也用它；
        - **产物只存增量**：由 ``manifest.changed_paths`` 反推，而不是"以为传了 ``{}``
          就只留了增量"（``out1.files`` 是整树，直接落盘会把基线全文写进产物）。

        无模板且无基线 → 返回 ``None``，``output.files`` 一字不改（v1.1 路径）。
        """
        if template_spec is None and not base_files:
            return None

        skeleton = template_spec.skeleton_files() if template_spec is not None else None
        reserved = (
            template_spec.reserved_paths() if template_spec is not None else frozenset()
        )
        deleted = _deleted_paths(output.structured)
        out1 = merge_trees(
            base_files,
            output.files,
            mode="patch" if base_files else "from_scratch",
            skeleton=skeleton,
            reserved_paths=reserved,
            base_paths=frozenset(base_files),
            deleted=deleted,
            max_file_bytes=self._max_patch_file_bytes,
        )

        stored = {path: out1.files[path] for path in out1.manifest.changed_paths
                  if path in out1.files}
        # 只有**增量**（patch）产物才写簿记文件。from_scratch 不是"变更集"：
        # 一旦写 manifest，下游会把它当增量 —— 评测侧按 changed_paths 做 judge
        # 语料分级，会把**全量**文件当"变更文件"喂全文（成本白涨），且与
        # docs/09 §9-T2.4 DoD ⑨「from_scratch 剥掉 manifest、changed_paths=None」冲突。
        # 2026-10-23 T5.1 真跑实测：from_scratch 产物里出现了 changed_paths=32 的
        # patch_manifest.json，即此处无条件写入所致。
        if out1.manifest.mode == "patch":
            stored[PATCH_MANIFEST_NAME] = out1.manifest_json()
        output.files = stored
        return out1

    async def _load_baseline(
        self, run: PipelineRun
    ) -> tuple[BaselineContext | None, dict[str, str]]:
        """按 ``run.context["baseline"]`` 加载基线**正文**（无基线返回 ``(None, {})``）。

        加载失败只告警并降级为"无基线"（与"上一版产物加载失败仅告警"同口径）：
        基线被删除时不该让整轮 run 失败，但 ``mode`` 会退化为 ``from_scratch``。
        """
        context = run.context if isinstance(run.context, dict) else {}
        ref = context.get("baseline")
        if not isinstance(ref, dict):
            return None, {}
        artifact_id = str(ref.get("artifact_id") or "").strip()
        if not artifact_id:
            return None, {}
        raw_version = ref.get("version")
        version = raw_version if isinstance(raw_version, int) else None
        try:
            loaded = await self._store.load(artifact_id, version)
        except Exception:  # noqa: BLE001 - 基线缺失/损坏：降级为从零生成
            logger.warning(
                "基线加载失败，按从零生成处理（run=%s baseline=%s v=%s）",
                run.id,
                artifact_id,
                version,
                exc_info=True,
            )
            return None, {}
        return (
            BaselineContext(
                artifact_id=artifact_id,
                version=loaded.version,
                content_hash=loaded.version_row.content_hash,
                file_count=len(loaded.files),
                paths=tuple(sorted(loaded.files)),
            ),
            dict(loaded.files),
        )

    # ------------------------------------------------------------------ 输入收集

    async def _collect_input(
        self,
        run: PipelineRun,
        stage: StageSpec,
        attempt: int,
        *,
        template_spec: TemplateSpec | None = None,
        baseline_ref: BaselineContext | None = None,
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
        repair_context, previous_output = await self._collect_repair_context(run, stage.role)
        return StageInput(
            requirement=requirement,
            project_name=project_name,
            upstream=upstream,
            feedback=feedback,
            attempt=attempt,
            repair_context=repair_context,
            previous_output=previous_output,
            template=self._collect_template(run, spec=template_spec),
            baseline=baseline_ref,
        )

    @staticmethod
    def _load_template_spec(run: PipelineRun) -> TemplateLoadResult:
        """按 ``run.context["template"]["id"]`` 读模板定义。

        与 :meth:`_collect_template` 分开是为了「一次 run 只读一次 YAML」：
        ``execute`` 先取 spec，再分别给提示词视图与骨架注入用。

        Returns:
            :class:`TemplateLoadResult`。``degraded_reason`` 非空 = 模板有引用但
            装载失败（被删 -> ``template_not_found``；改坏 -> ``template_invalid``），
            调用方据此落库（r5：降级必须可观测）。
        """
        context = run.context if isinstance(run.context, dict) else {}
        ref = context.get("template")
        if not isinstance(ref, dict):
            return TemplateLoadResult()
        template_id = str(ref.get("id") or "").strip()
        if not template_id:
            return TemplateLoadResult()
        try:
            return TemplateLoadResult(spec=load_template(template_id))
        except NotFoundError as exc:
            logger.warning(
                "模板已不存在，按通用模式继续（run=%s template=%s）: %s",
                run.id,
                template_id,
                exc,
            )
            return TemplateLoadResult(
                degraded_reason=f"{_TEMPLATE_DEGRADED_NOT_FOUND}: {template_id}"
            )
        except YmirError as exc:
            logger.warning(
                "模板装载失败，按通用模式继续（run=%s template=%s）: %s",
                run.id,
                template_id,
                exc,
            )
            return TemplateLoadResult(
                degraded_reason=f"{_TEMPLATE_DEGRADED_INVALID}: {_template_error_summary(exc)}"
            )

    async def _record_template_degraded(self, run: PipelineRun, *, reason: str) -> None:
        """把模板降级原因落进 ``run.context["template"]["degraded"]``（r5 硬要求）。

        实现纪律（逐条对应 docs/09「JSON 列」约定）：

        - **JSON 列纪律**：读 → dict 深拷贝 → 改副本 → **整体赋值**回 ``row.context``
          → commit。SQLAlchemy 不会感知 JSON 列的原地修改。
        - **只写一次**：``degraded`` 键已存在（另一个阶段先写过）则**不覆盖**，
          同一 run 多次降级保留首次原因。
        - **正常路径零写入**：本方法只在确实降级时被调用；模板加载成功时
          ``run.context["template"]`` 仍是 ``{"id","version"}`` 原样（§10 兼容契约）。
        - **不改 run 状态**：降级不等于失败，阶段照常推进。
        - **不发事件、不写回执表**：与本缺陷无关（事件域 ``events.run_id`` NOT NULL）。
        """
        async with self._session_factory() as session:
            row = await session.get(PipelineRun, run.id)
            if row is None:
                return
            context = copy.deepcopy(row.context) if isinstance(row.context, dict) else {}
            ref = context.get("template")
            if not isinstance(ref, dict) or not str(ref.get("id") or "").strip():
                return
            if "degraded" in ref:
                # 已有首次原因：只把内存对象对齐，不覆盖落库值
                run.context = context
                return
            ref["degraded"] = reason
            row.context = context
            await session.commit()
            # 同一阶段后续读取（提示词 / 骨架注入）看到的是同一份上下文
            run.context = copy.deepcopy(context)

    @classmethod
    def _collect_template(
        cls, run: PipelineRun, *, spec: TemplateSpec | None = None
    ) -> TemplateContext | None:
        """从 ``run.context["template"]`` 装载模板约束（v1.2，docs/09 §4.1.2）。

        ``run.context`` 只存**引用**（``{"id","version"}``，避免 JSON 列膨胀），
        因此这里按 id 重新读模板 YAML 并解成提示词视图 :class:`TemplateContext`
        （含 ``reserved_paths`` / ``seed_paths`` 两清单，**不含骨架正文**）。

        模板在 run 创建后被删除/改坏属配置漂移：**只告警并按通用模式继续**，
        不让整轮 run 失败（与"上一版产物加载失败仅告警"同口径）。降级原因由
        :meth:`_record_template_degraded` 落进 ``run.context``（本方法保持**只读**语义）。
        """
        resolved = spec if spec is not None else cls._load_template_spec(run).spec
        if resolved is None:
            return None
        return TemplateContext.from_spec(resolved)

    async def _collect_feedback(self, run_id: str, role_id: str) -> list[str]:
        """反馈消息（T04 驳回产生；此处已按「目标角色 = 本阶段角色」过滤）。

        v1.1：自动修复消息（``payload.source=auto_repair``）在 **Python 侧过滤掉** ——
        它已由 :meth:`_collect_repair_context` 解析成结构化修复上下文，若同时并入
        ``feedback`` 会把同一份失败信息注入两遍（成本翻倍且语义重复）。
        """
        rows = await self._feedback_messages(run_id, role_id)
        return [
            row.summary or ""
            for row in rows
            if row.summary and not _is_auto_repair(row)
        ]

    async def _feedback_messages(self, run_id: str, role_id: str) -> list[Message]:
        """某 run 指向某角色的全部反馈消息（按 seq 升序）。"""
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
            return list((await session.execute(stmt)).scalars().all())

    async def _collect_repair_context(
        self, run: PipelineRun, role_id: str
    ) -> tuple[RepairContext | None, UpstreamArtifact | None]:
        """解析最新一条自动修复消息（v1.1，docs/07 §4.4.4）。

        返回 ``(RepairContext | None, 上一版自身产物 | None)``；无该消息时双 ``None``
        （v1.0 行为不变）。上一版产物加载失败只告警，绝不阻断阶段。
        """
        rows = await self._feedback_messages(run.id, role_id)
        auto_repair = [row for row in rows if _is_auto_repair(row)]
        if not auto_repair:
            return None, None

        message = auto_repair[-1]  # seq 升序的末条 = 最新一轮
        payload = message.payload if isinstance(message.payload, dict) else {}
        context = payload.get("context")
        context = context if isinstance(context, dict) else {}
        previous = payload.get("previous")
        previous = dict(previous) if isinstance(previous, dict) else None

        repair_context = RepairContext(
            repair_round=_as_int(payload.get("repair_round"), 0),
            max_rounds=_as_int(payload.get("max_rounds"), 0),
            verdict=_as_dict(payload.get("verdict")),
            failed_checks=_as_check_list(payload.get("failed_checks")),
            previous=previous,
            directive=str(payload.get("directive") or ""),
            max_detail_chars=_as_int(
                context.get("max_detail_chars"), _DEFAULT_DETAIL_CHARS
            ),
        )
        previous_output = await self._load_previous_output(run, repair_context)
        return repair_context, previous_output

    async def _load_previous_output(
        self, run: PipelineRun, repair_context: RepairContext
    ) -> UpstreamArtifact | None:
        """按 ``previous.artifact_name/version`` 加载上一版自身产物（失败仅告警）。"""
        previous = repair_context.previous
        if not previous:
            return None
        name = str(previous.get("artifact_name") or "code")
        version = previous.get("version")
        version_int = version if isinstance(version, int) else None
        try:
            latest = await self._store.latest_by_name(run.project_id, name)
            if latest is None:
                return None
            loaded = await self._store.load(latest.artifact.id, version_int)
        except Exception:  # noqa: BLE001 - 上一版缺失/加载失败不影响本轮执行
            logger.warning(
                "定向修复：上一版产物加载失败，按未提供处理（run=%s name=%s version=%s）",
                run.id,
                name,
                version,
                exc_info=True,
            )
            return None
        repair_context.previous.setdefault("file_count", len(loaded.files))
        return UpstreamArtifact(
            name=name, version=loaded.version, files=loaded.files
        )

    # ------------------------------------------------------------------ 沙箱冒烟

    async def _run_sandbox_smoke(
        self, run: PipelineRun, output: StageOutput, *, files: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """在沙箱内执行代码产物的自测命令（尽力而为：失败仅记录，不阻断阶段）。

        ``files`` 缺省为 ``output.files``；**增量/模板模式下必须传合并后的整树**
        （docs/09 §4.2.2 / §4.1.3）：只写补丁会让 ``import`` 链缺基线文件，
        冒烟必然 ``ModuleNotFoundError``，那是假失败。
        """
        tree = output.files if files is None else files
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
            for path, content in tree.items():
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


# --------------------------------------------------------------------- 消息解包工具


def _is_auto_repair(message: Message) -> bool:
    """是否为自动修复消息（``payload.source == "auto_repair"``）。"""
    payload = message.payload
    return isinstance(payload, dict) and payload.get("source") == _AUTO_REPAIR_SOURCE


def _as_int(value: Any, default: int) -> int:
    """安全转 int（脏 payload 不炸阶段，例如字符串 / None / 浮点）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_dict(value: Any) -> dict[str, Any]:
    """安全转 dict（非 dict 返回空字典副本）。"""
    return dict(value) if isinstance(value, dict) else {}


def _as_check_list(value: Any) -> list[dict[str, Any]]:
    """安全转「失败检查列表」（非列表 / 非 dict 元素一律丢弃）。"""
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _deleted_paths(structured: Any) -> list[str]:
    """从 engineer 输出里取 ``deleted`` 清单（v1.2 ``CodeBundle.deleted``）。

    脏值一律忽略：非 dict / 非 list / 非字符串元素都跳过 —— patch 契约容错优先于
    教学式报错（docs/09 §4.2.2「为什么容错而不报错」）。
    """
    if not isinstance(structured, dict):
        return []
    raw = structured.get("deleted")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item.strip()]
