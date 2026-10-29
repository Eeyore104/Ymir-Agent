"""一次 run 的评测任务装配与端到端打分（v1.3 · T4.1 / T4.2，docs/11 §7.3-9）。

**为什么需要这个模块**：v1.2 里「把 run 装配成 ``EvalTask``」的逻辑只存在于
:class:`~ymir.evaluation.gate.HarnessEvaluationGate` 内部，而
``BenchmarkRunner._row_from_fallback`` 自己拼了一份**更弱的**
``EvalTask``（只绑套件 ``checks``，不带模板 ``layout`` / 基线精确引用）。
结果是同一个产物走「gate 裁决」与「批量补评」两条路径可能拿到不同的分数 ——
真实口径裂缝（docs/11 §7.4 T4.1）。

本模块把装配收敛成**唯一装配点**：

- :func:`build_task_for_run`：套件解析（``run.context["benchmark"]`` >
  流水线 ``repair.eval`` > ``default_suite``）→ 模板 ``checks`` 覆盖 → ``layout`` /
  ``baseline_ref`` 注入 → :class:`EvalTask`；
- :func:`evaluate_run`：``build_task_for_run`` → ``harness.evaluate_task``（与批量同源）。

调用方（gate / benchmark fallback / ``scripts/run_single_role.py --evaluate``）
**一律**走这里，禁止各自再拼一份 ``EvalTask``（T4.2 的一致性用例会抓）。

分层：``evaluation``(rank 1) → ``sop``(2) / ``templates``(2) / ``models``(5) 均为合法边。
"""

from __future__ import annotations

import logging
from typing import Any

from ymir.evaluation.evaluators.base import EvalCheck, EvalTask
from ymir.evaluation.harness import EvaluationHarness
from ymir.evaluation.report import TaskOut
from ymir.evaluation.suite import EvalSuite
from ymir.models.entities import PipelineRun
from ymir.sop.loader import load_pipeline
from ymir.sop.models import PipelineDefinition
from ymir.templates import load_template
from ymir.templates.models import TemplateSpec

logger = logging.getLogger(__name__)

__all__ = [
    "build_task_for_run",
    "evaluate_run",
    "resolve_selection",
    "resolve_suite_id",
    "template_spec_for_run",
]


# --------------------------------------------------------------------- 选择


def resolve_selection(
    run: PipelineRun, *, default_suite: str = "default"
) -> tuple[str, str | None]:
    """解析本次 run 该用哪个套件 / 哪个任务（唯一口径）。

    优先级（docs/09 §4.4.3）：
    ``run.context["benchmark"]["eval_suite"/"eval_task"]``（批量跑分写入）>
    流水线定义 ``repair.eval.suite/task`` > ``default_suite``（无任务 id）。

    Returns:
        ``(suite_id, task_id | None)``。``task_id is None`` 表示未指定任务，
        调用方取套件首个任务。
    """
    context = run.context if isinstance(run.context, dict) else {}
    benchmark = context.get("benchmark")
    benchmark = benchmark if isinstance(benchmark, dict) else {}
    suite_id = _first_str(benchmark.get("eval_suite"))
    task_id = _first_str(benchmark.get("eval_task"))

    if suite_id is None or task_id is None:
        spec = _repair_eval_spec(run.pipeline_id)
        if suite_id is None and spec is not None:
            suite_id = _first_str(spec.suite)
        if task_id is None and spec is not None:
            task_id = _first_str(spec.task)
    return (suite_id or default_suite), task_id


def resolve_suite_id(run: PipelineRun, *, default_suite: str = "default") -> str:
    """只取套件 id（供降级路径报告 ``EvalVerdict.suite_id``，**永不抛异常**）。"""
    return resolve_selection(run, default_suite=default_suite)[0]


def _repair_eval_spec(pipeline_id: str) -> Any | None:
    """读流水线的 ``repair.eval`` 缺省值（不可读时返回 ``None``，只告警）。

    ★ **不做进程内缓存**：v1.2 的 gate 用过实例字典缓存，但一次评测只读一次
    YAML（亚毫秒级），缓存带来的跨测试污染风险大于收益（docs/11 §7.4 T4.1）。
    """
    try:
        definition: PipelineDefinition = load_pipeline(pipeline_id)
    except Exception:  # noqa: BLE001 - 定义不可读时退回缺省套件
        logger.warning("评测装配：流水线定义不可读 %s", pipeline_id, exc_info=True)
        return None
    return definition.repair.eval if definition.repair else None


# --------------------------------------------------------------------- 模板


def template_spec_for_run(run: PipelineRun) -> TemplateSpec | None:
    """按 ``run.context["template"]["id"]`` 读模板（失败只告警，退回通用模式）。

    模板在 run 创建后被删除 / 改坏属配置漂移：**不让整轮评测失败**，退回套件
    ``checks`` 与「无 layout」口径（与 ``role_executor`` 的降级同口径）。
    """
    context = run.context if isinstance(run.context, dict) else {}
    ref = context.get("template")
    template_id = str(ref.get("id") or "").strip() if isinstance(ref, dict) else ""
    if not template_id:
        return None
    try:
        return load_template(template_id)
    except Exception:  # noqa: BLE001 - 模板不可读：退回套件口径
        logger.warning(
            "评测装配：模板不可读，退回套件 checks（run=%s template=%s）",
            run.id,
            template_id,
            exc_info=True,
        )
        return None


