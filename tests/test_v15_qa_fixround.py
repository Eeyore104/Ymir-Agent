"""v1.5 **修复轮**的独立复验（QA / 严过关）—— 只验证"是否真闭合"，不采信自述。

三个修复各写三组对照：

- **D-2（requirement 绑定归属 run）**：有 run / 无 run / 都没有 三档 + 分数同尺；
- **D-1（回归未检查的披露）**：探针跑过 / 探针 skipped / 无探针 三档 + 多码顺序；
- **D-3（node-express 的 D2 可达）**：真实形态树里 ``tsc`` 真跑 + ``tsc`` 失败阻断
  + ``node-build`` 保留 ``node:*`` + **反例**（环境缺失被误报成阻断）。

★ 本文件的核心问题是"那个多出来的码是不是**有条件**的"：
若实现**无条件**追加 ``baseline_promoted_regression_unknown:1``，那么把既有用例改成
"拆分后包含"就是**掩盖问题**；本文件用"探针跑过 ⇒ 不许有该码"这一条把它钉死。

纪律：零 Docker、零模型、零真实 DB。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from test_baseline_promote import (
    TEMPLATE_ID,
    _add_stage_row,
    _produce_incremental,
    _store_report,
)
from test_eval_parity_v15 import _harness, _install_harness
from ymir.api.routers.artifacts import (
    _report_regression_checked,
)
from ymir.artifacts.store import ArtifactStore
from ymir.evaluation.deps import check_requires_deps, missing_deps_for
from ymir.evaluation.evaluators.base import run_checks
from ymir.evaluation.gate import VERDICT_ARTIFACT_NAME
from ymir.evaluation.run_task import resolve_checks
from ymir.models.entities import DEFAULT_TENANT_ID
from ymir.storage.repositories import ProjectRepository
from ymir.templates import load_template
from ymir.templates.models import image_package_map

#: 该码的文本（不用常量拼接，防止改常量时用例跟着漂）
REGRESSION_UNKNOWN = "baseline_promoted_regression_unknown:1"


def _url(project_id: str) -> str:
    return f"/api/v1/projects/{project_id}/baselines/from-merge"


def _codes(resp: Any) -> list[str]:
    warning = resp.json()["data"].get("warning") or ""
    return [code for code in warning.split(",") if code]


# ==================================================== D-1 · 披露的**触发条件**


class TestRegressionUnknownIsConditional:
    """★ 核心：这个码**只在"没做过回归判定"时**出现，不是每次都有。"""

    async def test_probe_ran_healthy_adds_no_extra_code(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        """场景 1（不误报）：同一 run 有 ``eval_verdict`` 且探针真跑过（healthy）。

        期望：201 + ``warning`` **完全为空**（既无业务码、也无披露码）。
        若这里出现了 ``regression_unknown``，说明实现无条件追加 ⇒ 6 条用例被改成
        "包含"就是掩盖问题。
        """
        from test_eval_parity_v15 import _loaded_run
        from ymir.evaluation.gate import HarnessEvaluationGate
        from ymir.sop.models import StageSpec

        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        gate_harness, provider = _harness(fx.store, session_factory, [{}, {}])
        gate = HarnessEvaluationGate(harness=gate_harness, store=fx.store)
        run = await _loaded_run(session_factory, fx.run_id)
        verdict = await gate.evaluate(
            run=run,
            stage=StageSpec(id="evaluation", role="qa", inputs=[], outputs=["evaluation_report"]),
            attempt=1,
        )
        assert verdict.status == "pass"
        assert provider.index >= 2, "探针必须真的跑过（第二个会话）"
        latest = await fx.store.latest_by_name(fx.project_id, VERDICT_ARTIFACT_NAME)
        assert latest is not None
        payload = json.loads(latest.files["evaluation_report.json"])
        assert payload["tasks"][0]["baseline_runnability"]["status"] == "healthy"

        resp = await client.post(_url(fx.project_id), json={"run_id": fx.run_id})
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"].get("warning") is None, (
            f"探针跑过却仍披露 regression_unknown ⇒ 无条件追加：{resp.text}"
        )

    async def test_probe_skipped_adds_the_code(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        """场景 2：探针跑了但一条检查都没执行（``status=skipped``）⇒ 必须披露。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name=VERDICT_ARTIFACT_NAME,
            artifact_version=fx.code_version,
            baseline="skipped",
        )
        resp = await client.post(_url(fx.project_id), json={"run_id": fx.run_id})
        assert resp.status_code == 201, resp.text
        assert _codes(resp) == [REGRESSION_UNKNOWN], resp.text

    async def test_no_probe_at_all_adds_the_code(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        """场景 3：项目维度补评测（没有 ``baseline_runnability`` 键）⇒ 必须披露。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name=VERDICT_ARTIFACT_NAME,
            artifact_version=fx.code_version,
            baseline=None,  # ← 不写 baseline_runnability（= 探针没跑）
        )
        resp = await client.post(_url(fx.project_id), json={"run_id": fx.run_id})
        assert resp.status_code == 201, resp.text
        assert _codes(resp) == [REGRESSION_UNKNOWN], resp.text

    async def test_multi_code_order_is_business_diverged_regression(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        """多码顺序：业务码 → ``criteria_diverged:1`` → ``…_regression_unknown:1``。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name=VERDICT_ARTIFACT_NAME,
            artifact_version=fx.code_version,
            blocking_failed=("python -m pytest -q",),
            baseline=None,  # 探针没跑 ⇒ 回归未检查
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="evaluation_report",
            artifact_version=fx.code_version,
            baseline=None,
        )
        resp = await client.post(_url(fx.project_id), json={"run_id": fx.run_id, "force": True})
        assert resp.status_code == 201, resp.text
        assert _codes(resp) == [
            "baseline_promoted_unhealthy:1",
            "criteria_diverged:1",
            REGRESSION_UNKNOWN,
        ], resp.text


