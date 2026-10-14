"""角色运行时测试（T03）：注册 / 提示词渲染 / JSON 解析 / 契约校验 / 运行时链路。

单测纪律：假网关（0 真实调用）；模板用真实 ``config/roles``（顺带回归模板语法）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ymir.artifacts.store import ArtifactStore
from ymir.common.errors import InvalidParamError, NotFoundError
from ymir.config import PROJECT_ROOT
from ymir.gateway.gateway import ChatResult, ChatUsage
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
from ymir.roles.schemas import PRDDocument
from ymir.storage.repositories import ProjectRepository
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
