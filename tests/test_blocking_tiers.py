"""T03：blocking 判据扩容（v1.5，docs/17 §1.2 / §2-T03）的钉子。

**要防的裂缝（PRD §2-T1）**：「后端 pytest 全红、前端能构建」的交付树被判 ``healthy``
并放行提升 —— 因为 ``fastapi-vue3.yaml`` 里只有 ``fe-build web`` 标了 ``blocking``。
闸门的词是"不可交付"：D1（读不进来）/ D2（行为不符）必须阻断，D3（风格/类型）只扣分。

本文件按子项组织：

- **T03.2**：``tier`` / ``requires_deps`` / ``requires_build_output`` / ``no_tests_exit_code``
  必须穿过 ``templates.loader`` 的 ``CheckSpec(...)`` **重建块**与 ``run_task.resolve_checks``
  —— 两处"忘转发 = 静默消失"（本版最阴的两个坑）；
- **T03.3**：加载期校验 24 / 25 / 26 各有正反例；
- **T03.5**：``run_checks`` 的依赖缺失 / 无用例 / 缺构建物三条跳过路径；
- **T03.7**：``harness`` 按「组 × 档」算 ``unverified_groups`` 并追加两条 warning；
- **T03.8**：四套模板**同口径**（参数化，A6）。

纪律：零 Docker（脚本化假沙箱）、零真实模型（``FakeJudge``）、模板用**真实的**
``config/templates/*.yaml``（``tier`` 与 ``template_hash`` 才有真值）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ymir.artifacts.store import ArtifactStore
from ymir.common.errors import InvalidParamError
from ymir.evaluation.evaluators.base import (
    MISSING_BUILD_OUTPUT_PREFIX,
    MISSING_DEPS_PREFIX,
    NO_TESTS_COLLECTED,
    UNVERIFIED_GROUP_PREFIX,
    EvalCheck,
    run_checks,
)
from ymir.evaluation.harness import EvaluationHarness
from ymir.evaluation.run_task import build_task_for_project
from ymir.templates import DEFAULT_TEMPLATES_DIR, load_template
from ymir.templates.loader import TemplateDefinitionError

#: 真实的四套模板（A6 参数化的数据源）
TEMPLATE_IDS: tuple[str, ...] = (
    "fastapi-vue3",
    "python-cli",
    "data-pipeline",
    "node-express",
)

#: 镜像包表（测试用：与 ``templates.models`` 的两张表**同形**，但内容可裁剪）
IMAGE: dict[str, list[str]] = {
    "python": ["pytest", "ruff", "fastapi", "pandas"],
    "node": ["express", "typescript"],
}


# --------------------------------------------------------------------- 夹具


class _Session:
    """脚本化沙箱会话（按命令子串映射退出码；``executed`` 记录真的跑过什么）。"""

    def __init__(
        self, *, exit_codes: dict[str, int] | None = None, timeouts: tuple[str, ...] = ()
    ) -> None:
        self._exit_codes = exit_codes or {}
        self._timeouts = tuple(timeouts)
        self.files: dict[str, str] = {}
        self.executed: list[str] = []

    async def exec(self, cmd: str, *, timeout: float = 60.0, workdir: str | None = None) -> Any:
        from ymir.sandbox.base import ExecResult

        self.executed.append(cmd)
        if any(needle in cmd for needle in self._timeouts):
            return ExecResult(
                stdout="", stderr="", exit_code=None, duration_ms=60000, timed_out=True
            )
        code = 0
        for needle, value in self._exit_codes.items():
            if needle in cmd:
                code = value
                break
        return ExecResult(stdout="", stderr="", exit_code=code, duration_ms=1)

    async def read_file(self, path: str) -> str:
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def list_files(self, path: str = ".") -> list[str]:
        return sorted(self.files)

    async def set_network_policy(self, policy: Any) -> None:
        return None


class _Provider:
    """假 provider（复用同一会话，记录生命周期）。"""

    def __init__(self, session: _Session | None = None) -> None:
        from contextlib import asynccontextmanager

        self.session_obj = session or _Session()

        @asynccontextmanager
        async def session():  # noqa: ANN202
            yield self.session_obj

        self.session = session


def _write_template(
    tmp_path: Path,
    checks: list[dict[str, Any]],
    *,
    layout: dict[str, Any] | None = None,
    skeleton: list[dict[str, Any]] | None = None,
    language: str = "python",
) -> str:
    """铺一个最小可加载模板（只控制 checks/layout/skeleton 三处），返回模板 id。"""
    template_id = "tmp-tpl"
    payload: dict[str, Any] = {
        "id": template_id,
        "version": "1.0",
        "name": "临时模板",
        "stack": {"language": language},
        "layout": dict(layout or {"required_dirs": ["app"]}),
        "checks": checks,
    }
    if skeleton:
        payload["skeleton"] = {"files": skeleton}
    (tmp_path / f"{template_id}.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8"
    )
    return template_id


# ============================================================ T03.2 · 加载期转发


class TestCheckSpecForwarding:
    """★ T03.2：``_validate`` 的 ``CheckSpec(...)`` 重建块必须转发四个新字段。

    忘转发的后果是「YAML 写了 ``tier``、加载后静默变成 ``""``」——**没有任何报错**，
    下游全部按空档位处理（``blocking`` 还是 YAML 里的值，所以连阻断都不受影响，
    只有 ``unverified`` / 依赖探针 / 无用例 / 级联跳过四项静默失效）。
    """

    def test_tier_survives_the_rebuild(self) -> None:
        """完成判据（docs/17 §2-T03 表格）：读回 ``tier == "behavior"``。"""
        spec = load_template("fastapi-vue3")
        tiers = {check.run: check.tier for check in spec.checks}
        assert tiers["python -m pytest -q"] == "behavior"
        assert tiers["python -m compileall -q app"] == "loadable"
        assert tiers['python -c "from app.main import app"'] == "loadable"
        assert tiers["fe-build web"] == "loadable"
        assert tiers['ruff check --per-file-ignores "app/**:B008" .'] == "style"

    def test_requires_deps_survives_the_rebuild(self) -> None:
        spec = load_template("fastapi-vue3")
        by_run = {check.run: check for check in spec.checks}
        assert by_run["python -m pytest -q"].requires_deps == ["python:*"]
        assert by_run["fe-build web"].requires_deps == ["node:*"]
        assert by_run["python -m compileall -q app"].requires_deps == []

    def test_no_tests_exit_code_survives_the_rebuild(self) -> None:
        spec = load_template("fastapi-vue3")
        by_run = {check.run: check for check in spec.checks}
        assert by_run["python -m pytest -q"].no_tests_exit_code == 5
        assert by_run["python -m compileall -q app"].no_tests_exit_code is None

    def test_requires_build_output_survives_the_rebuild(self) -> None:
        spec = load_template("node-express")
        by_run = {check.run: check for check in spec.checks}
        assert by_run["node --test dist"].requires_build_output == "dist/"
        assert by_run["node-build ."].requires_build_output == ""

    def test_resolve_checks_forwards_all_four(self) -> None:
        """★ 第二处"忘转发"：模板 ``CheckSpec`` → ``EvalCheck``（``run_task.resolve_checks``）。"""
        task, _suite, _criteria = build_task_for_project(
            project_id="proj-t03", template_id="node-express"
        )
        by_run = {check.run: check for check in task.checks}
        assert by_run["node --test dist"].tier == "behavior"
        assert by_run["node --test dist"].requires_build_output == "dist/"
        assert by_run["node --test dist"].requires_deps == ["node:*"]
        fastapi_task, _s, _c = build_task_for_project(
            project_id="proj-t03", template_id="fastapi-vue3"
        )
        pytest_check = next(c for c in fastapi_task.checks if c.run == "python -m pytest -q")
        assert pytest_check.no_tests_exit_code == 5
        assert pytest_check.tier == "behavior"

    def test_image_packages_are_injected_by_the_assembly_point(self) -> None:
        """``image_packages`` 只能由装配点注入（否则环境探针静默失效）。"""
        task, _suite, _criteria = build_task_for_project(project_id="proj-t03")
        assert "python" in task.image_packages
        assert "node" in task.image_packages
        assert "pytest" in task.image_packages["python"]


# ============================================================ T03.3 · 校验 24/25/26


class TestValidation24Tiers:
    """校验 24：``tier`` ↔ ``blocking`` 一致 + ``blocking`` 必带 ``timeout_s >= 60``。"""

    def test_tier_values_are_read_back(self, tmp_path: Path) -> None:
        template_id = _write_template(
            tmp_path,
            [
                {
                    "type": "command",
                    "run": "python -m compileall -q app",
                    "tier": "loadable",
                    "blocking": True,
                    "timeout_s": 120,
                }
            ],
        )
        spec = load_template(template_id, templates_dir=tmp_path)
        assert spec.checks[0].tier == "loadable"

    def test_unknown_tier_is_rejected(self, tmp_path: Path) -> None:
        template_id = _write_template(
            tmp_path,
            [
                {
                    "type": "command",
                    "run": "x",
                    "tier": "critical",
                    "blocking": True,
                    "timeout_s": 120,
                }
            ],
        )
        with pytest.raises(TemplateDefinitionError) as excinfo:
            load_template(template_id, templates_dir=tmp_path)
        assert "校验 24" in str(excinfo.value)
        assert "tier" in str(excinfo.value)

    @pytest.mark.parametrize("tier", ["loadable", "behavior"])
    def test_blocking_tiers_must_be_blocking(self, tmp_path: Path, tier: str) -> None:
        """D1/D2 是"不可交付" ⇒ 标了档位却不阻断 = 模板作者自相矛盾。"""
        template_id = _write_template(
            tmp_path,
            [{"type": "command", "run": "x", "tier": tier, "blocking": False}],
        )
        with pytest.raises(TemplateDefinitionError) as excinfo:
            load_template(template_id, templates_dir=tmp_path)
        assert "校验 24" in str(excinfo.value)
        assert "blocking" in str(excinfo.value)

    def test_style_tier_must_not_be_blocking(self, tmp_path: Path) -> None:
        """D3 只扣分 ⇒ 标成阻断会让大量"能用但不够好"的树被判 fail（v1.4 B008 事故）。"""
        template_id = _write_template(
            tmp_path,
            [
                {
                    "type": "command",
                    "run": "ruff check .",
                    "tier": "style",
                    "blocking": True,
                    "timeout_s": 120,
                }
            ],
        )
        with pytest.raises(TemplateDefinitionError) as excinfo:
            load_template(template_id, templates_dir=tmp_path)
        assert "校验 24" in str(excinfo.value)
        assert "style" in str(excinfo.value)

    def test_blocking_requires_explicit_timeout(self, tmp_path: Path) -> None:
        """PRD T1-R4：``blocking`` 必须显式声明 ``timeout_s``，不得沿用默认值。"""
        template_id = _write_template(
            tmp_path, [{"type": "command", "run": "x", "tier": "loadable", "blocking": True}]
        )
        with pytest.raises(TemplateDefinitionError) as excinfo:
            load_template(template_id, templates_dir=tmp_path)
        assert "校验 24" in str(excinfo.value)
        assert "timeout_s" in str(excinfo.value)

    def test_blocking_timeout_floor(self, tmp_path: Path) -> None:
        """``timeout_s`` 低于 60 ⇒ 拒（后端检查产品要求 ≥120，这里 60 是硬底线）。"""
        template_id = _write_template(
            tmp_path,
            [
                {
                    "type": "command",
                    "run": "x",
                    "tier": "behavior",
                    "blocking": True,
                    "timeout_s": 30,
                }
            ],
        )
        with pytest.raises(TemplateDefinitionError) as excinfo:
            load_template(template_id, templates_dir=tmp_path)
        assert "校验 24" in str(excinfo.value)

    def test_tier_less_blocking_check_still_allowed(self, tmp_path: Path) -> None:
        """``tier`` 留空（老口径）⇒ 只受 timeout 约束，不与 blocking 冲突。"""
        template_id = _write_template(
            tmp_path,
            [{"type": "command", "run": "x", "blocking": True, "timeout_s": 120}],
        )
        spec = load_template(template_id, templates_dir=tmp_path)
        assert spec.checks[0].tier == ""
        assert spec.checks[0].blocking is True


class TestValidation25RequiresDeps:
    """校验 25：token 语法 + 具体包名 ∈ 镜像表 + ``*`` 词的语言表非空。"""

    def test_valid_tokens_load(self, tmp_path: Path) -> None:
        template_id = _write_template(
            tmp_path,
            [
                {"type": "command", "run": "x", "requires_deps": ["python:*"]},
                {"type": "command", "run": "y", "requires_deps": ["pandas"]},
            ],
        )
        spec = load_template(template_id, templates_dir=tmp_path)
        assert spec.checks[0].requires_deps == ["python:*"]
        assert spec.checks[1].requires_deps == ["pandas"]

    def test_unknown_package_is_rejected(self, tmp_path: Path) -> None:
        """★ typo 必须加载期红：否则它会变成"永久静默未验证"。"""
        template_id = _write_template(
            tmp_path,
            [{"type": "command", "run": "x", "requires_deps": ["pkg-not-in-image"]}],
        )
        with pytest.raises(TemplateDefinitionError) as excinfo:
            load_template(template_id, templates_dir=tmp_path)
        assert "校验 25" in str(excinfo.value)
        assert "pkg-not-in-image" in str(excinfo.value)

    def test_unknown_wildcard_is_rejected(self, tmp_path: Path) -> None:
        template_id = _write_template(
            tmp_path, [{"type": "command", "run": "x", "requires_deps": ["rust:*"]}]
        )
        with pytest.raises(TemplateDefinitionError) as excinfo:
            load_template(template_id, templates_dir=tmp_path)
        assert "校验 25" in str(excinfo.value)

    def test_duplicate_token_is_rejected(self, tmp_path: Path) -> None:
        template_id = _write_template(
            tmp_path,
            [{"type": "command", "run": "x", "requires_deps": ["python:*", "python:*"]}],
        )
        with pytest.raises(TemplateDefinitionError) as excinfo:
            load_template(template_id, templates_dir=tmp_path)
        assert "校验 25" in str(excinfo.value)

    def test_empty_token_is_rejected(self, tmp_path: Path) -> None:
        template_id = _write_template(
            tmp_path, [{"type": "command", "run": "x", "requires_deps": ["  "]}]
        )
        with pytest.raises(TemplateDefinitionError) as excinfo:
            load_template(template_id, templates_dir=tmp_path)
        assert "校验 25" in str(excinfo.value)

    def test_node_wildcard_needs_the_node_table(self, tmp_path: Path, monkeypatch: Any) -> None:
        """``node:*`` 依赖 **node 生态表**非空（空表 ⇒ 全部永久 skip ⇒ 必须加载期拒）。"""
        from ymir.templates import models as models_module

        monkeypatch.setattr(models_module, "SANDBOX_IMAGE_NODE_PACKAGES", frozenset())
        template_id = _write_template(
            tmp_path, [{"type": "command", "run": "x", "requires_deps": ["node:*"]}]
        )
        with pytest.raises(TemplateDefinitionError) as excinfo:
            load_template(template_id, templates_dir=tmp_path)
        assert "校验 25" in str(excinfo.value)
        assert "node:*" in str(excinfo.value)


class TestValidation26BuildOutput:
    """校验 26：``requires_build_output`` 形态 + 不得与源码目录/骨架重叠。"""

    def test_valid_build_output_loads(self, tmp_path: Path) -> None:
        template_id = _write_template(
            tmp_path,
            [{"type": "command", "run": "node --test dist", "requires_build_output": "dist/"}],
        )
        spec = load_template(template_id, templates_dir=tmp_path)
        assert spec.checks[0].requires_build_output == "dist/"

    def test_missing_trailing_slash_is_rejected(self, tmp_path: Path) -> None:
        template_id = _write_template(
            tmp_path,
            [{"type": "command", "run": "node --test dist", "requires_build_output": "dist"}],
        )
        with pytest.raises(TemplateDefinitionError) as excinfo:
            load_template(template_id, templates_dir=tmp_path)
        assert "校验 26" in str(excinfo.value)

    def test_overlap_with_required_dirs_is_rejected(self, tmp_path: Path) -> None:
        """★ 这条防的正是"把 ``web/`` 写成构建产物、抹平 v1.3.1 Q1 的不对称"。"""
        template_id = _write_template(
            tmp_path,
            [{"type": "command", "run": "x", "requires_build_output": "app/"}],
            layout={"required_dirs": ["app"]},
        )
        with pytest.raises(TemplateDefinitionError) as excinfo:
            load_template(template_id, templates_dir=tmp_path)
        assert "校验 26" in str(excinfo.value)
        assert "重叠" in str(excinfo.value)

    def test_overlap_with_skeleton_is_rejected(self, tmp_path: Path) -> None:
        template_id = _write_template(
            tmp_path,
            [{"type": "command", "run": "x", "requires_build_output": "dist/"}],
            skeleton=[{"path": "dist/index.js", "content": "module.exports = 1\n"}],
        )
        with pytest.raises(TemplateDefinitionError) as excinfo:
            load_template(template_id, templates_dir=tmp_path)
        assert "校验 26" in str(excinfo.value)

    def test_forbidden_dir_is_not_a_conflict(self) -> None:
        """``node-express`` 的 ``forbidden_dirs`` 含 ``dist``：构建产物不是源码 ⇒ 不冲突。"""
        spec = load_template("node-express")
        assert "dist" in spec.layout.forbidden_dirs
        assert {c.run: c.requires_build_output for c in spec.checks}["node --test dist"] == "dist/"


# ============================================================ T03.5 · run_checks


class TestRunChecksEnvironment:
    """``run_checks`` 的三条新跳过路径（A2 / A4 / A7 的机制层）。"""

    async def test_missing_deps_are_skipped_not_failed(self) -> None:
        """A2：交付树声明了镜像没有的包 ⇒ ``skipped(requires_missing_deps:<pkg>)``。"""
        session = _Session()
        check = EvalCheck(run="python -m pytest -q", requires_deps=["python:*"], blocking=True)
        runs = await run_checks(
            session,
            [check],
            missing_deps={"python": ("pkg-not-in-image",), "node": ()},
            image_packages=IMAGE,
        )
        assert runs[0].status == "skipped"
        assert runs[0].skipped_reason == f"{MISSING_DEPS_PREFIX}pkg-not-in-image"
        # 没跑过检查 ⇒ 连主命令都没执行
        assert session.executed == []

    async def test_declared_dep_present_still_runs(self) -> None:
        """反例（A2）：依赖在镜像里 ⇒ 检查**必须真的执行**，不许一律跳过。"""
        session = _Session()
        check = EvalCheck(run="python -m pytest -q", requires_deps=["python:*"], blocking=True)
        runs = await run_checks(
            session,
            [check],
            missing_deps={"python": (), "node": ()},
            image_packages=IMAGE,
        )
        assert runs[0].status == "passed"
        assert session.executed == ["python -m pytest -q"]

    async def test_concrete_package_absent_is_skipped(self) -> None:
        session = _Session()
        check = EvalCheck(run='python -c "import numpy"', requires_deps=["numpy"])
        runs = await run_checks(session, [check], missing_deps={}, image_packages=IMAGE)
        assert runs[0].skipped_reason == f"{MISSING_DEPS_PREFIX}numpy"
        assert session.executed == []

    async def test_no_tests_exit_code_maps_to_skipped(self) -> None:
        """A4：pytest 退出码 5（未收集到用例）⇒ ``skipped(no_tests_collected)``。"""
        session = _Session(exit_codes={"python -m pytest": 5})
        check = EvalCheck(
            run="python -m pytest -q", blocking=True, no_tests_exit_code=5
        )
        runs = await run_checks(session, [check], image_packages=IMAGE)
        assert runs[0].status == "skipped"
        assert runs[0].skipped_reason == NO_TESTS_COLLECTED
        assert runs[0].exit_code == 5  # 退出码保留（可追溯）
        assert runs[0].blocking is True  # 档位信息不丢，但 skipped 不进 blocking_failed

    async def test_other_nonzero_exit_is_still_failed(self) -> None:
        """反例：退出码 1（真失败）**不得**被误映射成 skipped。"""
        session = _Session(exit_codes={"python -m pytest": 1})
        check = EvalCheck(run="python -m pytest -q", blocking=True, no_tests_exit_code=5)
        runs = await run_checks(session, [check], image_packages=IMAGE)
        assert runs[0].status == "failed"

    async def test_timeout_is_still_failed(self) -> None:
        """超时按 failed 处理（PRD T1-R4：超时的树确实跑不动），**不**走无用例映射。"""
        session = _Session(timeouts=("python -m pytest",))
        check = EvalCheck(run="python -m pytest -q", blocking=True, no_tests_exit_code=5)
        runs = await run_checks(session, [check], image_packages=IMAGE)
        assert runs[0].status == "failed"
        assert runs[0].timed_out is True

    async def test_missing_build_output_cascades_to_skipped(self) -> None:
        """A7：``requires_build_output`` 缺失 ⇒ 级联 ``skipped``（不判 fail）。"""
        session = _Session()
        check = EvalCheck(
            run="node --test dist", requires_build_output="dist/", blocking=True
        )
        runs = await run_checks(session, [check], image_packages=IMAGE)
        assert runs[0].status == "skipped"
        assert runs[0].skipped_reason == f"{MISSING_BUILD_OUTPUT_PREFIX}dist/"
        assert session.executed == []

    async def test_build_output_present_runs_the_check(self) -> None:
        session = _Session()
        await session.write_file("dist/index.js", "export const x = 1;\n")
        check = EvalCheck(
            run="node --test dist", requires_build_output="dist/", blocking=True
        )
        runs = await run_checks(session, [check], image_packages=IMAGE)
        assert runs[0].status == "passed"
        assert session.executed == ["node --test dist"]

    async def test_tier_is_copied_into_check_run(self) -> None:
        """``CheckRun.tier`` 是 ``harness`` 判「组 × 档」的唯一依据，必须抄到。"""
        session = _Session()
        check = EvalCheck(run="x", group="backend", tier="behavior")
        runs = await run_checks(session, [check], image_packages=IMAGE)
        assert runs[0].tier == "behavior"
        assert runs[0].group == "backend"
        assert runs[0].to_dict()["tier"] == "behavior"

    async def test_deps_unknown_table_does_not_block(self) -> None:
        """镜像表未注入（老调用方）⇒ 不做依赖判定 ⇒ 检查照常执行。"""
        session = _Session()
        check = EvalCheck(run="python -m pytest -q", requires_deps=["python:*"])
        runs = await run_checks(session, [check])
        assert runs[0].status == "passed"


# ============================================================ T03.7 · 未验证


async def _project_with_code(
    store: ArtifactStore, session_factory: Any, files: dict[str, str]
) -> str:
    """铺一个只有 ``code`` 产物的项目（``files`` 由用例决定，便于控制依赖声明）。"""
    from ymir.storage.repositories import ProjectRepository

    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="t03")
        await session.commit()
        project_id = project.id
    await store.save(project_id=project_id, name="code", artifact_type="code", files=files)
    return project_id


def _harness(store: ArtifactStore, session_factory: Any, session: _Session) -> EvaluationHarness:
    from test_evaluation import FakeJudge

    return EvaluationHarness(
        store=store,
        judge=FakeJudge(),
        sandbox_provider=_Provider(session),
        session_factory=session_factory,
    )


class TestUnverifiedGroups:
    """★ T03.7：按「组 × 档」判未验证（**不是**按整组），并追加两条 warning。"""

    async def test_missing_deps_marks_group_unverified(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        """A2 的核心：``compileall`` 跑过了，但 ``behavior`` 档全跳过 ⇒ backend 未验证。"""
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        files = {
            "app/main.py": "def main():\n    return None\n",
            "requirements.txt": "pkg-not-in-image\n",
        }
        project_id = await _project_with_code(store, session_factory, files)
        session = _Session()
        harness = _harness(store, session_factory, session)
        task, suite, _criteria = build_task_for_project(
            project_id=project_id, template_id="fastapi-vue3"
        )

        outcome = await harness.evaluate_task(task, project_id=project_id, suite=suite)

        # ① 环境缺失不阻断
        assert outcome.blocking_failed == []
        # ② 跳过原因点名了具体的包（与"镜像没这个程序"可区分）
        reasons = [item.reason for item in outcome.skipped_checks]
        assert any(r == f"{MISSING_DEPS_PREFIX}pkg-not-in-image" for r in reasons)
        # ③ 「组 × 档」判：backend 的 behavior 档全跳过
        assert outcome.unverified_groups == ["backend"]
        assert f"{UNVERIFIED_GROUP_PREFIX}backend" in outcome.warnings
        # ④ compileall 真的跑过（不是"整组全跳过"）
        assert "python -m compileall -q app" in session.executed
        assert "python -m pytest -q" not in session.executed

    async def test_empty_tests_dir_is_skipped(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        """A4：退出码 5 ⇒ 报告披露 ``no_tests_collected``，且不算失败。"""
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        files = {"app/main.py": "def main():\n    return None\n"}
        project_id = await _project_with_code(store, session_factory, files)
        session = _Session(exit_codes={"python -m pytest": 5})
        harness = _harness(store, session_factory, session)
        task, suite, _criteria = build_task_for_project(
            project_id=project_id, template_id="fastapi-vue3"
        )

        outcome = await harness.evaluate_task(task, project_id=project_id, suite=suite)

        assert outcome.blocking_failed == []
        assert NO_TESTS_COLLECTED in outcome.warnings
        assert NO_TESTS_COLLECTED in [item.reason for item in outcome.skipped_checks]
        assert outcome.unverified_groups == ["backend"]

    async def test_compileall_failure_is_blocking(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        """A3：语法错误（``compileall`` 失败）⇒ D1 ⇒ ``blocking_failed`` 含该命令。

        这条与 A1 是**不同档**：``compileall`` 不 import ⇒ 环境缺失不会跳过它，
        "解释器读不进来"永远是阻断项（PRD T1-R3 的 D1）。
        """
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        files = {"app/main.py": "def main(:\n    return None\n"}  # 语法错误
        project_id = await _project_with_code(store, session_factory, files)
        session = _Session(exit_codes={"python -m compileall": 1})
        harness = _harness(store, session_factory, session)
        task, suite, _criteria = build_task_for_project(
            project_id=project_id, template_id="fastapi-vue3"
        )

        outcome = await harness.evaluate_task(task, project_id=project_id, suite=suite)

        assert "python -m compileall -q app" in outcome.blocking_failed
        assert outcome.passed is False

    async def test_backend_failure_is_unhealthy_not_unverified(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        files = {
            "app/main.py": "def main():\n    return None\n",
            "requirements.txt": "fastapi\n",
        }
        project_id = await _project_with_code(store, session_factory, files)
        session = _Session(exit_codes={"python -m pytest": 1})
        harness = _harness(store, session_factory, session)
        task, suite, _criteria = build_task_for_project(
            project_id=project_id, template_id="fastapi-vue3"
        )

        outcome = await harness.evaluate_task(task, project_id=project_id, suite=suite)

        assert outcome.blocking_failed == ["python -m pytest -q"]
        assert outcome.passed is False
        assert outcome.unverified_groups == []

    async def test_ruff_failure_is_not_blocking(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        """A5：``ruff`` 失败只扣分 ⇒ ``blocking_failed`` 仍空（D3 不阻断）。"""
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        files = {
            "app/main.py": "def main():\n    return None\n",
            "requirements.txt": "fastapi\n",
        }
        project_id = await _project_with_code(store, session_factory, files)
        session = _Session(exit_codes={"ruff check": 1})
        harness = _harness(store, session_factory, session)
        task, suite, _criteria = build_task_for_project(
            project_id=project_id, template_id="fastapi-vue3"
        )

        outcome = await harness.evaluate_task(task, project_id=project_id, suite=suite)

        assert outcome.blocking_failed == []
        assert outcome.unverified_groups == []

    async def test_ignored_dependency_lines_are_disclosed(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        """URL / ``-e`` / ``-r`` 行不参与判定，但**必须被计数披露**（不静默）。"""
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        files = {
            "app/main.py": "def main():\n    return None\n",
            "requirements.txt": "fastapi\n-e .\n-r other.txt\n",
        }
        project_id = await _project_with_code(store, session_factory, files)
        session = _Session()
        harness = _harness(store, session_factory, session)
        task, suite, _criteria = build_task_for_project(
            project_id=project_id, template_id="fastapi-vue3"
        )

        outcome = await harness.evaluate_task(task, project_id=project_id, suite=suite)

        runnability = outcome.dimension("runnability")
        assert runnability is not None
        refs = {item.ref for item in runnability.evidence}
        assert "deps_declaration" in refs
        detail = next(
            item.detail for item in runnability.evidence if item.ref == "deps_declaration"
        )
        assert detail == "deps_probe_ignored:2"

    async def test_cascading_skip_on_missing_build(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        """A7 端到端：缺 ``node-build`` ⇒ 构建与测试**双 skipped**，``blocking_failed`` 为空。"""
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        files = {
            "src/index.ts": "export const x = 1;\n",
            "package.json": '{"dependencies": {"express": "^4"}}',
        }
        project_id = await _project_with_code(store, session_factory, files)
        # 镜像里没有 node-build 这个可执行文件（其余命令照常 exit 0）
        session = _Session(exit_codes={"command -v node-build": 1})
        harness = _harness(store, session_factory, session)
        task, suite, _criteria = build_task_for_project(
            project_id=project_id, template_id="node-express"
        )

        outcome = await harness.evaluate_task(task, project_id=project_id, suite=suite)

        reasons = {item.command: item.reason for item in outcome.skipped_checks}
        assert reasons["node-build ."] == "requires_missing:node-build"
        assert reasons["node --test dist"] == f"{MISSING_BUILD_OUTPUT_PREFIX}dist/"
        assert outcome.blocking_failed == []
        # 两条阻断项都没真的执行 ⇒ backend 记未验证（不是"通过"）
        assert outcome.unverified_groups == ["backend"]
        assert "node --test dist" not in session.executed

    async def test_unverified_group_reaches_the_report_json(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        """T02×T03 的接缝：``unverified_groups`` 落进报告 JSON ⇒ 闸门能判 ``unverified``。"""
        from ymir.api.routers.artifacts import _report_health_status
        from ymir.evaluation.report import EvaluationReport

        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        files = {
            "app/main.py": "def main():\n    return None\n",
            "requirements.txt": "pkg-not-in-image\n",
        }
        project_id = await _project_with_code(store, session_factory, files)
        session = _Session()
        harness = _harness(store, session_factory, session)
        task, suite, _criteria = build_task_for_project(
            project_id=project_id, template_id="fastapi-vue3"
        )

        outcome = await harness.evaluate_task(task, project_id=project_id, suite=suite)
        report = EvaluationReport.build(
            suite_id=suite.id,
            suite_version=suite.version,
            project_id=project_id,
            weights={"runnability": 1.0},
            thresholds=suite.thresholds,
            tasks=[outcome],
            wall_time_ms=0,
            run_id="run-t03",
            artifact_version=1,
        )
        payload = json.loads(report.to_json())
        assert payload["tasks"][0]["unverified_groups"] == ["backend"]
        assert payload["criteria_source"] == "template"
        # 读取侧（T02）据此判 unverified（A2 的最后一段）
        status = _report_health_status(
            report.to_json(), 1, "code", "fastapi-vue3", payload["criteria_version"]
        )
        assert status == "unverified"


class TestProbeSideSameRuler:
    """T03.6：探针侧（基线独立执行）与合并树侧用**同一份**镜像包表。"""

    async def test_probe_skips_checks_with_missing_deps(self) -> None:
        """基线里声明了镜像没有的包 ⇒ 探针侧同样 ``skipped``（不是 unhealthy）。

        ★ 若两侧不同口径，会出现"A2 场景下合并树说跳过、探针说基线坏了"的自相矛盾，
        而 ``BaselineUnhealthyError`` 会把整棵树按"分数不可比"降级。
        """
        from ymir.evaluation.baseline_probe import BASELINE_SKIPPED, probe_baseline

        files = {
            "app/main.py": "def main():\n    return None\n",
            "requirements.txt": "pkg-not-in-image\n",
        }
        check = EvalCheck(
            run="python -m pytest -q",
            group="backend",
            tier="behavior",
            blocking=True,
            requires_deps=["python:*"],
        )
        session = _Session()
        probe = await probe_baseline(
            files=files,
            checks=[check],
            sandbox_provider=_Provider(session),
            image_packages=IMAGE,
        )

        assert probe.status == BASELINE_SKIPPED
        assert probe.unhealthy is False
        assert probe.checks[0].skipped is True
        assert probe.checks[0].skipped_reason == f"{MISSING_DEPS_PREFIX}pkg-not-in-image"
        assert session.executed == []  # 一条都没真的跑过

    async def test_probe_runs_when_deps_are_present(self) -> None:
        from ymir.evaluation.baseline_probe import BASELINE_HEALTHY, probe_baseline

        files = {
            "app/main.py": "def main():\n    return None\n",
            "requirements.txt": "pytest\n",
        }
        check = EvalCheck(run="python -m pytest -q", requires_deps=["python:*"], blocking=True)
        session = _Session()
        probe = await probe_baseline(
            files=files,
            checks=[check],
            sandbox_provider=_Provider(session),
            image_packages=IMAGE,
        )
        assert probe.status == BASELINE_HEALTHY
        assert session.executed == ["python -m pytest -q"]


class TestD3NodeTemplateStillReachable:
    """★ v1.5 修复轮 D-3：真实 Node 树里 D2 必须**真的跑得起来**。

    修复前的实测形态：``package.json`` 声明 ``lodash``（镜像没有）⇒
    ``node-build .`` / ``node --test dist`` / ``npx tsc --noEmit`` **三条全跳**、
    ``executed=[]`` ⇒ 这个模板的阻断判据在真实树上是**假的**。

    修复后的分工：
    - ``node-build .`` / ``node --test dist``：保留 ``node:*``（构建确实需要树内依赖）
      ⇒ 缺包时 ``skipped``（正确行为）；
    - ``npx tsc --noEmit``：**D2 + blocking**，只依赖 ``typescript``（镜像预装）
      ⇒ 在真实树上真的执行，``blocking_failed`` 不再恒空。
    """

    def test_node_d2_is_reachable_without_tree_npm_deps(self) -> None:
        """⑤（原 A6 的一条）：node-express 的 D2 必须有一条**不依赖** ``node:*``。

        理由：npm 依赖是**树特有**的（`package.json` 几乎必然声明镜像外的包），
        若 D2 全部挂 ``node:*``，真实树里一条都跑不了 ⇒ 阻断判据是假的。
        python 侧不同：`python:*` 只在"树声明了镜像外的包"时才跳，且
        `compileall`/`ruff` 不依赖它，所以不施加同一条约束。
        """
        spec = load_template("node-express")
        behavior = [c for c in spec.checks if c.tier == "behavior" and c.blocking]
        reachable = [
            c for c in behavior if not any(t == "node:*" for t in c.requires_deps)
        ]
        assert reachable, "node-express 的 D2 全部依赖 node:* ⇒ 真实树里 D2 不可达（D-3）"

    async def test_tsc_runs_when_tree_declares_unknown_npm_deps(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        files = {
            "src/index.ts": "export const x: number = 1;\n",
            # 镜像里**没有** lodash ⇒ node:* 的两条会跳，tsc 必须照跑
            "package.json": '{"dependencies": {"lodash": "^4.17.21"}}',
        }
        project_id = await _project_with_code(store, session_factory, files)
        session = _Session()
        harness = _harness(store, session_factory, session)
        task, suite, _criteria = build_task_for_project(
            project_id=project_id, template_id="node-express"
        )

        outcome = await harness.evaluate_task(task, project_id=project_id, suite=suite)

        # ① D2 真的执行了（修复前这里是空的 ⇒ 整个模板的阻断判据是假的）
        assert "npx tsc --noEmit" in session.executed
        assert outcome.blocking_failed == []  # 假沙箱 exit=0 ⇒ 无阻断项
        # （不在此断言 ``passed``：该夹具没有 requirement ⇒ 该维 0 分 ⇒ 总分低于门槛，
        #   与 D-3 无关；``passed`` 的判定由 A1/A5 的用例覆盖）
        # ② 依赖缺失的两条按环境缺失跳过，原因点名具体包
        reasons = {item.command: item.reason for item in outcome.skipped_checks}
        assert reasons["node-build ."] == "requires_missing_deps:lodash"
        assert reasons["node --test dist"] == "requires_missing_deps:lodash"
        # ③ 「组 × 档」判：`loadable` 档（node-build）**全跳过** ⇒ backend 记未验证
        #    （D1 没验过，必须披露）；而 `behavior` 档里 tsc **跑过** ⇒ D2 已验证 ——
        #    这正是 D-3 要的效果：不再"整组未验证"。
        assert outcome.unverified_groups == ["backend"]
        assert "npx tsc --noEmit" in session.executed

    async def test_tsc_failure_is_blocking_on_a_real_shaped_tree(
        self, session_factory: Any, tmp_path: Path
    ) -> None:
        """同一个真实形态的树，``tsc`` 失败 ⇒ **阻断**（verdict 层的材料）。"""
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        files = {
            "src/index.ts": "export const x: number = 1;\n",
            "package.json": '{"dependencies": {"lodash": "^4.17.21"}}',
        }
        project_id = await _project_with_code(store, session_factory, files)
        session = _Session(exit_codes={"npx tsc": 2})
        harness = _harness(store, session_factory, session)
        task, suite, _criteria = build_task_for_project(
            project_id=project_id, template_id="node-express"
        )

        outcome = await harness.evaluate_task(task, project_id=project_id, suite=suite)

        assert outcome.blocking_failed == ["npx tsc --noEmit"]
        assert outcome.passed is False


# ============================================================ T03.8 · A6 同口径


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_all_templates_declare_blocking_consistently(template_id: str) -> None:
    """A6（一条断言覆盖四套）：①D1 阻断 ②D2 阻断 ③D3 不阻断 ④阻断项声明超时+组。

    docs/17 §4 A6 的四条合并成一条参数化用例（``[<id>]`` ×4）：
    这里只断言**结构性**事实，不锁具体命令文本 —— 换命令不该红，换口径才该红。
    """
    spec = load_template(template_id)
    loadable = [c for c in spec.checks if c.tier == "loadable"]
    behavior = [c for c in spec.checks if c.tier == "behavior"]
    style = [c for c in spec.checks if c.tier == "style"]

    assert loadable and all(c.blocking for c in loadable), f"{template_id}: ①D1 缺失或未阻断"
    assert behavior and all(c.blocking for c in behavior), f"{template_id}: ②D2 缺失或未阻断"
    assert all(not c.blocking for c in style), f"{template_id}: ③D3 不许阻断"
    for check in spec.checks:
        if check.blocking:
            assert check.timeout_s is not None and check.timeout_s >= 60, (
                f"{template_id}: ④{check.run} 是阻断项却没有 >=60s 的显式超时"
            )
            assert check.group, f"{template_id}: ④{check.run} 缺 group"


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
class TestAllTemplatesShareOneRuler:
    """A6：四套模板统一口径（参数化；只断言**结构性**事实，不锁具体命令文本）。"""

    def test_has_at_least_one_loadable_blocking_check(self, template_id: str) -> None:
        spec = load_template(template_id)
        loadable = [c for c in spec.checks if c.tier == "loadable"]
        assert loadable, f"{template_id} 缺 D1（不可加载）判据"
        assert all(c.blocking for c in loadable), f"{template_id} 的 D1 必须阻断"

    def test_has_at_least_one_behavior_blocking_check(self, template_id: str) -> None:
        spec = load_template(template_id)
        behavior = [c for c in spec.checks if c.tier == "behavior"]
        assert behavior, f"{template_id} 缺 D2（行为不符）判据"
        assert all(c.blocking for c in behavior), f"{template_id} 的 D2 必须阻断"

    def test_style_checks_are_never_blocking(self, template_id: str) -> None:
        """D3 不阻断（`ruff check` 不许出现在 blocking 里）。

        ★ v1.5 修复轮 D-3：``tsc --noEmit`` **不再是 D3**（真实 Node 树里
        ``node-build`` / ``node --test`` 会因 npm 依赖缺失全跳，D2 因此不可达）——
        它改判 **D2（behavior + 阻断）**，只依赖 ``typescript``（镜像恒真）。
        python 侧的 ``ruff`` 仍是 D3："风格不达标"不是"用不了"。
        """
        spec = load_template(template_id)
        for check in spec.checks:
            if check.tier == "style":
                assert check.blocking is False, f"{template_id}: {check.run} 是 D3 却阻断"
        for check in spec.checks:
            if "ruff" in check.run:
                assert check.blocking is False, f"{template_id}: {check.run} 不应阻断"

    def test_blocking_checks_declare_timeout_and_group(self, template_id: str) -> None:
        spec = load_template(template_id)
        for check in spec.checks:
            if not check.blocking:
                continue
            assert check.timeout_s is not None and check.timeout_s >= 60, (
                f"{template_id}: {check.run} 是阻断项却没有 >=60s 的显式超时"
            )
            assert check.group, f"{template_id}: {check.run} 没有 group（未验证披露需要它）"

    def test_every_check_declares_a_tier(self, template_id: str) -> None:
        """v1.5 起四套模板**全部**显式声明档位（不留空 ⇒ 口径可被 A6 参数化）。"""
        spec = load_template(template_id)
        assert all(c.tier for c in spec.checks), f"{template_id} 有检查没声明 tier"


class TestTemplateRegressionInvariants:
    """A18：模板改动**不得**动权重 / 门槛 / 分组配分。"""

    def test_weights_and_thresholds_untouched(self) -> None:
        from ymir.evaluation.suite import EvalSuite

        spec = load_template("fastapi-vue3")
        assert spec.scoring.runnability_points == {
            "backend": 10,
            "frontend_static": 10,
            "frontend_build": 10,
        }
        default_suite = EvalSuite.load("default")
        assert default_suite.weights == {
            "requirement_conformance": 0.3,
            "runnability": 0.3,
            "convention": 0.2,
            "architecture": 0.2,
        }
        assert default_suite.thresholds.total == 75
        assert default_suite.thresholds.runnability == 60

    def test_fe_build_keeps_its_requires_files(self) -> None:
        """★ ``fe-build web`` 的 ``requires_files: ["web/"]`` 不得被构建物声明取代。

        它守的是"基线缺 ``web/`` ⇒ 探针侧 skipped（baseline_incomplete）"这条不对称；
        若有人把它改成 ``requires_build_output``，v1.3.1 的 Q1 定案就被悄悄推翻了。
        """
        spec = load_template("fastapi-vue3")
        fe_build = next(c for c in spec.checks if c.run == "fe-build web")
        assert fe_build.requires_files == ["web/"]
        assert fe_build.requires_build_output == ""
        assert fe_build.requires == "node"
        assert fe_build.requires_deps == ["node:*"]


def test_templates_dir_is_the_real_one() -> None:
    """参数化的数据源必须是仓库里的真实模板目录（防"测了临时目录"的自欺）。"""
    for template_id in TEMPLATE_IDS:
        assert (DEFAULT_TEMPLATES_DIR / f"{template_id}.yaml").exists()


def test_invalid_param_is_the_error_family() -> None:
    """校验失败必须是 1001 家族（``InvalidParamError``），前端据此换文案。"""
    assert issubclass(TemplateDefinitionError, InvalidParamError)
