"""T05.1 验收线的**端到端**部分（v1.5，docs/17 §4：A1 / A8 / A9 / A11 / A12）。

**为什么单独一个文件**：`test_blocking_tiers.py` / `test_deps_probe.py` /
`test_criteria_sources.py` / `test_baseline_promote.py` 钉的是**机制**（判据、探针、
装配、读取侧）。本文件只钉**跨模块的真实链路**：

- **A8（同尺总断言）**：同一个 run / 同一 `code` 版本，
  `HarnessEvaluationGate.evaluate` 与 `POST /projects/{id}/evaluations {save:true}`
  两份报告的 ``blocking_failed`` / ``total_score`` / ``runnability`` / ``passed`` /
  命令证据**完全相等** —— 任何口径漂移都会让这条红。
- **A9（闸门不可绕过）**：坏树经 gate 判 ``unhealthy`` 后，对**同一版本**补评测
  不能让结论变宽松 ⇒ ``from-merge`` 仍然 409。
- **A11（历史报告 fail-closed）**：v1.4 风格报告（无判据三键）⇒ 409 unknown；
  补判据后重评 ⇒ 正常参与判定。
- **A12（报告自证）**：``artifact_version`` / ``run_id`` / ``artifact_name`` /
  判据来源四者均非空（无模板时是**空串**而不是 null）。
- **A1（后端起红不再是 healthy）**：verdict ``fail`` + ``blocking_failed`` 含
  ``python -m pytest -q`` + ``from-merge`` 409 ``baseline_unhealthy`` + ``force`` 201。

**关键夹具技巧（脚本化会话按"第几次会话"给不同结论）**：
``harness.evaluate_task`` 先开一个会话跑合并树、再开一个会话跑基线探针。若两者用
同一份脚本，"合并树 pytest 红"必然也把基线判成 unhealthy ⇒ verdict 会退化成
``unknown(baseline_unhealthy)``，测不到 A1/A9 想测的东西。因此本文件用
:class:`_ScriptedProvider` **按会话序号**发脚本：第 1 次 = 合并树、第 2 次 = 探针。

**已知且刻意保留的不对称（A8 的边界，如实标注）**：补评测路径没有 ``PipelineRun``
⇒ ``EvalTask.baseline_ref`` 为空 ⇒ ``harness._probe`` 直接返回 ``None``（**不开**探针
会话）。因此补充报告**没有** ``baseline_runnability``，``regression`` 恒为 ``False``。
影响面：只有 ``regression`` 这一条判据（分数 / ``blocking_failed`` / ``passed`` 全部
不受影响 —— 那是 A8 明列的断言）。方向是**安全的**：冲突时读取侧取
``eval_verdict``（更严的那份）并披露 ``criteria_diverged:1``，不会放行坏树。
本版不改这条语义（把 ``baseline_ref`` 引进项目维度会牵动基线解析，属 v1.5 §5 的
"明确不做"第 6 条范围）。

纪律：零 Docker（脚本化沙箱）、零模型（``FakeJudge``）、零真实 DB（``client`` 夹具）。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from test_baseline_promote import TEMPLATE_ID, _add_stage_row, _produce_incremental, _store_report
from test_evaluation import FakeJudge, FakeSandboxSession
from ymir.artifacts.provenance import template_yaml_hash
from ymir.artifacts.store import ArtifactStore
from ymir.evaluation.gate import VERDICT_ARTIFACT_NAME, HarnessEvaluationGate
from ymir.evaluation.harness import EvaluationHarness
from ymir.models.entities import DEFAULT_TENANT_ID
from ymir.sop.models import StageSpec
from ymir.storage.repositories import RunRepository
from ymir.templates import DEFAULT_TEMPLATES_DIR

#: 坏树形态（A1/A9）：合并树 pytest 红，其余全绿
MERGED_RED = {"python -m pytest": 1}
#: 健康基线形态：探针侧全绿（否则 verdict 会退化成 unknown(baseline_unhealthy)）
PROBE_GREEN: dict[str, int] = {}


class _ScriptedProvider:
    """按**会话序号**发脚本的 provider（第 n 次 ``session()`` 用第 n 份脚本）。

    序号超界时重复最后一份（真跑里"多次评测"是常态，重复最后一份最不意外）。
    """

    def __init__(self, scripts: list[dict[str, int]]) -> None:
        self._scripts = list(scripts) or [{}]
        self.index = 0
        self.sessions: list[FakeSandboxSession] = []

    @asynccontextmanager
    async def session(self):  # noqa: ANN201
        script = self._scripts[min(self.index, len(self._scripts) - 1)]
        self.index += 1
        inner = FakeSandboxSession(exit_codes=dict(script))
        self.sessions.append(inner)
        yield inner


def _harness(
    store: ArtifactStore,
    session_factory: Any,
    scripts: list[dict[str, int]],
) -> tuple[EvaluationHarness, _ScriptedProvider]:
    provider = _ScriptedProvider(scripts)
    harness = EvaluationHarness(
        store=store,
        judge=FakeJudge(),
        sandbox_provider=provider,
        session_factory=session_factory,
    )
    return harness, provider


def _stage() -> StageSpec:
    """评测阶段（gate 落 ``eval_verdict`` 时用它做 ``stage_id``）。"""
    return StageSpec(id="evaluation", role="qa", inputs=[], outputs=["evaluation_report"])


async def _loaded_run(session_factory: Any, run_id: str) -> Any:
    async with session_factory() as session:
        run = await RunRepository(session).get(run_id)
    assert run is not None
    return run


async def _verdict_report(store: ArtifactStore, project_id: str) -> dict[str, Any]:
    """读最近一版 ``eval_verdict`` 的报告 JSON（gate 落盘产物）。"""
    latest = await store.latest_by_name(project_id, VERDICT_ARTIFACT_NAME)
    assert latest is not None, "gate 没有落 eval_verdict"
    return json.loads(latest.files["evaluation_report.json"])


def _install_harness(client: Any, harness: EvaluationHarness) -> None:
    """把脚本化 harness 装进测试 app（替掉默认的假 sandbox provider）。"""
    client._transport.app.state.evaluations = harness


def _evidence(report: dict[str, Any]) -> list[tuple[str, str]]:
    """从报告里抽「命令证据」的 ``(ref, detail)`` 序列（checks 的行为指纹）。

    报告不存 ``checks`` 本身（那是装配中间物），存的是**每条命令的执行证据**。
    ``checks`` 的**逐字段相等**由 ``tests/test_criteria_sources.py`` 在任务级钉住；
    本文件的 A8 断言"同一次评测产出的证据链逐条相同"，两者合起来才是"同尺"。
    """
    out: list[tuple[str, str]] = []
    for task in report.get("tasks", []):
        for dim in task.get("dimensions", []):
            if dim.get("name") != "runnability":
                continue
            for item in dim.get("evidence", []):
                if item.get("kind") == "command":
                    out.append((str(item.get("ref")), str(item.get("detail"))))
    return out


# ============================================================ A8 · 同尺总断言


class TestA8SameRuler:
    """A8：gate 与 Web 补评测对同一棵树必须给出**同一个判定**。"""

    async def test_supplementary_eval_matches_gate(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)

        # ---- ① gate 裁决（run 内路径）----
        gate_harness, gate_provider = _harness(
            fx.store, session_factory, [MERGED_RED, PROBE_GREEN]
        )
        gate = HarnessEvaluationGate(harness=gate_harness, store=fx.store)
        run = await _loaded_run(session_factory, fx.run_id)
        verdict = await gate.evaluate(run=run, stage=_stage(), attempt=1)
        gate_report = await _verdict_report(fx.store, fx.project_id)

        # ---- ② Web 补评测（项目维度路径，同一版本）----
        http_harness, http_provider = _harness(
            fx.store, session_factory, [MERGED_RED, PROBE_GREEN]
        )
        _install_harness(client, http_harness)
        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/evaluations",
            json={
                "artifact_name": "code",
                "version": fx.code_version,
                "suite_id": "default",
                "save": True,
                "requirement": "在现有项目上加导出接口",
            },
        )
        assert resp.status_code == 200, resp.text
        supplement = resp.json()["data"]

        # ---- ③ 同尺断言 ----
        assert gate_provider.index >= 2  # gate 路径：合并树 + 基线探针各一次会话
        assert http_provider.index >= 1  # 补评测路径：只有合并树（无 baseline_ref ⇒ 不探基线）
        assert supplement["tasks"][0]["blocking_failed"] == gate_report["tasks"][0][
            "blocking_failed"
        ]
        assert supplement["total_score"] == gate_report["total_score"]
        assert supplement["runnability"] == gate_report["runnability"]
        assert supplement["passed"] == gate_report["passed"]
        # 命令证据逐条相同（"同一个判定"的可观察形态）
        assert _evidence(supplement) == _evidence(gate_report)
        # 裁决与报告也不能互相矛盾（verdict 是 gate 的归约）
        assert verdict.status == ("pass" if gate_report["passed"] else "fail")
        assert list(verdict.blocking_failed) == list(gate_report["tasks"][0]["blocking_failed"])

    async def test_both_paths_use_the_template_criteria(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        """A8 的判据层：两份报告都自证 ``criteria_source="template"`` + 同模板同指纹。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        gate_harness, _gp = _harness(fx.store, session_factory, [MERGED_RED, PROBE_GREEN])
        gate = HarnessEvaluationGate(harness=gate_harness, store=fx.store)
        run = await _loaded_run(session_factory, fx.run_id)
        await gate.evaluate(run=run, stage=_stage(), attempt=1)
        gate_report = await _verdict_report(fx.store, fx.project_id)

        http_harness, _hp = _harness(fx.store, session_factory, [MERGED_RED, PROBE_GREEN])
        _install_harness(client, http_harness)
        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/evaluations",
            json={"artifact_name": "code", "version": fx.code_version, "save": True},
        )
        supplement = resp.json()["data"]
        expected_hash = template_yaml_hash(DEFAULT_TEMPLATES_DIR, TEMPLATE_ID)

        for report in (gate_report, supplement):
            assert report["criteria_source"] == "template"
            assert report["template_id"] == TEMPLATE_ID
            assert report["criteria_version"] == expected_hash


