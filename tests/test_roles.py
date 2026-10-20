"""角色运行时测试（T03）：注册 / 提示词渲染 / JSON 解析 / 契约校验 / 运行时链路。

单测纪律：假网关（0 真实调用）；模板用真实 ``config/roles``（顺带回归模板语法）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from ymir.artifacts.store import ArtifactStore
from ymir.common.errors import InvalidParamError, NotFoundError
from ymir.config import PROJECT_ROOT
from ymir.gateway.gateway import ChatResult, ChatUsage
from ymir.models.entities import Message
from ymir.models.enums import MessageType
from ymir.orchestrator.role_executor import RoleStageExecutor
from ymir.roles import (
    PromptRenderer,
    RoleOutputError,
    RoleRegistry,
    RoleRuntime,
    StageInput,
    UpstreamArtifact,
    extract_json,
    get_output_schema,
)
from ymir.roles.io import RepairContext, StageOutput
from ymir.roles.prompts import TRUNCATED_MARK, truncate_check_outputs
from ymir.roles.schemas import PRDDocument
from ymir.sop.models import StageSpec
from ymir.storage.repositories import ProjectRepository, RunRepository
from ymir.tools import build_code_files, build_document_files, read_artifacts

ROLES_DIR = PROJECT_ROOT / "config" / "roles"

PRD_JSON = json.dumps(
    {
        "title": "待办清单 API",
        "background": "需要一个最简待办服务",
        "goals": ["支持增删改查"],
        "user_stories": [
            {
                "id": "US-1",
                "title": "记录待办",
                "as_a": "用户",
                "i_want": "新增待办",
                "so_that": "不忘记事情",
                "acceptance_criteria": ["POST 返回 201"],
            }
        ],
        "requirements": [
            {
                "id": "R-1",
                "title": "新增接口",
                "description": "提供 POST /todos",
                "priority": "P0",
                "acceptance_criteria": ["创建成功返回 id"],
            }
        ],
        "out_of_scope": ["多用户"],
    },
    ensure_ascii=False,
)

CODE_JSON = json.dumps(
    {
        "files": [{"path": "main.py", "content": "print('hi')"}],
        "run_commands": ["python main.py"],
        "notes": "最小示例",
    },
    ensure_ascii=False,
)

DESIGN_JSON = json.dumps(
    {
        "title": "待办系统设计",
        "overview": "分层设计",
        "modules": [{"name": "api", "responsibility": "提供接口", "interfaces": ["create()"]}],
        "tech_stack": {"语言": "Python"},
        "directory_structure": "src/\n  main.py",
        "api_drafts": [],
        "data_models": [],
    },
    ensure_ascii=False,
)


class FakeGateway:
    """假网关：按脚本返回响应，记录每次调用参数（0 真实调用）。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat(
        self,
        messages,
        *,
        tier,
        run_id=None,
        stage_id=None,
        temperature=None,
        max_tokens=None,
        response_format=None,
    ) -> ChatResult:
        self.calls.append(
            {
                "messages": [dict(m) for m in messages],
                "tier": str(tier),
                "run_id": run_id,
                "stage_id": stage_id,
                "temperature": temperature,
            }
        )
        content = self.responses.pop(0) if self.responses else "{}"
        return ChatResult(
            content=content,
            provider="fake",
            model="fake-model",
            tier=str(tier),
            usage=ChatUsage(prompt_tokens=10, completion_tokens=20),
            cost_cny=0.0001,
            latency_ms=5,
        )


def _make_runtime(gateway: FakeGateway) -> RoleRuntime:
    return RoleRuntime(
        gateway=gateway,  # type: ignore[arg-type]
        registry=RoleRegistry(ROLES_DIR),
        renderer=PromptRenderer(ROLES_DIR / "prompts"),
    )


# --------------------------------------------------------------------- 注册表


