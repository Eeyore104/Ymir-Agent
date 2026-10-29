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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.merge import (
    DEFAULT_MAX_CHANGE_RATIO,
    DEFAULT_MAX_FILE_BYTES,
    PATCH_MANIFEST_NAME,
    MergeProvenance,
    MergeResult,
    ProtectedPathConflict,
    _bounded_warnings,
    merge_trees,
    parse_patch_manifest,
)
from ymir.artifacts.provenance import strip_bookkeeping, template_yaml_hash
from ymir.artifacts.store import ArtifactStore
from ymir.artifacts.versioning import compute_content_hash
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
from ymir.templates import DEFAULT_TEMPLATES_DIR, load_template
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

#: 从**模板**补进冒烟的命令数上限（v1.3.1 T4.1，docs/13 §7.3-6）。
#:
#: ★ 与 :data:`_SMOKE_MAX_COMMANDS` **独立**，不许合并：后者是"模型自报命令"的
#: 防滥用上限（5，语义 = 模型可能胡报一堆），本常量限制的是"我们从模板补进来的
#: 确定性命令"（2，语义 = 只补构建/编译这类必须验的）。两者的取值理由完全不同。
_SMOKE_MAX_TEMPLATE_COMMANDS = 2

#: 从模板补进冒烟时只取的检查组（``group``）。
#:
#: 为什么只取 ``frontend_build`` 而不把整个 ``checks`` 都补进来：
#: 其它组（backend / frontend_static …）要么与评测的确定性检查重复（重复执行
#: 只是白等），要么代价高、副作用不明；``frontend_build`` 恰恰是**评测侧看不到**
#: 的那一块 —— QA 证据链里缺"前端到底能不能构建"，W4 就是这么误判的。
_SMOKE_TEMPLATE_GROUP = "frontend_build"

#: ``requires`` 探测命令的超时（秒）：``command -v`` 是纯内存操作，给 10s 足够。
_SMOKE_REQUIRES_PROBE_TIMEOUT = 10.0

#: 冒烟报告文件名（与 ``evaluation``/``roles`` 侧读取的字面量同源）。
_SMOKE_REPORT_NAME = "smoke_report.json"

#: 「基线已知事实」段服务的角色（v1.3.1 T4.2）。
#:
#: 与 ``roles.prompts._BASELINE_FACT_ROLES`` **同值但不同职责**：这一份决定
#: "编排层要不要去取事实"，那一份决定"渲染层要不要输出该段"。刻意不跨模块
#: 复用同一个常量 —— 取数与渲染是两个独立开关，绑在一起会让"只改渲染范围"
#: 变成"顺手改了取数范围"。
_BASELINE_FACT_ROLE = "qa"

#: 事实的数据来源产物名（本 stage 的上游 ``code``）。
_BASELINE_FACT_SOURCE_ARTIFACT = "code"

#: 冒烟事实摘要的字符上限（与 ``roles.prompts._MAX_SMOKE_SUMMARY_CHARS`` 同源）。
_BASELINE_FACT_SMOKE_CHARS = 400

#: 自动修复消息的来源标识（与 ``RepairController`` 发出的 ``payload.source`` 一致）
_AUTO_REPAIR_SOURCE = "auto_repair"

#: 失败检查 output 摘要的缺省总长上限（与 ``RepairContextSpec`` 缺省一致）
_DEFAULT_DETAIL_CHARS = 1200

#: 模板降级原因前缀（r5 硬要求：必须落进 ``run.context["template"]["degraded"]``）
_TEMPLATE_DEGRADED_NOT_FOUND = "template_not_found"
_TEMPLATE_DEGRADED_INVALID = "template_invalid"

#: 降级原因里的异常摘要长度上限（JSON 列不留长栈，tooltip 够读即可）
_TEMPLATE_DEGRADED_DETAIL_CHARS = 200

#: 受保护路径命中后的**定向重试**上限（v1.3 Q1 第二段：恰好 1 次，不多不少）。
PROTECTED_RETRY_MAX: int = 1

