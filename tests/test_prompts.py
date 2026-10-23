"""提示词渲染测试：v1.1 快照保护 + v1.2 模板注入（docs/09 §4.1.2 / §9-T1.2）。

## 快照保护（最重要）

``tests/fixtures/prompts_v11/<role>.txt`` 是 v1.2 **改动前**用同一份输入跑
``PromptRenderer.build_user`` 得到的**逐字节**产物（4 个角色各一份）。任何
"No template" 路径的渲染变化都会让这些用例失败 —— 这是 §10-1/§10-11 兼容契约的
可执行证明。

快照覆盖了全部渲染分支（上游产物 + 同机读副本跳过 + 超长截断 + 历史反馈 +
定向修复四小节 + 重跑提示），所以小节顺序漂移、分隔符变化、缩进变化都会被抓到。

### 如何重新生成（仅在**确认**要变更 v1.1 契约时）

```
YMIR_UPDATE_PROMPT_SNAPSHOTS=1 uv run pytest tests/test_prompts.py -q
```

生成脚本就是本模块自身：``_build_snapshot_input()`` 是唯一输入来源，
避免"生成器与断言用了两份不同输入"的经典陷阱。

## 模板注入（T1.2）

渲染范围口径（docs/09 §4.1.2）：

- ``## 技术栈模板`` → pm / architect / engineer / qa **四角色都渲染**；
- ``## 目录规范`` → architect / engineer；
- ``## 模板模式例外`` + ``reserved_paths`` / ``seed_paths`` 两清单 → **仅 engineer**。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import ymir.orchestrator.role_executor as role_executor_module
from ymir.common.errors import NotFoundError
from ymir.config import PROJECT_ROOT
from ymir.orchestrator.role_executor import RoleStageExecutor
from ymir.roles import PromptRenderer, RoleRegistry, StageInput, UpstreamArtifact
from ymir.roles.io import RepairContext, TemplateContext
from ymir.templates import load_template_file

ROLES_DIR = PROJECT_ROOT / "config" / "roles"
SNAPSHOT_DIR = Path(__file__).resolve().parent / "fixtures" / "prompts_v11"
ALL_ROLES = ("pm", "architect", "engineer", "qa")

#: 设置该环境变量即用当前代码重写快照（仅限**有意**变更 v1.1 契约时使用）
_UPDATE_ENV = "YMIR_UPDATE_PROMPT_SNAPSHOTS"


def _build_snapshot_input() -> StageInput:
    """构造快照输入（确定性：无时间戳 / 无随机 / 无平台相关换行）。

    刻意同时带上 ``upstream``（含同名机读副本与超长文件）、``feedback``、
    ``attempt>1`` 与 ``repair_context``：把 ``build_user`` 的全部分支压进一份输入，
    这样"某段被挪了位置"也会暴露，而不只是"内容还在"。
    """
    return StageInput(
        requirement="实现一个待办清单 API：支持增删改查，数据落 SQLite。\n第二条需求行。",
        project_name="demo-snapshot",
        upstream=[
            UpstreamArtifact(
                name="prd",
                version=2,
                files={
                    "prd.md": "# PRD\n\n- 验收标准：POST 返回 201\n",
                    "prd.json": '{"title": "机读副本"}',
                    "smoke_report.json": '{"all_ok": false}',
                    "long.md": "长" * 9000,
                },
            )
        ],
        feedback=["补充验收标准", "补充错误码说明"],
        attempt=2,
        repair_context=RepairContext(
            repair_round=1,
            max_rounds=2,
            verdict={"total": 62.5, "runnability": 0.0, "thresholds": {"total": 75.0}},
            failed_checks=[
                {
                    "command": "python -m pytest -q",
                    "exit_code": 1,
                    "timed_out": False,
                    "output": "E   ModuleNotFoundError: No module named 'app'\n",
                },
                {"command": "ruff check .", "exit_code": 2, "output": "F401 unused import"},
            ],
            previous={"artifact_name": "code", "version": 3},
            directive="补齐 app 包的入口，修掉未使用导入。",
            max_detail_chars=200,
        ),
        previous_output=UpstreamArtifact(
            name="code",
            version=3,
            files={"app/main.py": "print('old')\n", "code.json": "{}"},
        ),
    )


def _renderer() -> PromptRenderer:
    return PromptRenderer(ROLES_DIR / "prompts")


def _read_snapshot(path: Path) -> str:
    """按**字节**读快照（不经平台换行转换，保证"逐字节一致"真的可比）。"""
    return path.read_bytes().decode("utf-8")


@pytest.mark.parametrize("role_id", ALL_ROLES)
def test_build_user_without_template_matches_v11_snapshot(role_id: str) -> None:
    """``StageInput.template is None`` 时与 v1.1 输出**逐字节一致**（§10-1/§10-11）。"""
    role = RoleRegistry(ROLES_DIR).get(role_id)
    text = _renderer().build_user(role, _build_snapshot_input())
    path = SNAPSHOT_DIR / f"{role_id}.txt"
    if os.environ.get(_UPDATE_ENV) == "1":
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        pytest.skip(f"已重写快照 {path.name}")
    assert path.exists(), f"缺少快照文件 {path}（用 {_UPDATE_ENV}=1 生成）"
    expected = _read_snapshot(path)
    assert text == expected, f"{role_id} 的用户消息与 v1.1 快照不一致"


def test_snapshot_input_covers_all_render_branches() -> None:
    """自检：快照输入确实覆盖了全部分支（防止快照"看起来有但没覆盖"）。"""
    role = RoleRegistry(ROLES_DIR).get("architect")
    text = _renderer().build_user(role, _build_snapshot_input())
    assert "## 原始需求" in text
    assert "## 上游产物" in text
    assert "smoke_report.json" in text  # 独立 json 注入
    assert "prd.json" not in text  # 同名机读副本跳过
    assert "内容过长已截断" in text  # 超长文件截断
    assert "## 历史反馈（必须针对性修订）" in text
    assert "## 定向修复（第 1/2 轮）" in text
    assert "### 失败检查（只修这些）" in text
    assert "### 上一版产物（v3）" in text
    assert "第 2 次执行" in text


# --------------------------------------------------------------------- 模板注入（T1.2）

#: 骨架正文里的"防泄漏"标记：清单只许出现路径，正文任何片段都不得出现在提示词里
_SKELETON_MARKER = "SKELETON_BODY_MARKER_42"

_TEMPLATE_YAML = f"""\
id: fastapi-vue3
version: "1.0"
name: FastAPI + Vue3 全栈
description: 测试用模板（骨架含 ruff.toml，与真模板同形）
supports: [from_scratch, patch]