def resolve_checks(
    spec: TemplateSpec | None, base: EvalTask | None
) -> list[EvalCheck]:
    """确定性检查命令：模板 ``checks`` 覆盖套件 ``checks``（v1.2 §4.1.2）。

    无模板（或模板已不可读）时**逐条沿用套件**，与 v1.1 完全一致（§10-9）。
    v1.3 起把 ``group`` / ``blocking`` / ``requires`` / ``timeout_s`` 一并映射 ——
    ``requires`` 是 node 模板在无工具链镜像里优雅跳过的唯一依据（docs/11 §7.5-3）。
    v1.3.1 起再映射 ``requires_files``（docs/13 §7.2.2）：探针据此把「基线缺该检查
    依赖的文件」判成 ``skipped`` 而不是 ``unhealthy``；合并树侧**不消费**它。
    """
    if spec is None:
        return list(base.checks) if base is not None else []
    return [
        EvalCheck(
            type=check.type,
            run=check.run,
            weight=check.weight,
            group=check.group,
            blocking=check.blocking,
            requires=check.requires,
            timeout_s=check.timeout_s,
            requires_files=list(check.requires_files),
        )
        for check in spec.checks
    ]


# --------------------------------------------------------------------- 装配


def build_task_for_run(
    run: PipelineRun,
    *,
    artifact_name: str = "code",
    default_suite: str = "default",
) -> tuple[EvalTask, EvalSuite]:
    """把「一次 run」装配成可评测任务（**唯一装配点**，T4.1 / T4.2）。

    装配项（与原 ``HarnessEvaluationGate.evaluate`` 内的逻辑逐项一致）：

    - 套件 / 任务：:func:`resolve_selection`；
    - ``requirement``：``run.context["requirement"]``；
    - ``scenario``：套件中该任务的场景短标识（无任务时为空串）；
    - ``checks``：模板 ``checks`` 覆盖套件 ``checks``（:func:`resolve_checks`）；
    - ``layout``：``TemplateSpec.to_layout_rules()``（纯数据；``None`` = v1.1 口径）；
    - ``scoring``：``TemplateSpec.to_scoring_rules()``（纯数据；未声明 ⇒ ``None`` ⇒ 老口径）；
    - ``baseline_skeleton``：``TemplateSpec.skeleton_files()``（纯数据，v1.3.1 T1.1）——
      **本函数是它唯一的注入点**；探针据此把「骨架补缺后的基线」写进沙箱。
      ``evaluation``(rank 1) 因此无需 import ``templates``(2)。
    - ``baseline_ref``：``run.context["baseline"]``（R9：评测侧据它精确加载基线）；
    - ``project_id`` / ``run_id`` / ``artifact_name``：追溯字段。

    Returns:
        ``(task, suite)``。两个值都必须回传 —— 报告与裁决需要套件的
        ``id`` / ``version`` / ``weights`` / ``thresholds``。

    Raises:
        YmirError 子类（套件不存在等）：由调用方决定是否降级为 ``unknown``。
    """
    suite_id, task_id = resolve_selection(run, default_suite=default_suite)
    suite = EvalSuite.load(suite_id)
    base = suite.task(task_id) if task_id else None
    if base is None and suite.tasks:
        base = suite.tasks[0]

    spec = template_spec_for_run(run)
    # v1.3（Q3）：只有模板**显式声明** runnability_points 才注入 scoring；
    # 否则保持 ``None`` ⇒ 评估器走 v1.2 老口径（分数零变化，§7.5-7 红线）。
    scoring_rules = spec.to_scoring_rules() if spec is not None else None
    scoring = scoring_rules if scoring_rules is not None and scoring_rules.enabled else None
    task = EvalTask(
        id=f"run:{run.id}",
        requirement=str((run.context or {}).get("requirement", "")),
        scenario=base.scenario if base is not None else "",
        project_id=run.project_id,
        run_id=run.id,
        artifact_name=artifact_name,
        checks=resolve_checks(spec, base),
        layout=spec.to_layout_rules() if spec is not None else None,
        scoring=scoring,
        # v1.3.1（T1.1）：骨架以**纯数据**跨层（evaluation 不得 import templates）
        baseline_skeleton=spec.skeleton_files() if spec is not None else {},
        baseline_ref=_baseline_ref(run.context),
    )
    return task, suite


async def evaluate_run(
    harness: EvaluationHarness,
    run: PipelineRun,
    *,
    artifact_name: str = "code",
    default_suite: str = "default",
) -> TaskOut:
    """端到端：``build_task_for_run`` → ``harness.evaluate_task``（与批量同源）。

    这是「单跑一个 run 出四维分数」的**唯一**入口：``scripts/run_single_role.py
    --evaluate``、gate 裁决、benchmark 补评都必须走它，保证同一产物在任何路径下
    得到**逐项相等**的分数（T4.2 一致性用例）。
    """
    task, suite = build_task_for_run(
        run, artifact_name=artifact_name, default_suite=default_suite
    )
    return await harness.evaluate_task(task, project_id=run.project_id, suite=suite)


# --------------------------------------------------------------------- 工具


def _first_str(value: Any) -> str | None:
    """把配置值归一为「非空字符串或 None」。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _baseline_ref(context: dict[str, Any] | None) -> dict[str, Any] | None:
    """取基线精确引用（``run.context["baseline"]``，R9：按它加载而非猜最新版本）。"""
    ref = (context or {}).get("baseline")
    return ref if isinstance(ref, dict) and ref else None
