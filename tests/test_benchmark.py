"""benchmark-12 套件结构护栏（docs/07 §4.1 / T1.2）。

CI 只验证**结构与基线不可变性**（零费用、零 Docker、零模型调用）：

1. 12 个任务、``binding_mode: per_task``、``scenario`` 去重后 ≥8 类；
2. 每任务 ``len(checks) >= 2`` 且至少 1 条**执行型**（``python -m pytest`` /
   ``ruff check`` / ``python -m <被测模块>`` 入口命令；纯文件存在性不算）；
3. 每任务 checks 权重合计 1.0（浮点容差 1e-6）；
4. 依赖白名单：checks 的 ``run`` 与 ``requirement`` 文本不得命中
   ``pandas|numpy|requests|bs4|scrapy|flask|fastapi|httpx|pydantic``
   （「禁止使用 X」这类**声明语句**豁免）；
5. 无 URL 字面量（``https?://`` 零命中，任务数据一律内嵌，避免联网依赖）；
6. 转写指纹：每条 ``requirement`` 与产品规格文件逐字一致（首句固定指纹 +
   规格文件全量比对双保险，任何改写都会失败）。

真实跑分只走 ``scripts/run_benchmark.py``（T2），本文件不发 run、不启沙箱。
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from ymir.config import PROJECT_ROOT
from ymir.evaluation import (
    ARCHITECTURE,
    CONVENTION,
    REQUIREMENT,
    RUNNABILITY,
    EvaluationHarness,
)
from ymir.evaluation.baseline import FAILURE_LABELS
from ymir.evaluation.baseline_probe import BASELINE_UNHEALTHY
from ymir.evaluation.benchmark import (
    FAILURE_BASELINE_UNHEALTHY,
    FAILURE_ORDER,
    FAILURE_REGRESSION,
)
from ymir.evaluation.suite import BINDING_MODE_PER_TASK, EvalSuite

SUITES_DIR = PROJECT_ROOT / "evals" / "suites"
SPEC_PATH = PROJECT_ROOT / ".workbuddy" / "tmp" / "benchmark12_tasks.md"
SUITE_ID = "benchmark-12"
EXPECTED_TASKS = 12
MIN_SCENARIOS = 8

#: 12 类场景（设计 §4.1 / 产品清单 §1，去重后必须恰为这 12 个短标识）
EXPECTED_SCENARIOS = {
    "rest_crud",
    "cli_tool",
    "data_cleaning",
    "file_batch",
    "html_parsing",
    "algo_lib",
    "config_parsing",
    "text_processing",
    "state_machine",
    "validator",
    "sql_query",
    "json_transform",
}

#: 执行型检查入口：pytest / ruff / ``python -m <被测模块>`` / 直接跑脚本
_EXECUTABLE = re.compile(
    r"^(?:python3?\s+-m\s+[\w.]+|python3?\s+\S+\.py|ruff\s+check(?:\.exe)?)(?:\s|$)"
)

#: 纯「文件存在性」命令（不算执行型，套件里不允许出现）
_EXISTENCE_ONLY = re.compile(r"^(?:test|ls|cat|head|tail|stat|find|dir)\b")

#: 禁用第三方依赖（任务实现与检查只允许 标准库 + pytest + ruff）
_FORBIDDEN_DEPS = re.compile(
    r"pandas|numpy|requests|bs4|scrapy|flask|fastapi|httpx|pydantic",
    re.IGNORECASE,
)

#: 「禁止 / 不得 / 禁用 …」声明语句（命中禁用词但属约束声明，需豁免）
_NEGATION = re.compile(r"禁止|禁用|不得|不允许|不可使用|避免|勿用|不使用")

#: 子句切分（中文 / 英文标点 + 换行），用于按句判定豁免
_CLAUSE_SPLIT = re.compile(r"[。；;，,、（）()\n]")

#: suite 顶层允许字段（docs/07 §4.1.2；pydantic 会静默忽略多余键，故按原始 YAML 校验）
ALLOWED_SUITE_KEYS = {
    "id",
    "version",
    "description",
    "binding_mode",
    "weights",
    "thresholds",
    "tasks",
}

#: 任务允许字段（只认 4 个；绑定由 T2 批量脚本建 run 后注入）
ALLOWED_TASK_KEYS = {"id", "scenario", "requirement", "checks"}

#: URL 字面量（零容忍：任务数据必须内嵌，评测期断网）
_URL = re.compile(r"https?://")

#: 首句固定指纹（产品规格 `benchmark12_tasks.md` 每条 requirement 的第一句，
#: 逐字冻结）：即使规格文件不在仓库里，改写基线也会被测出。
FINGERPRINTS: dict[str, str] = {
    "task-rest-crud-01": (
        "用标准库 http.server 实现内存待办 REST API，禁用第三方框架与数据库。"
    ),
    "task-cli-notes-02": (
        "用标准库实现命令行笔记工具，入口 python -m notes，数据存当前目录 JSON。"
    ),
    "task-data-clean-03": (
        "实现 CSV 清洗函数，输入为内嵌 CSV 字符串（表头 name,age,city，"
        "行数不少于 8）：去除完全重复行；age 非整数或缺失的行丢弃；"
        "city 缺失填 unknown；name 去首尾空格。"
    ),
    "task-file-batch-04": (
        "实现目录文件批处理模块：给定目录路径，按扩展名分组统计文件数；"
        "把文件名转为小写并用下划线替换空格与非法字符；"
        "生成 manifest.json 记录原名、新名与字节大小；新名冲突时追加三位序号。"
    ),
    "task-parse-html-05": (
        "实现 HTML 内容抽取函数，输入为内嵌的一段 HTML 字符串"
        "（含 title、三个以上链接、一个表格、注释与 script）：仅用标准库解析，"
        "返回标题文本、链接列表（href 与文字）、表格二维数组；"
        "注释与 script 内容不得出现在结果中。"
    ),
    "task-algo-lru-06": (
        "实现 LRU 缓存类与 TopK 函数：缓存容量固定，get 命中时刷新最近使用顺序，"
        "超容量淘汰最久未使用项，容量为 0 时 put 不抛错且 get 返回 None；"
        "TopK 返回出现次数最高的 K 个元素，次数降序、次数相同按元素升序。"
    ),
    "task-config-ini-07": (
        "实现配置解析器：支持 [section] 分段、key=value、# 与 ; 注释、"
        "双引号值、${section.key} 引用替换；语法错误抛异常并带行号。"
    ),
    "task-text-wordcount-08": (
        "实现中英文混排词频统计函数：英文按空白与标点切分后转小写，"
        "中文按连续汉字切二元组，支持传入停用词列表过滤，"
        "返回出现次数最高的 N 个词及次数（N 由参数指定，次数相同按词升序）。"
    ),
    "task-state-order-09": (
        "实现订单状态机：状态为 created/paid/shipped/closed/cancelled；"
        "允许 created→paid/cancelled、paid→shipped/cancelled、shipped→closed；"
        "非法迁移抛异常且状态不变；成功迁移记录前后状态。"
    ),
    "task-validator-form-10": (
        "实现表单字段校验器：username 为 3 到 20 位字母数字下划线；"
        "email 含 @ 与点且不含空白；age 为 0 到 150 的整数。"
    ),
    "task-sql-query-11": (
        "使用标准库 sqlite3 内存库实现订单查询：建 customers 与 orders 表"
        "并写入不少于 10 行种子数据；提供按客户汇总金额（金额降序、同额按客户名升序）、"
        "按金额区间分页（page、size）、按状态计数三个函数，返回字典列表。"
    ),
    "task-json-transform-12": (
        "实现嵌套 JSON 扁平化与聚合：输入为内嵌订单 JSON 字符串（含客户名与明细数组）；"
        "输出每个客户的订单数与总金额（按总金额降序），"
        "并返回明细扁平化列表（订单号、商品、数量、单价）；"
        "明细缺失或金额为字符串时跳过该行并计入异常计数。"
    ),
}


# --------------------------------------------------------------------- 工具


def _suite() -> EvalSuite:
    """加载被测套件（真实 ``evals/suites/benchmark-12.yaml``）。"""
    return EvalSuite.load(SUITE_ID, suites_dir=SUITES_DIR)


class _EmptyStore:
    """空产物库：``per_task`` 未绑定任务**不应**取任何产物。"""

    async def list_artifacts(self, project_id: str, **kwargs: Any) -> list[Any]:
        return []

    async def load(
        self, artifact_id: str, version: int | None = None, **kwargs: Any
    ) -> Any:
        raise AssertionError(f"未绑定任务不得加载产物：{artifact_id}")


class _ExplodingJudge:
    """被调用即失败的 judge（证明零模型调用）。"""

    async def judge(self, **kwargs: Any) -> Any:
        raise AssertionError(f"评测未执行时不得调用模型：{kwargs}")


class _ExplodingProvider:
    """被进入即失败的沙箱 provider（证明短路时不起沙箱）。"""

    @asynccontextmanager
    async def session(self):  # noqa: ANN201 - 与 harness 协议的 Any 签名对齐
        raise AssertionError("未绑定任务不得启动沙箱")
        yield None  # pragma: no cover - 不可达


def _is_executable(run: str) -> bool:
    """是否属执行型检查（pytest / ruff / ``python -m <模块>`` / 脚本入口）。"""
    return bool(_EXECUTABLE.match(run.strip()))


def _forbidden_hits(text: str) -> list[str]:
    """返回命中的禁用依赖词；「禁止 / 不得使用 X」这类声明语句豁免。"""
    hits: list[str] = []
    for clause in _CLAUSE_SPLIT.split(text):
        if not clause.strip() or _NEGATION.search(clause):
            continue
        hits.extend(match.group(0) for match in _FORBIDDEN_DEPS.finditer(clause))
    return hits


def _first_sentence(text: str) -> str:
    """取第一条句号（含）为止的文本。"""
    index = text.find("。")
    return text if index < 0 else text[: index + 1]


def _parse_spec(path: Path) -> dict[str, str]:
    """从产品规格文件解析 ``{task_id: requirement}``（去 Markdown 反引号）。"""
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"^### ", text, flags=re.MULTILINE)[1:]
    parsed: dict[str, str] = {}
    for block in blocks:
        head = block.splitlines()[0] if block.splitlines() else ""
        task_id = head.split(" · ")[0].strip()
        match = re.search(r"^- \*\*requirement\*\*：(.+)$", block, flags=re.MULTILINE)
        if task_id and match is not None:
            parsed[task_id] = match.group(1).replace("`", "").strip()
    return parsed


def _raw_yaml() -> dict[str, Any]:
    """按原始 YAML 解析套件（pydantic 会静默忽略多余键，字段护栏必须看原文）。"""
    import yaml

    raw = yaml.safe_load((SUITES_DIR / f"{SUITE_ID}.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


# --------------------------------------------------------------------- 结构护栏


class TestBenchmarkSuiteStructure:
    """benchmark-12 结构护栏（``pytest -k structure`` 全绿即 CI 通过）。"""

    def test_structure_suite_metadata(self) -> None:
        suite = _suite()
        assert suite.id == SUITE_ID
        assert suite.version == "1.1"
        assert suite.binding_mode == BINDING_MODE_PER_TASK  # 任务级绑定，不跨项目取材
        assert suite.weights[REQUIREMENT] == pytest.approx(0.3)
        assert suite.weights[RUNNABILITY] == pytest.approx(0.3)
        assert suite.weights[CONVENTION] == pytest.approx(0.2)
        assert suite.weights[ARCHITECTURE] == pytest.approx(0.2)
        assert suite.thresholds.total == 75
        assert suite.thresholds.runnability == 60

    def test_structure_task_count_and_scenarios(self) -> None:
        suite = _suite()
        assert len(suite.tasks) == EXPECTED_TASKS
        assert len({task.id for task in suite.tasks}) == EXPECTED_TASKS
        scenarios = {task.scenario for task in suite.tasks}
        assert "" not in scenarios, "每个任务必须声明 scenario"
        assert len(scenarios) >= MIN_SCENARIOS, f"场景同质化：仅 {len(scenarios)} 类"
        assert scenarios == EXPECTED_SCENARIOS
        for task in suite.tasks:
            assert re.fullmatch(r"[a-z][a-z0-9_]*", task.scenario), task.scenario
            assert task.requirement.strip(), f"{task.id} 缺 requirement"

    def test_structure_checks_are_executable(self) -> None:
        for task in _suite().tasks:
            assert len(task.checks) >= 2, f"{task.id} 检查不足 2 条"
            for check in task.checks:
                assert check.type == "command", f"{task.id} 检查类型非法：{check.type}"
                assert _is_executable(check.run), f"{task.id} 非执行型检查：{check.run}"
                assert not _EXISTENCE_ONLY.match(check.run.strip()), (
                    f"{task.id} 纯文件存在性检查不算执行型：{check.run}"
                )

    def test_structure_check_weights_sum_to_one(self) -> None:
        for task in _suite().tasks:
            total = sum(check.weight for check in task.checks)
            assert abs(total - 1.0) <= 1e-6, f"{task.id} checks 权重合计 {total}"

    def test_structure_tasks_declare_no_binding_fields(self) -> None:
        # 绑定由 T2 批量脚本建 run 后注入，套件不得写死项目 / run / 产物版本
        for task in _suite().tasks:
            assert task.project_id is None, f"{task.id} 不得声明 project_id"
            assert task.run_id is None, f"{task.id} 不得声明 run_id"
            assert task.artifact_id is None, f"{task.id} 不得声明 artifact_id"
            assert task.version is None, f"{task.id} 不得声明 version"

    def test_structure_yaml_keys_whitelisted(self) -> None:
        # 按原始 YAML 校验字段白名单：多余键（如 batch）不会被 pydantic 报错，必须显式拦
        raw = _raw_yaml()
        extra = set(raw) - ALLOWED_SUITE_KEYS
        assert extra == set(), f"suite 顶层多余字段：{sorted(extra)}"
        assert "batch" not in raw, "批次标识走脚本 --batch-id，不得写进 suite"
        tasks = raw["tasks"]
        assert isinstance(tasks, list) and len(tasks) == EXPECTED_TASKS
        for item in tasks:
            assert set(item) == ALLOWED_TASK_KEYS, (
                f"{item.get('id')} 字段集合非法：{sorted(set(item))}（只允许 4 个字段）"
            )

    def test_structure_dependency_whitelist(self) -> None:
        offenders: list[str] = []
        for task in _suite().tasks:
            if _forbidden_hits(task.requirement):
                offenders.append(f"{task.id}/requirement: {_forbidden_hits(task.requirement)}")
            for check in task.checks:
                hits = _forbidden_hits(check.run)
                if hits:
                    offenders.append(f"{task.id}/check({check.run}): {hits}")
        assert offenders == [], f"命中禁用第三方依赖（只允许 标准库 + pytest + ruff）：{offenders}"

    def test_structure_no_url_literals(self) -> None:
        raw = (SUITES_DIR / f"{SUITE_ID}.yaml").read_text(encoding="utf-8")
        hits = _URL.findall(raw)
        assert hits == [], f"套件不得含 URL 字面量（评测期断网）：{hits}"

    def test_structure_requirement_fingerprints(self) -> None:
        suite = _suite()
        assert set(FINGERPRINTS) == {task.id for task in suite.tasks}
        for task in suite.tasks:
            actual = _first_sentence(task.requirement)
            assert actual == FINGERPRINTS[task.id], (
                f"{task.id} requirement 首句被改写（基线不可变）：{actual!r}"
            )

    def test_structure_requirement_matches_product_spec(self) -> None:
        if not SPEC_PATH.is_file():
            pytest.skip(
                "产品规格文件缺失，已由固定指纹用例兜底："
                f"{SPEC_PATH}（CI 中 .workbuddy/ 被 gitignore 属正常）"
            )
        spec = _parse_spec(SPEC_PATH)
        assert len(spec) == EXPECTED_TASKS, f"产品规格解析出 {len(spec)} 条 requirement"
        for task in _suite().tasks:
            assert task.id in spec, f"{task.id} 不在产品规格中"
            assert task.requirement == spec[task.id], (
                f"{task.id} requirement 与产品规格不逐字一致（转写被改写）"
            )

    async def test_structure_suite_loads_without_model_or_sandbox(self) -> None:
        # 「套件可加载 + --no-judge 下零模型调用」：12 个任务全未绑定 -> 全 skipped，
        # judge / 沙箱 / 产物加载三者一次都不该被触达（断言用爆炸式替身）。
        harness = EvaluationHarness(
            store=_EmptyStore(),
            judge=_ExplodingJudge(),
            sandbox_provider=_ExplodingProvider(),
        )
        report = await harness.run_suite(SUITE_ID, suites_dir=SUITES_DIR)

        assert len(report.tasks) == EXPECTED_TASKS
        assert [task.id for task in report.tasks] == [task.id for task in _suite().tasks]
        assert report.executed_count == 0
        assert report.skipped_count == EXPECTED_TASKS
        assert {task.skip_reason for task in report.tasks} == {"unbound_task"}
        assert report.cost_cny == 0.0
        assert report.total_tokens == 0
        assert report.first_pass_rate is None


# --------------------------------------------------------------------- T2.1/T2.2/T2.3 批量跑分


import importlib.util  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from ymir.common.errors import InvalidParamError  # noqa: E402
from ymir.evaluation.baseline import BaselineTable  # noqa: E402
from ymir.evaluation.benchmark import (  # noqa: E402
    FAILURE_BELOW_THRESHOLD,
    FAILURE_CHECK_FAILED,
    FAILURE_DEGRADED_SCORING,
    FAILURE_DEPENDENCY_MISSING,
    FAILURE_MISSING_ARTIFACT,
    FAILURE_TIMEOUT,
    BenchmarkOptions,
    BenchmarkRunner,
    BenchmarkState,
    FailureSignals,
    classify_failure,
)
from ymir.evaluation.report import DimensionOut, EvidenceOut, TaskOut  # noqa: E402
from ymir.models.base import utcnow  # noqa: E402
from ymir.models.entities import ModelCall  # noqa: E402
from ymir.storage.repositories import RunRepository  # noqa: E402

SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_benchmark.py"
BATCH = "test-batch"

FAIL_HISTORY: list[dict] = [
    {
        "round": 0,
        "verdict": "fail",
        "total": 62.4,
        "runnability": 48.0,
        "failed_checks": ["python -m pytest -q"],
        "artifact_version": 1,
        "degraded": False,
    },
    {
        "round": 1,
        "verdict": "pass",
        "total": 81.3,
        "runnability": 80.0,
        "failed_checks": [],
        "artifact_version": 2,
        "degraded": False,
    },
]


class FakeOrchestrator:
    """假编排：launch 时直接把 run 推到终态（零真实执行、零模型调用）。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        history: list[dict] | None = FAIL_HISTORY,
        status: str = "completed",
        cost: float = 0.5,
    ) -> None:
        self._sf = session_factory
        self._history = history
        self._status = status
        self._cost = cost
        self.launch_calls: list[str] = []

    async def launch(self, run_id: str) -> None:
        self.launch_calls.append(run_id)
        async with self._sf() as session:
            run = await RunRepository(session).get(run_id)
            assert run is not None
            run.status = self._status
            run.started_at = utcnow()
            run.finished_at = utcnow()
            if self._history is not None:
                context = dict(run.context or {})
                context["repair"] = {
                    "round": self._history[-1]["round"],
                    "max_rounds": 2,
                    "history": [dict(entry) for entry in self._history],
                    "stop_reason": None,
                    "accepted": None,
                }
                run.context = context
            session.add(
                ModelCall(
                    run_id=run_id,
                    provider="fake",
                    model="fake-model",
                    tier="executor",
                    cost_cny=self._cost,
                )
            )
            await session.commit()

    async def shutdown(self) -> None:
        return None


