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
- :func:`build_task_for_project`（**v1.5 T01.1**）：项目维度（补评测）的同构入口，
  模板来源按「归属 run > ``patch_manifest.provenance`` > 套件」三档回退；
- :func:`evaluate_run`：``build_task_for_run`` → ``harness.evaluate_task``（与批量同源）；
- :func:`build_run_report`（**v1.5 T01.5**）：run 维度的唯一报告构建器
  （``gate`` 与 CLI 的落盘正文因此不可能漂移）。

调用方（gate / benchmark fallback / ``scripts/run_single_role.py --evaluate`` /
``api.routers.evaluations``）**一律**走这里，禁止各自再拼一份 ``EvalTask``
（T4.2 的一致性用例会抓）。

分层：``evaluation``(rank 1) → ``sop``(2) / ``templates``(2) / ``artifacts``(3) /
``models``(5) 均为合法边。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ymir.artifacts.merge import PATCH_MANIFEST_NAME, parse_patch_manifest
from ymir.artifacts.provenance import template_yaml_hash
from ymir.evaluation.aggregator import normalize_weights
from ymir.evaluation.evaluators.base import EvalCheck, EvalTask
from ymir.evaluation.harness import EvaluationHarness
from ymir.evaluation.report import (
    CRITERIA_SOURCE_SUITE,
    CRITERIA_SOURCE_TEMPLATE,
    EvaluationReport,
    TaskOut,
)
from ymir.evaluation.suite import EvalSuite
from ymir.models.entities import PipelineRun
from ymir.sop.loader import load_pipeline
from ymir.sop.models import PipelineDefinition
from ymir.templates import DEFAULT_TEMPLATES_DIR, load_template
from ymir.templates.models import TemplateSpec, image_package_map

logger = logging.getLogger(__name__)

__all__ = [
    "CRITERIA_SOURCE_SUITE",
    "CRITERIA_SOURCE_TEMPLATE",
    "TaskCriteria",
    "build_run_report",
    "build_task_for_project",
    "build_task_for_run",
    "criteria_for_template",
    "evaluate_run",
    "evaluate_run_report",
    "resolve_checks",
    "resolve_selection",
    "resolve_suite_id",
    "template_spec_for_run",
]


# --------------------------------------------------------------------- 判据来源


@dataclass(frozen=True)
class TaskCriteria:
    """一次评测「用的是哪把尺子」（v1.5 §1.1.4 判据三元组，纯数据）。

    Attributes:
        template_id: 判据归属的模板 id；套件口径为 ``""``。
        criteria_source: ``template``（模板 ``checks``）| ``suite``（只有套件 ``checks``，
            **未绑定模板**）。
        criteria_version: 模板 YAML 原始字节 sha256；套件口径为 ``""``。

    ★ ``criteria_source`` 恒为非空枚举（``suite`` 是**显式**的弱口径字面量，不是
    "字段缺失 ⇒ 弱口径"这种隐式判定 —— 隐式判定正是 v1.4 §0.3 那条旁路的成因）。
    """

    template_id: str = ""
    criteria_source: str = CRITERIA_SOURCE_SUITE
    criteria_version: str = ""

    @property
    def bound(self) -> bool:
        """是否绑定了模板判据（``False`` ⇒ 结论不得给"有模板的树"背书）。"""
        return self.criteria_source == CRITERIA_SOURCE_TEMPLATE


