"""v1.5 **QA 独立对抗**用例（Edawrd / 严过关）—— 不复用实施侧的断言口径。

与 `tests/test_*_v15*` 的区别：那些文件钉的是"实施者认为该成立"的形态，本文件
按**攻击面**组织，只写"如果实现退化成 v1.4 的形态，这一条必须红"。

攻击面 → 用例：

- **A.3 创建顺序不是判据**：直接改库里的 `artifacts.created_at`，让弱报告
  （`evaluation_report`）在遍历顺序上**先于** `eval_verdict` —— 这正是 v1.4 §0.3
  那条静默旁路的成因。实施侧用例只调换了"先存哪一份"，没有改过 `created_at`，
  因此旧实现（按 created_at + 首个结论即返回）在那些用例里也会绿。
- **A.5 产物名绑定**：`prd` 的 healthy 报告不能给 `code` 背书（HTTP 端到端）。
- **B.4 `requires_deps` 的边界**：声明镜像**没有**的包 ⇒ 跳过；声明镜像**有**的包
  ⇒ **必须真跑**（"一律跳过换绿色"是最容易造假的地方）。
- **C.2/C.3 `unverified` 语义**：真实 harness 产出的报告（不是手写 JSON）走
  `from-merge` ⇒ 201 + `baseline_promoted_unverified:1`，且 `MergedHealth.known` 为真。
- **C.4**："什么都没评"（空 tasks / 全 skipped）仍是 `""`，不能被当成"未验证"。
- **U1（as-built 未取证项）**：真实 Vue 前端（`web/package.json` 声明 vue）在本镜像
  下 `fe-build` 是否真的只能 `unverified`。

纪律：零 Docker（脚本化沙箱）、零模型（``FakeJudge``）、零真实 DB（``client`` 夹具）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import update

from test_baseline_promote import (
    TEMPLATE_ID,
    _add_stage_row,
    _produce_incremental,
    _store_report,
)
from test_eval_parity_v15 import _harness
from ymir.api.routers.artifacts import _known_merged_health, _report_health_status
from ymir.artifacts.provenance import MergedHealth
from ymir.artifacts.store import ArtifactStore
from ymir.evaluation.gate import VERDICT_ARTIFACT_NAME
from ymir.models.entities import DEFAULT_TENANT_ID, Artifact

#: 后端 pytest 全红（其余全绿）
MERGED_RED = {"python -m pytest": 1}


async def _set_created_at(
    session_factory: Any, project_id: str, name: str, when: datetime
) -> None:
    """**直接改库**：把某产物 artifact 的 ``created_at`` 挪到指定时刻。

    这是本文件里最关键的一把"攻击扳手"：``store.list_artifacts`` 按
    ``created_at asc`` 排序，v1.4 的读取侧就是靠这个顺序取"第一份能给出结论的报告"。
    实施侧用例只调换了**插入顺序**，插入顺序与 ``created_at`` 在实践中同向，
    因此它们无法证伪"读取依赖创建顺序"这一条。
    """
    async with session_factory() as session:
        await session.execute(
            update(Artifact)
            .where(Artifact.project_id == project_id, Artifact.name == name)
            .values(created_at=when)
        )
        await session.commit()


def _artifacts_url(project_id: str) -> str:
    return f"/api/v1/projects/{project_id}/baselines/from-merge"


# ===================================================== A.3 · 创建顺序不是判据


class TestCreatedAtIsNotTheCriterion:
    """★★ 攻击面 A.3：把弱报告的 ``created_at`` 改到**早于** gate 裁决。

    v1.4 的形态（按 artifact 创建顺序 + "首个结论即返回"）在这两条用例下必然返回
    ``healthy`` ⇒ 提升 201（坏树被放行）。现在的优先级 tuple 让它返回 ``unhealthy``
    ⇒ 409。两个方向都测：弱报告在前、弱报告在后。
    """

    async def test_weak_report_created_first_still_loses(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        # ① gate 先裁决（unhealthy）—— 真实时间线上它更早
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name=VERDICT_ARTIFACT_NAME,
            artifact_version=fx.code_version,
            blocking_failed=("python -m pytest -q",),
        )
        # ② 用户随后补评测（healthy），落在 evaluation_report
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="evaluation_report",
            artifact_version=fx.code_version,
        )
        # ③ ★ 攻击：把 weak report 的创建时间挪到 gate 裁决**之前**
        now = datetime.now(UTC)
        await _set_created_at(
            session_factory, fx.project_id, "evaluation_report", now - timedelta(days=3)
        )
        await _set_created_at(session_factory, fx.project_id, VERDICT_ARTIFACT_NAME, now)

        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        reading = await _known_merged_health(
            store,
            project_id=fx.project_id,
            run_id=fx.run_id,
            tenant_id=DEFAULT_TENANT_ID,
            artifact_version=fx.code_version,
            artifact_name="code",
        )
        assert reading.status == "unhealthy", "弱报告凭创建顺序赢了 ⇒ v1.4 旁路复现"
        assert reading.source_name == VERDICT_ARTIFACT_NAME
        assert reading.divergent is True

        rejected = await client.post(_artifacts_url(fx.project_id), json={"run_id": fx.run_id})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["details"]["reason"] == "baseline_unhealthy"

    async def test_weak_report_created_last_still_loses(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        """反方向：弱报告**更新**（补评测刚做完）也必须输给 gate 裁决。"""
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
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="evaluation_report",
            artifact_version=fx.code_version,
        )
        now = datetime.now(UTC)
        await _set_created_at(
            session_factory, fx.project_id, VERDICT_ARTIFACT_NAME, now - timedelta(days=3)
        )
        await _set_created_at(session_factory, fx.project_id, "evaluation_report", now)

        rejected = await client.post(_artifacts_url(fx.project_id), json={"run_id": fx.run_id})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["details"]["reason"] == "baseline_unhealthy"


# ===================================================== A.5 · 产物名绑定（HTTP）


class TestAnotherArtifactCannotEndorse:
    """攻击面 A.5：`prd` 的 healthy 报告不能给 `code` 这棵树背书。

    版本号在同一个项目里是**按产物各自递增**的，`prd` v2 与 `code` v2 完全可能同时
    存在。若读取侧只按 `(run_id, version)` 匹配，对 `prd` 评一次就能把坏 `code`
    洗成 healthy（无 force、无 warning）。
    """

    async def test_prd_report_does_not_unlock_code_promotion(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="evaluation_report",
            artifact_version=fx.code_version,
            artifact_name="prd",  # ★ 报告自称评的是 prd
        )

        rejected = await client.post(_artifacts_url(fx.project_id), json={"run_id": fx.run_id})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["details"]["reason"] == "baseline_health_unknown"

    async def test_same_report_naming_code_does_unlock(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        """对照组：同一份 healthy 报告只要自称评的是 `code`，就必须解锁（201）。"""
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="evaluation_report",
            artifact_version=fx.code_version,
            artifact_name="code",
        )

        ok = await client.post(_artifacts_url(fx.project_id), json={"run_id": fx.run_id})
        assert ok.status_code == 201, ok.text
        assert not ok.json()["data"].get("warning")


# ===================================================== B.4 · requires_deps 边界


class TestDepsBoundaryAtTemplateLevel:
    """攻击面 B.4：`requires_deps` 必须"只放过真的缺的"，不能"一律跳过换绿"。

    这里走**真实模板**（`fastapi-vue3`）+ **真实镜像包表**（`templates.models`），
    而不是裁剪过的测试用镜像表 —— 否则"镜像表里到底有没有这个包"这条事实没被验。
    """

    async def _evaluate(
        self,
        store: ArtifactStore,
        session_factory: Any,
        fx: Any,
        scripts: list[dict[str, int]],
        *,
        template_id: str | None = TEMPLATE_ID,
    ) -> Any:
        harness, _provider = _harness(store, session_factory, scripts)
        return await harness.evaluate_project(
            fx.project_id,
            suite_id="default",
            artifact_name="code",
            version=fx.code_version,
            requirement="在现有项目上加导出接口",
            run_id=fx.run_id,
            template_id=template_id,
        )

    async def test_declared_missing_dep_skips_only_the_dependent_checks(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        """声明镜像**没有**的包 ⇒ 只有依赖它的检查跳过（compileall / ruff 照跑）。"""
        fx = await _produce_incremental(
            session_factory,
            tmp_path,
            patch_files={
                "app/export.py": "def export() -> None:\n    return None\n",
                "requirements.txt": "python-multipart>=0.0.9\n",
            },
        )
        report = await self._evaluate(fx.store, session_factory, fx, [{}])
        task = report.tasks[0]

        skipped = {item.command: item.reason for item in task.skipped_checks}
        assert skipped.get("python -m pytest -q") == (
            "requires_missing_deps:python-multipart"
        ), skipped
        assert skipped.get('python -c "from app.main import app"') == (
            "requires_missing_deps:python-multipart"
        ), skipped
        assert task.blocking_failed == []
        assert task.unverified_groups == ["backend"]
        # ★ 不是"整棵树未验证"：compileall（D1）真的执行了
        executed = [
            item.ref
            for dim in task.dimensions
            for item in dim.evidence
            if item.kind == "command"
        ]
        assert "python -m compileall -q app" in executed, executed

    async def test_declared_present_dep_must_really_run(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        """★★ 反例（最容易造假的地方）：依赖在镜像表里 ⇒ `pytest` **必须真跑**。

        若实现为了"绿"把 `requires_deps: ["python:*"]` 一律判跳过，这条会红 ——
        因为技能要求"环境缺失才跳过"，而这里的包（pytest / fastapi）镜像里**有**。
        """
        fx = await _produce_incremental(
            session_factory,
            tmp_path,
            patch_files={
                "app/export.py": "def export() -> None:\n    return None\n",
                "requirements.txt": "pytest>=8.0\nfastapi>=0.115\n",
            },
        )
        report = await self._evaluate(fx.store, session_factory, fx, [MERGED_RED])
        task = report.tasks[0]

        assert [item.command for item in task.skipped_checks] == [], task.skipped_checks
        assert task.blocking_failed == ["python -m pytest -q"]
        assert task.passed is False
        # 分数照算照披露（不是 0 分糊弄）
        assert report.total_score > 0
        # B.1：verdict 归约要把"阻断项失败"与命令名都写进 reason
        from ymir.evaluation.gate import reduce_to_verdict
        from ymir.evaluation.suite import EvalSuite

        verdict = reduce_to_verdict(EvalSuite.load("default"), task, artifact_name="code")
        assert verdict.status == "fail"
        assert verdict.failure_class == "check_failed"
        assert "阻断项失败" in verdict.reason
        assert "python -m pytest -q" in verdict.reason


# ===================================================== C · unverified 语义


class TestUnverifiedEndToEnd:
    """攻击面 C：真实 harness 产出的 `unverified` 报告必须"放行 + 留痕"。

    与实施侧「手写 JSON」的用例不同：这里用**真评测**（脚本化沙箱）产出的报告，
    再让它走 `from-merge` —— 覆盖"写入侧的 ``unverified_groups`` 字段名 / 读取侧的
    判定 / 提升闸门的分支"三处是否真的接上了。
    """

    async def test_real_unverified_report_promotes_with_trace(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(
            session_factory,
            tmp_path,
            patch_files={
                "app/export.py": "def export() -> None:\n    return None\n",
                "requirements.txt": "python-multipart>=0.0.9\n",
            },
        )
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        harness, _provider = _harness(fx.store, session_factory, [{}])
        report = await harness.evaluate_project(
            fx.project_id,
            artifact_name="code",
            version=fx.code_version,
            requirement="在现有项目上加导出接口",
            run_id=fx.run_id,
            template_id=TEMPLATE_ID,
        )
        assert report.tasks[0].unverified_groups == ["backend"]
        await report.save(fx.store, project_id=fx.project_id, run_id=fx.run_id,
                          name=VERDICT_ARTIFACT_NAME)

        resp = await client.post(_artifacts_url(fx.project_id), json={"run_id": fx.run_id})
        assert resp.status_code == 201, resp.text
        # ★ v1.5 修复轮 D-1 之后这里是**两码**：补评测报告（项目维度）不跑探针
        #   ⇒ 除了"未验证"还要披露"回归未检查"。按 ``,`` 拆分后逐码断言，
        #   不用严格相等（后端顺序固定，但用例不该锁死拼接细节）。
        codes = resp.json()["data"]["warning"].split(",")
        assert "baseline_promoted_unverified:1" in codes, resp.text
        assert "baseline_promoted_regression_unknown:1" in codes, resp.text
        assert codes[0] == "baseline_promoted_unverified:1", "业务码必须排在最前"

    def test_unverified_counts_as_known(self) -> None:
        """C.3：`unverified` 必须算"已知"（否则 strict_health 会 409，违反 Q2）。"""
        assert MergedHealth(status="unverified").known is True
        assert MergedHealth(status="unverified").unverified is True
        assert MergedHealth(status="").known is False

    def test_nothing_evaluated_is_still_unknown(self) -> None:
        """C.4："什么都没评" ≠ "没验证过"：空 tasks / 全 skipped 仍返回 ``""``。"""
        assert _report_health_status('{"tasks": []}', None, None, None, None) == ""
        all_skipped = (
            '{"tasks": [{"status": "skipped", "unverified_groups": ["backend"]},'
            ' {"status": "skipped"}]}'
        )
        assert _report_health_status(all_skipped, None, None, None, None) == ""


# ===================================================== U1 · 真实 Vue 前端


class TestU1RealVueFrontendIsUnverified:
    """as-built §7.3 U1 的复核：镜像缺 `vue`/`vite` ⇒ 真实前端的构建检查只能跳过。

    这不是"实施者说了算"的口径 —— 它由"树里声明了什么 ∩ 镜像表里有什么"决定。
    本用例用一棵真实的 `web/package.json`（声明 vue + vite）跑**真模板**的
    `fe-build web`，确认它落到 `unverified` 而不是 `failed`，也确认**不能**落到
    `healthy`（否则就是"没验证过却宣称验过了"）。
    """

    async def test_fe_build_is_skipped_for_a_real_vue_package_json(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        web_pkg = (
            '{"name": "web", "dependencies": {"vue": "^3.4.0"},'
            ' "devDependencies": {"vite": "^5.0.0"}}'
        )
        fx = await _produce_incremental(
            session_factory,
            tmp_path,
            patch_files={
                "app/export.py": "def export() -> None:\n    return None\n",
                "web/package.json": web_pkg,
            },
        )
        harness, _provider = _harness(fx.store, session_factory, [{}])
        report = await harness.evaluate_project(
            fx.project_id,
            artifact_name="code",
            version=fx.code_version,
            requirement="加一个前端请求封装",
            run_id=fx.run_id,
            template_id=TEMPLATE_ID,
        )
        task = report.tasks[0]
        skipped = {item.command: item.reason for item in task.skipped_checks}
        assert skipped.get("fe-build web") == "requires_missing_deps:vite", skipped
        assert task.blocking_failed == []
        assert "frontend_build" in task.unverified_groups


# ===================================================== U3 · 补评测的 regression


class TestU3RegressionAsymmetry:
    """as-built §7.3 U3 的复核：补评测路径的 ``regression`` 恒 False。

    U3 的结论是"方向安全（读取侧取更严的 ``eval_verdict``）"。本类证明这个
    "安全"是**有条件的**：只有当同一 run 存在 ``eval_verdict`` 时才成立。
    若只有补评测报告（例如 CLI 未加 ``--evaluate``、或 run 内 gate 没落盘），
    一棵"相对基线回归"的树会被判 ``healthy`` 并**静默**提升（无 warning）。

    场景刻意选 **D3（ruff）回归**：它是"能跑、只是不规范"，因此不进
    ``blocking_failed`` —— 唯一能揭示它的信号就是 ``regression``，而补评测路径
    恰好没有这个信号（``EvalTask.baseline_ref`` 为 None ⇒ 探针不跑）。
    """

    async def _gate_and_supplement(self, fx: Any, session_factory: Any) -> tuple[Any, Any]:
        from test_eval_parity_v15 import _loaded_run
        from ymir.evaluation.gate import HarnessEvaluationGate
        from ymir.models.entities import PipelineRun  # noqa: F401
        from ymir.sop.models import StageSpec

        gate_harness, _gp = _harness(fx.store, session_factory, [{"ruff": 1}, {}])
        gate = HarnessEvaluationGate(harness=gate_harness, store=fx.store)
        run = await _loaded_run(session_factory, fx.run_id)
        stage = StageSpec(id="evaluation", role="qa", inputs=[], outputs=["evaluation_report"])
        await gate.evaluate(run=run, stage=stage, attempt=1)
        latest = await fx.store.latest_by_name(fx.project_id, VERDICT_ARTIFACT_NAME)
        gate_report = json.loads(latest.files["evaluation_report.json"])

        sup_harness, _sp = _harness(fx.store, session_factory, [{"ruff": 1}])
        supplement = await sup_harness.evaluate_project(
            fx.project_id,
            artifact_name="code",
            version=fx.code_version,
            requirement="在现有项目上加导出接口",
            run_id=fx.run_id,
            template_id=TEMPLATE_ID,
        )
        return gate_report, supplement

    async def test_gate_sees_the_regression_supplement_does_not(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        gate_report, supplement = await self._gate_and_supplement(fx, session_factory)

        assert gate_report["tasks"][0]["blocking_failed"] == []
        assert gate_report["tasks"][0]["regression"] is True
        assert _report_health_status(json.dumps(gate_report), None, None, None, None) == (
            "unhealthy"
        )

        # ★ 同尺裂缝：同一棵树、同一条 ruff 失败，补评测报告里没有 regression
        assert supplement.tasks[0].blocking_failed == []
        assert supplement.tasks[0].regression is False
        assert _report_health_status(
            supplement.to_json(), None, None, None, None
        ) == "healthy"

    async def test_supplement_only_tree_is_promoted_with_regression_unknown(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        """★ 修复轮 D-1 之后的契约：没有 `eval_verdict` 时，回归树**仍** 201，
        但必须披露 `baseline_promoted_regression_unknown:1`（不再静默）。

        ★ 这里**不改判定**（仍是 201）：根治要把「基线引用」引进项目维度评测
        （`docs/17` §5「明确不做」第 6 条，v1.6 候选）；本版只保证"没验过"被说出来。
        """
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        _gate_report, supplement = await self._gate_and_supplement(fx, session_factory)
        await supplement.save(
            fx.store, project_id=fx.project_id, run_id=fx.run_id, name="evaluation_report"
        )
        # 只删掉 gate 的那份，模拟"run 内没有裁决"（CLI 未加 --evaluate 的真实形态）
        async with session_factory() as session:
            from sqlalchemy import delete

            from ymir.models.entities import Artifact

            await session.execute(
                delete(Artifact).where(
                    Artifact.project_id == fx.project_id,
                    Artifact.name == VERDICT_ARTIFACT_NAME,
                )
            )
            await session.commit()

        resp = await client.post(_artifacts_url(fx.project_id), json={"run_id": fx.run_id})
        assert resp.status_code == 201, resp.text
        # ★ D-1：补评测报告没有探针字段 ⇒ "回归未检查"必须被披露
        warning = resp.json()["data"].get("warning") or ""
        assert "baseline_promoted_regression_unknown:1" in warning.split(","), resp.text


# ===================================================== 缺陷记录 · requirement 串味


class TestRequirementIsBoundToTheRun:
    """★ 修复轮 D-2 之后的契约：**有归属 run 时，`POST /evaluations` 忽略 caller 的 requirement**。

    修复前的裂缝（v1.5 真跑 R-a `04703115`）：同一棵树，gate 的 `eval_verdict v2`
    total=**92.65**（需求维 100.00），补评测 `evaluation_report v1` total=**62.65**
    （需求维 **0.00**，调用方传了占位串），其余三维逐位相同 —— 差值恰好是该维权重。

    现在的判据：
    1. `requirement` 取自归属 run（报告 `tasks[0].requirement` 等于 run 的那段）；
    2. 报告根部 `requirement_source == "run"`（自证输入同源）；
    3. `total_score` 与 gate 同版本的报告**一致**（同尺 = 判据同源 + 输入同源）。
    """

    async def test_caller_requirement_is_ignored_when_run_is_known(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        from test_eval_parity_v15 import _install_harness, _loaded_run, _verdict_report
        from ymir.evaluation.gate import VERDICT_ARTIFACT_NAME as GATE_NAME
        from ymir.evaluation.gate import HarnessEvaluationGate
        from ymir.sop.models import StageSpec

        fx = await _produce_incremental(session_factory, tmp_path)
        # 让 run 的需求能被 FakeJudge 打分（它按 ``- [id]`` 条目数给分）——
        # 这样"用 run 的 requirement"与"用 caller 的"分数**必然不同**，断言才有鉴别力。
        async with session_factory() as session:
            from ymir.storage.repositories import RunRepository

            run = await RunRepository(session).get(fx.run_id)
            assert run is not None
            context = dict(run.context or {})
            context["requirement"] = "- [R-1] 在现有项目上加导出接口"
            run.context = context
            await session.commit()

        # ① gate 裁决（run 维度，含探针）⇒ 基准分数
        gate_harness, _gp = _harness(fx.store, session_factory, [{}, {}])
        gate = HarnessEvaluationGate(harness=gate_harness, store=fx.store)
        loaded = await _loaded_run(session_factory, fx.run_id)
        stage = StageSpec(id="evaluation", role="qa", inputs=[], outputs=["evaluation_report"])
        await gate.evaluate(run=loaded, stage=stage, attempt=1)
        gate_report = await _verdict_report(fx.store, fx.project_id)
        assert GATE_NAME == VERDICT_ARTIFACT_NAME

        # ② 补评测：caller 传一段**完全不相干**的 requirement
        harness, _provider = _harness(fx.store, session_factory, [{}, {}])
        _install_harness(client, harness)
        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/evaluations",
            json={
                "artifact_name": "code",
                "version": fx.code_version,
                "save": True,
                "requirement": "v1.5 真跑补评测取证",  # ← 应被忽略
            },
        )
        assert resp.status_code == 200, resp.text
        report = resp.json()["data"]

        # ③ 三条判据
        assert report["requirement_source"] == "run"
        assert report["tasks"][0]["requirement"] == "- [R-1] 在现有项目上加导出接口"
        assert report["tasks"][0]["requirement"] != "v1.5 真跑补评测取证"
        assert report["total_score"] == gate_report["total_score"], (
            "同一棵树、同一需求 ⇒ 分数必须与 gate 一致（输入同尺）"
        )


# ===================================================== I · 真实用户路径（Web）


class TestUiShapedRequestEndToEnd:
    """攻击面 I：前端的**逐字**请求体（无 ``run_id``、无 ``requirement``）。

    `web/src/views/EvalReport.vue` 只发 ``artifact_name`` / ``version`` / ``save``。
    服务端必须自己反查出归属 run 与模板，否则报告拿不到 ``criteria_source="template"``
    ⇒ 提升闸门永远 unknown（v1.4 的老痛点）。

    ★ 与真跑取证的差别：这里刻意**不传** ``requirement`` —— 传了就等于替服务端
    选了一把尺子（v1.5 真跑的补评测就是传了占位串，requirement 维度被静默判 0）。
    """

    async def test_frontend_body_shape_unlocks_a_healthy_tree(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        from test_eval_parity_v15 import _install_harness

        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        harness, _provider = _harness(fx.store, session_factory, [{}])
        _install_harness(client, harness)

        resp = await client.post(
            f"/api/v1/projects/{fx.project_id}/evaluations",
            json={"artifact_name": "code", "version": fx.code_version, "save": True},
        )
        assert resp.status_code == 200, resp.text
        report = resp.json()["data"]
        # 服务端反查：run 归属 + 判据来源
        assert report["run_id"] == fx.run_id
        assert report["artifact_version"] == fx.code_version
        assert report["criteria_source"] == "template"
        assert report["template_id"] == TEMPLATE_ID
        # requirement 来自归属 run（不是空串、也不是调用方塞的）
        assert report["tasks"][0]["requirement"] == "在现有项目上加导出接口"

        promoted = await client.post(_artifacts_url(fx.project_id), json={"run_id": fx.run_id})
        assert promoted.status_code == 201, promoted.text
        # ★ 修复轮 D-1：这条路只有补评测报告（无 eval_verdict ⇒ 无探针）
        #   ⇒ "回归未检查"必须披露；否则"没验过回归"与"没有回归"长得一样。
        warning = promoted.json()["data"].get("warning") or ""
        assert "baseline_promoted_regression_unknown:1" in warning.split(","), promoted.text