class FakeHarness:
    """假 harness：兜底评测只记录调用，不产生真实成本。"""

    def __init__(self, *, passed: bool = True, artifact_name: str | None = "code") -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self._passed = passed
        self._artifact_name = artifact_name
        self.judge_disabled = False

    def without_judge(self) -> FakeHarness:
        """模拟 ``--no-judge``：返回自身并记录「已关闭 judge」。"""
        self.judge_disabled = True
        return self
        self.judge_disabled = False


    async def evaluate_task(self, task, *, project_id, version_override=None, suite=None):  # noqa: ANN001, ANN003
        self.calls.append((task.id, project_id, task.run_id))
        return TaskOut(
            id=task.id,
            status="evaluated",
            artifact_ref={"artifact_id": "a1", "name": self._artifact_name, "version": 2},
            dimensions=[
                DimensionOut(name="requirement_conformance", label="需求", score=88.0, weight=0.3),
                DimensionOut(
                    name="runnability",
                    label="可运行",
                    score=90.0,
                    weight=0.3,
                    evidence=[
                        EvidenceOut(
                            kind="command",
                            ref="python -m pytest -q",
                            detail="exit=0 timed_out=False",
                        )
                    ],
                ),
                DimensionOut(name="convention", label="规范", score=88.0, weight=0.2),
                DimensionOut(name="architecture", label="架构", score=88.0, weight=0.2),
            ],
            total_score=88.0 if self._passed else 62.4,
            runnability=90.0 if self._passed else 48.0,
            passed=self._passed,
            thresholds={"total": 75.0, "runnability": 60.0},
            wall_time_ms=10,
            tokens=0,
            cost_cny=0.0,
        )