def criteria_for_template(
    template_id: str | None,
    spec: TemplateSpec | None,
    *,
    template_hash: str | None = None,
) -> TaskCriteria:
    """组装判据三元组（**唯一**写入口，纯函数、零 IO）。

    Args:
        template_id: 模板 id（调用方反查或产物溯源给出）。
        spec: 已加载的模板；``None``（无模板 / 不可读）⇒ 套件口径。
        template_hash: 已知的模板指纹（来自产物溯源）；空 ⇒ **现算**
            （``provenance.template_yaml_hash``，与提升闸门同一实现）。

    Returns:
        :class:`TaskCriteria`。``spec is None`` ⇒ ``("", "suite", "")`` 显式弱口径。
    """
    text = str(template_id or "").strip()
    if spec is None or not text:
        return TaskCriteria()
    version = str(template_hash or "").strip() or template_yaml_hash(
        DEFAULT_TEMPLATES_DIR, text
    )
    return TaskCriteria(
        template_id=text,
        criteria_source=CRITERIA_SOURCE_TEMPLATE,
        criteria_version=version,
    )


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
            # ★ v1.5（docs/17 §1.1.6）：四个新字段必须**逐字段转发**。
            #   这是一处与 ``templates.loader`` 校验 24 同形的坑：漏转发的后果是
            #   "模板写了 tier/requires_deps，评测侧拿到的却是默认值"——没有报错，
            #   只是 A2/A4/A7 静默失效。
            tier=check.tier,
            requires_deps=list(check.requires_deps),
            requires_build_output=check.requires_build_output,
            no_tests_exit_code=check.no_tests_exit_code,
        )
        for check in spec.checks
    ]


# --------------------------------------------------------------------- 装配


def _assemble(  # noqa: PLR0913 - 装配项就是这么多，收成 dataclass 反而更难读
    spec: TemplateSpec | None,
    base: EvalTask | None,
    criteria: TaskCriteria,
    *,
    task_id: str,
    requirement: str,
    project_id: str | None,
    run_id: str | None,
    artifact_name: str,
    version: int | None = None,
    baseline_ref: dict[str, Any] | None = None,
    requirement_source: str = "",
) -> EvalTask:
    """把「模板 + 套件基准任务」装配成 :class:`EvalTask`（**两个入口的唯一实现**）。

    ``build_task_for_run``（run 维度）与 ``build_task_for_project``（项目维度）
    **只允许**在这里拼装，杜绝第三份实现（v1.5 §1.1.2 的"双实现裂缝"）。

    Args:
        spec: 模板；``None`` ⇒ 套件口径（``layout`` / ``scoring`` / 骨架全关）。
        base: 套件基准任务（提供 ``scenario`` 与兜底 ``checks``）。
        criteria: 判据三元组（随任务进入报告，供读取侧判「同不同源」）。
        task_id: 任务 id（run 维度 ``run:<id>``、项目维度 ``project:<id>``）。
        requirement: 原始需求文本。
        project_id: 取材项目。
        run_id: 归属 run（★ 项目维度**刻意传 None**：``EvalTask.run_id`` 会参与
            基线解析与 judge 成本归属，v1.5 §5 明确不在本版改这条语义）。
        artifact_name: 目标产物名。
        version: 目标产物版本（``None`` = latest）。
        baseline_ref: 基线精确引用（``run.context["baseline"]``）。
        requirement_source: ``requirement`` 的来源（``run`` / ``caller`` / ``none``，
            v1.5 D-2）。由调用方**依据是否有归属 run** 决定，报告据此自证输入同尺。

    Returns:
        :class:`EvalTask`。
    """
    # v1.3（Q3）：只有模板**显式声明** runnability_points 才注入 scoring；
    # 否则保持 ``None`` ⇒ 评估器走 v1.2 老口径（分数零变化，§7.5-7 红线）。
    scoring_rules = spec.to_scoring_rules() if spec is not None else None
    scoring = scoring_rules if scoring_rules is not None and scoring_rules.enabled else None
    return EvalTask(
        id=task_id,
        requirement=requirement,
        scenario=base.scenario if base is not None else "",
        project_id=project_id,
        run_id=run_id,
        artifact_name=artifact_name,
        version=version,
        checks=resolve_checks(spec, base),
        layout=spec.to_layout_rules() if spec is not None else None,
        scoring=scoring,
        # v1.3.1（T1.1）：骨架以**纯数据**跨层（evaluation 不得 import templates）
        baseline_skeleton=spec.skeleton_files() if spec is not None else {},
        baseline_ref=baseline_ref,
        criteria_source=criteria.criteria_source,
        template_id=criteria.template_id,
        criteria_version=criteria.criteria_version,
        requirement_source=requirement_source,
        # ★ v1.5（T03.6）：镜像预装清单以**纯数据**注入（``evaluation`` 侧据此判
        # ``requires_deps``）。只有装配点能填它 ⇒ "忘注入"就是"环境探针静默失效"，
        # 所以两个入口共用 ``_assemble`` 这一点是刻意的。
        image_packages=_image_package_map(),
    )


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
    - ``criteria_source`` / ``template_id`` / ``criteria_version``（v1.5 T01.3）：
      由 :func:`criteria_for_template` 从**同一份** ``spec`` 派生 ⇒ 报告自证判据来源。

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
    criteria = criteria_for_template(_template_id_of_run(run), spec)
    run_requirement = str((run.context or {}).get("requirement", "") or "")
    task = _assemble(
        spec,
        base,
        criteria,
        task_id=f"run:{run.id}",
        requirement=run_requirement,
        # gate 路径的需求**永远**来自 run（与补评测的 D-2 修复同源）
        requirement_source="run" if run_requirement.strip() else "none",
        project_id=run.project_id,
        run_id=run.id,
        artifact_name=artifact_name,
        baseline_ref=_baseline_ref(run.context),
    )
    return task, suite