stack:
  language: python
  runtime: "3.12"
  backend: fastapi
  frontend: vue3
  dependencies: [fastapi, uvicorn]
  package_manager: uv

layout:
  required_dirs: [app, tests, web/src]
  required_files: [pyproject.toml]
  snake_case_py: true
  max_file_lines: 600
  allowed_prefixes: [app/, tests/]
  frontend:
    required_dirs: [web/src]
    required_glob: ["web/src/**/*.vue"]
    forbidden_imports: [jquery]

skeleton:
  max_files: 5
  max_bytes: 32768
  files:
    - path: pyproject.toml
      reserved: true
      content: |
        [project]
        name = "{_SKELETON_MARKER}"
    - path: ruff.toml
      reserved: true
      content: |
        line-length = 100
    - path: app/main.py
      reserved: false
      content: |
        app = None  # {_SKELETON_MARKER}
    - path: requirements.txt
      reserved: false
      content: |
        fastapi>=0.115

checks:
  - {{ type: command, run: "python -m compileall -q app", weight: 1 }}
  - {{ type: command, run: "python -m pytest -q", weight: 2 }}

prompting:
  pm: PRD 必须声明后端为 FastAPI。
  architect: 目录结构必须与模板 layout 一致。
  engineer: 严格遵守模板目录规范；固定提供的文件不要输出。
  qa: 证据链须引用模板检查命令的执行输出。