def _make_runner(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    orchestrator: Any,
    harness: Any,
    limit: int | None = None,
    budget_cny: float | None = None,
) -> BenchmarkRunner:
    return BenchmarkRunner(
        session_factory=session_factory,
        orchestrator=orchestrator,
        harness=harness,
        suite_id="benchmark-12",
        batch_id=BATCH,
        limit=limit,
        budget_cny=budget_cny,
        sandbox="docker",
        data_dir=tmp_path / "bench",
        meta={
            "model_routing": "planner=m1/executor=m2",
            "sandbox": "docker",
            "image": "ymir-sandbox:latest",
            "image_probe": {"pytest": "8.3.3", "ruff": "0.6.9"},
            "network": "none",
            "judge": "m2/temp=0.1",
            "pipeline_version": "1.1",
        },
    )


async def test_runner_limit_marks_partial(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """``--limit 2``：只发起 2 个 run，其余标 ``skipped{not_run}``，``partial=true``。"""
    orchestrator = FakeOrchestrator(session_factory)
    harness = FakeHarness()
    runner = _make_runner(
        session_factory, tmp_path, orchestrator=orchestrator, harness=harness, limit=2
    )
    state_path = tmp_path / "bench" / "state.json"

    state = await runner.run(
        BenchmarkOptions(suite_id="benchmark-12", batch_id=BATCH, limit=2, state_path=state_path)
    )

    assert len(orchestrator.launch_calls) == 2
    assert state.partial is True
    done = [row for row in state.tasks.values() if row["status"] == "done"]
    skipped = [row for row in state.tasks.values() if row["status"] == "skipped"]
    assert len(done) == 2 and len(skipped) == 10
    assert {row["skip_reason"] for row in skipped} == {"not_run"}
    assert all(row["failure_class"] is None for row in skipped)  # 未执行不填失败分类
    assert state_path.exists()


async def test_runner_resume_skips_done_tasks(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """``--resume``：done 不重跑，只补剩余（用 launch 次数断言）。"""
    orchestrator = FakeOrchestrator(session_factory)
    runner = _make_runner(
        session_factory, tmp_path, orchestrator=orchestrator, harness=FakeHarness(), limit=2
    )
    state_path = tmp_path / "bench" / "state.json"
    first = await runner.run(
        BenchmarkOptions(suite_id="benchmark-12", batch_id=BATCH, limit=2, state_path=state_path)
    )
    assert len(orchestrator.launch_calls) == 2

    runner2 = _make_runner(
        session_factory, tmp_path, orchestrator=orchestrator, harness=FakeHarness()
    )
    state = await runner2.run(
        BenchmarkOptions(
            suite_id="benchmark-12", batch_id=BATCH, resume=True, state_path=state_path
        )
    )

    assert len(orchestrator.launch_calls) == 12  # 2 + 剩余 10
    assert state.partial is False
    assert all(row["status"] == "done" for row in state.tasks.values())
    # 首批 done 行未被覆盖（run_id 保持首次的值）
    for task_id, row in first.tasks.items():
        if row["status"] == "done":
            assert state.tasks[task_id]["run_id"] == row["run_id"]


async def test_state_file_is_atomic_and_parseable(tmp_path: Path) -> None:
    """状态文件原子写：无 ``.tmp`` 残留，内容可解析（中断后可续跑）。"""
    path = tmp_path / "bench" / "state-benchmark-12-b.json"
    state = BenchmarkState(batch_id=BATCH, suite_id="benchmark-12", suite_version="1.1")
    state.tasks["task-a"] = {"status": "done", "cost_cny": 0.1}
    state.save(path)

    assert path.exists()
    assert not path.with_suffix(".json.tmp").exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == 1
    assert raw["tasks"]["task-a"]["status"] == "done"
    assert raw["batch_id"] == BATCH
    # 模拟「写了一半就崩」：tmp 残留不影响主文件解析
    path.with_suffix(".json.tmp").write_text('{"schema": 1, "tas', encoding="utf-8")
    assert BenchmarkState.load(path).tasks["task-a"]["status"] == "done"


async def test_budget_exhausted_skips_without_calls(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """批次预算为 0：所有任务 ``skipped{budget}``，零 launch、零 harness 调用。"""
    orchestrator = FakeOrchestrator(session_factory)
    harness = FakeHarness()
    runner = _make_runner(
        session_factory, tmp_path, orchestrator=orchestrator, harness=harness, budget_cny=0.0
    )

    state = await runner.run(
        BenchmarkOptions(suite_id="benchmark-12", batch_id=BATCH, budget_cny=0.0)
    )

    assert orchestrator.launch_calls == []
    assert harness.calls == []
    assert {row["skip_reason"] for row in state.tasks.values()} == {"budget"}
    assert state.spent_cny == 0.0


async def test_fallback_scoring_when_history_empty(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """``history`` 为空：用同套件补一次完整评测，``score_source="fallback"`` 且归属 run。"""
    orchestrator = FakeOrchestrator(session_factory, history=None)
    harness = FakeHarness(passed=True)
    runner = _make_runner(
        session_factory, tmp_path, orchestrator=orchestrator, harness=harness, limit=1
    )

    state = await runner.run(
        BenchmarkOptions(suite_id="benchmark-12", batch_id=BATCH, limit=1)
    )

    assert len(harness.calls) == 1
    evaluated_task_id, project_id, run_id = harness.calls[0]
    # v1.3（T4.1）：兜底补评改走 run_task，任务 id 与 gate 同源（``run:<uuid>``），
    # 状态行仍按套件任务 id 归档
    assert evaluated_task_id.startswith("run:")
    assert project_id and run_id  # 兜底评测也归属到该 run（judge 成本可追溯）
    row = state.tasks["task-rest-crud-01"]
    assert row["score_source"] == "fallback"
    assert row["final_pass"] is True
    assert row["cost_source"] == "measured"


async def test_pipeline_scoring_uses_history_without_extra_harness_call(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """主路径读 history：不额外调 harness，``score_source="pipeline"``，成本为实测。"""
    orchestrator = FakeOrchestrator(session_factory, cost=0.3172)
    harness = FakeHarness()
    runner = _make_runner(
        session_factory, tmp_path, orchestrator=orchestrator, harness=harness, limit=1
    )

    state = await runner.run(
        BenchmarkOptions(suite_id="benchmark-12", batch_id=BATCH, limit=1)
    )

    assert harness.calls == []  # 主路径不补评
    row = next(row for row in state.tasks.values() if row["status"] == "done")
    assert row["score_source"] == "pipeline"
    assert row["first_total"] == 62.4
    assert row["final_total"] == 81.3
    assert row["repair_rounds"] == 1
    assert row["cost_cny"] == 0.3172
    assert state.spent_cny == 0.3172


def test_classify_failure_six_classes() -> None:
    """六分类 first-match：每类各 1 条构造用例 + 一条「全过」反例。"""
    missing = classify_failure(FailureSignals(has_code_artifact=False))
    assert missing[0] == FAILURE_MISSING_ARTIFACT

    dependency = classify_failure(
        FailureSignals(
            checks=[{"command": "python -m pytest -q", "exit_code": 1,
                     "output": "ModuleNotFoundError: No module named 'pandas'"}]
        )
    )
    assert dependency[0] == FAILURE_DEPENDENCY_MISSING

    timeout = classify_failure(
        FailureSignals(
            checks=[{"command": "python -m pytest -q", "exit_code": None, "timed_out": True}]
        )
    )
    assert timeout[0] == FAILURE_TIMEOUT

    check_failed = classify_failure(
        FailureSignals(
            checks=[{"command": "python -m pytest -q", "exit_code": 5,
                     "output": "no tests ran"}]
        )
    )
    assert check_failed[0] == FAILURE_CHECK_FAILED
    assert "no tests ran" in check_failed[1]

    below = classify_failure(
        FailureSignals(total=71.0, runnability=80.0, thresholds={"total": 75, "runnability": 60})
    )
    assert below[0] == FAILURE_BELOW_THRESHOLD

    degraded = classify_failure(FailureSignals(total=90.0, runnability=90.0, degraded=True))
    assert degraded[0] == FAILURE_DEGRADED_SCORING

    assert classify_failure(FailureSignals(total=90.0, runnability=90.0)) == (None, "")


async def test_baseline_table_has_12_rows_and_meta() -> None:
    """基线表：12 行无空缺、表头元信息齐全、done 行字段完整。"""
    suite = EvalSuite.load("benchmark-12")
    state = BenchmarkState(
        batch_id=BATCH,
        suite_id=suite.id,
        suite_version=suite.version,
        pipeline_id="software-dev",
        pipeline_version="1.1",
        started_at="2026-10-19T02:10:00+00:00",
        meta={
            "model_routing": "planner=m1/executor=m2",
            "sandbox": "docker",
            "image": "ymir-sandbox:latest",
            "image_probe": {"pytest": "8.3.3", "ruff": "0.6.9"},
            "network": "none",
            "judge": "m2/temp=0.1",
        },
        limit=3,
        partial=True,
        budget_cny=20.0,
        spent_cny=1.0,
    )
    state.tasks["task-rest-crud-01"] = {
        "status": "done",
        "first_total": 62.4,
        "first_pass": False,
        "final_total": 81.3,
        "final_pass": True,
        "repair_rounds": 1,
        "cost_cny": 0.3172,
        "cost_source": "measured",
        "wall_time_ms": 173600,
        "score_source": "pipeline",
        "failure_class": None,
        "failure_reason": "",
        "artifact_version": 2,
        "sandbox": "docker",
    }
    state.tasks["task-cli-notes-02"] = {
        "status": "done",
        "first_total": 71.0,
        "first_pass": False,
        "final_total": 71.0,
        "final_pass": False,
        "repair_rounds": 2,
        "cost_cny": 0.6841,
        "cost_source": "measured",
        "wall_time_ms": 402100,
        "score_source": "pipeline",
        "failure_class": "below_threshold",
        "failure_reason": "total 71.0 < 75",
        "artifact_version": 3,
        "sandbox": "docker",
    }
    state.tasks["task-data-clean-03"] = {
        "status": "skipped",
        "skip_reason": "budget",
        "cost_cny": 0.0,
        "failure_class": None,
        "failure_reason": "批次预算达上限",
    }

    table = BaselineTable.from_state(state, suite)
    markdown = table.to_markdown()

    assert len(table.rows) == 12
    assert len(table.executed) == 2 and len(table.skipped) == 10
    for line in (
        f"批次：{BATCH}",
        "部分执行：是（limit=3）",
        "planner=m1/executor=m2",
        "pytest 8.3.3 / ruff 0.6.9 预置",
        "judge=m2/temp=0.1",
        "network=none",
        "套件版本：benchmark-12 v1.1",
        "流水线：software-dev v1.1",
        "成本口径：实测",
        "首轮通过率",
        "最终通过率",
        "评分来源：pipeline 2 / fallback 0",
    ):
        assert line in markdown, line
    assert "未执行（批次预算达上限）" in markdown  # skipped 行填 skip_reason
    assert "below_threshold" not in markdown  # 展示中文标签而非英文码
    assert "未达门槛" in markdown


async def test_baseline_rejects_mixed_sandbox() -> None:
    """docker 与 local 混表 -> InvalidParamError（口径不可混）。"""
    suite = EvalSuite.load("benchmark-12")
    state = BenchmarkState(batch_id=BATCH, suite_id=suite.id, suite_version=suite.version)
    for index, task in enumerate(suite.tasks[:2]):
        state.tasks[task.id] = {
            "status": "done",
            "final_total": 80.0,
            "final_pass": True,
            "first_total": 80.0,
            "first_pass": True,
            "sandbox": "docker" if index == 0 else "local",
        }
    with pytest.raises(InvalidParamError):
        BaselineTable.from_state(state, suite)


async def test_baseline_render_is_byte_stable(tmp_path: Path) -> None:
    """同一状态文件两次渲染字节一致（含落盘）。"""
    suite = EvalSuite.load("benchmark-12")
    state = BenchmarkState(
        batch_id=BATCH, suite_id=suite.id, suite_version=suite.version,
        started_at="2026-10-19T02:10:00+00:00",
    )
    state.tasks["task-rest-crud-01"] = {
        "status": "done", "first_total": 62.4, "first_pass": False,
        "final_total": 81.3, "final_pass": True, "repair_rounds": 1,
        "cost_cny": 0.3172, "wall_time_ms": 173600, "score_source": "pipeline",
        "sandbox": "docker",
    }

    first = BaselineTable.from_state(state, suite)
    second = BaselineTable.from_state(state, suite)
    assert first.to_markdown() == second.to_markdown()
    assert first.to_json() == second.to_json()

    md_one, json_one = first.save(tmp_path / "bench")
    md_bytes = md_one.read_bytes()
    json_bytes = json_one.read_bytes()
    md_two, json_two = second.save(tmp_path / "bench")
    assert md_two.read_bytes() == md_bytes
    assert json_two.read_bytes() == json_bytes


def _load_cli() -> Any:
    spec = importlib.util.spec_from_file_location("ymir_run_benchmark_cli", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    cli = _load_cli()
    with pytest.raises(SystemExit) as excinfo:
        cli.parse_args(["--help"])
    assert excinfo.value.code == 0


def test_cli_preflight_failure_exits_2_without_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """预检失败：exit 2 + stderr 含重建命令 + 完全不构造 runner（零 launch）。"""
    cli = _load_cli()
    calls: list[Any] = []

    def fake_run(cmd: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=125, stdout="", stderr="No such image")

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("预检失败时不得构造 runner / 发起 run")

    monkeypatch.setattr(cli, "_run", fake_run)
    monkeypatch.setattr(cli, "build_runner", explode)

    with pytest.raises(SystemExit) as excinfo:
        import asyncio

        asyncio.run(cli.run_main(cli.parse_args(["--batch-id", BATCH])))

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert cli.BUILD_HINT in captured.err
    assert "python:3.12-slim" not in captured.err  # 绝不提示回退基础镜像
    assert len(calls) == 1  # 镜像检查失败即停，不再探测 pytest/ruff


def test_cli_preflight_reports_missing_tool_without_docker_noise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """旧镜像缺工具（实测：镜像有 pytest 无 ruff）：报「镜像缺少 ruff」+ 重建提示。

    与上一例的分工：上一例是「镜像不存在」（docker 侧错误）；本例是「镜像能跑但
    工具没装」。分类错误的后果是把可自愈的问题误报成「请先构建沙箱镜像」，
    并把 docker 的 usage 噪音当诊断信息打印。
    """
    cli = _load_cli()
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        if "pytest" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="pytest 8.4.2", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            returncode=126,
            stdout="",
            stderr='exec: "ruff": executable file not found in $PATH\n'
            "Run 'docker run --help' for more information",
        )

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("预检失败时不得构造 runner / 发起 run")

    monkeypatch.setattr(cli, "_run", fake_run)
    monkeypatch.setattr(cli, "build_runner", explode)

    with pytest.raises(SystemExit) as excinfo:
        import asyncio

        asyncio.run(cli.run_main(cli.parse_args(["--batch-id", BATCH])))

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "镜像缺少 ruff" in captured.err  # 分类正确：工具缺失 ≠ 镜像缺失
    assert "请重建镜像" in captured.err
    assert cli.BUILD_HINT in captured.err
    assert "Run 'docker run --help'" not in captured.err  # docker 噪音行被剔除
    assert len(seen) == 3  # image → pytest → ruff，逐个探测到失败即停


def test_cli_report_only_does_not_build_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--report-only``：只重算表，不预检、不构造 runner、不发起 run。"""
    cli = _load_cli()
    cli.DATA_DIR = tmp_path / "bench"
    cli.DATA_DIR.mkdir(parents=True, exist_ok=True)
    state_path = cli.state_path_for("benchmark-12", BATCH, cli.DATA_DIR)
    state = BenchmarkState(batch_id=BATCH, suite_id="benchmark-12", suite_version="1.1")
    state.tasks["task-rest-crud-01"] = {
        "status": "done", "first_total": 80.0, "first_pass": True,
        "final_total": 80.0, "final_pass": True, "repair_rounds": 0,
        "cost_cny": 0.1, "score_source": "pipeline", "sandbox": "docker",
    }
    state.save(state_path)

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("--report-only 不得构造 runner")

    monkeypatch.setattr(cli, "build_runner", explode)
    import asyncio

    code = asyncio.run(cli.run_main(cli.parse_args(["--batch-id", BATCH, "--report-only"])))

    assert code == 0
    assert (cli.DATA_DIR / f"baseline-{BATCH}.md").exists()
    assert (cli.DATA_DIR / f"baseline-{BATCH}.json").exists()


def test_cli_budget_wiring_passes_budget_to_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--budget-cny`` 透传到 runner（预算的零调用语义由 runner 用例保证）。"""
    cli = _load_cli()
    cli.DATA_DIR = tmp_path / "bench"
    seen: dict[str, Any] = {}

    class FakeEngine:
        async def dispose(self) -> None:
            return None

    class FakeRunner:
        async def run(self, options: Any) -> BenchmarkState:
            seen["options"] = options
            return BenchmarkState(batch_id=options.batch_id, suite_id=options.suite_id)

        async def shutdown(self) -> None:
            return None

    async def fake_build(settings: Any, **kwargs: Any) -> Any:
        seen["kwargs"] = kwargs
        return FakeEngine(), FakeRunner()

    monkeypatch.setattr(cli, "build_runner", fake_build)
    monkeypatch.setattr(cli, "preflight", lambda image: {"pytest": "8.3.3", "ruff": "0.6.9"})
    import asyncio

    code = asyncio.run(
        cli.run_main(cli.parse_args(["--batch-id", BATCH, "--budget-cny", "5", "--json"]))
    )

    assert code == 0
    assert seen["options"].budget_cny == 5.0
    assert seen["kwargs"]["budget_cny"] == 5.0


async def test_no_judge_uses_deterministic_harness(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """``--no-judge``：兜底评测走「无 judge 副本」（零 judge 成本口径）。"""
    orchestrator = FakeOrchestrator(session_factory, history=None)
    harness = FakeHarness()
    runner = _make_runner(
        session_factory, tmp_path, orchestrator=orchestrator, harness=harness, limit=1
    )

    state = await runner.run(
        BenchmarkOptions(suite_id="benchmark-12", batch_id=BATCH, limit=1, no_judge=True)
    )

    assert harness.judge_disabled is True
    assert len(harness.calls) == 1
    row = next(row for row in state.tasks.values() if row["status"] == "done")
    assert row["score_source"] == "fallback"


def test_harness_without_judge_returns_copy() -> None:
    """``EvaluationHarness.without_judge`` 只摘掉 judge，其余引用不变（零重建）。"""
    from ymir.evaluation.harness import EvaluationHarness

    marker = object()
    harness = EvaluationHarness(store="store", judge=marker)  # type: ignore[arg-type]
    clone = harness.without_judge()

    assert clone is not harness
    assert clone._judge is None  # noqa: SLF001 - 断言内部状态（副本语义）
    assert harness._judge is marker  # noqa: SLF001 - 原实例不受影响
    assert clone.store == harness.store




# --------------------------------------------------------------------- 六分类：明细


def _detail_history(
    *,
    command: str = "python -m pytest -q",
    exit_code: int | None = 1,
    timed_out: bool = False,
    output: str = "",
    total: float = 70.0,
    runnability: float = 80.0,
) -> list[dict[str, Any]]:
    """构造带 ``failed_check_details`` 的单轮失败历史（新字段形态）。"""
    return [
        {
            "round": 0,
            "verdict": "fail",
            "total": total,
            "runnability": runnability,
            "failed_checks": [command],
            "failed_check_details": [
                {
                    "command": command,
                    "exit_code": exit_code,
                    "timed_out": timed_out,
                    "output": output,
                }
            ],
            "artifact_version": 1,
            "degraded": False,
        }
    ]


async def _row_of_history(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """跑一个任务并把它的状态行取出来（零真实执行）。"""
    orchestrator = FakeOrchestrator(session_factory, history=history)
    runner = _make_runner(
        session_factory, tmp_path, orchestrator=orchestrator, harness=FakeHarness(), limit=1
    )
    state = await runner.run(BenchmarkOptions(suite_id="benchmark-12", batch_id=BATCH, limit=1))
    return next(row for row in state.tasks.values() if row["status"] == "done")


async def test_history_details_classify_dependency_missing(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """主路径：明细命中 ``No module named`` -> ``dependency_missing``（不再靠启发式）。"""
    row = await _row_of_history(
        session_factory,
        tmp_path,
        _detail_history(output="ModuleNotFoundError: No module named 'pandas'"),
    )
    assert row["failure_class"] == FAILURE_DEPENDENCY_MISSING
    assert "pandas" in row["failure_reason"]


async def test_history_details_classify_check_failed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """主路径：``exit_code=1`` 且无依赖特征 -> ``check_failed``，原因取输出首行。"""
    row = await _row_of_history(
        session_factory,
        tmp_path,
        _detail_history(exit_code=1, output="AssertionError: 404 != 201\n第二行不取"),
    )
    assert row["failure_class"] == FAILURE_CHECK_FAILED
    assert row["failure_reason"] == "AssertionError: 404 != 201"


async def test_history_details_timeout_beats_check_failed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """主路径：``timed_out=True`` 优先于 ``exit_code != 0`` -> ``timeout``。"""
    row = await _row_of_history(
        session_factory,
        tmp_path,
        _detail_history(exit_code=1, timed_out=True, output="killed after 120s"),
    )
    assert row["failure_class"] == FAILURE_TIMEOUT


async def test_history_without_details_falls_back_to_heuristics(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """向后兼容：旧 history 只有 ``failed_checks``（命令名）也不崩，回退到门槛启发式。"""
    legacy = [
        {
            "round": 0,
            "verdict": "fail",
            "total": 70.0,
            "runnability": 80.0,
            "failed_checks": ["python -m pytest -q"],
            "artifact_version": 1,
            "degraded": False,
        }
    ]
    row = await _row_of_history(session_factory, tmp_path, legacy)
    assert row["failure_class"] == FAILURE_BELOW_THRESHOLD




# --------------------------------------------------------------------- C11：judge 成本归属


from pydantic import BaseModel  # noqa: E402

from ymir.evaluation.evaluators.base import LLMJudge  # noqa: E402
from ymir.gateway.gateway import ChatResult, ChatUsage  # noqa: E402


class _JudgeOut(BaseModel):
    verdict: str = "ok"


class _RecordingGateway:
    """记录 ``chat`` kwargs 的假网关（零真实调用、零成本）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: Any,
        *,
        tier: Any,
        run_id: str | None = None,
        stage_id: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: Any = None,
    ) -> ChatResult:
        self.calls.append({"tier": str(tier), "run_id": run_id, "stage_id": stage_id})
        return ChatResult(
            content='{"verdict": "ok"}',
            provider="fake",
            model="fake-model",
            tier=str(tier),
            usage=ChatUsage(prompt_tokens=1, completion_tokens=2),
            cost_cny=0.0001,
            latency_ms=1,
        )


async def test_llm_judge_with_run_binds_cost_to_run() -> None:
    """C11：``LLMJudge.with_run`` 后的 judge 调用必须带 ``run_id/stage_id``。

    这是「judge 成本归属到 run」（R3 / 批次预算不失真）的**唯一**可观测证据点：
    变异去掉绑定后，``model_calls.run_id`` 会变空、批次成本统计偏低。
    """
    gateway = _RecordingGateway()
    judge = LLMJudge(gateway, tier="executor")  # type: ignore[arg-type]

    bound = judge.with_run("run-abc", "coding")
    outcome = await bound.judge(system="s", user="u", schema=_JudgeOut)

    assert outcome.data.verdict == "ok"
    assert gateway.calls[0] == {"tier": "executor", "run_id": "run-abc", "stage_id": "coding"}

    # 副本语义：原 judge 不受影响，未绑定时不透传（v1.0 行为不变）
    await judge.judge(system="s", user="u", schema=_JudgeOut)
    assert gateway.calls[1] == {"tier": "executor", "run_id": None, "stage_id": None}


# ======================================= v1.3（T1.2 / Q4）：失败分类扩到八类 + 顺序


def test_failure_order_is_eight_categories() -> None:
    """``FAILURE_ORDER`` 由六类扩到八类，且顺序是**口径**（first-match）。"""
    assert FAILURE_ORDER == (
        FAILURE_BASELINE_UNHEALTHY,
        FAILURE_MISSING_ARTIFACT,
        FAILURE_DEPENDENCY_MISSING,
        FAILURE_TIMEOUT,
        FAILURE_REGRESSION,
        FAILURE_CHECK_FAILED,
        FAILURE_BELOW_THRESHOLD,
        FAILURE_DEGRADED_SCORING,
    )
    # 两个新值排位为刻意设计：不可比最前，回归先于 check_failed
    assert FAILURE_ORDER[0] == "baseline_unhealthy"
    assert FAILURE_ORDER.index("regression") < FAILURE_ORDER.index("check_failed")


def test_classify_baseline_unhealthy_wins_over_everything() -> None:
    """基线不健康 → ``baseline_unhealthy``（"这行不可比"先于一切质量结论）。

    ★ 口径：``baseline_status`` 是健康度三态字面量 ``"unhealthy"``，
    不是失败分类值 ``"baseline_unhealthy"``（两个常量字面量不同，混用会永不命中）。
    """
    failure_class, reason = classify_failure(
        FailureSignals(
            has_code_artifact=False,  # 缺文件也要让位
            baseline_status=BASELINE_UNHEALTHY,
        )
    )
    assert failure_class == FAILURE_BASELINE_UNHEALTHY
    assert "不可比" in reason
    # 传失败分类值（错误用法）不会命中 —— 锁定口径区分，防回归
    wrong, _ = classify_failure(FailureSignals(baseline_status=FAILURE_BASELINE_UNHEALTHY))
    assert wrong is None


def test_classify_regression_before_check_failed() -> None:
    """回归优先于 ``check_failed``：原因给"基线通过、合并树在某命令失败"（R7 固定格式）。"""
    signals = FailureSignals(
        checks=[
            {
                "command": "python -m pytest -q",
                "exit_code": 1,
                "timed_out": False,
                "output": "1 failed",
            }
        ],
        baseline_status="healthy",
        regression=True,
        total=70.0,
        runnability=50.0,
    )
    failure_class, reason = classify_failure(signals)
    assert failure_class == FAILURE_REGRESSION
    assert "基线通过" in reason
    assert "python -m pytest -q" in reason


def test_classify_check_failed_when_not_regression() -> None:
    """``regression=False`` ⇒ 仍归 ``check_failed``（互斥，不误用回归分类）。"""
    signals = FailureSignals(
        checks=[
            {
                "command": "python -m pytest -q",
                "exit_code": 1,
                "timed_out": False,
                "output": "1 failed",
            }
        ],
        baseline_status="skipped",
        regression=False,
    )
    failure_class, _reason = classify_failure(signals)
    assert failure_class == FAILURE_CHECK_FAILED


def test_classify_blocking_failed_prefixes_reason() -> None:
    """Q4：阻断项失败复用 ``check_failed``，原因带「阻断项」前缀（不新增分类）。"""
    signals = FailureSignals(
        checks=[
            {
                "command": "fe-build web",
                "exit_code": 1,
                "timed_out": False,
                "output": "vite build failed",
            }
        ],
        blocking_failed=["fe-build web"],
    )
    failure_class, reason = classify_failure(signals)
    assert failure_class == FAILURE_CHECK_FAILED
    assert reason.startswith("阻断项失败")


def test_failure_labels_cover_new_categories() -> None:
    """``baseline.FAILURE_LABELS`` 与 ``FAILURE_ORDER`` 同源（两类新分类都有中文标签）。"""
    assert FAILURE_LABELS["regression"] == "回归"
    assert FAILURE_LABELS["baseline_unhealthy"] == "基线不健康"
    assert set(FAILURE_ORDER) <= set(FAILURE_LABELS)


async def test_history_row_carries_incremental_keys(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """``_row_from_history`` 读回三键：``baseline_status`` / ``regression`` / ``comparable``。"""
    history = [
        {
            "round": 0,
            "verdict": "fail",
            "total": 70.0,
            "runnability": 50.0,
            "failed_checks": ["python -m pytest -q"],
            "failed_check_details": [
                {
                    "command": "python -m pytest -q",
                    "exit_code": 1,
                    "timed_out": False,
                    "output": "1 failed",
                }
            ],
            "artifact_version": 1,
            "degraded": False,
            "baseline_status": "healthy",
            "regression": True,
            "blocking_failed": [],
        }
    ]
    row = await _row_of_history(session_factory, tmp_path, history)
    assert row["baseline_status"] == "healthy"
    assert row["regression"] is True
    assert row["comparable"] is True
    assert row["failure_class"] == FAILURE_REGRESSION


async def test_history_row_old_entry_keeps_defaults(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """旧状态文件（无三键）→ 默认值 ``None`` / ``False`` / ``True``（渲染不变）。"""
    legacy = [
        {
            "round": 0,
            "verdict": "fail",
            "total": 70.0,
            "runnability": 80.0,
            "failed_checks": [],
            "artifact_version": 1,
            "degraded": False,
        }
    ]
    row = await _row_of_history(session_factory, tmp_path, legacy)
    assert row["baseline_status"] is None
    assert row["regression"] is False
    assert row["comparable"] is True
    assert row["failure_class"] == FAILURE_BELOW_THRESHOLD