def build_task_for_project(  # noqa: PLR0913 - 与 §1.1.2 的设计签名逐项一致
    *,
    project_id: str,
    artifact_name: str = "code",
    version: int | None = None,
    template_id: str | None = None,
    template_hash: str | None = None,
    manifest_files: Mapping[str, str] | None = None,
    run_id: str | None = None,
    requirement: str | None = None,
    requirement_source: str = "",
    default_suite: str = "default",
    suites_dir: Any = None,
) -> tuple[EvalTask, EvalSuite, TaskCriteria]:
    """把「一个项目的某版产物」装配成可评测任务（补评测的唯一装配点，v1.5 T01.1）。

    与 :func:`build_task_for_run` **逐项同构**（共用 :func:`_assemble`），差别只在
    没有 ``PipelineRun`` 对象 ⇒ 模板来源要按优先级回退（docs/17 §1.1.3 候选③）：

    =========  ==============================================  ==========================
    优先级     来源                                             ``criteria_version``
    =========  ==============================================  ==========================
    1          调用方反查的 ``template_id``（归属 run 的模板）  **现算** YAML 指纹
    2          ``manifest_files`` 里 ``patch_manifest.provenance``  产物里**记的**指纹
               ``.template_id`` / ``.template_hash``
    3          都拿不到（手工导入 / 脚本写入）                  ``""``（``suite`` 显式弱口径）
    =========  ==============================================  ==========================

    ★ 第 2 档用**产物里记的** ``template_hash`` 而不是现算：那是"当时那把尺子"，
    与报告语义（"我按哪一版判据评的"）一致；若模板后来改过，提升侧现算的指纹
    自然不等 ⇒ 读取侧按"拿不到结论"处理（判据不同源 = 不可比，不需要额外机制）。

    ★ **失败降级**（§1.1.6）：模板不可读 / manifest 损坏 ⇒ 退化到第 3 档
    （套件判据 + ``criteria_source="suite"``），**不让评测本身 500**。

    Args:
        project_id: 项目 id（仅用于任务 id 与追溯）。
        artifact_name: 目标产物名。
        version: 目标产物版本（``None`` = latest）。
        template_id: 调用方反查出的模板 id（最高优先级）。
        template_hash: 与 ``template_id`` 同源的模板指纹（可空 ⇒ 现算）。
        manifest_files: 目标产物版本的**文件集**（含 ``patch_manifest.json`` 时作第 2 档）。
        run_id: 归属 run（**只进报告，不进 ``EvalTask``** —— 见 :func:`_assemble`）。
        requirement: 原始需求（``None`` ⇒ 空串，由调用方先解析）。
        requirement_source: 需求的来源（``run`` / ``caller`` / ``none``，v1.5 D-2）。
            缺省 ``""`` ⇒ **按"有需求还是没需求"推断**（``caller`` / ``none``），
            调用方（端点）知道归属 run 时应当显式传 ``"run"``。
        default_suite: 套件 id（与 ``evaluate_project`` 的 ``suite_id`` 同义）。
        suites_dir: 套件目录覆盖（测试用）。

    Returns:
        ``(task, suite, criteria)`` —— 报告根部需要 ``criteria`` 三元组自证判据来源。

    Raises:
        YmirError 子类（套件不存在等）：由调用方决定是否降级。
    """
    suite = EvalSuite.load(default_suite, suites_dir=suites_dir)
    base = suite.tasks[0] if suite.tasks else None
    # ``run_id`` 刻意**不**下传（见 docstring）：它只用于调用方的报告归属，
    # ``EvalTask.run_id`` 会牵动基线解析与 judge 成本归属（v1.5 §5 边界）。
    _ = run_id
    spec, criteria = _resolve_project_criteria(
        template_id=template_id,
        template_hash=template_hash,
        manifest_files=manifest_files,
    )
    resolved_source = requirement_source or (
        "caller" if str(requirement or "").strip() else "none"
    )
    task = _assemble(
        spec,
        base,
        criteria,
        task_id=f"project:{project_id}",
        requirement=requirement or "",
        requirement_source=resolved_source,
        project_id=project_id,
        # ★ 刻意不传 run_id：``EvalTask.run_id`` 参与基线解析与 judge 成本归属，
        #   本版不改那条语义（docs/17 §5「明确不做」第 6 条）。
        run_id=None,
        artifact_name=artifact_name,
        version=version,
    )
    return task, suite, criteria