# ============================================================ A1 · 后端起红


class TestA1BackendRedIsNotHealthy:
    """A1：后端 ``pytest`` 全红的树必须被判不可交付。"""

    async def test_backend_pytest_failure_blocks_promotion(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        gate_harness, _gp = _harness(fx.store, session_factory, [MERGED_RED, PROBE_GREEN])
        gate = HarnessEvaluationGate(harness=gate_harness, store=fx.store)
        run = await _loaded_run(session_factory, fx.run_id)

        verdict = await gate.evaluate(run=run, stage=_stage(), attempt=1)

        # ① verdict 层
        assert verdict.status == "fail"
        assert verdict.failure_class == "check_failed"
        assert "阻断项失败" in verdict.reason
        assert "python -m pytest -q" in verdict.reason
        assert list(verdict.blocking_failed) == ["python -m pytest -q"]
        assert verdict.total_score > 0  # 分数照算照披露（不是 0 分糊弄）
        # ② 落盘报告层
        report = await _verdict_report(fx.store, fx.project_id)
        assert report["tasks"][0]["blocking_failed"] == ["python -m pytest -q"]
        assert report["tasks"][0]["passed"] is False
        # ③ 提升闸门层
        url = f"/api/v1/projects/{fx.project_id}/baselines/from-merge"
        rejected = await client.post(url, json={"run_id": fx.run_id})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["details"]["reason"] == "baseline_unhealthy"

        forced = await client.post(url, json={"run_id": fx.run_id, "force": True})
        assert forced.status_code == 201, forced.text
        assert forced.json()["data"]["warning"] == "baseline_promoted_unhealthy:1"


# ============================================================ A9 · 闸门不可绕过


class TestA9SupplementaryCannotBypass:
    """★★ A9：这是"弱口径静默放行坏树"那条旁路的**总钉子**。"""

    async def test_supplementary_eval_cannot_bypass_unhealthy_gate(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        # ① gate 已判 unhealthy（坏树）
        gate_harness, _gp = _harness(fx.store, session_factory, [MERGED_RED, PROBE_GREEN])
        gate = HarnessEvaluationGate(harness=gate_harness, store=fx.store)
        run = await _loaded_run(session_factory, fx.run_id)
        verdict = await gate.evaluate(run=run, stage=_stage(), attempt=1)
        assert verdict.status == "fail"

        # ② 对**同一版本**补评测
        http_harness, _hp = _harness(fx.store, session_factory, [MERGED_RED, PROBE_GREEN])
        _install_harness(client, http_harness)
        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/evaluations",
            json={
                "artifact_name": "code",
                "version": fx.code_version,
                "save": True,
                "requirement": "在现有项目上加导出接口",
            },
        )
        assert resp.status_code == 200, resp.text
        supplement = resp.json()["data"]
        # ★ 前半段：补评测的 blocking_failed **非空**（T03 之前这里恒为空 ⇒ 旁路）
        assert supplement["tasks"][0]["blocking_failed"] == ["python -m pytest -q"]
        assert supplement["passed"] is False

        # ★ 后半段：提升仍然 409（不得 201）
        rejected = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["details"]["reason"] == "baseline_unhealthy"


# ============================================================ A11 · 历史报告


class TestA11LegacyReportFailClosed:
    """A11：无判据来源字段的报告 = 弱口径 = **不作为提升依据**。"""

    async def test_legacy_report_without_criteria_is_unknown(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        # v1.4 风格报告：无 criteria_source / template_id / criteria_version，且"看起来健康"
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            artifact_version=fx.code_version,
            criteria_source=None,
            template_id=None,
        )
        url = f"/api/v1/projects/{fx.project_id}/baselines/from-merge"

        rejected = await client.post(url, json={"run_id": fx.run_id})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["details"]["reason"] == "baseline_health_unknown"

    async def test_re_evaluation_makes_the_tree_promotable(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        """A11 的后半句："补齐判据来源后重评 ⇒ 正常参与判定"。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            artifact_version=fx.code_version,
            criteria_source=None,
            template_id=None,
        )
        # 重评一次（健康的树：探针与合并树都全绿）
        http_harness, _hp = _harness(fx.store, session_factory, [{}, {}])
        _install_harness(client, http_harness)
        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/evaluations",
            json={
                "artifact_name": "code",
                "version": fx.code_version,
                "save": True,
                "requirement": "在现有项目上加导出接口",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["criteria_source"] == "template"

        promoted = await client.post(
            f"/api/v1/projects/{fx.project_id}/baselines/from-merge",
            json={"run_id": fx.run_id},
        )
        assert promoted.status_code == 201, promoted.text
        # ★ 修复轮 D-1：重评出来的仍是**项目维度**报告（无探针）
        #   ⇒ 除"判据强口径"外还要披露"回归未检查"。A11 要验的是"不再 409 unknown"。
        warning = promoted.json()["data"].get("warning") or ""
        assert "baseline_promoted_regression_unknown:1" in warning.split(","), promoted.text


# ============================================================ A12 · 报告自证


class TestA12ReportSelfDescribes:
    """A12：报告四要素非空（无模板时 ``template_id`` 是**空串**，不是 null）。"""

    async def test_report_self_describes_criteria_source(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        http_harness, _hp = _harness(fx.store, session_factory, [{}, {}])
        _install_harness(client, http_harness)
        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/evaluations",
            json={
                "artifact_name": "code",
                "version": fx.code_version,
                "save": True,
                "requirement": "在现有项目上加导出接口",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]

        # ① jq 四键（契约 C）：
        #   curl POST /evaluations | jq '.data | {artifact_version, run_id,
        #                                        template_id, criteria_version}'
        assert data["artifact_version"] == fx.code_version
        assert data["run_id"] == fx.run_id
        assert data["template_id"] == TEMPLATE_ID
        assert data["criteria_version"] == template_yaml_hash(
            DEFAULT_TEMPLATES_DIR, TEMPLATE_ID
        )
        assert data["criteria_source"] == "template"
        # ② artifact_name 在任务级（报告根部的"产物身份"就是它）
        assert data["tasks"][0]["artifact_ref"]["name"] == "code"
        # ③ 四个键都**不是** null（空值必须是空串）
        for key in ("artifact_version", "run_id", "template_id", "criteria_version"):
            assert data[key] is not None, f"{key} 不该是 null"
        # ④ 落盘的那一版与响应同源
        stored = await ArtifactStore(session_factory, tmp_path / "artifacts").latest_by_name(
            fx.project_id, "evaluation_report"
        )
        assert stored is not None
        payload = json.loads(stored.files["evaluation_report.json"])
        assert payload["template_id"] == TEMPLATE_ID
        assert payload["run_id"] == fx.run_id

    async def test_suite_criteria_reports_empty_strings_not_null(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        """无模板归属 ⇒ ``criteria_source="suite"`` + 空串（A12 的"显式空"要求）。"""
        from ymir.storage.repositories import ProjectRepository

        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        async with session_factory() as session:
            project = await ProjectRepository(session).create(name="no-template")
            await session.commit()
            project_id = project.id
        await store.save(
            project_id=project_id,
            name="code",
            artifact_type="code",
            files={"main.py": "def main():\n    return None\n"},
        )
        http_harness, _hp = _harness(store, session_factory, [{}, {}])
        _install_harness(client, http_harness)

        resp = await client.post(
            f"/api/v1/projects/{project_id}/evaluations",
            json={"artifact_name": "code", "save": True, "requirement": "x"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["criteria_source"] == "suite"
        assert data["template_id"] == ""
        assert data["criteria_version"] == ""
        assert data["template_id"] is not None


# ============================================================ D-2 · 需求同源


class TestD2RequirementIsShared:
    """★ v1.5 修复轮 D-2：补评测的 ``requirement`` 必须与 gate **同源**。

    修复前的实测（真跑 R-a `04703115`）：同一棵树，gate 的 `eval_verdict v2`
    total=**92.65**（需求维 100.00），补评测的 `evaluation_report v1` total=**62.65**
    （需求维 **0.00**，因为调用方传的是 `requirement="v1.5 真跑补评测取证"`）。
    其余三维逐位相同，差值恰好是该维权重 × 100。
    ⇒ 判据同尺的前提是**输入同尺**：有归属 run 就用 run 的需求。
    """

    async def test_supplementary_eval_ignores_caller_requirement(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        # 让 run 的 requirement 可被 FakeJudge 打分（它按 ``- [id]`` 条目数给分）
        async with session_factory() as session:
            run = await RunRepository(session).get(fx.run_id)
            assert run is not None
            context = dict(run.context or {})
            context["requirement"] = "- [R-1] 在现有项目上加导出接口"
            run.context = context
            await session.commit()

        harness, _provider = _harness(fx.store, session_factory, [{}, {}])
        _install_harness(client, harness)
        url = f"/api/v1/projects/{fx.project_id}/evaluations"

        first = await client.post(
            url,
            json={
                "artifact_name": "code",
                "version": fx.code_version,
                "save": True,
                "requirement": "完全不同的另一段需求（不带条目）",
            },
        )
        second = await client.post(
            url,
            json={
                "artifact_name": "code",
                "version": fx.code_version,
                "save": False,
                "requirement": "- [R-9] 又一段不相干的需求",
            },
        )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        first_data = first.json()["data"]
        second_data = second.json()["data"]
        # ★ 核心断言：调用方换 requirement 不该改分
        assert first_data["total_score"] == second_data["total_score"]
        # 且报告自证"我用的是 run 的需求"
        assert first_data["requirement_source"] == "run"
        assert second_data["requirement_source"] == "run"

    async def test_orphan_artifact_uses_caller_requirement(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        """无归属 run（手工导入的产物）⇒ 才允许用调用方的 requirement。"""
        from ymir.storage.repositories import ProjectRepository

        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        async with session_factory() as session:
            project = await ProjectRepository(session).create(name="orphan-req")
            await session.commit()
            project_id = project.id
        await store.save(
            project_id=project_id,
            name="code",
            artifact_type="code",
            files={"app/main.py": "def main():\n    return None\n"},
            run_id=None,
        )
        harness, _provider = _harness(store, session_factory, [{}, {}])
        _install_harness(client, harness)

        resp = await client.post(
            f"/api/v1/projects/{project_id}/evaluations",
            json={
                "artifact_name": "code",
                "save": False,
                "requirement": "- [R-1] 手工导入产物的需求",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["requirement_source"] == "caller"
        assert data["run_id"] is None

    async def test_missing_requirement_is_labelled_none(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        """两边都没有需求 ⇒ ``requirement_source="none"``（显式，不猜）。"""
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        from ymir.storage.repositories import ProjectRepository

        async with session_factory() as session:
            project = await ProjectRepository(session).create(name="no-req")
            await session.commit()
            project_id = project.id
        await store.save(
            project_id=project_id,
            name="code",
            artifact_type="code",
            files={"app/main.py": "def main():\n    return None\n"},
        )
        harness, _provider = _harness(store, session_factory, [{}, {}])
        _install_harness(client, harness)

        resp = await client.post(
            f"/api/v1/projects/{project_id}/evaluations",
            json={"artifact_name": "code", "save": False},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["requirement_source"] == "none"


# ============================================================ 夹具自检


@pytest.mark.parametrize("scripts", [([MERGED_RED, PROBE_GREEN]), ([{}, {}])])
async def test_scripted_provider_feeds_sessions_in_order(
    session_factory: Any, tmp_path: Path, scripts: list[dict[str, int]]
) -> None:
    """夹具自检：会话按序号取脚本（否则 A1/A9 会静默测错东西）。"""
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    _harness_obj, provider = _harness(store, session_factory, scripts)
    from ymir.sandbox.base import NetworkPolicy

    async with provider.session() as first:  # type: ignore[union-attr]
        assert first.network_policy is NetworkPolicy.RESTRICTED
        first_result = await first.exec("python -m pytest -q")
    async with provider.session() as second:  # type: ignore[union-attr]
        second_result = await second.exec("python -m pytest -q")
    assert provider.index == 2
    assert first_result.exit_code == (1 if "python -m pytest" in scripts[0] else 0)
    assert second_result.exit_code == (1 if "python -m pytest" in scripts[1] else 0)


def test_default_tenant_is_used() -> None:
    """（防误改）本文件的夹具走默认租户，与 ``client`` 夹具一致。"""
    assert DEFAULT_TENANT_ID == "default"