class TestReportRegressionCheckedUnits:
    """``_report_regression_checked`` 的口径（含"会不会误判为已检查"的反例）。"""

    @staticmethod
    def _payload(**task: Any) -> dict[str, Any]:
        body = {"id": "t1", "status": "evaluated"}
        body.update(task)
        return {"tasks": [body]}

    def test_missing_key_means_not_checked(self) -> None:
        assert _report_regression_checked(self._payload()) is False

    def test_null_means_not_checked(self) -> None:
        assert _report_regression_checked(self._payload(baseline_runnability=None)) is False

    def test_skipped_probe_means_not_checked(self) -> None:
        body = self._payload(baseline_runnability={"status": "skipped", "passed": False})
        assert _report_regression_checked(body) is False

    @pytest.mark.parametrize("status", ["healthy", "unhealthy"])
    def test_real_probe_statuses_count_as_checked(self, status: str) -> None:
        body = self._payload(baseline_runnability={"status": status, "passed": status == "healthy"})
        assert _report_regression_checked(body) is True

    def test_skipped_task_is_not_evidence(self) -> None:
        """任务没执行 ⇒ 它的探针字段不能当"检查过"的证据。"""
        body = {
            "tasks": [
                {"id": "t1", "status": "skipped", "baseline_runnability": {"status": "healthy"}}
            ]
        }
        assert _report_regression_checked(body) is False

    def test_garbage_is_not_checked(self) -> None:
        assert _report_regression_checked({}) is False
        assert _report_regression_checked({"tasks": "nope"}) is False

    def test_probe_object_without_status_is_treated_as_checked(self) -> None:
        """**残余口径（记录现状）**：``baseline_runnability`` 是 dict 但**没有 status**
        （只可能来自手改正文）⇒ 当前实现算"检查过"，即**不披露**。

        这不会由写入侧产生：``BaselineProbeOut.status`` 是
        ``Literal["healthy","unhealthy","skipped"]`` 且默认 ``"skipped"``，序列化后
        必然带合法值。因此这只是"坏正文"的 fail-open（与"防串味不防伪造"同族），
        方向是"少披露"而不是"误放行"。**修它请同时改本用例**。
        """
        assert _report_regression_checked(self._payload(baseline_runnability={})) is True