def _resolve_project_criteria(
    *,
    template_id: str | None,
    template_hash: str | None,
    manifest_files: Mapping[str, str] | None,
) -> tuple[TemplateSpec | None, TaskCriteria]:
    """按优先级回退解析补评测的判据来源（第 1/2/3 档，**绝不抛**）。

    Returns:
        ``(spec, criteria)``。``(None, TaskCriteria())`` = 第 3 档（套件口径）。
    """
    text = str(template_id or "").strip()
    if text:
        spec = _safe_load_template(text, source="调用方反查")
        if spec is not None:
            return spec, criteria_for_template(text, spec, template_hash=template_hash)
        # 调用方明确说了是哪套模板，但模板不可读 ⇒ 不回退到产物溯源（那会拿另一把
        # 尺子给这棵树背书），直接落第 3 档。
        return None, TaskCriteria()

    prov_id, prov_hash = _manifest_template(manifest_files)
    if prov_id:
        spec = _safe_load_template(prov_id, source="产物溯源")
        if spec is not None:
            return spec, criteria_for_template(prov_id, spec, template_hash=prov_hash)
    return None, TaskCriteria()


def _manifest_template(
    manifest_files: Mapping[str, str] | None,
) -> tuple[str, str]:
    """从产物文件集里取 ``patch_manifest.provenance`` 的 (``template_id``, ``template_hash``)。

    错误正文 / 缺文件 / 非 patch 产物一律返回 ``("", "")``（**绝不抛**）。
    """
    if not manifest_files:
        return "", ""
    try:
        manifest = parse_patch_manifest(manifest_files.get(PATCH_MANIFEST_NAME))
    except Exception:  # noqa: BLE001 - 损坏的 manifest 只降级，不影响评测本身
        logger.warning("判据来源：patch_manifest 解析失败，按套件口径处理", exc_info=True)
        return "", ""
    prov = getattr(manifest, "provenance", None) if manifest is not None else None
    if prov is None:
        return "", ""
    return (
        str(getattr(prov, "template_id", "") or "").strip(),
        str(getattr(prov, "template_hash", "") or "").strip(),
    )


def _safe_load_template(template_id: str, *, source: str) -> TemplateSpec | None:
    """读模板（不可读 ⇒ 告警并返回 ``None``，**不让评测失败**）。"""
    try:
        return load_template(template_id)
    except Exception:  # noqa: BLE001 - 模板不可读：退回套件口径（与 role_executor 同纪律）
        logger.warning(
            "补评测装配：模板不可读，退回套件 checks（source=%s template=%s）",
            source,
            template_id,
            exc_info=True,
        )
        return None