"""


def _template_context(tmp_path: Path) -> TemplateContext:
    """YAML -> loader（17 条校验）-> ``TemplateContext``（含 from_spec 桥接）。"""
    path = tmp_path / "fastapi-vue3.yaml"
    path.write_text(_TEMPLATE_YAML, encoding="utf-8", newline="\n")
    spec = load_template_file(path, known_roles=set(ALL_ROLES))
    return TemplateContext.from_spec(spec)


def _template_input(tmp_path: Path) -> StageInput:
    return StageInput(
        requirement="实现待办清单 API",
        project_name="demo-template",
        template=_template_context(tmp_path),
    )


def test_template_sections_rendered_for_all_four_roles(tmp_path: Path) -> None:
    """``## 技术栈模板`` 段四角色都渲染（docs/09 §4.1.2 渲染范围口径）。"""
    registry = RoleRegistry(ROLES_DIR)
    renderer = _renderer()
    for role_id in ALL_ROLES:
        text = renderer.build_user(registry.get(role_id), _template_input(tmp_path))
        assert "## 技术栈模板（本项目的既定约束）" in text, role_id
        assert "fastapi-vue3 v1.0" in text, role_id
        assert "依赖白名单" in text and "fastapi、uvicorn" in text, role_id
        # prompting 只注入本角色的那一条
        assert "PRD 必须声明后端为 FastAPI。" not in text or role_id == "pm", role_id


def test_layout_section_only_for_architect_and_engineer(tmp_path: Path) -> None:
    """``## 目录规范`` 段只给 architect / engineer。"""
    registry = RoleRegistry(ROLES_DIR)
    renderer = _renderer()
    rendered = {
        role_id: renderer.build_user(registry.get(role_id), _template_input(tmp_path))
        for role_id in ALL_ROLES
    }
    for role_id in ("architect", "engineer"):
        assert "## 目录规范（设计必须遵守）" in rendered[role_id], role_id
        assert "必须存在的目录：app、tests、web/src" in rendered[role_id], role_id
        assert "仅静态检查，不执行前端构建" in rendered[role_id], role_id
    for role_id in ("pm", "qa"):
        assert "## 目录规范" not in rendered[role_id], role_id


def test_engineer_only_gets_template_exception_and_both_skeleton_lists(
    tmp_path: Path,
) -> None:
    """engineer 独有「模板模式例外」+ 两个骨架清单；pm/qa/architect 都没有。"""
    registry = RoleRegistry(ROLES_DIR)
    renderer = _renderer()
    engineer = renderer.build_user(registry.get("engineer"), _template_input(tmp_path))
    assert "## 模板模式例外（优先于系统提示的通用要求）" in engineer
    assert "不再适用" in engineer and "优先零依赖" in engineer
    assert "## 骨架文件（分两类，务必区分）" in engineer
    assert "### 固定提供，不要输出（输出也会被丢弃）" in engineer
    assert "### 已给最小版本，你需要输出完整版本（可整文件替换）" in engineer

    for role_id in ("pm", "qa", "architect"):
        text = renderer.build_user(registry.get(role_id), _template_input(tmp_path))
        assert "## 模板模式例外" not in text, role_id
        assert "## 骨架文件" not in text, role_id


def test_skeleton_paths_are_split_correctly_not_merged(tmp_path: Path) -> None:
    """双向断言（r2 防回归）：``app/main.py`` 只在 seed 清单；``pyproject.toml`` /
    ``ruff.toml`` 只在 reserved 清单。合并成单一清单会让本用例失败。"""
    registry = RoleRegistry(ROLES_DIR)
    text = _renderer().build_user(registry.get("engineer"), _template_input(tmp_path))
    reserved_block = text.split("### 固定提供，不要输出（输出也会被丢弃）", 1)[1].split(
        "### 已给最小版本", 1
    )[0]
    seed_block = text.split("### 已给最小版本，你需要输出完整版本（可整文件替换）", 1)[1]

    assert "- pyproject.toml" in reserved_block
    assert "- ruff.toml" in reserved_block
    assert "app/main.py" not in reserved_block

    assert "- app/main.py" in seed_block
    assert "- requirements.txt" in seed_block
    assert "pyproject.toml" not in seed_block
    assert "ruff.toml" not in seed_block


def test_skeleton_lists_carry_paths_only_no_body(tmp_path: Path) -> None:
    """两个清单**只含路径不含正文**（断言骨架 content 字符串不出现）。"""
    registry = RoleRegistry(ROLES_DIR)
    text = _renderer().build_user(registry.get("engineer"), _template_input(tmp_path))
    assert _SKELETON_MARKER not in text
    assert "line-length = 100" not in text
    assert "fastapi>=0.115" not in text