# ==================================================== D-2 · requirement 绑归属 run


class TestRequirementSourceThreeStates:
    """D-2：``requirement_source`` 必须有区分度（run / caller / none）。"""

    async def test_run_requirement_wins_and_score_matches_gate(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        """有归属 run ⇒ 忽略 caller；需求与分数都必须与 gate 同版报告一致。"""
        from test_eval_parity_v15 import _loaded_run, _verdict_report
        from ymir.evaluation.gate import HarnessEvaluationGate
        from ymir.sop.models import StageSpec
        from ymir.storage.repositories import RunRepository

        fx = await _produce_incremental(session_factory, tmp_path)
        # 让两段需求对 FakeJudge 产生**不同**分数（它按 ``- [id]`` 条目数给分）：
        # run 的需求 1 条 ⇒ 4/5；caller 的占位串 0 条 ⇒ 0 分 ⇒ 总分必然不同。
        async with session_factory() as session:
            run = await RunRepository(session).get(fx.run_id)
            assert run is not None
            context = dict(run.context or {})
            context["requirement"] = "- [R-1] 在现有项目上加导出接口"
            run.context = context
            await session.commit()

        gate_harness, _gp = _harness(fx.store, session_factory, [{}, {}])
        gate = HarnessEvaluationGate(harness=gate_harness, store=fx.store)
        loaded = await _loaded_run(session_factory, fx.run_id)
        await gate.evaluate(
            run=loaded,
            stage=StageSpec(id="evaluation", role="qa", inputs=[], outputs=["evaluation_report"]),
            attempt=1,
        )
        gate_report = await _verdict_report(fx.store, fx.project_id)

        harness, _hp = _harness(fx.store, session_factory, [{}, {}])
        _install_harness(client, harness)
        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/evaluations",
            json={
                "artifact_name": "code",
                "version": fx.code_version,
                "save": True,
                "requirement": "v1.5 真跑补评测取证",  # ← 必须被忽略
            },
        )
        assert resp.status_code == 200, resp.text
        report = resp.json()["data"]

        assert report["requirement_source"] == "run"
        assert report["tasks"][0]["requirement"] == "- [R-1] 在现有项目上加导出接口"
        assert report["total_score"] == gate_report["total_score"], (
            "输入不同源 ⇒ 分数必然不同；相等才说明用的是同一段需求"
        )

    async def test_caller_requirement_used_when_no_run(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        """无归属 run（手工导入的产物）⇒ 用 caller 的，来源标 ``caller``。"""
        project_id, version = await _hand_imported_code(session_factory, tmp_path)
        harness, _hp = _harness(
            ArtifactStore(session_factory, tmp_path / "artifacts"), session_factory, [{}]
        )
        _install_harness(client, harness)

        resp = await client.post(
            f"/api/v1/projects/{project_id}/evaluations",
            json={
                "artifact_name": "code",
                "version": version,
                "save": False,
                "requirement": "手工导入的树，需求由调用方给",
            },
        )
        assert resp.status_code == 200, resp.text
        report = resp.json()["data"]
        assert report["requirement_source"] == "caller"
        assert report["tasks"][0]["requirement"] == "手工导入的树，需求由调用方给"

    async def test_none_when_neither_side_has_a_requirement(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        """两边都没有 ⇒ ``none``（且需求是空串，不是"猜"来的）。"""
        project_id, version = await _hand_imported_code(session_factory, tmp_path)
        harness, _hp = _harness(
            ArtifactStore(session_factory, tmp_path / "artifacts"), session_factory, [{}]
        )
        _install_harness(client, harness)

        resp = await client.post(
            f"/api/v1/projects/{project_id}/evaluations",
            json={"artifact_name": "code", "version": version, "save": False},
        )
        assert resp.status_code == 200, resp.text
        report = resp.json()["data"]
        assert report["requirement_source"] == "none"
        assert report["tasks"][0]["requirement"] == ""


# ==================================================== D-3 · node-express 的 D2 可达


class _RecordingSession:
    """记录 ``exec`` 的脚本化会话（按命令子串给退出码）。"""

    def __init__(self, exit_codes: dict[str, int] | None = None) -> None:
        self._exit_codes = exit_codes or {}
        self.executed: list[str] = []
        self.files: dict[str, str] = {}

    async def exec(self, cmd: str, *, timeout: float = 60.0, workdir: str | None = None) -> Any:
        from ymir.sandbox.base import ExecResult

        self.executed.append(cmd)
        code = 0
        for needle, value in self._exit_codes.items():
            if needle in cmd:
                code = value
                break
        return ExecResult(stdout="", stderr="", exit_code=code, duration_ms=1)

    async def list_files(self, path: str = ".") -> list[str]:
        return sorted(self.files)

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def read_file(self, path: str) -> str:
        return self.files[path]


NODE_TREE: dict[str, str] = {
    "src/index.ts": "export const x: number = 1;\n",
    "package.json": '{"dependencies": {"lodash": "^4.17.21"}}',
}


class TestNodeExpressD2IsReachable:
    """D-3：真实形态的 Node 树里，``tsc`` 必须**真跑**（它是现在唯一的可达 D2）。"""

    async def test_tsc_runs_while_the_build_checks_skip(self) -> None:
        spec = load_template("node-express")
        checks = resolve_checks(spec, None)
        missing, _ignored = missing_deps_for(NODE_TREE, image_package_map())
        session = _RecordingSession()

        runs = await run_checks(
            session, checks, missing_deps=missing, image_packages=image_package_map()
        )
        by_cmd = {run.command: run for run in runs}

        assert "npx tsc --noEmit" in session.executed, session.executed
        assert by_cmd["npx tsc --noEmit"].status == "passed"
        assert by_cmd["npx tsc --noEmit"].tier == "behavior"
        assert by_cmd["npx tsc --noEmit"].blocking is True
        # 构建类两条仍按环境缺失跳过（不是 fail）
        assert by_cmd["node-build ."].skipped_reason == "requires_missing_deps:lodash"
        assert by_cmd["node --test dist"].skipped_reason == "requires_missing_deps:lodash"

    async def test_tsc_failure_becomes_a_blocking_item(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        project_id = await _project_with_code(store, session_factory, NODE_TREE)
        harness, _p = _harness(store, session_factory, [{"npx tsc": 2}])

        from ymir.evaluation.run_task import build_task_for_project

        task, suite, _criteria = build_task_for_project(
            project_id=project_id, template_id="node-express"
        )
        outcome = await harness.evaluate_task(task, project_id=project_id, suite=suite)

        assert outcome.blocking_failed == ["npx tsc --noEmit"]
        assert outcome.passed is False

    def test_node_build_keeps_the_node_wildcard(self) -> None:
        """`node-build` 的 ``node:*`` 必须保留：构建确实需要树内依赖。"""
        spec = load_template("node-express")
        build = next(c for c in spec.checks if c.run == "node-build .")
        assert build.requires_deps == ["node:*"]
        assert build.requires == "node-build"
        assert build.tier == "loadable" and build.blocking is True

    def test_environment_gap_is_not_reported_as_a_blocking_failure(self) -> None:
        """★★ 反例：``tsc`` 的依赖判定只看**镜像表**，不看树里缺什么。

        树声明了镜像外的 ``lodash``（环境缺失）⇒ ``tsc`` 仍被判"可执行"；
        在真实沙箱里它必然因 ``TS2307 Cannot find module 'lodash'`` 失败 ⇒ 进
        ``blocking_failed``。**这与 T1-R2「环境缺失一律 skipped、不阻断」冲突。**

        本用例只钉住这个**机制**（判定结果），供裁决用：若将来把 tsc 改成
        "树声明了镜像外包 ⇒ 跳过"，这条会红 —— 届时请连同 docs/17 §1.2.2 一起改。
        """
        missing, _ignored = missing_deps_for(NODE_TREE, image_package_map())
        assert missing["node"] == ("lodash",)
        reason = check_requires_deps(
            ["typescript"], missing_deps=missing, image_packages=image_package_map()
        )
        assert reason == "", (
            f"tsc 声明了具体包名 ⇒ 只查镜像表；树里缺包不会让它跳过（reason={reason!r}）"
        )


# ==================================================== A6 · 四套模板覆盖复核


class TestA6StillCoversAllFourTemplates:
    """A6 迁移后是否仍覆盖四套（没有借"node 专属类"把某套的缺口藏起来）。"""

    def test_four_templates_each_declare_d1_and_d2_without_style_blocking(self) -> None:
        for template_id in ("fastapi-vue3", "python-cli", "data-pipeline", "node-express"):
            spec = load_template(template_id)
            d1 = [c for c in spec.checks if c.tier == "loadable"]
            d2 = [c for c in spec.checks if c.tier == "behavior"]
            style = [c for c in spec.checks if c.tier == "style"]
            assert d1 and all(c.blocking for c in d1), f"{template_id}: D1 缺失/未阻断"
            assert d2 and all(c.blocking for c in d2), f"{template_id}: D2 缺失/未阻断"
            assert all(not c.blocking for c in style), f"{template_id}: style 却阻断"
            for check in spec.checks:
                if check.blocking:
                    assert check.timeout_s and check.timeout_s >= 60, f"{template_id}: {check.run}"
                    assert check.group, f"{template_id}: {check.run} 缺 group"


# ==================================================== 夹具


async def _project_with_code(
    store: ArtifactStore, session_factory: Any, files: dict[str, str]
) -> str:
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="fix-round")
        await session.commit()
        project_id = project.id
    await store.save(
        project_id=project_id,
        name="code",
        artifact_type="code",
        files=files,
        created_by="user",
    )
    return project_id


async def _hand_imported_code(session_factory: Any, tmp_path: Path) -> tuple[str, int]:
    """手工导入的 code 产物：**版本行没有 run_id** ⇒ 端点反查不到归属 run。"""
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="hand-imported")
        await session.commit()
        project_id = project.id
    saved = await store.save(
        project_id=project_id,
        name="code",
        artifact_type="code",
        files={"app/main.py": "def main() -> None:\n    return None\n"},
        created_by="user",
    )
    return project_id, saved.version


__all__ = ["DEFAULT_TENANT_ID"]


class TestNoSpuriousThirdCode:
    """补一条"码集合**恰好**是这两个"的断言。

    被修复轮改过的两条用例（`TestUnverifiedEndToEnd` / `TestUiShapedRequestEndToEnd`）
    从"严格相等"改成了"拆分后包含" —— 契约确实变了（多一个披露码），但"包含"会让
    **第三个不该出现的码**溜过去。这里在**不改它们**的前提下，把该组合的码集合钉死。
    """

    async def test_unverified_without_probe_has_exactly_two_codes(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(
            session_factory,
            tmp_path,
            patch_files={
                "app/export.py": "def export() -> None:/n    return None\n",
                "requirements.txt": "python-multipart>=0.0.9\n",
            },
        )
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        harness, _p = _harness(fx.store, session_factory, [{}])
        report = await harness.evaluate_project(
            fx.project_id,
            artifact_name="code",
            version=fx.code_version,
            requirement="在现有项目上加导出接口",
            run_id=fx.run_id,
            template_id=TEMPLATE_ID,
        )
        assert report.tasks[0].unverified_groups == ["backend"]
        await report.save(
            fx.store, project_id=fx.project_id, run_id=fx.run_id, name=VERDICT_ARTIFACT_NAME
        )

        resp = await client.post(_url(fx.project_id), json={"run_id": fx.run_id})
        assert resp.status_code == 201, resp.text
        assert _codes(resp) == [
            "baseline_promoted_unverified:1",
            REGRESSION_UNKNOWN,
        ], resp.text
