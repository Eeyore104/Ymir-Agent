"""模板模型与加载器测试（v1.2 · T1.1，docs/09 §4.1.1 / §4.1.5）。

覆盖口径：
- **17 条加载期校验逐条 1 条用例**（``test_checkNN_*``），其中骨架 6 条为重点；
- 字段名反断言（``type`` 存在 / ``kind`` 不存在）+ ``kind`` 写进 YAML 必 1001；
- ``to_layout_rules()`` 与 YAML 一致；``SkeletonSpec`` 两类路径互补；
- ``list_templates`` 让坏模板以 ``loading_error`` 暴露而不整体失败。

fixture 全部放在 ``tests/fixtures/templates/``，**不依赖** ``config/templates/``
（后者由 T1.4 落两套真模板）。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from ymir.common.contracts import LayoutRules
from ymir.common.errors import BizCode, NotFoundError
from ymir.config import PROJECT_ROOT
from ymir.templates import (
    SANDBOX_IMAGE_PACKAGES,
    CheckSpec,
    TemplateDefinitionError,
    TemplateSpec,
    check_reserved_subset,
    default_role_ids,
    dependency_name,
    list_templates,
    load_template,
    load_template_file,
)
from ymir.templates.models import SkeletonSpec

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "templates"
#: **真实**模板目录（T1.4 落的两套垂直模板）
TEMPLATES_DIR = PROJECT_ROOT / "config" / "templates"
REAL_TEMPLATE_IDS = ("fastapi-vue3", "python-cli")
#: 已注册角色（与 config/roles/*.yaml 一致；显式传入以免测试依赖工作目录）
ROLES = {"pm", "architect", "engineer", "qa"}


def _demo() -> dict[str, Any]:
    """读取合规模板 fixture（每次返回深拷贝，用例可安全修改）。"""
    raw = yaml.safe_load((FIXTURE_DIR / "demo.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return copy.deepcopy(raw)


def _write(data: dict[str, Any], tmp_path: Path, stem: str = "demo") -> Path:
    path = tmp_path / f"{stem}.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _expect_1001(tmp_path: Path, data: dict[str, Any], *, check_no: int | None = None) -> str:
    """断言加载期 1001（可选断言错误信息里带「校验 N」编号）。"""
    with pytest.raises(TemplateDefinitionError) as excinfo:
        load_template_file(_write(data, tmp_path), known_roles=ROLES)
    assert excinfo.value.biz_code == BizCode.INVALID_PARAM
    message = str(excinfo.value)
    if check_no is not None:
        assert f"校验 {check_no}" in message, message
    return message


# --------------------------------------------------------------------- 基线：合法模板


def test_demo_fixture_loads_and_normalizes() -> None:
    """合规 fixture 加载通过，路径保持 POSIX 归一化形态。"""
    spec = load_template_file(FIXTURE_DIR / "demo.yaml", known_roles=ROLES)
    assert isinstance(spec, TemplateSpec)
    assert spec.id == "demo"
    assert spec.stack.frontend == "vue3"
    assert spec.skeleton.reserved_paths == {".env.example", "pyproject.toml"}
    assert spec.skeleton.seed_paths == {"app/main.py", "requirements.txt"}
    assert set(spec.skeleton_files()) == spec.skeleton.all_paths


def test_load_template_by_id_ok() -> None:
    """按 id 加载（目录扫描口径）。"""
    spec = load_template("demo", templates_dir=FIXTURE_DIR, known_roles=ROLES)
    assert spec.id == "demo"


def test_load_template_missing_is_1002() -> None:
    """不存在的模板 → ``NotFoundError``（1002）。"""
    with pytest.raises(NotFoundError) as excinfo:
        load_template("nope", templates_dir=FIXTURE_DIR, known_roles=ROLES)
    assert excinfo.value.biz_code == BizCode.NOT_FOUND


# --------------------------------------------------------------------- 17 条校验逐条


def test_check01_id_must_equal_file_stem(tmp_path: Path) -> None:
    """校验 1：``id`` 必须等于文件名主干。"""
    data = _demo()
    data["id"] = "other"
    _expect_1001(tmp_path, data, check_no=1)


def test_check02_version_not_empty(tmp_path: Path) -> None:
    """校验 2：``version`` 非空。"""
    data = _demo()
    data["version"] = "   "
    _expect_1001(tmp_path, data, check_no=2)


def test_check03_language_must_be_python(tmp_path: Path) -> None:
    """校验 3：``stack.language == "python"``（锁定 L2）。"""
    data = _demo()
    data["stack"]["language"] = "java"
    _expect_1001(tmp_path, data, check_no=3)


def test_check04_dependencies_must_subset_image(tmp_path: Path) -> None:
    """校验 4：``stack.dependencies ⊆ 镜像清单``（fail-fast）。"""
    data = _demo()
    data["stack"]["dependencies"] = ["fastapi", "numpy"]
    message = _expect_1001(tmp_path, data, check_no=4)
    assert "numpy" in message


def test_check04_dependency_names_tolerate_version_specifiers() -> None:
    """校验 4 的正向口径：``fastapi>=0.115`` / ``uvicorn[standard]>=0.30`` 视为合规。"""
    assert dependency_name("fastapi>=0.115") == "fastapi"
    assert dependency_name("uvicorn[standard]>=0.30") == "uvicorn"
    assert {"pytest", "fastapi", "alembic"} <= SANDBOX_IMAGE_PACKAGES


@pytest.mark.parametrize("bad", ["../etc", "/etc/passwd", "C:/windows", "a/../../b"])
def test_check05_layout_paths_normalized(tmp_path: Path, bad: str) -> None:
    """校验 5：``layout.required_*`` / ``allowed_prefixes`` 过 ``normalize_rel_path``。"""
    data = _demo()
    data["layout"]["required_dirs"] = [bad]
    _expect_1001(tmp_path, data, check_no=5)


def test_check06_conventions_path_invalid(tmp_path: Path) -> None:
    """校验 6：``conventions[].path`` 过路径安全校验。"""
    data = _demo()
    data["conventions"] = [{"path": "../escape.md", "required": True}]
    _expect_1001(tmp_path, data, check_no=6)


def test_check06_conventions_path_duplicated(tmp_path: Path) -> None:
    """校验 6：``conventions[].path`` 互不重复（重复即静默覆盖）。"""
    data = _demo()
    data["conventions"] = [{"path": "README.md"}, {"path": "./README.md"}]
    _expect_1001(tmp_path, data, check_no=6)


@pytest.mark.parametrize("bad", ["../x", "/abs/x.py", "D:/x.py"])
def test_check07_skeleton_path_normalized(tmp_path: Path, bad: str) -> None:
    """校验 7：``skeleton.files[].path`` 过 ``normalize_rel_path``（骨架 6 条之一）。"""
    data = _demo()
    data["skeleton"]["files"][0]["path"] = bad
    _expect_1001(tmp_path, data, check_no=7)


def test_check07_skeleton_path_duplicated(tmp_path: Path) -> None:
    """校验 7：``skeleton.files[].path`` 互不重复。"""
    data = _demo()
    data["skeleton"]["files"][1]["path"] = "pyproject.toml"
    _expect_1001(tmp_path, data, check_no=7)


def test_check08_skeleton_content_empty(tmp_path: Path) -> None:
    """校验 8：``content`` 空串 → 1001（pydantic ``min_length=1``）。"""
    data = _demo()
    data["skeleton"]["files"][0]["content"] = ""
    _expect_1001(tmp_path, data)


def test_check08_skeleton_content_whitespace_only(tmp_path: Path) -> None:
    """校验 8：纯空白正文同样视为空 → 校验 8。"""
    data = _demo()
    data["skeleton"]["files"][0]["content"] = "   \n\n\t"
    _expect_1001(tmp_path, data, check_no=8)


def test_check09_reserved_subset_invariant(tmp_path: Path) -> None:
    """校验 9：``reserved_paths ⊄ skeleton.files`` → 1001。

    模型层面该不变量恒真（reserved 由 ``files[].reserved`` 派生），因此定点测
    校验函数本身；YAML 层面的真实形态是"skeleton 下多写一个 reserved_paths 键"，
    由 ``extra="forbid"`` 拒掉（下一条用例）。
    """
    with pytest.raises(TemplateDefinitionError) as excinfo:
        check_reserved_subset("demo", {"a.py", "b.py"}, {"a.py"})
    assert excinfo.value.biz_code == BizCode.INVALID_PARAM
    assert "校验 9" in str(excinfo.value)
    # 正向：子集时静默通过
    check_reserved_subset("demo", {"a.py"}, {"a.py", "b.py"})


def test_check09_explicit_reserved_paths_key_rejected(tmp_path: Path) -> None:
    """校验 9 的 YAML 形态：``skeleton.reserved_paths`` 是未知字段 → 1001。"""
    data = _demo()
    data["skeleton"]["reserved_paths"] = ["b.py"]
    _expect_1001(tmp_path, data)


def test_check10_too_many_skeleton_files(tmp_path: Path) -> None:
    """校验 10：``len(files) > max_files`` → 校验 10（骨架 6 条之一）。"""
    data = _demo()
    data["skeleton"]["max_files"] = 3
    _expect_1001(tmp_path, data, check_no=10)


def test_check10_total_bytes_exceeded(tmp_path: Path) -> None:
    """校验 10：骨架总字节 > ``max_bytes`` → 校验 10（骨架 6 条之一）。"""
    data = _demo()
    data["skeleton"]["max_bytes"] = 16
    _expect_1001(tmp_path, data, check_no=10)


def test_check11_skeleton_secret_fingerprint(tmp_path: Path) -> None:
    """校验 11：骨架内容命中密钥指纹（``sk-`` 样本）→ 校验 11（骨架 6 条之一）。"""
    data = _demo()
    data["skeleton"]["files"][0]["content"] = "OPENAI_KEY=sk-" + "a1B2c3D4e5F6g7H8i9J0kLmN\n"
    _expect_1001(tmp_path, data, check_no=11)


def test_check12_skeleton_conventions_overlap(tmp_path: Path) -> None:
    """校验 12：``skeleton`` 与 ``conventions`` 路径交叉 → 校验 12（骨架 6 条之一）。"""
    data = _demo()
    data["conventions"] = [{"path": "app/main.py", "required": True}]
    _expect_1001(tmp_path, data, check_no=12)


def test_check13_checks_not_empty(tmp_path: Path) -> None:
    """校验 13：``checks`` 不得为空。"""
    data = _demo()
    data["checks"] = []
    _expect_1001(tmp_path, data, check_no=13)


def test_check13_kind_field_rejected(tmp_path: Path) -> None:
    """校验 13：YAML 写 ``kind: command`` → 1001（``extra="forbid"`` 生效）。

    这条是"防碰巧能跑"的关键：``EvalCheck.type`` 有默认值 ``"command"``，
    若允许额外字段，``kind`` 会被静默忽略（docs/09 §13 共享知识）。
    """
    data = _demo()
    data["checks"] = [{"kind": "command", "run": "python -m pytest -q", "weight": 1}]
    message = _expect_1001(tmp_path, data)
    assert "kind" in message


@pytest.mark.parametrize("bad_run", ["pip install fastapi", "https://x/y", "npm install vue"])
def test_check14_check_run_must_be_offline(tmp_path: Path, bad_run: str) -> None:
    """校验 14：检查命令不得联网 / 安装（离线可复现）。"""
    data = _demo()
    data["checks"] = [{"type": "command", "run": bad_run, "weight": 1}]
    _expect_1001(tmp_path, data, check_no=14)


def test_check15_weight_must_be_positive(tmp_path: Path) -> None:
    """校验 15：``weight <= 0`` → 1001（pydantic ``gt=0`` 前置拦截）。"""
    data = _demo()
    data["checks"] = [{"type": "command", "run": "ruff check .", "weight": 0}]
    _expect_1001(tmp_path, data)


def test_check16_supports_not_empty(tmp_path: Path) -> None:
    """校验 16：``supports`` 非空且取值合法。"""
    data = _demo()
    data["supports"] = []
    _expect_1001(tmp_path, data, check_no=16)


def test_check16_supports_illegal_value(tmp_path: Path) -> None:
    """校验 16：``supports`` 出现未定义模式 → 1001（Literal 拦截）。"""
    data = _demo()
    data["supports"] = ["from_scratch", "rewrite"]
    _expect_1001(tmp_path, data)


def test_check17_prompting_unknown_role(tmp_path: Path) -> None:
    """校验 17：``prompting`` 的键必须是已注册角色 id。"""
    data = _demo()
    data["prompting"]["devops"] = "部署约束"
    message = _expect_1001(tmp_path, data, check_no=17)
    assert "devops" in message


def test_check17_default_role_ids_from_config() -> None:
    """校验 17 的缺省角色来源：``config/roles/*.yaml`` 文件名主干。"""
    ids = default_role_ids()
    assert {"pm", "architect", "engineer", "qa"} <= ids


# ---------------------------------------------------------------- 模型层硬约束（非 17 条）


def test_checkspec_has_type_not_kind() -> None:
    """``CheckSpec.model_fields`` 含 ``type``、**不含** ``kind``（反断言）。"""
    fields = CheckSpec.model_fields
    assert "type" in fields
    assert "kind" not in fields
    assert fields["type"].default == "command"


def test_unknown_field_rejected(tmp_path: Path) -> None:
    """拼错的字段必须 1001，不能静默忽略（``extra="forbid"``）。"""
    data = _demo()
    data["stack"]["runtime_version"] = "3.12"
    message = _expect_1001(tmp_path, data)
    assert "runtime_version" in message


def test_to_layout_rules_matches_yaml() -> None:
    """``to_layout_rules()`` 返回的 ``LayoutRules`` 字段与 YAML 一致。"""
    spec = load_template_file(FIXTURE_DIR / "demo.yaml", known_roles=ROLES)
    rules = spec.to_layout_rules()
    raw = _demo()
    assert isinstance(rules, LayoutRules)
    assert rules.required_dirs == raw["layout"]["required_dirs"]
    assert rules.required_files == raw["layout"]["required_files"]
    assert rules.any_of_files == raw["layout"]["any_of_files"]
    assert rules.forbidden_dirs == raw["layout"]["forbidden_dirs"]
    assert rules.snake_case_py is raw["layout"]["snake_case_py"]
    assert rules.max_file_lines == raw["layout"]["max_file_lines"]
    assert rules.allowed_prefixes == raw["layout"]["allowed_prefixes"]
    assert rules.frontend is not None
    assert rules.frontend.required_glob == raw["layout"]["frontend"]["required_glob"]
    assert rules.frontend.forbidden_imports == raw["layout"]["frontend"]["forbidden_imports"]


def test_layout_rules_roundtrip_json() -> None:
    """``LayoutRules`` 可序列化往返（跨层纯数据的必要条件）。"""
    spec = load_template_file(FIXTURE_DIR / "demo.yaml", known_roles=ROLES)
    rules = spec.to_layout_rules()
    assert LayoutRules.model_validate(rules.model_dump()) == rules


def test_skeleton_paths_partition() -> None:
    """``seed_paths`` 与 ``reserved_paths`` 互补且并集 == ``all_paths``。"""
    spec = load_template_file(FIXTURE_DIR / "demo.yaml", known_roles=ROLES)
    skeleton: SkeletonSpec = spec.skeleton
    assert not (skeleton.reserved_paths & skeleton.seed_paths)
    assert skeleton.reserved_paths | skeleton.seed_paths == skeleton.all_paths
    assert skeleton.all_paths == set(spec.skeleton_files())


def test_skeleton_files_returns_plain_mapping() -> None:
    """``skeleton_files()`` 必须是纯 ``dict[str, str]``（供 rank 3 的 merge_trees）。"""
    spec = load_template_file(FIXTURE_DIR / "demo.yaml", known_roles=ROLES)
    files = spec.skeleton_files()
    assert type(files) is dict  # noqa: E721 - 刻意断言"纯 dict"，不是 Mapping 子类
    assert files["app/main.py"].startswith("from fastapi import FastAPI")


# --------------------------------------------------------------------- 注册表列举


def test_list_templates_reports_loading_error(tmp_path: Path) -> None:
    """坏模板不让整体失败：好模板可用、坏模板带 ``loading_error``。"""
    good = _demo()
    _write(good, tmp_path, stem="demo")
    bad = _demo()
    bad["id"] = "broken"
    bad["checks"] = []
    _write(bad, tmp_path, stem="broken")

    entries = list_templates(templates_dir=tmp_path, known_roles=ROLES)
    by_id = {entry.id: entry for entry in entries}
    assert by_id["demo"].ok and by_id["demo"].loading_error is None
    assert not by_id["broken"].ok
    assert "校验 13" in (by_id["broken"].loading_error or "")


def test_list_templates_summary_has_no_skeleton_content() -> None:
    """对外摘要只给路径、**不回传骨架正文**（``GET /templates`` 口径）。"""
    entries = list_templates(templates_dir=FIXTURE_DIR, known_roles=ROLES)
    assert len(entries) == 1
    summary = entries[0].to_summary()
    assert summary["reserved_paths"] == [".env.example", "pyproject.toml"]
    assert summary["seed_paths"] == ["app/main.py", "requirements.txt"]
    dumped = repr(summary)
    assert "FastAPI()" not in dumped  # 骨架正文不得出现
    assert "requires-python" not in dumped


def test_list_templates_empty_dir(tmp_path: Path) -> None:
    """空目录 / 不存在的目录 → 空列表（不抛异常）。"""
    assert list_templates(templates_dir=tmp_path, known_roles=ROLES) == []
    assert list_templates(templates_dir=tmp_path / "nope", known_roles=ROLES) == []


# ------------------------------------------------- 两套真模板（v1.2 · T1.4，config/templates）


@pytest.fixture(scope="module")
def real_templates() -> dict[str, TemplateSpec]:
    """从**真实** ``config/templates/`` 加载两套模板（17 条校验全过才算可用）。"""
    return {tid: load_template(tid, known_roles=ROLES) for tid in REAL_TEMPLATE_IDS}


def test_real_templates_load_through_all_checks(real_templates: dict[str, TemplateSpec]) -> None:
    """DoD ①：两模板都通过 17 条加载期校验并列出。"""
    assert set(real_templates) == {"fastapi-vue3", "python-cli"}
    listed = {entry.id: entry for entry in list_templates(known_roles=ROLES)}
    for template_id in REAL_TEMPLATE_IDS:
        assert listed[template_id].ok, listed[template_id].loading_error


def test_real_templates_checks_are_executable(
    real_templates: dict[str, TemplateSpec],
) -> None:
    """DoD ②：每个模板 ``checks`` ≥3 条，且至少 1 条执行型（pytest/compileall/python -m）。"""
    for template_id, spec in real_templates.items():
        assert len(spec.checks) >= 3, template_id
        assert all(check.type == "command" for check in spec.checks), template_id
        runs = [check.run for check in spec.checks]
        executable_tokens = ("pytest", "compileall", "python -m", "python -c")
        assert any(token in run for run in runs for token in executable_tokens), (
            f"{template_id} 缺少执行型检查：{runs}"
        )


def test_fastapi_skeleton_exactly_at_max_files(
    real_templates: dict[str, TemplateSpec],
) -> None:
    """DoD ③：``fastapi-vue3`` 骨架**恰等于**上限（加第 6 个即超限，须先调 max_files）。"""
    spec = real_templates["fastapi-vue3"]
    assert len(spec.skeleton.files) == spec.skeleton.max_files == 5
    assert spec.reserved_paths() == {"pyproject.toml", "ruff.toml", ".env.example"}
    assert spec.seed_paths() == {"requirements.txt", "app/main.py"}


def test_python_cli_skeleton_has_four_files(
    real_templates: dict[str, TemplateSpec],
) -> None:
    """DoD ③：``python-cli`` 是 4 个骨架文件（**不**硬套 5 的上限断言）。"""
    spec = real_templates["python-cli"]
    assert len(spec.skeleton.files) == 4
    assert spec.reserved_paths() == {"pyproject.toml", "ruff.toml"}
    assert spec.seed_paths() == {"ymir_cli/__init__.py", "requirements.txt"}


def test_skeletons_contain_no_test_files(real_templates: dict[str, TemplateSpec]) -> None:
    """DoD ④：骨架**不含任何 test 文件**（否则 ``has_tests`` 白送结构分，§12-3）。

    用 ``tests/`` 或文件名里出现 ``test`` 都算违规 —— 后者能挡住 ``test_utils.py``。
    """
    for template_id, spec in real_templates.items():
        paths = set(spec.skeleton_files())
        assert not [path for path in paths if "test" in path.lower()], template_id
        assert not [path for path in paths if path.startswith("tests/")], template_id


def test_env_example_has_no_secret(real_templates: dict[str, TemplateSpec]) -> None:
    """DoD ⑤：``.env.example`` 内容无真实 key（走 ``common.secrets.scan_secret``）。"""
    from ymir.common.secrets import scan_secret

    files = real_templates["fastapi-vue3"].skeleton_files()
    content = files[".env.example"]
    assert scan_secret(content) is None
    assert "APP_ENV=dev" in content


def test_real_templates_structure_fields(
    real_templates: dict[str, TemplateSpec],
) -> None:
    """DoD ⑥：技术栈与目录规范的硬结构（前端/后端形态、必需目录）。"""
    fastapi = real_templates["fastapi-vue3"]
    assert fastapi.stack.frontend == "vue3"
    assert fastapi.stack.backend == "fastapi"
    assert {"app", "tests"} <= set(fastapi.layout.required_dirs)

    cli = real_templates["python-cli"]
    assert cli.stack.frontend == "none"
    assert {"ymir_cli", "tests"} <= set(cli.layout.required_dirs)


def test_ruff_toml_consistency_with_checks_and_layout(
    real_templates: dict[str, TemplateSpec],
) -> None:
    """DoD ③'：含 ``ruff check .`` 的模板必须有 ``ruff.toml`` 且 ``line-length == 100``。

    行长度（每行字符）与 ``layout.max_file_lines``（每文件行数）是两个轴：
    这里断言前者为 100（文档定案值），后者不小于 100（不矛盾）。
    """
    for template_id, spec in real_templates.items():
        assert any("ruff check" in check.run for check in spec.checks), template_id
        content = spec.skeleton_files()["ruff.toml"]
        line_length = _toml_int(content, "line-length")
        assert line_length == 100, template_id
        assert spec.layout.max_file_lines >= line_length, template_id
        # 单一来源：pyproject.toml 里不得再写 [tool.ruff]
        assert "[tool.ruff]" not in spec.skeleton_files()["pyproject.toml"], template_id


def test_real_template_unknown_field_rejected(
    tmp_path: Path, real_templates: dict[str, TemplateSpec]
) -> None:
    """DoD ⑦：真模板形态下拼错字段 → 1001（``extra="forbid"`` 在真 YAML 上同样生效）。"""
    raw = yaml.safe_load((TEMPLATES_DIR / "fastapi-vue3.yaml").read_text(encoding="utf-8"))
    raw["stack"]["runtime_verison"] = "3.12"  # 故意拼错
    with pytest.raises(TemplateDefinitionError) as excinfo:
        load_template_file(_write(raw, tmp_path, stem="fastapi-vue3"), known_roles=ROLES)
    assert excinfo.value.biz_code == BizCode.INVALID_PARAM
    assert "runtime_verison" in str(excinfo.value)


def _toml_int(content: str, key: str) -> int | None:
    """从极简 TOML 片段里读一个整数键（骨架 ruff.toml 只有扁平的 key = value）。"""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key} ") and "=" in stripped:
            raw = stripped.split("=", 1)[1].strip().strip('"')
            try:
                return int(raw)
            except ValueError:
                return None
    return None