def test_template_context_has_two_lists_and_no_single_skeleton_paths() -> None:
    """``TemplateContext`` 暴露两清单字段，**不存在**单一 ``skeleton_paths``。"""
    context = TemplateContext(
        id="t", reserved_paths=frozenset({"a"}), seed_paths=frozenset({"b"})
    )
    assert context.reserved_paths == {"a"}
    assert context.seed_paths == {"b"}
    assert not hasattr(context, "skeleton_paths")
    fields = TemplateContext.__dataclass_fields__
    assert "reserved_paths" in fields and "seed_paths" in fields
    assert "skeleton_paths" not in fields


def test_template_context_from_spec_has_no_skeleton_body(tmp_path: Path) -> None:
    """``from_spec`` 产出的上下文**不含骨架正文**（只保留路径）。"""
    context = _template_context(tmp_path)
    assert context.reserved_paths == {"pyproject.toml", "ruff.toml"}
    assert context.seed_paths == {"app/main.py", "requirements.txt"}
    dumped = repr(context)
    assert _SKELETON_MARKER not in dumped
    assert context.dependencies == ("fastapi", "uvicorn")
    assert context.layout is not None and context.layout.max_file_lines == 600
    assert context.checks[0]["run"] == "python -m compileall -q app"


def test_qa_gets_check_commands_only(tmp_path: Path) -> None:
    """``## 检查命令口径`` 段只给 qa，且命令与权重来自模板 checks。"""
    registry = RoleRegistry(ROLES_DIR)
    renderer = _renderer()
    qa_text = renderer.build_user(registry.get("qa"), _template_input(tmp_path))
    assert "## 检查命令口径（与确定性判定一致）" in qa_text
    assert "- `python -m compileall -q app`（权重 1）" in qa_text
    assert "- `python -m pytest -q`（权重 2）" in qa_text
    for role_id in ("pm", "architect", "engineer"):
        text = renderer.build_user(registry.get(role_id), _template_input(tmp_path))
        assert "## 检查命令口径" not in text, role_id


# ------------------------------------------- 编排层装载（role_executor._collect_template）


class _FakeRun:
    """最小 run 替身（``_collect_template`` 只读 ``id`` 与 ``context``）。"""

    def __init__(self, context: object) -> None:
        self.id = "run-fake"
        self.context = context


def test_collect_template_none_when_context_has_no_template() -> None:
    """无模板的 run → ``StageInput.template is None``（v1.1 行为不变）。"""
    executor = RoleStageExecutor.__new__(RoleStageExecutor)  # 静态方法，无需装配依赖
    assert executor._collect_template(_FakeRun({"context_refs": []})) is None
    assert executor._collect_template(_FakeRun(None)) is None
    assert executor._collect_template(_FakeRun({"template": {"id": "  "}})) is None


def test_collect_template_builds_context_with_two_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run.context["template"]`` → 按 id 重读模板 → 含两清单的上下文（无正文）。"""
    path = tmp_path / "fastapi-vue3.yaml"
    path.write_text(_TEMPLATE_YAML, encoding="utf-8", newline="\n")
    spec = load_template_file(path, known_roles=set(ALL_ROLES))
    monkeypatch.setattr(role_executor_module, "load_template", lambda template_id: spec)

    executor = RoleStageExecutor.__new__(RoleStageExecutor)  # 静态方法，无需装配依赖
    context = executor._collect_template(
        _FakeRun({"template": {"id": "fastapi-vue3", "version": "1.0"}})
    )

    assert context is not None
    assert context.id == "fastapi-vue3"
    assert context.reserved_paths == {"pyproject.toml", "ruff.toml"}
    assert context.seed_paths == {"app/main.py", "requirements.txt"}
    assert _SKELETON_MARKER not in repr(context)


def test_collect_template_degrades_when_template_load_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模板在 run 创建后被删除/改坏 → 只告警、按通用模式继续（不炸阶段）。"""

    def _boom(template_id: str) -> None:
        raise NotFoundError(f"模板不存在: {template_id}")

    monkeypatch.setattr(role_executor_module, "load_template", _boom)
    executor = RoleStageExecutor.__new__(RoleStageExecutor)
    assert executor._collect_template(_FakeRun({"template": {"id": "gone"}})) is None
