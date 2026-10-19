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