def _template_id_of_run(run: PipelineRun) -> str:
    """取 ``run.context["template"]["id"]``（无则空串）。"""
    context = run.context if isinstance(run.context, dict) else {}
    ref = context.get("template")
    return str(ref.get("id") or "").strip() if isinstance(ref, dict) else ""


def _image_package_map() -> dict[str, list[str]]:
    """沙箱镜像预装清单（生态名 -> 包名，**纯数据**，v1.5 T03.6）。

    来源是 ``templates.models`` 的两张权威表（与 ``docker/sandbox.Dockerfile``
    由 CI 钉子 ``tests/test_sandbox_image_manifest.py`` 对齐）。这里转成
    ``dict[str, list[str]]`` 是为了让 ``EvalTask``/报告都是可 JSON 化的纯数据。
    """
    return {language: sorted(packages) for language, packages in image_package_map().items()}


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
    outcome, _suite, _report = await evaluate_run_report(
        harness, run, artifact_name=artifact_name, default_suite=default_suite
    )
    return outcome


async def evaluate_run_report(
    harness: EvaluationHarness,
    run: PipelineRun,
    *,
    artifact_name: str = "code",
    default_suite: str = "default",
    repair_round: int = 0,
) -> tuple[TaskOut, EvalSuite, EvaluationReport]:
    """装配 → 评测 → **报告**（v1.5 T04.2：CLI ``--evaluate`` 落盘用）。

    与 :func:`evaluate_run` **同一份装配与同一次评测**（前者是它的薄壳），区别只在
    多回传 ``suite`` 与由 :func:`build_run_report` 构建的报告 —— CLI 因此不需要
    二次装配，也不需要自己拼报告（"落盘结论与裁决同源"）。

    Args:
        harness: 评测编排。
        run: 归属 run。
        artifact_name: 目标产物名。
        default_suite: 套件 id。
        repair_round: 当前修复轮次（``gate.repair_round_of(run.context)``；缺省 0）。

    Returns:
        ``(outcome, suite, report)``。报告**未落盘**（落盘是调用方的决定与责任）。
    """
    task, suite = build_task_for_run(
        run, artifact_name=artifact_name, default_suite=default_suite
    )
    outcome = await harness.evaluate_task(task, project_id=run.project_id, suite=suite)
    report = build_run_report(run, suite, outcome, repair_round=repair_round)
    return outcome, suite, report


def build_run_report(
    run: PipelineRun,
    suite: EvalSuite,
    outcome: TaskOut,
    *,
    repair_round: int = 0,
    verdict_source: str = "harness",
) -> EvaluationReport:
    """run 维度的**唯一报告构建器**（v1.5 T01.5，docs/17 §1.3.2）。

    ``gate.HarnessEvaluationGate._build_report`` 与 CLI ``--evaluate`` 都委托本函数 ⇒
    「gate 落盘的正文」与「CLI 落盘的正文」在字段集合上**不可能**漂移（A13/A14 的
    第三层保障，A8"同尺"的一部分）。

    ★ 判据三键**不在这里传**：``EvaluationReport.build`` 从 ``tasks[0]`` 派生
    （``task.criteria_*`` 由 :func:`build_task_for_run` 写入）。

    Args:
        run: 归属 run（只取 ``id`` / ``project_id``）。
        suite: 本次用的套件（提供 id / version / weights / thresholds）。
        outcome: 单任务评测结果。
        repair_round: 修复轮次（0 = 首轮）。
        verdict_source: 裁决来源（gate 与 CLI 均为 ``harness``）。

    Returns:
        :class:`EvaluationReport`（未落盘）。
    """
    artifact_version = outcome.artifact_ref.get("version")
    return EvaluationReport.build(
        suite_id=suite.id,
        suite_version=suite.version,
        project_id=run.project_id,
        weights=normalize_weights(suite.weights),
        thresholds=suite.thresholds,
        tasks=[outcome],
        wall_time_ms=int(getattr(outcome, "wall_time_ms", 0) or 0),
        run_id=run.id,
        artifact_version=artifact_version if isinstance(artifact_version, int) else None,
        repair_round=repair_round,
        verdict_source=verdict_source,  # type: ignore[arg-type]
    )


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