#: 兜底 ``ignore`` 重合时的告警短码前缀（v1.3 §7.5-1）。
#:
#: 与 ``merge.PROTECTED_HIT_WARNING_PREFIX``（模板本就声明 ``ignore``）区分开：
#: 本前缀表示"**reject 撞了两次墙后被迫降级**"，T5.1 可据此量化重试命中率。
PROTECTED_FORCED_IGNORE_PREFIX: str = "protected_path_forced_ignore:"


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
        # v1.3.1（T4.2）：**仅 qa** 补「基线已知事实」（取数在编排层、渲染在 prompts）。
        # 放在 runtime.execute 之前（QA 要拿它组提示词），失败只告警不阻断。
        if stage.role == _BASELINE_FACT_ROLE:
            stage_input = replace(
                stage_input,
                baseline=await self._baseline_facts(run, stage_input.baseline, stage_input),
            )
        output = await self._runtime.execute(
            stage.role, stage_input, run_id=run.id, stage_id=stage.id
        )

        # ---- 阶段一（合法化）+ 阶段二（合入）：骨架注入 / patch 契约 ----------
        # 顺序是硬要求（docs/09 §4.1.3 S1）：runtime.execute 之后、smoke 之前、save 之前。
        # 无模板且无基线时整段跳过 ⇒ v1.1 路径逐字节不变。
        output, merged, protected_retry = await self._execute_with_patch_contract(
            run=run,
            stage=stage,
            stage_input=stage_input,
            output=output,
            template_spec=template_spec,
            base_files=base_files,
        )
        smoke_files = merged.files if merged is not None else output.files

        smoke: dict[str, Any] | None = None
        if stage.use_sandbox and self._sandbox_service is not None:
            smoke = await self._run_sandbox_smoke(
                run, output, files=smoke_files, template_spec=template_spec
            )
            output.files[_SMOKE_REPORT_NAME] = (
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
                # ---- v1.3 T1.3 / T1.1：纯增量键（旧读取方全部忽略即可）----
                "change_ratio": merged.manifest.change_ratio,
                "warnings": list(merged.manifest.warnings),
                "protected_hits": merged.manifest.counts.protected_hits,
            }
            if protected_retry is not None:
                # 仅在发生过"reject -> 重试"时出现（T5.1 据此量化重试命中率）
                payload["patch"]["protected_retry"] = protected_retry
            if merged.symbol_removals:
                # v1.3.1 T3.2：符号删除被拦下的明细（≤10 条），审批卡片据此告诉
                # 人工"到底哪几个公共符号没了"，而不是只给一个数字。
                payload["patch"]["symbol_removals"] = [
                    {"path": item.path, "symbols": list(item.symbols)}
                    for item in merged.symbol_removals[:10]
                ]

        return StageOutcome(
            output_refs=[saved.ref()],
            summary=output.summary,
            payload=payload,
            changed_paths=(
                tuple(merged.manifest.changed_paths) if merged is not None else ()
            ),
        )

    # ------------------------------------------------------------------ patch 契约

    async def _execute_with_patch_contract(
        self,
        *,
        run: PipelineRun,
        stage: StageSpec,
        stage_input: StageInput,
        output: StageOutput,
        template_spec: TemplateSpec | None,
        base_files: dict[str, str],
    ) -> tuple[StageOutput, MergeResult | None, dict[str, Any] | None]:
        """阶段一/二合并 + 受保护路径**三段式**处置（v1.3 Q1，docs/11 §7.3-4）。

        ① **首次**：按模板策略合并（``reject`` 为默认）——命中即抛
           :class:`ProtectedPathConflict`；
        ② **定向重试 1 次**：把「受保护路径清单 + 请改用新文件」经
           ``StageInput.feedback`` 回喂模型（复用既有反馈通道，不新增提示词通道），
           重新 ``runtime.execute``；两次调用的成本 / tokens **累加**到返回的
           ``StageOutput``（成本不漏计）；
        ③ **零成本兜底**：重试仍冲突 ⇒ 对**第二次产出**以 ``ignore`` 再合一次
           （纯函数、不调模型），manifest 记 ``protected_path_forced_ignore:<n>``，
           阶段继续 —— **不整轮失败**（T5.1 DoD ②）。

        ★ v1.3.1（T3.2 / M3）：「符号删除」冲突走**同一套**三段式（
        :class:`SymbolRegressionConflict` 继承 :class:`ProtectedPathConflict`，
        上面那个 ``except`` 零改动复用），但第三段必须**同时**把
        ``symbol_deletions`` 从 ``reject`` 降为 ``ignore`` —— 否则第二次
        ``_apply_patch_contract` 会再次抛出且无人捕获。回喂文案按 ``reason``
        分支（:func:`_protected_feedback`，M2）：删了符号却被告知"请新建文件"
        会把模型推向"新建空壳 + 入口留空"，比不拦更糟。

        Returns:
            ``(stage_output, merge_result, protected_retry)``。
            ``protected_retry`` 仅在发生过重试时非 ``None``（进 payload，
            供 T5.1 量化重试命中率）。

        Note:
            **偏离 §7.3-4 的一处**：该函数的返回多带了第三项 ``protected_retry``
            （设计原文为 2 元组）。原因：payload 的 ``protected_retry`` 键需要
            "是否重试过 + 命中路径"，而 :class:`MergeResult` 是 frozen 且不含该信息，
            用 manifest 短码反推只能得到"被迫降级"，拿不到"重试成功"这一支。
            这是**私有**方法（无外部调用点），改动面为零。
        """
        if template_spec is None and not base_files:
            return output, None, None

        policy: str = template_spec.protected.on_hit if template_spec is not None else "reject"
        #: 符号删除策略（v1.3.1 T3.2）。缺省 ``off`` ⇒ 与 v1.3 行为完全一致。
        symbol_policy: str = (
            template_spec.protected.symbol_deletions if template_spec is not None else "off"
        )
        try:
            merged = self._apply_patch_contract(
                output,
                template_spec=template_spec,
                base_files=base_files,
                on_protected_hit=policy,
                symbol_deletions=symbol_policy,
            )
            _ensure_manifest_synced(output, merged)
            self._attach_provenance(
                run=run,
                output=output,
                merged=merged,
                base_files=base_files,
                template_spec=template_spec,
                protected_policy=policy,
                symbol_policy=symbol_policy,
            )
            return output, merged, None
        except ProtectedPathConflict as conflict:
            first_conflict = conflict

        # ---- 第二段：定向重试 1 次（复用 feedback 通道回喂纠正指令）----
        logger.warning(
            "受保护路径冲突，发起 %s 次定向重试：run=%s stage=%s paths=%s",
            PROTECTED_RETRY_MAX,
            run.id,
            stage.id,
            list(first_conflict.paths),
        )
        retry_input = replace(
            stage_input,
            feedback=[_protected_feedback(first_conflict), *stage_input.feedback],
            attempt=stage_input.attempt + 1,
        )
        retry_output = await self._runtime.execute(
            stage.role, retry_input, run_id=run.id, stage_id=stage.id
        )
        combined = _accumulate_outputs(output, retry_output)
        retry_info: dict[str, Any] = {
            "paths": list(first_conflict.paths),
            "attempts": PROTECTED_RETRY_MAX,
        }
        try:
            merged = self._apply_patch_contract(
                combined,
                template_spec=template_spec,
                base_files=base_files,
                on_protected_hit="reject",
                symbol_deletions=symbol_policy,
            )
            _ensure_manifest_synced(combined, merged)
            # ★ 第二段的生效策略是「模板声明值 + on_protected_hit 被写死成 reject」，
            #   与第一段可能不同（第一段若模板声明 ignore 就不会进到这里）——
            #   因此这里显式传 "reject"，不复制第一段的变量。
            self._attach_provenance(
                run=run,
                output=combined,
                merged=merged,
                base_files=base_files,
                template_spec=template_spec,
                protected_policy="reject",
                symbol_policy=symbol_policy,
            )
            return combined, merged, retry_info
        except ProtectedPathConflict as second_conflict:
            # ---- 第三段：零成本兜底（ignore 再合一次，纯函数不调模型）----
            logger.warning(
                "重试后仍命中受保护路径，按 ignore 兜底合并（保留基线版本，不整轮失败）："
                "run=%s stage=%s paths=%s",
                run.id,
                stage.id,
                list(second_conflict.paths),
            )
            # ★ M3（v1.3.1）：第三段必须**同时降级两个策略** —— 只把
            # ``on_protected_hit`` 降成 ``ignore`` 而 ``symbol_deletions`` 仍是
            # ``reject``，第二次 ``_apply_patch_contract`` 会**再次**抛
            # ``SymbolRegressionConflict`` 且此处无人捕获 ⇒ 阶段异常。
            # 只有声明 ``reject`` 才降级：声明 ``off`` 时**不得**擅自开启检测
            # （否则"未配置 ⇒ 行为与 v1.3 一致"立刻破功）。
            fallback_symbols = "ignore" if symbol_policy == "reject" else symbol_policy
            merged = self._apply_patch_contract(
                combined,
                template_spec=template_spec,
                base_files=base_files,
                on_protected_hit="ignore",
                symbol_deletions=fallback_symbols,
            )
            # ★ P3（v1.3.1 QA）：forced 短码里的条数必须把**两类降级都算上**。
            # 只取 ``protected_hits`` 时，若第三段是因**符号删除**兜底的，会写出
            # ``protected_path_forced_ignore:0`` —— 读起来像"根本没降级"，还白占
            # ``_bounded_warnings`` 三个名额之一（实测过
            # ``['protected_path_forced_ignore:0', 'symbol_regression_ignored:1']``）。
            # 用**相加**而不是 ``or``：两者可能同时非零（protected 命中的路径被 S4
            # 短路，但符号命中可能落在**别的**路径上），``or`` 会漏计。
            # v1.3 里 ``symbol_removals`` 恒为 0 ⇒ 退化成 ``protected_hits``
            # ⇒ 既有 manifest **逐字节不变**（短码文案/标签不动，前端按原样字符串消费）。
            hits = merged.manifest.counts.protected_hits + merged.manifest.counts.symbol_removals
            # P3-4（QA）：forced 短码**前置**（它是最重要的降级信号，不能被
            # 先到的 change_ratio 短码挤掉），并统一走 ``_bounded_warnings`` 裁剪
            # （≤3 条 / ≤64 字节）—— 手写 ``[:3]`` 在将来加第 3 类短码时会静默截断。
            merged.manifest.warnings = _bounded_warnings(
                [
                    f"{PROTECTED_FORCED_IGNORE_PREFIX}{hits}",
                    *merged.manifest.warnings,
                ]
            )
            # ★★ F1（QA P2）：`_apply_patch_contract` 返回前已把 manifest 序列化进
            # `output.files`，上面的 warnings 追加只改了内存对象。必须**重新序列化回写**，
            # 否则「内存说有 forced_ignore、落盘没有」——产物与报告不一致且无法追溯。
            _reserialize_manifest(combined, merged)
            # P3-3（QA）：把"改完记得回写"变成**机制**——返回前强制核对
            _ensure_manifest_synced(combined, merged)
            # ★ 第三段的生效策略是**运行时降级后**的值（ignore），不是模板声明值 ——
            #   W6 重建若用声明值，要么重新抛冲突、要么把当时被丢弃的模型内容纳入
            #   ⇒ ``merged_files_hash`` 必然不符（M3）。
            self._attach_provenance(
                run=run,
                output=combined,
                merged=merged,
                base_files=base_files,
                template_spec=template_spec,
                protected_policy="ignore",
                symbol_policy=fallback_symbols,
            )
            retry_info["forced_ignore"] = list(second_conflict.paths)
            return combined, merged, retry_info

    def _apply_patch_contract(
        self,
        output: StageOutput,
        *,
        template_spec: TemplateSpec | None,
        base_files: dict[str, str],
        on_protected_hit: str = "reject",
        symbol_deletions: str = "off",
    ) -> MergeResult | None:
        """阶段一/阶段二合并（`docs/09 §4.1.3 S1 + §4.2.2`）。

        - **阶段一（合法化）**：``merge_trees(base=基线正文, patch=模型输出, mode=patch)``
          —— 传基线**正文**（不是 ``{}``）才能逐字节判定 noop；骨架按优先级表注入；
        - **阶段二（合入）**：``out1.files`` 即整棵交付树，冒烟用它、评测也用它；
        - **产物只存增量**：由 ``manifest.changed_paths`` 反推，而不是"以为传了 ``{}``
          就只留了增量"（``out1.files`` 是整树，直接落盘会把基线全文写进产物）。

        无模板且无基线 → 返回 ``None``，``output.files`` 一字不改（v1.1 路径）。

        v1.3（T1.1）：``protected_paths`` **只在 ``patch`` 模式（有基线正文）生效** ——
        ``from_scratch`` 时模型创建 ``tests/conftest.py`` 是**必需的**，若也拦，
        产物会缺掉夹具文件（回归防线无从建立）。这是本实现相对设计的一处**必要澄清**。

        v1.3.1（T3.2）：``symbol_deletions`` 与 ``protected_paths`` **同一条边界** ——
        ``from_scratch``（无基线）时**不进入**检测（``merge_trees`` 侧的条件是
        ``base_norm`` 非空，这里再传 ``off`` 是为了让调用点不依赖那个隐式条件）。
        """
        if template_spec is None and not base_files:
            return None

        skeleton = template_spec.skeleton_files() if template_spec is not None else None
        reserved = (
            template_spec.reserved_paths() if template_spec is not None else frozenset()
        )
        protected = (
            template_spec.protected_paths()
            if template_spec is not None and base_files
            else frozenset()
        )
        max_change_ratio = (
            template_spec.protected.max_change_ratio
            if template_spec is not None
            else DEFAULT_MAX_CHANGE_RATIO
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
            protected_paths=protected,
            on_protected_hit=on_protected_hit,  # type: ignore[arg-type]
            max_change_ratio=max_change_ratio,
            symbol_deletions=symbol_deletions if base_files else "off",  # type: ignore[arg-type]
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

    def _attach_provenance(
        self,
        *,
        run: PipelineRun,
        output: StageOutput,
        merged: MergeResult,
        base_files: dict[str, str],
        template_spec: TemplateSpec | None,
        protected_policy: str,
        symbol_policy: str,
    ) -> MergeProvenance | None:
        """把合入溯源写进 ``manifest`` 并**回写落盘正文**（v1.3.1 T5.1）。

        写入时机（容易错的那一条）：本方法在 ``_execute_with_patch_contract``
        **返回之前**调用，即**冒烟之前** —— 那时 ``smoke_report.json`` 还不存在，
        与 :func:`ymir.artifacts.provenance.strip_bookkeeping` 的剔除口径严格同源
        （docs/13 §7.3-7 6c）。若改到冒烟之后写，``patch_files_hash`` 会多算一个
        文件，W6 重建时 ``merged_files_hash`` 必不符。

        ★ 写完必须 :func:`_reserialize_manifest` + :func:`_ensure_manifest_synced`：
        ``_apply_patch_contract`` 返回前已把 manifest 序列化进 ``output.files``，
        只改内存对象就会「内存有、落盘没有」（v1.3 的 F1 教训）。

        Args:
            protected_policy: 本次**实际传给** ``_apply_patch_contract`` 的值
                （①模板声明值 / ②``"reject"`` / ③``"ignore"``），不是模板声明值。
            symbol_policy: 同上（①模板声明值 / ②模板声明值 / ③兜底值）。

        Returns:
            写入的 :class:`MergeProvenance`；``from_scratch``（无基线 / 非 patch）
            返回 ``None``（产物里**不写**该键 ⇒ 与 v1.3 逐字节一致）。
        """
        prov = _build_provenance(
            run=run,
            output=output,
            merged=merged,
            base_files=base_files,
            template_spec=template_spec,
            protected_policy=protected_policy,
            symbol_policy=symbol_policy,
            max_file_bytes=self._max_patch_file_bytes,
        )
        if prov is None:
            return None
        merged.manifest.provenance = prov
        _reserialize_manifest(output, merged)
        _ensure_manifest_synced(output, merged)
        return prov

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

    async def _baseline_facts(
        self,
        run: PipelineRun,
        baseline: BaselineContext | None,
        stage_input: StageInput | None = None,
    ) -> BaselineContext | None:
        """给 QA 的基线上下文补三块**事实**（v1.3.1 T4.2，docs/13 §7.3-5）。

        失败只 ``logger.warning`` 并返回**原 baseline**，**绝不阻断阶段**
        （取证材料缺失不该让整轮 run 失败）。

        Args:
            run: 当前 run（仅用于日志）。
            baseline: 已加载的基线视图；``None``（无基线引用）⇒ 原样返回 ``None``
                —— 事实挂在 :class:`BaselineContext` 上，没有载体就不造一个。
            stage_input: 本 stage 的输入；**事实的来源**。``upstream`` 里那条
                ``code`` 就是 ``read_artifacts`` 为**本次**这一 stage 解析出的那一版。

        数据来源（都已在手，不新增读取通道）：
            - ``patch_changed_paths`` / ``skeleton_added`` ← 上述 ``code`` 产物的
              ``patch_manifest.json``（``parse_patch_manifest``；缺失 / 坏 JSON ⇒ 空）；
            - ``smoke_summary`` ← **同一版本**产物里的 ``smoke_report.json``（缺失 ⇒ 空串）。

        ★★ **禁止取「该项目最新 ``code`` 产物」**：项目里可能已存在更晚的版本
        （重跑 / 并行 run / 人工补跑），取最新会把「别人那一版」的补丁清单与冒烟
        结果塞进本次 QA 的上下文 —— 事实与结论来自不同产物，正是 v1.3 F7/F8 的
        同一类错误。实现上这一点是**结构性保证**：本方法**根本不碰 ``self._store``**，
        事实只能来自 ``stage_input.upstream``；即使有人想"顺手查一下最新"也无从下手。
        """
        if baseline is None:
            return None
        try:
            artifact = _upstream_by_name(stage_input, _BASELINE_FACT_SOURCE_ARTIFACT)
            if artifact is None:
                # 没有上游 code 产物：三块事实都无从取，原样返回（渲染层据此不输出该段）
                return baseline
            files = dict(artifact.files)
            manifest = parse_patch_manifest(files.get(PATCH_MANIFEST_NAME))
            changed = tuple(manifest.changed_paths) if manifest is not None else ()
            skeleton = tuple(manifest.skeleton_added) if manifest is not None else ()
            summary = _summarize_smoke(files.get(_SMOKE_REPORT_NAME, ""))
            return replace(
                baseline,
                patch_changed_paths=changed,
                skeleton_added=skeleton,
                smoke_summary=summary,
            )
        except Exception:  # noqa: BLE001 - 取证材料缺失只降级，绝不阻断阶段
            logger.warning(
                "QA 基线事实填充失败，按无事实处理（run=%s stage_input=%s）",
                run.id,
                stage_input is not None,
                exc_info=True,
            )
            return baseline

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
        self,
        run: PipelineRun,
        output: StageOutput,
        *,
        files: dict[str, str] | None = None,
        template_spec: TemplateSpec | None = None,
    ) -> dict[str, Any]:
        """在沙箱内执行代码产物的自测命令（尽力而为：失败仅记录，不阻断阶段）。

        ``files`` 缺省为 ``output.files``；**增量/模板模式下必须传合并后的整树**
        （docs/09 §4.2.2 / §4.1.3）：只写补丁会让 ``import`` 链缺基线文件，
        冒烟必然 ``ModuleNotFoundError``，那是假失败。

        v1.3.1（T4.1，docs/13 §7.3-6）：``template_spec`` 非空时**追加模板的
        ``frontend_build`` 组命令**（≤2 条，与模型自报命令按字符串去重）。原因见
        :data:`_SMOKE_TEMPLATE_GROUP`：那块结果评测侧看不到，QA 证据链会缺
        "前端到底能不能构建"。

        ★ 三条硬纪律：

        1. **只有模板命令带 ``"source": "template"``**，模型命令的条目结构**一字不动**
           ⇒ 无模板时 ``smoke_report.json`` 与 v1.3 **逐字节一致**（红线 A9）；
        2. ``requires`` 缺失 ⇒ 记 ``skipped`` + ``reason="requires_missing:<bin>"``，
           **不执行、零等待**（不烧 300s 的构建超时）；
        3. ``all_ok`` **三态**（M6）：``executed`` 为空时必须是 ``None`` 而不是
           ``True`` —— ``all(空集)`` 恒为 ``True``，报告会显示"冒烟全过"而实际
           一条没跑（无 node 镜像 + 只有 ``frontend_build`` 组时必然踩到）。
        """
        tree = output.files if files is None else files
        commands = [
            str(cmd).strip()
            for cmd in output.structured.get("run_commands", [])
            if str(cmd).strip()
        ][:_SMOKE_MAX_COMMANDS]
        template_checks = _smoke_template_checks(template_spec)
        report: dict[str, Any] = {
            "backend": getattr(self._sandbox_service.sandbox, "backend", "unknown"),  # type: ignore[union-attr]
            "network": str(self._sandbox_network),
            "executed_at": datetime.now(UTC).isoformat(),
            "commands": [],
            "all_ok": None,
            "error": None,
        }
        if not commands and not template_checks:
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
            # ---- v1.3.1：模板命令（去重后追加；条目多一个 source 字段）----
            for check in template_checks:
                command = str(check.run or "").strip()
                if command in commands:
                    continue  # 模型已经自报过同一条：不重复执行（省时且避免噪音）
                report["commands"].append(
                    await self._run_smoke_template_check(session, check)
                )
            executed = [item for item in report["commands"] if not item.get("skipped")]
            if executed:
                report["all_ok"] = all(
                    item.get("exit_code") == 0 and not item.get("timed_out")
                    for item in executed
                )
            else:
                # ★ M6：**绝不**让 ``all(空集) == True`` 冒充"全部通过"
                report["all_ok"] = None
                report["error"] = "skipped: 全部命令因环境缺失跳过"
        finally:
            try:
                await self._sandbox_service.destroy(managed)
            except Exception:  # noqa: BLE001 - 销毁失败仅告警
                logger.exception("沙箱会话销毁失败（run %s）", run.id)
        return report

    async def _run_smoke_template_check(
        self, session: Any, check: Any
    ) -> dict[str, Any]:
        """执行一条模板命令，返回冒烟报告条目（**唯一**带 ``source`` 的形态）。

        ``requires`` 非空 ⇒ 先 ``command -v <bin>`` 探测：非 0 即记 ``skipped``，
        **不执行主命令**（否则要白等满 ``timeout_s``，构建类检查是 300s）。
        """
        command = str(check.run or "").strip()
        timeout = (
            float(check.timeout_s) if check.timeout_s else float(self._exec_timeout)
        )
        requires = str(check.requires or "").strip()
        if requires:
            available = False
            try:
                probe = await session.exec(
                    f"command -v {requires}", timeout=_SMOKE_REQUIRES_PROBE_TIMEOUT
                )
                available = probe.exit_code == 0 and not probe.timed_out
            except Exception:  # noqa: BLE001 - 探测失败按"环境缺失"处置（不执行）
                logger.warning("冒烟 requires 探测失败，按缺失跳过（%s）", requires)
            if not available:
                return {
                    "command": command,
                    "source": "template",
                    "skipped": True,
                    "reason": f"requires_missing:{requires}",
                }
        try:
            result = await session.exec(command, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 单命令异常同样记录，不阻断
            return {
                "command": command,
                "source": "template",
                "error": str(exc),
                "exit_code": None,
            }
        return {
            "command": command,
            "source": "template",
            "stdout": result.stdout[:_SMOKE_STREAM_LIMIT],
            "stderr": result.stderr[:_SMOKE_STREAM_LIMIT],
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
        }


# --------------------------------------------------------------------- 消息解包工具


def _upstream_by_name(
    stage_input: StageInput | None, name: str
) -> UpstreamArtifact | None:
    """取本 stage 输入里名为 ``name`` 的**已完成解析**的上游产物（无则 ``None``）。

    ★ 这是「事实只取本 stage 那一版」的唯一入口：``stage_input.upstream`` 由
    ``_collect_input`` 的 ``read_artifacts`` 一次性解析，之后不再变动。
    **绝不去 store 查"最新"** —— 那会把别人那一版的事实掺进本次 QA。
    """
    if stage_input is None:
        return None
    for artifact in stage_input.upstream:
        if artifact.name == name:
            return artifact
    return None


def _summarize_smoke(raw: str) -> str:
    """把 ``smoke_report.json`` 压成一行**事实摘要**（只给数字与首个失败命令）。

    形如 ``命令 2 条 / exit=0 1 条 / 首个非 0 命令：`fe-build web```
    （有跳过时追加 `` / 其中 1 条因环境缺失跳过``）。

    ★ 只给事实、不给判词：不写「通过 / 不通过」，判定由 QA 自己下。
    ★ 缺失 / 坏 JSON / 无命令 ⇒ 返回空串（渲染层显示「（无冒烟记录）」），绝不抛。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    commands = payload.get("commands")
    if not isinstance(commands, list) or not commands:
        return ""
    items = [item for item in commands if isinstance(item, dict)]
    skipped = sum(1 for item in items if item.get("skipped"))
    executed = [item for item in items if not item.get("skipped")]
    passed = sum(
        1 for item in executed if item.get("exit_code") == 0 and not item.get("timed_out")
    )
    first_bad = ""
    for item in executed:
        if item.get("exit_code") != 0 or item.get("timed_out"):
            first_bad = str(item.get("command") or "").strip()
            break
    if first_bad:
        bad_text = f"`{first_bad}`"
    elif not executed:
        bad_text = "无（全部跳过）"
    else:
        bad_text = "无"
    parts = [f"命令 {len(items)} 条", f"exit=0 {passed} 条", f"首个非 0 命令：{bad_text}"]
    if skipped:
        parts.append(f"其中 {skipped} 条因环境缺失跳过")
    summary = " / ".join(parts)
    if len(summary) > _BASELINE_FACT_SMOKE_CHARS:
        summary = summary[:_BASELINE_FACT_SMOKE_CHARS] + "…（已截断）"
    return summary


def _smoke_template_checks(template_spec: TemplateSpec | None) -> list[Any]:
    """挑出要从模板补进冒烟的检查（``frontend_build`` 组，≤2 条，自身去重）。

    Args:
        template_spec: 模板定义；``None``（无模板 / 通用模式）⇒ 返回空清单 ——
            这是「无模板时 ``smoke_report.json`` 与 v1.3 逐字节一致」的入口条件。

    Returns:
        ``CheckSpec`` 列表（保序）：只取 ``group == _SMOKE_TEMPLATE_GROUP`` 且
        ``run`` 非空的条目，按命令字符串去重，最多 :data:`_SMOKE_MAX_TEMPLATE_COMMANDS` 条。

    Note:
        与模型自报命令的去重在调用方做（那里才有模型命令清单）；本函数只负责
        "模板内部不重复"，两个层次分开，避免把两处状态揉成一个参数。
    """
    if template_spec is None:
        return []
    picked: list[Any] = []
    seen: set[str] = set()
    for check in template_spec.checks:
        if str(getattr(check, "group", "") or "").strip() != _SMOKE_TEMPLATE_GROUP:
            continue
        command = str(getattr(check, "run", "") or "").strip()
        if not command or command in seen:
            continue
        seen.add(command)
        picked.append(check)
        if len(picked) >= _SMOKE_MAX_TEMPLATE_COMMANDS:
            break
    return picked


#: 回喂模型：命中受保护路径的纠正指令（v1.3 Q1 第二段）。
#:
#: ★ **逐字节冻结**：v1.3 的 38 条文案断言都钉在这段字面量上。v1.3.1 只允许
#: 在它**旁边**加新模板（``_SYMBOL_REGRESSION_FEEDBACK``），不得改这里的任何一个字。
_LEGACY_PROTECTED_FEEDBACK: str = (
    "受保护路径不可修改（{paths}）：这些文件由基线提供且必须保持原样，"
    "本次产出中对它们的改动已被服务端拒绝。\n"
    "请改为新建文件实现你的目标（例如 `tests/test_<新功能>.py` 或新的实现模块），"
    "不要重写、不要删除 {paths}；如需新增夹具，请在你新建的文件里定义。"
)

#: 回喂模型：删掉仍被引用的公共符号的纠正指令（v1.3.1 T3.2 / M2）。
#:
#: ★ 与上面那段**必须不同**：删了 ``run`` 却被告知"请改为新建文件"，模型会照做 ——
#: 把子命令搬到新文件、入口留空壳（真实事故就是这么发生的），比不拦更糟。
#: 因此本段必须给出**真正可行的两条路**：恢复符号，或同步改完所有引用。
#: 且**不得出现「受保护路径」字样**（用例断言）：那会让模型以为整个文件不能碰。
_SYMBOL_REGRESSION_FEEDBACK: str = (
    "删除的公共符号仍被其它文件引用（{paths}）：这些文件里的公共函数/类被"
    "未改动的文件 import 或调用，本次产出中对它们的删除已被服务端拒绝。\n"
    "请二选一：① **恢复这些符号**（保留原有函数/类及其签名），"
    "或 ② 同步更新所有引用它们的文件后再删除。\n"
    "不要靠新建空壳文件、改名或把实现搬到别处绕过 —— 引用方仍会 ImportError。"
)


def _protected_feedback(conflict: ProtectedPathConflict) -> str:
    """回喂模型的纠正指令（v1.3 Q1 第二段，docs/11 §7.3-4；v1.3.1 T3.2 按 reason 分支）。

    复用 :attr:`StageInput.feedback` 通道（``prompts.build_user`` 渲染成「历史反馈」节），
    **不新增提示词通道、不改 ``RoleRuntime.execute`` 签名**（B4）。

    文案纪律（R1 ②）：不只说"禁止"，必须**给出替代路径**，否则模型只会换个写法继续撞。

    ★ M2（v1.3.1）：**必须按 ``conflict.reason`` 分支**。v1.3 的实现只拼
    ``conflict.paths``，固定输出「受保护路径不可修改…请改为新建文件」，
    **完全不读** ``message`` —— 对"符号回归"用同一段文案是错误的引导（见
    :data:`_SYMBOL_REGRESSION_FEEDBACK` 的说明）。
    ``protected_path`` 分支仍走 :data:`_LEGACY_PROTECTED_FEEDBACK`，**逐字节不变**。
    """
    paths = "、".join(conflict.paths) or "（受保护路径）"
    if conflict.reason == "symbol_regression":
        return _SYMBOL_REGRESSION_FEEDBACK.format(paths=paths)
    return _LEGACY_PROTECTED_FEEDBACK.format(paths=paths)


def _build_provenance(
    *,
    run: PipelineRun,
    output: StageOutput,
    merged: MergeResult,
    base_files: dict[str, str],
    template_spec: TemplateSpec | None,
    protected_policy: str,
    symbol_policy: str,
    max_file_bytes: int,
) -> MergeProvenance | None:
    """算出本次合入的溯源（v1.3.1 T5.1，docs/13 §7.3-7）。纯函数（除模板哈希读盘）。

    ``from_scratch`` / 无基线 ⇒ ``None``：产物里不写 ``provenance`` 键
    （``manifest_json`` 会 pop），与 v1.3 逐字节一致（A9）。判据用
    ``mode == "patch"``：它是 ``merge_trees`` 对所有"有基线"场景的唯一取值，
    比"``base_files`` 非空"更贴近落盘事实。

    ★ 两个内容哈希都过 :func:`strip_bookkeeping`（剔除 ``patch_manifest.json`` /
    ``smoke_report.json``）：前者是产物自身的簿记、后者此刻尚未生成，二者都不是补丁
    内容；把 ``patch_manifest.json`` 算进去还会造成"哈希依赖自身"的不动点问题。
    """
    if merged.manifest.mode != "patch" or not base_files:
        return None
    context = run.context if isinstance(run.context, dict) else {}
    ref = context.get("baseline")
    ref_dict = ref if isinstance(ref, dict) else {}
    raw_version = ref_dict.get("version")
    template_id = (
        str(getattr(template_spec, "id", "") or "") if template_spec is not None else ""
    )
    return MergeProvenance(
        base_artifact_id=str(ref_dict.get("artifact_id") or "").strip(),
        base_version=raw_version if isinstance(raw_version, int) else None,
        base_content_hash=merged.manifest.base_content_hash,
        patch_files_hash=compute_content_hash(strip_bookkeeping(output.files)),
        template_id=template_id,
        template_hash=template_yaml_hash(DEFAULT_TEMPLATES_DIR, template_id),
        merged_files_hash=compute_content_hash(strip_bookkeeping(merged.files)),
        protected_policy=protected_policy,
        symbol_policy=symbol_policy,
        max_file_bytes=max_file_bytes,
    )


def _reserialize_manifest(output: StageOutput, merged: MergeResult) -> None:
    """把内存里的 ``manifest`` **重新序列化**回 ``output.files``（F1，QA P2）。

    只在 ``_apply_patch_contract`` 已经写过簿记文件（``mode == "patch"``）时生效：
    ``from_scratch`` 产物**不该**带 ``patch_manifest.json``（docs/09 §9-T2.4 DoD ⑨），
    此处不新造该文件。

    纪律：**任何对 ``merged.manifest`` 的事后修改都必须跟一次本调用**，
    否则落盘正文落后于内存对象（最典型的坑就是第三段兜底追加短码）。
    """
    if PATCH_MANIFEST_NAME not in output.files:
        return
    output.files[PATCH_MANIFEST_NAME] = merged.manifest_json()


def _ensure_manifest_synced(output: StageOutput, merged: MergeResult) -> None:
    """返回前强制核对「内存 manifest == 落盘正文」（P3-3，QA）。

    只在簿记文件存在时生效（``from_scratch`` 不写 manifest）。**不匹配即抛**：
    产物里的 `patch_manifest.json` 一旦落后于内存对象，下游（评测 / 审批卡片 /
    基线表）看到的告警与冲突就与实际合并结果不一致，且没有任何报错 —— 那是最难查的
    一类缺陷。把检查放在唯一的出口，比"记得在每次改 manifest 后调 _reserialize"可靠。

    ★ 用显式 ``raise`` 而不是 ``assert``：``python -O`` 会剥离断言（QA-08 既有结论），
    机制不能在优化模式下失效。
    """
    if PATCH_MANIFEST_NAME not in output.files:
        return
    expected = merged.manifest_json()
    actual = output.files[PATCH_MANIFEST_NAME]
    if actual != expected:
        raise RuntimeError(
            "patch_manifest.json 与内存 manifest 不一致（写盘落后于内存）："
            f"expected={expected[:120]}… actual={actual[:120]}…"
        )


def _accumulate_outputs(base: StageOutput, retry: StageOutput) -> StageOutput:
    """把「首次产出」与「重试产出」合成一个 ``StageOutput``（成本不漏计）。

    - **文件 / 摘要 / 结构化数据**取重试那次（它才是修正后的结果）；
    - **成本 / tokens / schema 尝试次数**两次相加（两次都真的花了钱）。
    """
    return StageOutput(
        files=retry.files,
        summary=retry.summary,
        structured=retry.structured,
        provider=retry.provider or base.provider,
        model=retry.model or base.model,
        tier=retry.tier or base.tier,
        prompt_tokens=base.prompt_tokens + retry.prompt_tokens,
        completion_tokens=base.completion_tokens + retry.completion_tokens,
        cost_cny=round(base.cost_cny + retry.cost_cny, 6),
        schema_attempts=base.schema_attempts + retry.schema_attempts,
    )


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