class TestRoleRegistry:
    def test_loads_four_roles(self) -> None:
        registry = RoleRegistry(ROLES_DIR)
        assert registry.ids() == ["architect", "engineer", "pm", "qa"]
        pm = registry.get("pm")
        assert pm.tier.value == "planner"
        assert pm.output_schema == "PRDDocument"
        engineer = registry.get("engineer")
        assert engineer.tier.value == "executor"
        assert "code_write" in engineer.capabilities

    def test_get_missing_raises(self) -> None:
        with pytest.raises(NotFoundError):
            RoleRegistry(ROLES_DIR).get("nobody")

    def test_duplicate_id_raises(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text(
            "id: dup\nname: A\nsystem_prompt: x.j2\noutput_schema: PRDDocument\n", encoding="utf-8"
        )
        (tmp_path / "b.yaml").write_text(
            "id: dup\nname: B\nsystem_prompt: y.j2\noutput_schema: PRDDocument\n", encoding="utf-8"
        )
        with pytest.raises(InvalidParamError):
            RoleRegistry(tmp_path)


# --------------------------------------------------------------------- 提示词


class TestPrompts:
    def test_render_system_injects_schema(self) -> None:
        registry = RoleRegistry(ROLES_DIR)
        renderer = PromptRenderer(ROLES_DIR / "prompts")
        text = renderer.render_system(registry.get("pm"))
        assert "产品经理" in text
        assert "user_stories" in text  # schema 注入
        assert "output_schema" not in text  # 模板变量已全部替换

    def test_build_user_full_context(self) -> None:
        registry = RoleRegistry(ROLES_DIR)
        renderer = PromptRenderer(ROLES_DIR / "prompts")
        role = registry.get("architect")
        inp = StageInput(
            requirement="需求X",
            project_name="demo",
            upstream=[
                UpstreamArtifact(
                    name="prd",
                    version=2,
                    files={
                        "prd.md": "文档内容",
                        "prd.json": "{}",
                        "big.md": "长" * 9000,
                        "smoke_report.json": '{"all_ok": false}',
                    },
                )
            ],
            feedback=["补充验收标准"],
            attempt=2,
        )
        text = renderer.build_user(role, inp)
        assert "需求X" in text
        assert "demo" in text
        assert "[prd]" in text and "v2" in text
        assert "文档内容" in text
        assert "prd.json" not in text  # 同名机读副本跳过
        assert "smoke_report.json" in text  # 独立信号需注入下游（T03 演示发现修复）
        assert '"all_ok": false' in text
        assert "截断" in text  # 超长文件截断
        assert "反馈 1" in text and "补充验收标准" in text
        assert "第 2 次执行" in text


# --------------------------------------------------------------------- JSON 解析


class TestExtractJson:
    def test_plain(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced(self) -> None:
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_wrapped_with_prose(self) -> None:
        text = '好的，以下是结果：\n{"a": 1, "b": {"c": 2}}\n希望对你有帮助。'
        assert extract_json(text) == {"a": 1, "b": {"c": 2}}

    def test_invalid_raises(self) -> None:
        with pytest.raises(RoleOutputError):
            extract_json("完全没有 JSON")


# --------------------------------------------------------------------- 输出契约


class TestSchemas:
    def test_unknown_schema(self) -> None:
        with pytest.raises(InvalidParamError):
            get_output_schema("NoSuchDoc")

    def test_prd_markdown(self) -> None:
        doc = PRDDocument.model_validate(json.loads(PRD_JSON))
        md = doc.to_markdown()
        assert "# 待办清单 API" in md
        assert "R-1" in md and "P0" in md
        assert "US-1" in md
        assert "- [ ] 创建成功返回 id" in md

    def test_prd_requires_min_items(self) -> None:
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            PRDDocument.model_validate(
                {
                    "title": "x",
                    "background": "b",
                    "goals": [],
                    "user_stories": [],
                    "requirements": [],
                }
            )


# --------------------------------------------------------------------- 运行时


class TestRoleRuntime:
    async def test_pm_success_flow(self) -> None:
        gateway = FakeGateway([PRD_JSON])
        runtime = _make_runtime(gateway)
        output = await runtime.execute(
            "pm",
            StageInput(requirement="做一个待办清单", project_name="demo"),
            run_id="r1",
            stage_id="prd",
        )
        assert set(output.files) == {"prd.md", "prd.json"}
        assert "# 待办清单 API" in output.files["prd.md"]
        assert json.loads(output.files["prd.json"])["title"] == "待办清单 API"
        assert output.structured["title"] == "待办清单 API"
        assert output.schema_attempts == 1
        assert output.summary.startswith("PRD《待办清单 API》")
        # planner 档（分级路由）与调用参数
        assert gateway.calls[0]["tier"] == "planner"
        assert gateway.calls[0]["run_id"] == "r1"
        assert gateway.calls[0]["stage_id"] == "prd"

    async def test_engineer_uses_executor_tier_and_code_files(self) -> None:
        gateway = FakeGateway([CODE_JSON])
        runtime = _make_runtime(gateway)
        output = await runtime.execute("engineer", StageInput(requirement="x"))
        assert gateway.calls[0]["tier"] == "executor"
        assert "main.py" in output.files
        assert "code.json" in output.files

    async def test_validation_retry_then_success(self) -> None:
        gateway = FakeGateway(["这不是 JSON", PRD_JSON])
        runtime = _make_runtime(gateway)
        output = await runtime.execute("pm", StageInput(requirement="x"))
        assert output.schema_attempts == 2
        assert len(gateway.calls) == 2
        # 第二次调用携带纠错指令（历史输出 + 错误说明）
        second = gateway.calls[1]["messages"]
        assert any("未通过校验" in m["content"] for m in second if m["role"] == "user")
        # 用量跨两次累加
        assert output.prompt_tokens == 20
        assert output.completion_tokens == 40

    async def test_all_attempts_failed_raises(self) -> None:
        gateway = FakeGateway(["bad-1", "bad-2"])
        runtime = _make_runtime(gateway)
        with pytest.raises(RoleOutputError):
            await runtime.execute("pm", StageInput(requirement="x"))
        assert len(gateway.calls) == 2  # retry.max_attempts=2

    async def test_architect_gets_upstream_in_prompt(self) -> None:
        gateway = FakeGateway([DESIGN_JSON])
        runtime = _make_runtime(gateway)
        stage_input = StageInput(
            requirement="x",
            upstream=[
                UpstreamArtifact(name="prd", version=1, files={"prd.md": "# 上游PRD内容"})
            ],
        )
        await runtime.execute("architect", stage_input)
        user_msg = next(
            m for m in gateway.calls[0]["messages"] if m["role"] == "user"
        )
        assert "上游PRD内容" in user_msg["content"]


# --------------------------------------------------------------------- 工具


class TestTools:
    def test_build_code_files(self) -> None:
        from ymir.roles.schemas import CodeBundle

        bundle = CodeBundle.model_validate(json.loads(CODE_JSON))
        files = build_code_files(bundle, name="code")
        assert files["main.py"] == "print('hi')\n"  # 统一换行
        meta = json.loads(files["code.json"])
        assert meta["run_commands"] == ["python main.py"]

    def test_build_code_files_rejects_conflict(self) -> None:
        from ymir.roles.schemas import CodeBundle

        bundle = CodeBundle.model_validate(
            {"files": [{"path": "a/b.py", "content": "1"}, {"path": "./a/b.py", "content": "2"}]}
        )
        with pytest.raises(InvalidParamError):
            build_code_files(bundle)

    def test_build_document_files(self) -> None:
        doc = PRDDocument.model_validate(json.loads(PRD_JSON))
        files = build_document_files(doc, name="prd")
        assert set(files) == {"prd.md", "prd.json"}

    async def test_read_artifacts(self, session_factory, tmp_path: Path) -> None:
        async with session_factory() as session:
            project = await ProjectRepository(session).create(name="read-artifacts")
            await session.commit()
        store = ArtifactStore(session_factory, tmp_path / "artifacts")
        await store.save(
            project_id=project.id, name="prd", artifact_type="prd", files={"prd.md": "v1"}
        )
        result = await read_artifacts(store, project.id, ["prd", "design"])
        assert [item.name for item in result] == ["prd"]  # design 未产出：跳过
        assert result[0].version == 1
        assert result[0].files["prd.md"] == "v1"


# --------------------------------------------------------------------- 定向修复（v1.1 T3.3）

#: 自动修复消息（与 ``RepairController`` 发出的 payload 同构，§4.4.4）
AUTO_REPAIR_PAYLOAD: dict = {
    "source": "auto_repair",
    "repair_round": 1,
    "max_rounds": 2,
    "verdict": {
        "total": 62.4,
        "runnability": 48.0,
        "thresholds": {"total": 75, "runnability": 60},
    },
    "failed_checks": [
        {
            "command": "python -m pytest -q",
            "exit_code": 1,
            "timed_out": False,
            "output": "FAILED tests/test_api.py::test_create - AssertionError: 404 != 201",
        }
    ],
    "previous": {"artifact_name": "code", "version": 1},
    "directive": "只修改与上述失败检查相关的实现；已通过的部分保持不动。",
}

AUTO_REPAIR_SUMMARY = "[自动修复 第1轮] 未达标 total=62.4(<75) runnability=48.0(<60)"


class CapturingRuntime:
    """假运行时：记录 ``StageInput`` 并返回固定产物（零真实模型）。"""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.inputs: list[StageInput] = []
        self._files = files or {"main.py": "x = 1\n"}

    async def execute(self, role, stage_input, *, run_id=None, stage_id=None):  # noqa: ANN001
        self.inputs.append(stage_input)
        return StageOutput(files=dict(self._files), summary="修订完成", structured={})


def _coding_stage() -> StageSpec:
    """coding 阶段（无上游 inputs：聚焦修复上下文，不牵扯产物解析）。"""
    return StageSpec(id="coding", role="engineer", inputs=[], outputs=["code"])


async def _repair_fixture(
    session_factory,
    tmp_path: Path,
    *,
    code_versions: tuple[str, ...] = ("v1-content\n",),
    messages: tuple[dict, ...] = (),
):
    """建 project + run + 若干 code 版本 + 反馈消息，返回执行器观测句柄。"""
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="repair-roles")
        run = await RunRepository(session).create(
            project_id=project.id, requirement="做一个 CLI 工具"
        )
        await session.commit()
        project_id, run_id = project.id, run.id

    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    for content in code_versions:
        await store.save(
            project_id=project_id,
            name="code",
            artifact_type="code",
            files={"main.py": content},
            run_id=run_id,
            stage_id="coding",
        )

    async with session_factory() as session:
        for seq, item in enumerate(messages, start=1):
            session.add(
                Message(
                    run_id=run_id,
                    seq=seq,
                    type=MessageType.FEEDBACK.value,
                    from_role=item.get("from_role", "evaluator"),
                    to_role=item.get("to_role", "engineer"),
                    summary=item.get("summary"),
                    payload=item.get("payload", {}),
                )
            )
        await session.commit()

    async with session_factory() as session:
        loaded_run = await RunRepository(session).get(run_id)

    runtime = CapturingRuntime()
    executor = RoleStageExecutor(
        session_factory=session_factory, store=store, runtime=runtime  # type: ignore[arg-type]
    )
    return executor, runtime, loaded_run, store


class TestRepairContextInjection:
    """定向修复上下文：采集 / 过滤 / 上一版产物 / 提示词渲染（v1.1）。"""

    async def test_repair_context_collected(self, session_factory, tmp_path: Path) -> None:
        executor, runtime, run, _ = await _repair_fixture(
            session_factory,
            tmp_path,
            messages=(
                {
                    "summary": AUTO_REPAIR_SUMMARY,
                    "payload": AUTO_REPAIR_PAYLOAD,
                },
            ),
        )

        await executor.execute(run=run, stage=_coding_stage(), attempt=2)

        stage_input = runtime.inputs[0]
        context = stage_input.repair_context
        assert context is not None
        assert context.repair_round == 1
        assert context.max_rounds == 2
        assert context.verdict["total"] == 62.4
        assert context.verdict["thresholds"] == {"total": 75, "runnability": 60}
        assert context.failed_checks[0]["command"] == "python -m pytest -q"
        assert context.failed_checks[0]["exit_code"] == 1
        assert "定向" in context.directive or "只修改" in context.directive
        assert context.max_detail_chars == 1200  # 缺省（payload 未带 context 块）

    async def test_previous_output_is_previous_version(
        self, session_factory, tmp_path: Path
    ) -> None:
        # 当前 code 已是 v2，payload 指向 v1 -> 必须注入 v1 的内容（失败的那一版）
        executor, runtime, run, _ = await _repair_fixture(
            session_factory,
            tmp_path,
            code_versions=("v1-content\n", "v2-content\n"),
            messages=({"summary": AUTO_REPAIR_SUMMARY, "payload": AUTO_REPAIR_PAYLOAD},),
        )

        await executor.execute(run=run, stage=_coding_stage(), attempt=3)

        previous = runtime.inputs[0].previous_output
        assert previous is not None
        assert previous.version == 1
        assert previous.files["main.py"] == "v1-content\n"
        assert runtime.inputs[0].repair_context is not None
        assert runtime.inputs[0].repair_context.previous["file_count"] == 1

    async def test_auto_repair_message_excluded_from_feedback(
        self, session_factory, tmp_path: Path
    ) -> None:
        executor, runtime, run, _ = await _repair_fixture(
            session_factory,
            tmp_path,
            messages=(
                {
                    "summary": AUTO_REPAIR_SUMMARY,
                    "payload": AUTO_REPAIR_PAYLOAD,
                },
                {
                    "summary": "请补充错误处理（人工驳回）",
                    "payload": {"source": "manual_reject"},
                },
            ),
        )

        await executor.execute(run=run, stage=_coding_stage(), attempt=2)

        feedback = runtime.inputs[0].feedback
        assert feedback == ["请补充错误处理（人工驳回）"]  # 自动修复消息不进 feedback
        assert AUTO_REPAIR_SUMMARY not in feedback

    async def test_no_auto_repair_message_keeps_none(
        self, session_factory, tmp_path: Path
    ) -> None:
        # v1.0 行为不变：无自动修复消息时两者均 None
        executor, runtime, run, _ = await _repair_fixture(
            session_factory,
            tmp_path,
            messages=({"summary": "普通反馈", "payload": {}},),
        )

        await executor.execute(run=run, stage=_coding_stage(), attempt=2)

        stage_input = runtime.inputs[0]
        assert stage_input.repair_context is None
        assert stage_input.previous_output is None
        assert stage_input.feedback == ["普通反馈"]

    async def test_previous_output_missing_artifact_does_not_fail(
        self, session_factory, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # 上一版产物缺失（payload 指向 v9）：只告警，阶段照常执行
        payload = dict(AUTO_REPAIR_PAYLOAD)
        payload["previous"] = {"artifact_name": "code", "version": 9}
        executor, runtime, run, _ = await _repair_fixture(
            session_factory,
            tmp_path,
            messages=({"summary": AUTO_REPAIR_SUMMARY, "payload": payload},),
        )

        with caplog.at_level(logging.WARNING, logger="ymir.orchestrator.role_executor"):
            outcome = await executor.execute(run=run, stage=_coding_stage(), attempt=2)

        assert runtime.inputs[0].repair_context is not None
        assert runtime.inputs[0].previous_output is None  # 加载失败不阻断
        assert outcome.output_refs  # 阶段仍然产出
        # 「失败只告警不阻断」的语义要能观测：WARNING 必须留痕且带定位信息
        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING and "上一版产物加载失败" in record.getMessage()
        ]
        assert warnings, caplog.text
        assert "version=9" in warnings[0].getMessage()
        assert "name=code" in warnings[0].getMessage()


class TestRepairPromptRendering:
    """定向修复段的提示词渲染（含 output 摘要预算）。"""

    def _stage_input(self, *, max_detail_chars: int = 1200) -> StageInput:
        context = RepairContext(
            repair_round=1,
            max_rounds=2,
            verdict={
                "total": 62.4,
                "runnability": 48.0,
                "thresholds": {"total": 75, "runnability": 60},
            },
            failed_checks=list(AUTO_REPAIR_PAYLOAD["failed_checks"]),
            previous={"artifact_name": "code", "version": 1, "file_count": 2},
            directive=str(AUTO_REPAIR_PAYLOAD["directive"]),
            max_detail_chars=max_detail_chars,
        )
        previous = UpstreamArtifact(
            name="code", version=1, files={"main.py": "# 上一版\ndef handle():\n    return 1\n"}
        )
        return StageInput(
            requirement="做一个 CLI 工具",
            attempt=2,
            repair_context=context,
            previous_output=previous,
        )

    def _render(self, stage_input: StageInput) -> str:
        renderer = PromptRenderer(ROLES_DIR / "prompts")
        engineer = RoleRegistry(ROLES_DIR).get("engineer")
        return renderer.build_user(engineer, stage_input)

    def test_build_user_contains_repair_section(self) -> None:
        text = self._render(self._stage_input())

        assert "## 定向修复（第 1/2 轮）" in text
        assert "### 未达标结论" in text
        assert "total=62.4（门槛 75.0）" in text
        assert "runnability=48.0（门槛 60.0）" in text
        assert "### 失败检查（只修这些）" in text
        assert "`python -m pytest -q` exit=1 timed_out=false" in text
        assert "FAILED tests/test_api.py::test_create" in text
        assert "### 上一版产物（v1）" in text
        assert "上一版" in text and "handle" in text
        assert "### 修复指令" in text
        assert str(AUTO_REPAIR_PAYLOAD["directive"]) in text

    def test_repair_section_absent_without_context(self) -> None:
        # v1.0 行为不变：无修复上下文时不出现该段
        text = self._render(StageInput(requirement="做一个 CLI 工具"))
        assert "## 定向修复" not in text
        assert "### 失败检查" not in text

    def test_previous_output_not_duplicated_in_upstream(self) -> None:
        """去重守卫：上一版产物不得在「上游产物」段重复出现（同名即跳过）。

        两种场景一并覆盖（空段消隐 / 部分保留标题）：
        - 上游**全部**与上一版同名 -> 只渲染修复段那一份，且**不输出空标题**；
        - 上游**部分**同名 -> 标题保留，被过滤项不出现，其余项照常渲染。
        """
        v1 = UpstreamArtifact(name="code", version=1, files={"main.py": "# v1-sentinel\n"})
        v2 = UpstreamArtifact(name="code", version=2, files={"main.py": "# v2-sentinel\n"})
        prd = UpstreamArtifact(name="prd", version=1, files={"prd.md": "# prd-sentinel\n"})

        # --- 场景一：全部被过滤 ---
        only_previous = self._stage_input()
        only_previous.previous_output = v1
        only_previous.upstream = [v2]
        text = self._render(only_previous)

        assert text.count("#### 文件：main.py") == 1  # 只渲染一次
        assert text.count("v1-sentinel") == 1  # 出现的是「上一版」
        assert "v2-sentinel" not in text  # 同名上游被跳过（不是简单重复标题）
        assert "## 上游产物" not in text  # 空标题段消隐（B2）
        assert "## 定向修复（第 1/2 轮）" in text

        # --- 场景二：部分被过滤 ---
        partial = self._stage_input()
        partial.previous_output = v1
        partial.upstream = [v2, prd]
        text = self._render(partial)

        assert "## 上游产物" in text  # 还有剩余项 -> 标题保留
        assert "prd-sentinel" in text  # 剩余项照常渲染
        assert "v2-sentinel" not in text  # 被过滤项不出现
        assert text.count("#### 文件：main.py") == 1

    def test_repair_output_truncated_within_budget(self) -> None:
        long_output = "E" * 5000
        checks = [
            {"command": f"cmd-{index}", "exit_code": 1, "timed_out": False, "output": long_output}
            for index in range(5)
        ]
        snippets = truncate_check_outputs(checks, 300)

        assert len(snippets) == 5
        assert sum(len(item) for item in snippets) <= 300  # 总预算硬约束
        assert any(TRUNCATED_MARK in item for item in snippets)

        stage_input = self._stage_input(max_detail_chars=300)
        stage_input.repair_context = RepairContext(  # type: ignore[misc]
            repair_round=1,
            max_rounds=2,
            verdict={"total": 62.4, "runnability": 48.0, "thresholds": {"total": 75}},
            failed_checks=checks,
            previous=None,
            directive="d",
            max_detail_chars=300,
        )
        text = self._render(stage_input)
        assert TRUNCATED_MARK in text
        # 渲染后：失败检查段的输出摘要总量不得超过预算（header 允许少量额外字符）
        body = text.split("### 失败检查（只修这些）", 1)[1].split("### 修复指令", 1)[0]
        payload_chars = sum(
            len(line.strip()) for line in body.splitlines() if line.startswith("   ")
        )
        assert payload_chars <= 300 + 4 * len(checks)  # 容差 = 缩进（每行 3 空格）


def test_repair_context_is_package_exported() -> None:
    """包级导出（v1.1，T4 首笔）：``RepairContext`` 可从 ``ymir.roles`` 直接消费。"""
    import ymir.roles as roles_pkg
    from ymir.roles import RepairContext as ExportedRepairContext

    assert "RepairContext" in roles_pkg.__all__
    assert ExportedRepairContext is RepairContext
    context = ExportedRepairContext(repair_round=1, max_rounds=2, directive="只改失败项")
    assert context.max_detail_chars == 1200  # 缺省与 RepairContextSpec 一致
    assert context.failed_checks == []
