"""评测 harness（架构设计 §3.7、§12.2；docs/07 §4.1 增量）：四维评估的编排层。

``run_suite(suite_id, project_id=...)`` 批量 / ``evaluate_task(task, project_id=...)`` 单任务：

1. 加载套件（``EvalSuite.load``）与待评产物（``ArtifactStore``）；
2. 每个 task 独立沙箱会话（保证隔离与可复现），把产物写入工作区；
3. 四维评估器并行调度（``asyncio.gather``）；
4. :func:`~ymir.evaluation.aggregator.aggregate` 加权总分 + 双门槛 + 证据链。

绑定语义（docs/07 §4.1.2 唯一权威表）：

- ``binding_mode="single"``（缺省，v1.0）：用 ``run_suite(project_id=...)`` 的项目，
  ``artifact_name`` 未命中时回退 ``code``（保持 v1.0 行为逐字节一致）；
- ``binding_mode="per_task"``（基准套件）：用任务自己的 ``project_id``，**永不回退**
  到其它项目、**永不回退** ``_DEFAULT_ARTIFACT``；未绑定 / 产物缺失的任务
  直接短路为 ``status="skipped"``（不启沙箱、不调 judge、``cost_cny=0``）。

沙箱经 :class:`SandboxProvider` 抽象注入：单测注入假 provider（0 Docker、0 费用）；
judge 经 :class:`~ymir.evaluation.evaluators.base.Judge` 抽象注入。
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol

from ymir.artifacts.merge import (
    PATCH_MANIFEST_NAME,
    merge_trees,
    parse_patch_manifest,
)
from ymir.artifacts.store import BASELINE_TYPE
from ymir.config import Settings
from ymir.evaluation.aggregator import aggregate
from ymir.evaluation.baseline_probe import (
    BaselineProbe,
    BaselineProbeCache,
    baseline_cache_key,
    probe_baseline,
)
from ymir.evaluation.evaluators import default_evaluators
from ymir.evaluation.evaluators.base import (
    DIMENSION_ORDER,
    EvalContext,
    EvalScore,
    EvalTask,
    Evaluator,
    Evidence,
    Judge,
    LLMJudge,
)
from ymir.evaluation.report import (
    SKIP_MISSING_ARTIFACT,
    SKIP_UNBOUND_TASK,
    STATUS_EVALUATED,
    BaselineCheckOut,
    BaselineProbeOut,
    DimensionOut,
    EvaluationReport,
    SkippedCheckOut,
    TaskOut,
)
from ymir.evaluation.suite import EvalSuite
from ymir.sandbox.base import NetworkPolicy, Sandbox, SandboxSession
from ymir.sandbox.factory import build_sandbox
from ymir.storage.repositories import RunRepository

logger = logging.getLogger(__name__)

_DEFAULT_ARTIFACT = "code"


class SandboxProvider(Protocol):
    """沙箱会话提供者（异步上下文，进入即启动、退出即销毁）。"""

    def session(self) -> Any:
        """返回异步上下文管理器，产出 :class:`SandboxSession`。"""
        ...


#: 增量模式下「可作参考产物」的体量上限（超限不进 references，docs/09 §4.2.5 C9/R3）。
#: 只在增量分支生效 —— 非增量路径的 references 组成必须与 v1.1 完全一致。
_REFERENCE_MAX_FILES: int = 200
_REFERENCE_MAX_BYTES: int = 1024 * 1024

#: 基线不可用时的报告警示文案（R9：必须让人看见"这次分数不可比"）。
BASELINE_UNAVAILABLE_WARNING: str = (
    "本次未使用基线（基线不可用），只评了补丁自身，分数与「基于基线」的运行不可比"
)

#: 基线自身就失败（T1.2 三态之二）：**按 unknown 降级、不阻断**，但必须披露。
BASELINE_UNHEALTHY_WARNING: str = (
    "基线自身检查未通过（基线不健康）：本次分数与「基于健康基线」的运行不可比"
)

#: 前端构建未执行（T2.4 降级路径；本批次无 frontend_build 组，属前向兼容常量）。
FRONTEND_BUILD_SKIPPED_WARNING: str = "前端构建未执行（环境缺失：node/npm 不可用）"

#: 前端构建组名（与模板 ``scoring.runnability_points`` 的键同口径）。
_FRONTEND_BUILD_GROUP: str = "frontend_build"


def _within_reference_budget(files: Mapping[str, str]) -> bool:
    """参考产物是否在预算内（文件数 / 总字节）。"""
    if len(files) > _REFERENCE_MAX_FILES:
        return False
    return sum(len(content.encode("utf-8")) for content in files.values()) <= (
        _REFERENCE_MAX_BYTES
    )


class BackendSandboxProvider:
    """默认 provider：包装一个沙箱后端（延迟构建，首次进入才探测后端）。"""

    def __init__(
        self,
        *,
        loader: Callable[[], Sandbox] | None = None,
        sandbox: Sandbox | None = None,
        network: NetworkPolicy = NetworkPolicy.RESTRICTED,
    ) -> None:
        self._sandbox = sandbox
        self._loader = loader
        self._network = network

    def _resolve(self) -> Sandbox:
        if self._sandbox is None:
            if self._loader is not None:
                self._sandbox = self._loader()
            else:
                self._sandbox = build_sandbox(Settings())
        return self._sandbox

    @asynccontextmanager
    async def session(self):  # noqa: ANN201
        sandbox = self._resolve()
        session = await sandbox.start(network=self._network)
        try:
            yield session
        finally:
            try:
                await sandbox.destroy(session)
            except Exception:  # noqa: BLE001 - 销毁失败不阻断评测结果
                logger.exception("评测沙箱会话销毁失败")


class EvaluationHarness:
    """评测编排：单任务 / 套件 / 项目级评测。"""

    def __init__(
        self,
        *,
        store: Any,
        evaluators: Sequence[Evaluator] | None = None,
        judge: Judge | None = None,
        sandbox_provider: SandboxProvider | None = None,
        exec_timeout: float = 120.0,
        session_factory: Any = None,
        events: Any = None,
    ) -> None:
        self._store = store
        self._evaluators: list[Evaluator] = list(evaluators) if evaluators else default_evaluators()
        self._judge = judge
        self._sandbox_provider = sandbox_provider
        self._exec_timeout = exec_timeout
        self._session_factory = session_factory
        # 可选事件总线注入（L6）：CLI/脚本带 run_id 时据此补发 evaluation.completed
        self._events = events
        #: 基线探测进程内 LRU（v1.3 T1.2，docs/11 §7.7-R2 ②）：
        #: 同一批次同一基线多次评测只付一次探测成本。
        self._probe_cache = BaselineProbeCache()

    @property
    def store(self) -> Any:
        return self._store

    def with_evaluators(self, evaluators: Sequence[Evaluator]) -> EvaluationHarness:
        """返回替换了评估器集合的新 harness（权重随套件变化时用）。"""
        clone = EvaluationHarness(
            store=self._store,
            evaluators=evaluators,
            judge=self._judge,
            sandbox_provider=self._sandbox_provider,
            exec_timeout=self._exec_timeout,
            session_factory=self._session_factory,
            events=self._events,
        )
        # 共享同一份探测缓存（同一批次里换评估器不应让缓存失效）
        clone._probe_cache = self._probe_cache
        return clone

    async def emit_evaluation_completed(self, run_id: str, report: EvaluationReport) -> None:
        """补发 ``evaluation.completed`` 事件（L6：harness/CLI 无 run 绑定时不发）。

        仅当注入了 events 且提供 run_id 时生效；载荷与 API 路径保持一致。
        """
        if self._events is None:
            return
        await self._events.emit(
            run_id,
            "evaluation.completed",
            {
                "suite_id": report.suite_id,
                "total_score": report.total_score,
                "runnability": report.runnability,
                "passed": report.passed,
                "verdict": "pass" if report.passed else "fail",
            },
        )

    # ------------------------------------------------------------------ 单任务

    async def evaluate_task(
        self,
        task: EvalTask,
        *,
        project_id: str | None,
        version_override: int | None = None,
        suite: EvalSuite | None = None,
    ) -> TaskOut:
        """评测单个任务：加载产物 -> 建沙箱 -> 四维并行 -> 聚合。

        ``project_id is None`` 且为 ``per_task`` 套件 -> 未绑定（``unbound_task``）；
        ``per_task`` 下产物缺失 -> ``missing_artifact``。两种短路均**不启沙箱、
        不调 judge、零成本**。``single`` 模式沿用 v1.0 行为（含 ``code`` 回退）。
        """
        started = time.perf_counter()
        allow_default_fallback = not _is_per_task(suite)
        if not allow_default_fallback and project_id is None:
            logger.info("评测任务未绑定产物，跳过：task=%s", task.id)
            return self._skipped(
                task,
                reason=SKIP_UNBOUND_TASK,
                suite=suite,
                project_id=None,
                started=started,
            )

        workdir, artifact_ref, references, base_files = await self._load_task(
            task, project_id, version_override, allow_default_fallback=allow_default_fallback
        )
        if not allow_default_fallback and artifact_ref.get("name") is None:
            logger.info(
                "评测任务产物缺失，跳过：task=%s project=%s artifact=%s",
                task.id,
                project_id,
                task.artifact_name,
            )
            return self._skipped(
                task,
                reason=SKIP_MISSING_ARTIFACT,
                suite=suite,
                project_id=project_id,
                started=started,
            )

        scores = await self._run_evaluators(task, project_id, workdir, references)
        limits = suite.thresholds if suite is not None else None
        weights = suite.weights if suite is not None else None
        result = aggregate(scores, weights=weights, thresholds=limits)

        dimensions = [
            DimensionOut.from_score(score, weight=result.weights.get(score.name, score.weight))
            for score in scores
        ]

        # ---- v1.3 增量健康度装配（T1.2 / Q4）：本段只在有基线 / 有新检查结构时产出 ----
        runs = [run for score in scores for run in score.check_runs]
        blocking_failed = [
            run.command for run in runs if run.blocking and run.status == "failed"
        ]
        skipped_checks = [
            SkippedCheckOut(command=run.command, reason=run.skipped_reason)
            for run in runs
            if run.status == "skipped"
        ]
        probe = await self._probe(task, base_files)
        regression, regression_reason = self._regression(probe, runs)

        warnings: list[str] = []
        if task.baseline_unavailable:
            warnings.append(BASELINE_UNAVAILABLE_WARNING)
        if probe is not None and probe.unhealthy:
            warnings.append(BASELINE_UNHEALTHY_WARNING)
        if any(
            run.group == _FRONTEND_BUILD_GROUP and run.status == "skipped"
            for run in runs
        ):
            warnings.append(FRONTEND_BUILD_SKIPPED_WARNING)

        wall_time_ms = int((time.perf_counter() - started) * 1000)
        return TaskOut(
            id=task.id,
            scenario=task.scenario,
            status=STATUS_EVALUATED,
            skip_reason=None,
            project_id=project_id,
            run_id=task.run_id,
            requirement=task.requirement,
            artifact_ref=artifact_ref,
            dimensions=dimensions,
            total_score=result.total_score,
            runnability=result.runnability,
            # Q4：阻断项失败 ⇒ 不可交付（TaskOut.model_post_init 亦有同一不变量）
            passed=bool(result.passed and not blocking_failed),
            thresholds={
                "total": (limits.total if limits else 75.0),
                "runnability": (limits.runnability if limits else 60.0),
            },
            wall_time_ms=wall_time_ms,
            cost_cny=round(sum(score.cost_cny for score in scores), 6),
            tokens=sum(score.tokens for score in scores),
            warnings=warnings,
            baseline_runnability=_probe_out(probe),
            regression=regression,
            regression_reason=regression_reason,
            blocking_failed=blocking_failed,
            skipped_checks=skipped_checks,
        )

    async def _probe(
        self, task: EvalTask, base_files: dict[str, str]
    ) -> BaselineProbe | None:
        """基线独立执行（T1.2）：**只在增量模式 + 基线正文可用时**探测。

        关闭条件（docs/11 §7.5-7）：``baseline_ref`` 为空（非增量）或基线正文不可用
        （``base_files == {}``）⇒ 直接返回 ``None`` ⇒ ``TaskOut`` 全字段与 v1.2 一致。
        """
        if not task.baseline_ref or not base_files:
            return None
        checks = list(task.checks)
        return await probe_baseline(
            files=base_files,
            checks=checks,
            sandbox_provider=self._sandbox_provider,
            exec_timeout=self._exec_timeout,
            cache=self._probe_cache,
            cache_key=baseline_cache_key(base_files, checks),
        )

    @staticmethod
    def _regression(
        probe: BaselineProbe | None, runs: Sequence[Any]
    ) -> tuple[bool, str]:
        """「基线健康 + 合并树有失败」= 回归（T1.2 三态之三）。

        构造上互斥（``probe.healthy`` 才可能命中）⇒ 与 ``check_failed`` 不重叠
        （docs/11 §7.7-R7 ①）。
        """
        if probe is None or not probe.healthy:
            return False, ""
        failed = [run for run in runs if run.status == "failed"]
        if not failed:
            return False, ""
        return True, f"基线通过，合并树在 `{failed[0].command}` 失败"

    def _skipped(
        self,
        task: EvalTask,
        *,
        reason: str,
        suite: EvalSuite | None,
        project_id: str | None,
        started: float,
    ) -> TaskOut:
        """构造「未执行」任务结果（零沙箱、零 judge、零成本）。"""
        limits = suite.thresholds if suite is not None else None
        return TaskOut.skipped_task(
            task.id,
            reason=reason,
            requirement=task.requirement,
            scenario=task.scenario,
            project_id=project_id,
            run_id=task.run_id,
            thresholds={
                "total": (limits.total if limits else 75.0),
                "runnability": (limits.runnability if limits else 60.0),
            },
            wall_time_ms=int((time.perf_counter() - started) * 1000),
        )

    def without_judge(self) -> EvaluationHarness:
        """返回**不带 judge** 的副本（v1.1：``--no-judge`` 纯确定性口径，judge 成本 0）。

        只复制引用、不重建评估器与沙箱：零成本演练 / RK5 复现都用它，保证
        「同一套件、同一沙箱、只是不调模型」。
        """
        clone = copy.copy(self)
        clone._judge = None
        return clone

    def _bound_judge(self, task: EvalTask) -> Any:
        """按任务绑定 judge（v1.1 C11）：``task.run_id`` 非空且 judge 支持 ``with_run``
        时返回绑定副本，使 judge 成本归属到该 run（否则批次成本统计偏低）。

        鸭子判定，**不改 ``Judge`` Protocol** —— 既有 fake judge（无 ``with_run``）
        原样返回，行为与 v1.0 完全一致。
        """
        judge = self._judge
        if judge is None or not task.run_id:
            return judge
        binder = getattr(judge, "with_run", None)
        if not callable(binder):
            return judge
        return binder(task.run_id)

    async def _run_evaluators(
        self,
        task: EvalTask,
        project_id: str | None,
        workdir: dict[str, str],
        references: dict[str, dict[str, str]],
    ) -> list[EvalScore]:
        if self._sandbox_provider is None:
            return await self._gather(task, workdir, references, None)
        try:
            async with self._sandbox_provider.session() as session:
                for path, content in workdir.items():
                    await session.write_file(path, content)
                return await self._gather(task, workdir, references, session)
        except Exception:  # noqa: BLE001 - 沙箱不可用：降级为无沙箱判定，不整体失败
            # 与「每 task 独立会话」的隔离语义一致：会话获取 / 写入失败时记录告警，
            # 退回 sandbox=None（runnability 走 ast 语法降级，convention 跳过 lint），
            # 保证评测仍产出完整报告而非 API 500。
            logger.warning(
                "评测沙箱会话不可用，降级为无沙箱判定（task=%s）", task.id, exc_info=True
            )
            degraded = await self._gather(task, workdir, references, None)
            for score in degraded:
                score.degraded = True  # 整场沙箱降级：所有维度标记 degraded（QA-10）
            return degraded

    async def _gather(
        self,
        task: EvalTask,
        workdir: dict[str, str],
        references: dict[str, dict[str, str]],
        sandbox: SandboxSession | None,
    ) -> list[EvalScore]:
        ctx = EvalContext(
            task=task,
            workdir=workdir,
            references=references,
            sandbox=sandbox,
            judge=self._bound_judge(task),
            exec_timeout=self._exec_timeout,
            layout=task.layout,
            scoring=task.scoring,
        )
        raw = await asyncio.gather(
            *(self._safe_evaluate(evaluator, ctx) for evaluator in self._evaluators)
        )
        order = {name: index for index, name in enumerate(DIMENSION_ORDER)}
        return sorted(raw, key=lambda score: order.get(score.name, len(order)))

    async def _safe_evaluate(self, evaluator: Evaluator, ctx: EvalContext) -> EvalScore:
        try:
            return await evaluator.evaluate(ctx)
        except Exception as exc:  # noqa: BLE001 - 单维异常降级，不拖垮整份报告
            logger.exception("评估器 %s 执行失败", evaluator.name)
            return EvalScore(
                name=evaluator.name,
                score=0.0,
                weight=evaluator.weight,
                rationale=f"评估器异常：{exc}",
                evidence=[Evidence("metric", evaluator.name, f"执行异常：{exc}")],
                degraded=True,  # 单维降级标记（QA-10）：供前端显著提示，勿当产物劣质
            )

    # ------------------------------------------------------------------ 套件 / 项目

    async def run_suite(
        self,
        suite_id: str,
        *,
        project_id: str | None = None,
        version_override: int | None = None,
        suites_dir: Any = None,
        task_ids: Sequence[str] | None = None,
    ) -> EvaluationReport:
        """批量评测套件内全部（或 ``task_ids`` 指定子集）任务，产出汇总报告。

        ``task_ids`` 用于限量校准 / 续跑（只评子集，保持套件内声明顺序）；
        ``per_task`` 模式下每个任务用自己的 ``project_id``，``project_id`` 参数
        仅作报告级记录，**不用于**产物解析（docs/07 §4.1.2）。
        """
        suite = EvalSuite.load(suite_id, suites_dir=suites_dir)
        harness = self.with_evaluators(default_evaluators(suite.weights))
        started = time.perf_counter()
        tasks: list[TaskOut] = []
        for task in _select_tasks(suite, task_ids):
            tasks.append(
                await harness.evaluate_task(
                    task,
                    project_id=_bound_project_id(suite, task, project_id),
                    version_override=version_override,
                    suite=suite,
                )
            )
        wall_time_ms = int((time.perf_counter() - started) * 1000)
        return EvaluationReport.build(
            suite_id=suite.id,
            suite_version=suite.version,
            project_id=project_id,
            weights=_normalized(suite.weights),
            thresholds=suite.thresholds,
            tasks=tasks,
            wall_time_ms=wall_time_ms,
        )

    async def evaluate_project(
        self,
        project_id: str,
        *,
        suite_id: str = "default",
        artifact_name: str = _DEFAULT_ARTIFACT,
        version: int | None = None,
        requirement: str | None = None,
        suites_dir: Any = None,
    ) -> EvaluationReport:
        """项目级评测：对指定产物（默认 ``code``）跑套件口径的四维评估。"""
        suite = EvalSuite.load(suite_id, suites_dir=suites_dir)
        checks = suite.tasks[0].checks if suite.tasks else []
        if requirement is None:
            requirement = await self._latest_requirement(project_id)
        task = EvalTask(
            id=f"project:{project_id}",
            requirement=requirement or "",
            artifact_name=artifact_name,
            version=version,
            checks=checks,
        )
        harness = self.with_evaluators(default_evaluators(suite.weights))
        started = time.perf_counter()
        task_out = await harness.evaluate_task(task, project_id=project_id, suite=suite)
        wall_time_ms = int((time.perf_counter() - started) * 1000)
        return EvaluationReport.build(
            suite_id=suite.id,
            suite_version=suite.version,
            project_id=project_id,
            weights=_normalized(suite.weights),
            thresholds=suite.thresholds,
            tasks=[task_out],
            wall_time_ms=wall_time_ms,
        )

    # ------------------------------------------------------------------ 加载

    async def _load_task(
        self,
        task: EvalTask,
        project_id: str | None,
        version_override: int | None,
        *,
        allow_default_fallback: bool = True,
    ) -> tuple[dict[str, str], dict[str, Any], dict[str, dict[str, str]], dict[str, str]]:
        """加载任务产物（``allow_default_fallback=False`` 时禁止回退 ``code``）。

        返回值：``(workdir, artifact_ref, references, base_files)``。

        - 第三项为「同项目其它产物」引用集（四维交叉引用用）；
        - **第四项为基线正文**（v1.3 T1.2 / B6：基线解析完原本就被丢弃，现在多带
          一份给「基线独立执行」，**零额外 DB 读取**）；无基线时恒为 ``{}``；
        - 产物未命中时返回 ``artifact_ref={"name": None, ...}``，由调用方决定短路。

        v1.2 增量（docs/09 §4.2.5）：
        - 产物内**有 ``patch_manifest.json``** → ``workdir`` 返回**合并树**
          （基线 ⊕ 补丁），并把 ``changed_paths`` 写回 ``task``（judge 语料分级）；
        - ``references`` **排除** ``type=baseline`` 的产物（C9：基线是用户自己的
          工程快照，几百个文件，既不参与交叉引用也不该进 judge 语料）；增量模式下
          再额外剔除超限产物（R3 大基线性能）。
        """
        artifacts = await self._store.list_artifacts(project_id) if project_id else []
        references: dict[str, dict[str, str]] = {}
        by_name: dict[str, Any] = {}
        for artifact in artifacts:
            # v1.2：基线是用户自己的工程快照，既不参与交叉引用也不进 judge 语料（C9）。
            # 增量合并时再按需溯源（见 `_resolve_baseline`），避免白读几百个文件。
            if getattr(artifact, "type", "") == BASELINE_TYPE:
                continue
            loaded = await self._store.load(artifact.id)
            references[artifact.name] = loaded.files
            by_name[artifact.name] = artifact

        target = self._resolve_target(
            task, by_name, allow_default_fallback=allow_default_fallback
        )
        if target is None:
            return {}, {"name": None, "version": None}, references, {}

        version = version_override if version_override is not None else task.version
        loaded = await self._store.load(target.id, version)
        artifact_ref = {
            "artifact_id": target.id,
            "name": target.name,
            "version": loaded.version,
        }
        workdir = loaded.files
        manifest = parse_patch_manifest(workdir.get(PATCH_MANIFEST_NAME))
        if manifest is None:
            return workdir, artifact_ref, references, {}

        # 产物内的簿记文件不是源码，永远不进评测工作区（不参与 lint / 测试）
        client_files = {
            path: content
            for path, content in workdir.items()
            if path != PATCH_MANIFEST_NAME
        }
        if not manifest.base_content_hash:
            # ``from_scratch`` + 模板骨架：产物本身就是整树、没有基线可合
            return client_files, artifact_ref, references, {}

        base_files = await self._resolve_baseline(
            task, manifest.base_content_hash, project_id
        )
        if not base_files:
            # R9 红线：**禁止**拿"当前基线"顶上去合并 —— 那等价于把补丁自动重放到
            # 模型从未见过的树，会产出一个"看着正常、实际不可比"的分数。
            # 处置：不合并、只评补丁自身，并在报告里显式标注不可比（不阻断，同 unknown 语义）。
            task.baseline_unavailable = True
            logger.warning(
                "基线不可用：patch_manifest.base_content_hash 无匹配基线，"
                "本次不合并、分数与基线内运行不可比（task=%s hash=%s）",
                task.id,
                manifest.base_content_hash[:12],
            )
            return client_files, artifact_ref, references, {}

        merged = merge_trees(
            base_files, client_files, mode="merge", deleted=manifest.deleted
        )
        task.changed_paths = frozenset(manifest.changed_paths)
        references = {
            name: files
            for name, files in references.items()
            if _within_reference_budget(files)
        }
        return merged.files, artifact_ref, references, dict(base_files)

    async def _resolve_baseline(
        self,
        task: EvalTask,
        expected_hash: str,
        project_id: str | None,
    ) -> dict[str, str]:
        """解析基线正文（R9：只认**精确引用**或**指纹匹配**，绝不猜当前基线）。

        优先级（team-lead r5 定案）：

        1. ``run.context["baseline"]`` 的 ``(artifact_id, version)`` **精确加载**；
        2. 按 ``base_content_hash`` 在该项目基线产物的**全部历史版本**里匹配
           （实现委托给 ``ArtifactStore.find_baseline_files``，与合并导出共用一份逻辑）；
        3. 都失败 → 返回 ``{}``（调用方标记 ``baseline_unavailable``，不合并）。

        返回 ``{}`` 是唯一失败表达 —— 本方法**没有**任何"回退到 latest"的分支。
        """
        ref = await self._baseline_ref_of(task)
        if ref is not None:
            artifact_id = str(ref.get("artifact_id") or "").strip()
            raw_version = ref.get("version")
            version = raw_version if isinstance(raw_version, int) else None
            if artifact_id:
                try:
                    exact = await self._store.load(artifact_id, version)
                except Exception:  # noqa: BLE001 - 精确引用失效：退回指纹匹配
                    logger.warning(
                        "基线精确引用加载失败（task=%s baseline=%s v=%s），改按指纹匹配",
                        task.id,
                        artifact_id,
                        version,
                        exc_info=True,
                    )
                else:
                    if (
                        expected_hash
                        and exact.version_row.content_hash != expected_hash
                    ):
                        logger.warning(
                            "基线引用与 patch_manifest 指纹不一致（task=%s ref=%s manifest=%s）",
                            task.id,
                            exact.version_row.content_hash[:12],
                            expected_hash[:12],
                        )
                    return exact.files

        if not expected_hash or not project_id:
            return {}
        try:
            return await self._store.find_baseline_files(project_id, expected_hash)
        except Exception:  # noqa: BLE001 - 检索失败同样按"不可用"处理，绝不猜
            logger.warning(
                "基线指纹检索失败（task=%s hash=%s）", task.id, expected_hash[:12], exc_info=True
            )
            return {}

    async def _baseline_ref_of(self, task: EvalTask) -> dict[str, Any] | None:
        """取基线的精确引用：``task.baseline_ref`` > ``run.context["baseline"]``。"""
        if isinstance(task.baseline_ref, dict) and task.baseline_ref:
            return task.baseline_ref
        if self._session_factory is None or not task.run_id:
            return None
        try:
            async with self._session_factory() as session:
                run = await RunRepository(session).get(task.run_id)
        except Exception:  # noqa: BLE001 - 读不到 run 就退回指纹匹配
            logger.warning("基线引用查询失败（run=%s）", task.run_id, exc_info=True)
            return None
        context = run.context if run is not None and isinstance(run.context, dict) else {}
        ref = context.get("baseline")
        return ref if isinstance(ref, dict) and ref else None

    @staticmethod
    def _resolve_target(
        task: EvalTask,
        by_name: Mapping[str, Any],
        *,
        allow_default_fallback: bool = True,
    ) -> Any | None:
        """解析任务目标产物。

        ``allow_default_fallback=False``（``per_task``）时**禁止**回退
        ``_DEFAULT_ARTIFACT``，且按 ID 直取失败时返回 ``None``（不再伪造引用）。
        """
        if task.artifact_id is not None:
            for artifact in by_name.values():
                if artifact.id == task.artifact_id:
                    return artifact
            if not allow_default_fallback:
                return None
            return _Ref(task.artifact_id, task.artifact_name or "artifact")
        if task.artifact_name and task.artifact_name in by_name:
            return by_name[task.artifact_name]
        if allow_default_fallback and _DEFAULT_ARTIFACT in by_name:
            return by_name[_DEFAULT_ARTIFACT]
        return None

    async def _latest_requirement(self, project_id: str) -> str:
        """从项目最新一条 run 的 context 取原始需求（项目级评测用）。"""
        if self._session_factory is None:
            return ""
        try:
            from sqlalchemy import select

            from ymir.models.entities import PipelineRun

            async with self._session_factory() as session:
                stmt = (
                    select(PipelineRun)
                    .where(PipelineRun.project_id == project_id)
                    .order_by(PipelineRun.created_at.desc())
                    .limit(1)
                )
                run = (await session.execute(stmt)).scalar_one_or_none()
        except Exception:  # noqa: BLE001 - 需求缺失不影响评测执行
            return ""
        if run is None or not isinstance(run.context, dict):
            return ""
        return str(run.context.get("requirement", ""))


class _Ref:
    """轻量产物引用（按 ID 直取、无 name 场景的回退）。"""

    def __init__(self, artifact_id: str, name: str) -> None:
        self.id = artifact_id
        self.name = name


def _is_per_task(suite: EvalSuite | None) -> bool:
    """套件是否按任务绑定产物（``None`` 视为既有单项目路径）。"""
    return suite is not None and suite.is_per_task


def _bound_project_id(
    suite: EvalSuite, task: EvalTask, project_id: str | None
) -> str | None:
    """解析任务的实际取材项目。

    - ``per_task``：**只用**任务自己的 ``project_id``（未绑定 -> ``None`` ->
      ``unbound_task``；永不回退到 ``run_suite`` 传入的项目，避免跨项目取材）；
    - ``single``：沿用 ``run_suite(project_id=...)``（v1.0 行为不变）。
    """
    if suite.is_per_task:
        return task.project_id
    return project_id


def _select_tasks(suite: EvalSuite, task_ids: Sequence[str] | None) -> list[EvalTask]:
    """按 ``task_ids`` 取子集（保持套件声明顺序）；``None`` 表示全部。"""
    if task_ids is None:
        return list(suite.tasks)
    wanted = list(dict.fromkeys(task_ids))
    known = {task.id for task in suite.tasks}
    unknown = [task_id for task_id in wanted if task_id not in known]
    if unknown:
        logger.warning("套件 %s 中不存在这些任务，已忽略：%s", suite.id, unknown)
    selected = set(wanted)
    return [task for task in suite.tasks if task.id in selected]


def _normalized(weights: Mapping[str, float]) -> dict[str, float]:
    from ymir.evaluation.aggregator import normalize_weights

    return normalize_weights(weights)


def _probe_out(probe: BaselineProbe | None) -> BaselineProbeOut | None:
    """``BaselineProbe`` -> 报告形态（``None`` 原样返回，保证旧报告逐字节一致）。"""
    if probe is None:
        return None
    return BaselineProbeOut(
        status=probe.status,
        score=probe.score,
        passed=probe.passed,
        checks=[
            BaselineCheckOut(
                command=item.command,
                exit_code=item.exit_code,
                timed_out=item.timed_out,
                skipped=item.skipped,
                output=item.output,
            )
            for item in probe.checks
        ],
        reason=probe.reason,
        duration_ms=probe.duration_ms,
        cached=probe.cached,
    )


def build_eval_harness(
    settings: Settings,
    store: Any,
    *,
    gateway: Any = None,
    judge: Judge | None = None,
    sandbox_provider: SandboxProvider | None = None,
    exec_timeout: float | None = None,
    session_factory: Any = None,
    events: Any = None,
) -> EvaluationHarness:
    """按运行配置装配评测 harness（app 装配与脚本共用）。"""
    effective_judge = judge
    if effective_judge is None and gateway is not None:
        effective_judge = LLMJudge(gateway)
    effective_provider = sandbox_provider
    if effective_provider is None:
        effective_provider = BackendSandboxProvider(
            loader=lambda: build_sandbox(settings),
            network=NetworkPolicy(settings.sandbox_network),
        )
    return EvaluationHarness(
        store=store,
        judge=effective_judge,
        sandbox_provider=effective_provider,
        exec_timeout=float(settings.sandbox_exec_timeout if exec_timeout is None else exec_timeout),
        session_factory=session_factory,
        events=events,
    )
