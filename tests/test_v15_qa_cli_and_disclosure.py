"""v1.5 **QA 独立对抗**用例（第二批）：T4 CLI 落盘 + 读取侧披露的边界。

只写实施侧用例**没有覆盖**的形态：

- **E.1 / A19**：落盘是**新版本**，且既有一版**逐字节未被改写**（覆盖 = 破坏产物
  不可变契约；这里用内容快照 + content_hash 双证）。
- **E.2**：CLI 落盘**之后** `from-merge` 真的不再是 `baseline_health_unknown`
  （HTTP 层，不是 `_known_merged_health` 的函数层）。
- **E.6 / A17**：**什么都没落** ⇒ 仍 409 unknown（口径不放松），且落盘失败时文案
  必须是 `absent` 而**不能**是 pass（不谎报）。
- **D 的边界**：败者若是一份"给不出结论"的报告（v1.4 风格 / 版本不匹配），
  **不得**被算成 `criteria_diverged`（否则 warning 会变成噪声，用户无法分辨）。
- **单码场景**：`force` + `unhealthy`、以及 `unverified`，warning 必须**恰好一个码**。

纪律：零 Docker、零模型、零真实 DB。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_baseline_promote import (
    TEMPLATE_ID,
    _add_stage_row,
    _produce_incremental,
    _store_report,
)
from test_cli_verdict import CLI
from test_eval_parity_v15 import _harness
from ymir.api.routers.artifacts import CRITERIA_DIVERGED_CODE
from ymir.artifacts.provenance import (
    BASELINE_PROMOTED_UNHEALTHY_CODE,
    BASELINE_PROMOTED_UNVERIFIED_CODE,
)
from ymir.evaluation.gate import VERDICT_ARTIFACT_NAME

#: 报告产物名（CLI 落盘用的那个）
VERDICT_NAME = VERDICT_ARTIFACT_NAME


def _url(project_id: str) -> str:
    return f"/api/v1/projects/{project_id}/baselines/from-merge"


async def _project_report(fx: Any, session_factory: Any, scripts: list[dict[str, int]]) -> Any:
    """用真实装配点产出一份"项目维度"的报告（与 CLI `--evaluate` 同一条路径）。"""
    harness, _provider = _harness(fx.store, session_factory, scripts)
    return await harness.evaluate_project(
        fx.project_id,
        artifact_name="code",
        version=fx.code_version,
        requirement="在现有项目上加导出接口",
        run_id=fx.run_id,
        template_id=TEMPLATE_ID,
    )


class TestCliPersistenceIsAppendOnly:
    """E.1 / A19：CLI 落盘是**新版本**，既有版本一个字节都不许动。"""

    async def test_second_persist_keeps_the_first_version_intact(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        report = await _project_report(fx, session_factory, [{}])

        first = await CLI.persist_verdict(
            fx.store, report, run_id=fx.run_id, project_id=fx.project_id
        )
        assert first is not None and first.version == 1
        artifact_id = _artifact_id(await fx.store.list_artifacts(fx.project_id), VERDICT_NAME)
        loaded_v1 = await fx.store.load(artifact_id, 1)
        snapshot_v1 = dict(loaded_v1.files)
        hash_v1 = loaded_v1.version_row.content_hash

        second = await CLI.persist_verdict(
            fx.store, report, run_id=fx.run_id, project_id=fx.project_id
        )
        assert second is not None and second.version == 2

        again = await fx.store.load(loaded_v1.artifact.id, 1)
        assert dict(again.files) == snapshot_v1, "v1 被覆写了（产物不可变契约被破坏）"
        assert again.version_row.content_hash == hash_v1

    async def test_persisted_payload_carries_the_three_keys(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        """A13 三件套 + 判据来源：``run_id`` / ``artifact_version`` / ``verdict_source``。"""
        import json

        fx = await _produce_incremental(session_factory, tmp_path)
        report = await _project_report(fx, session_factory, [{}])
        await CLI.persist_verdict(fx.store, report, run_id=fx.run_id, project_id=fx.project_id)

        latest = await fx.store.latest_by_name(fx.project_id, VERDICT_ARTIFACT_NAME)
        assert latest is not None
        payload = json.loads(latest.files["evaluation_report.json"])
        assert payload["run_id"] == fx.run_id
        assert payload["artifact_version"] == fx.code_version
        assert payload["verdict_source"] == "harness"
        assert payload["criteria_source"] == "template"
        assert payload["template_id"] == TEMPLATE_ID


class TestCliUnlocksPromotionOverHttp:
    """E.2：落盘**之后** `from-merge` 不再是 `baseline_health_unknown`。"""

    async def test_cli_verdict_unlocks_the_http_gate(
        self, client: Any, session_factory: Any, tmp_path: Path
    ) -> None:
        fx = await _produce_incremental(session_factory, tmp_path)
        await _add_stage_row(
            session_factory, run_id=fx.run_id, artifact_id=fx.code_id, version=fx.code_version
        )
        # 落盘之前：409 unknown（fail-closed，与 A17 同口径）
        before = await client.post(_url(fx.project_id), json={"run_id": fx.run_id})
        assert before.status_code == 409, before.text
        assert before.json()["details"]["reason"] == "baseline_health_unknown"

        report = await _project_report(fx, session_factory, [{}])
        await CLI.persist_verdict(
            fx.store, report, run_id=fx.run_id, project_id=fx.project_id
        )

        after = await client.post(_url(fx.project_id), json={"run_id": fx.run_id})
        assert after.status_code == 201, after.text
        # ★ 修复轮 D-1：落盘的这份是**项目维度**评测（无探针）⇒ 回归未检查要披露。
        #   A14 的"解锁"判据是"不再是 unknown"，与"是否还有披露码"无关。
        warning = after.json()["data"].get("warning") or ""
        assert "baseline_promoted_regression_unknown:1" in warning.split(","), after.text


class TestPersistFailureNeverClaimsPass:
    """E.5：落盘失败时文案必须是 `absent` —— 不许把"没落盘"说成"通过"。"""

    async def test_failed_persist_yields_absent_line(self, capsys: Any) -> None:
        class _BrokenReport:
            async def save(self, *args: Any, **kwargs: Any) -> Any:
                raise OSError("disk full")

        from ymir.common.contracts import EvalVerdict

        saved = await CLI.persist_verdict(
            object(), _BrokenReport(), run_id="r-1", project_id="p-1"
        )
        assert saved is None
        assert "结论未落盘" in capsys.readouterr().err

        lines = CLI.verdict_lines(
            EvalVerdict(status="pass", reason="双门槛通过"),
            saved_version=saved,
            artifact_version=3,
        )
        assert len(lines) == 1
        assert "absent（未裁决）" in lines[0]
        assert "pass" not in lines[0]


class TestDivergenceOnlyCountsReadableLosers:
    """D 的边界：败者"给不出结论"时**不得**披露分歧（否则 warning 变噪声）。"""

    async def test_legacy_loser_does_not_create_divergence(
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
            name=VERDICT_ARTIFACT_NAME,
            artifact_version=fx.code_version,
            blocking_failed=("python -m pytest -q",),
        )
        # 败者是一份 v1.4 风格报告（无判据三键）⇒ 读取侧给不出结论 ⇒ 不算分歧
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="evaluation_report",
            artifact_version=fx.code_version,
            criteria_source=None,
            template_id=None,
        )
        resp = await client.post(_url(fx.project_id), json={"run_id": fx.run_id, "force": True})
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["warning"] == BASELINE_PROMOTED_UNHEALTHY_CODE

    async def test_loser_for_another_version_does_not_create_divergence(
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
            name=VERDICT_ARTIFACT_NAME,
            artifact_version=fx.code_version,
        )
        # 败者绑的是**另一版**树（版本号不匹配）⇒ 对本棵树给不出结论 ⇒ 不算分歧
        await _store_report(
            session_factory,
            tmp_path,
            run_id=fx.run_id,
            project_id=fx.project_id,
            name="evaluation_report",
            artifact_version=fx.code_version + 41,
            blocking_failed=("python -m pytest -q",),
        )
        resp = await client.post(_url(fx.project_id), json={"run_id": fx.run_id})
        assert resp.status_code == 201, resp.text
        assert not resp.json()["data"].get("warning"), resp.text


class TestSingleCodeStaysExact:
    """单码场景的 warning 必须**恰好**是一个码（既有断言不破）。"""

    async def test_unverified_single_report_is_exactly_one_code(
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
            name=VERDICT_ARTIFACT_NAME,
            artifact_version=fx.code_version,
            unverified_groups=("backend",),
        )
        resp = await client.post(_url(fx.project_id), json={"run_id": fx.run_id})
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["warning"] == BASELINE_PROMOTED_UNVERIFIED_CODE

    async def test_divergent_code_is_not_appended_without_a_second_report(
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
            name=VERDICT_ARTIFACT_NAME,
            artifact_version=fx.code_version,
            blocking_failed=("python -m pytest -q",),
        )
        resp = await client.post(_url(fx.project_id), json={"run_id": fx.run_id, "force": True})
        assert resp.status_code == 201, resp.text
        warning = resp.json()["data"]["warning"]
        assert warning == BASELINE_PROMOTED_UNHEALTHY_CODE
        assert CRITERIA_DIVERGED_CODE not in warning


def _artifact_id(artifacts: list[Any], name: str) -> str:
    for artifact in artifacts:
        if getattr(artifact, "name", "") == name:
            return str(artifact.id)
    raise AssertionError(f"没有名为 {name} 的产物")
